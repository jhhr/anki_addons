"""What Opus knows about each word, as features for the triage model.

    py -3.10 word_array/research/triage_opus.py pilot            # one batch, printed (costs)
    py -3.10 word_array/research/triage_opus.py run [--limit N]  # every word not yet answered
    py -3.10 word_array/research/triage_opus.py status
    py -3.10 word_array/research/triage_opus.py export --dir DIR   # batches for subagents
    py -3.10 word_array/research/triage_opus.py ingest --dir DIR   # their answers, cached

`run` asks through `claude -p` (triage_claude.py), which needs the CLI logged in to the
subscription: on the user's PC, not in a cloud session. There, `export` writes each pending
batch as `DIR/batch_NNNN.md` (the instructions, the numbered words, the JSON schema, and the path
to write the answer to) with `batch_NNNN.keys.json` beside it, for subagents of the session to
answer, one batch each; `ingest` caches every `batch_NNNN.answer.json` found under the same keys
as `run` would, so a word is answered once whichever way it went. It can be run again as more
answers land.

Every note in `words.jsonl` (triage_extract.py), judged or not, goes to Opus in batches of
`--batch` words in a fixed random order, so that no batch is all easy or all rare words and a
word's answer does not lean on its neighbours. For each word, in the sense its note gives, the
model estimates the learner vocabulary size at which it is typically known, the JLPT level, how
familiar natives are with it, its register, domain and origin (a loanword's source language),
whether its meaning follows from its parts, what kind of item it is, and, where the note is one
sense of several, how common that sense is among them.

Each word's answer is cached in `opus_features.jsonl` under a key of the model, the prompt's
version and the word's own text in the prompt, so a word is paid for once whatever batch it
lands in; a batch that loses a word leaves it for the next run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import triage_claude as tc
import triage_data as td

PROMPT_VERSION = 1
BATCH = 50
SEED = 20260930
MAX_OTHER_SENSES = 8
SENSE_CHARS = 110

VOCAB_SIZES = [500, 1000, 2000, 3000, 5000, 8000, 12000, 18000, 25000, 35000, 50000, 80000]
JLPT = ["N5", "N4", "N3", "N2", "N1", "beyond"]
REGISTERS = ["neutral", "formal-written", "literary-archaic", "colloquial", "slang-vulgar",
             "honorific-humble", "technical", "childish-mimetic"]
DOMAINS = ["general", "daily-life", "people-relationships", "emotion-mind", "body-health",
           "food", "nature", "places-travel", "work-economy", "politics-law-society",
           "science-tech", "education-language", "arts-entertainment", "sports-games",
           "history-military", "religion-myth-fantasy", "other"]
ORIGINS = ["wago", "kango", "gairaigo", "wasei-eigo", "hybrid", "mimetic", "proper-noun"]
LOAN_SOURCES = ["none", "English", "Dutch", "Portuguese", "German", "French", "Italian",
                "Spanish", "Russian", "Chinese", "Korean", "Sanskrit", "other"]
COMPOSITIONAL = ["single-unit", "transparent", "partly", "opaque"]
SENSE = ["main", "common", "secondary", "rare", "n/a"]
KINDS = ["word", "phrase-of-parts", "idiom-set-phrase", "grammar", "proper-noun-real",
         "proper-noun-fictional"]

INSTRUCTIONS = """You are an expert in Japanese lexicography and in how adult foreign learners \
acquire Japanese vocabulary. You will get a numbered list of Japanese vocabulary items, each \
with its reading, part of speech and the one sense (meaning) the item is about. Judge each item \
IN THAT SENSE, not the word's commonest sense.

For every item give:

- learner_vocab_size: the vocabulary size (number of distinct words known) at which a typical \
adult learner of Japanese usually knows this item in this sense. The learner studies with \
frequency-ordered courses and reads and watches native material (novels, manga, anime, games, \
news). Anchors: 500 = the first words anyone learns (食べる, 大きい, 学校); 2000 = everyday \
basics (経験, 準備, 急ぐ); 5000 = common in any novel or news item (概して, 途端, 批判); \
12000 = an advanced learner's words (落ち目, 陰謀, 勝機); 25000 = what an educated native reads \
but seldom says (漸く in kanji, 専権事項); 50000+ = rare, specialist, archaic or regional. A \
sense that is rare in its own right sits higher than the word's common sense.
- jlpt: the JLPT level this item in this sense would sit at, or "beyond" if above N1.
- native_familiarity: how well an educated adult native speaker knows this item in this sense: \
5 uses it daily, 4 everyone knows it, 3 most adults know it, 2 mainly the educated or \
specialists, 1 obscure.
- register, domain: the best fitting value.
- origin: wago (native Japanese), kango (Sino-Japanese), gairaigo (loan from a Western or other \
modern language), wasei-eigo (made in Japan from English parts), hybrid (mixed parts, e.g. \
kanji + katakana, jūbako/yutō readings), mimetic (onomatopoeia), proper-noun.
- loan_source: the source language of a gairaigo or wasei-eigo item; "none" for wago and kango \
(Sino-Japanese is not a loan here); "Chinese" only for modern borrowings such as ラーメン.
- compositional: single-unit (one morpheme, nothing to compose), transparent (a learner who \
knows the parts can work out this sense), partly (the parts hint at it), opaque (idiomatic; the \
parts mislead or say nothing).
- sense_commonness: when the item lists the word's other senses, how common THIS sense is \
among them: main (the sense most uses of the word have), common, secondary, rare; "n/a" when \
no other senses are listed.
- kind: word; phrase-of-parts (a phrase of separate words, e.g. a verb with its object, that \
means just what its parts say); idiom-set-phrase (a fixed expression, including 4-kanji idioms); \
grammar (a function word or grammatical pattern); proper-noun-real; proper-noun-fictional (a \
name or term from one work of fiction).

Answer for every item, by its number, with the JSON the schema asks for and nothing else."""

SCHEMA = {
    "type": "object",
    "properties": {
        "words": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "learner_vocab_size": {"type": "integer", "enum": VOCAB_SIZES},
                    "jlpt": {"type": "string", "enum": JLPT},
                    "native_familiarity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "register": {"type": "string", "enum": REGISTERS},
                    "domain": {"type": "string", "enum": DOMAINS},
                    "origin": {"type": "string", "enum": ORIGINS},
                    "loan_source": {"type": "string", "enum": LOAN_SOURCES},
                    "compositional": {"type": "string", "enum": COMPOSITIONAL},
                    "sense_commonness": {"type": "string", "enum": SENSE},
                    "kind": {"type": "string", "enum": KINDS},
                },
                "required": ["id", "learner_vocab_size", "jlpt", "native_familiarity",
                             "register", "domain", "origin", "loan_source", "compositional",
                             "sense_commonness", "kind"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["words"],
    "additionalProperties": False,
}

FEATURES = "opus_features.jsonl"


def short(text: str, limit: int = SENSE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def item_text(row: dict) -> str:
    """How one word reads in the prompt, without its number: the cache key is made of this."""
    word = row["word"] or row["kanjified"]
    spelling = ""
    if row["kanjified"] and row["kanjified"] != word:
        spelling = ("; rarely written " if row["ignore_kanjified"] else "; also written ") + \
            row["kanjified"]
    lines = [f"{word} ({row['reading']}{spelling}), {row['pos'] or 'unknown part of speech'}",
             f"   this sense: {short(row['meaning'], 300)}"]
    others = list(dict.fromkeys(
        s["meaning"] for s in row["siblings"]
        if s["relation"] == td.MEANING and s["meaning"] and s["meaning"] != row["meaning"]
    ))
    if others:
        lines.append("   the word's other senses:")
        for meaning in others[:MAX_OTHER_SENSES]:
            lines.append(f"   - {short(meaning)}")
        if len(others) > MAX_OTHER_SENSES:
            lines.append(f"   - ...and {len(others) - MAX_OTHER_SENSES} more")
    readings = [s for s in row["siblings"] if s["relation"] == td.READING]
    if readings:
        read_as = sorted({s["reading"] for s in readings if s["reading"]})
        if read_as:
            lines.append("   the same spelling is also read: " + ", ".join(read_as))
    return "\n".join(lines)


def cache_key(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\n{PROMPT_VERSION}\n{text}".encode("utf-8")).hexdigest()


def batch_prompt(texts: list[str]) -> str:
    parts = [f"{len(texts)} items:\n"]
    for i, text in enumerate(texts, 1):
        parts.append(f"[{i}] {text}")
    return "\n\n".join(parts) + "\n"


def read_cache() -> dict[str, dict]:
    return {row["key"]: row for row in td.read_jsonl(td.data_file(FEATURES))}


def pending(rows: list[dict], model: str, cache: dict) -> list[tuple[dict, str, str]]:
    """`(row, text, key)` of every word not answered yet, in the fixed random order."""
    order = sorted(rows, key=lambda r: r["nid"])
    random.Random(SEED).shuffle(order)
    out = []
    for row in order:
        text = item_text(row)
        key = cache_key(model, text)
        if key not in cache:
            out.append((row, text, key))
    return out


def valid(answer: dict) -> bool:
    return all(k in answer for k in SCHEMA["properties"]["words"]["items"]["required"])


def run_batches(batches: list, model: str, effort: str, workers: int, echo: bool = False) -> None:
    path = td.data_file(FEATURES)
    lock = threading.Lock()
    done = Counter()
    started = time.time()

    def one(batch: list) -> None:
        prompt = batch_prompt([text for _, text, _ in batch])
        t0 = time.time()
        result, message, usage = tc.ask(prompt, INSTRUCTIONS, SCHEMA, model=model, effort=effort)
        seconds = time.time() - t0
        answers = {a.get("id"): a for a in (result or {}).get("words", []) if isinstance(a, dict)}
        rows = []
        for i, (row, text, key) in enumerate(batch, 1):
            answer = answers.get(i)
            if answer is None or not valid(answer):
                continue
            answer = {k: v for k, v in answer.items() if k != "id"}
            rows.append({"key": key, "nid": row["nid"], "model": model, "effort": effort,
                         "version": PROMPT_VERSION, "answer": answer})
        with lock:
            with path.open("a", encoding="utf-8") as out:
                for r in rows:
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
            done["batches"] += 1
            done["words"] += len(rows)
            done["lost"] += len(batch) - len(rows)
            rate = done["batches"] / max(time.time() - started, 1) * 3600
            tc.log(f"batch {done['batches']}/{len(batches)}: {len(rows)}/{len(batch)} answered"
                   f" in {seconds:.0f}s ({rate:.0f} batches/h, {done['lost']} lost so far)")
            if result is None:
                tc.log(f"no answer: {message.strip()[:300]}")
            if echo:
                print(prompt)
                print(json.dumps(result, ensure_ascii=False, indent=1))
                print("usage", json.dumps(usage))

    tc.Pool(workers).run(batches, one)


def export(batches: list, folder, model: str) -> int:
    """Each batch as a file a subagent can answer, and the keys to cache its answer under."""
    folder.mkdir(parents=True, exist_ok=True)
    for i, batch in enumerate(batches, 1):
        name = f"batch_{i:04d}"
        answer = (folder / f"{name}.answer.json").resolve()
        text = "\n\n".join([
            INSTRUCTIONS,
            "Answer with one JSON object matching this JSON Schema, and write it (only the JSON)"
            f" with your Write tool to:\n{answer}",
            json.dumps(SCHEMA, ensure_ascii=False),
            batch_prompt([t for _, t, _ in batch]),
        ])
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
        keys = [{"id": j, "nid": row["nid"], "key": key}
                for j, (row, _, key) in enumerate(batch, 1)]
        (folder / f"{name}.keys.json").write_text(
            json.dumps({"model": model, "keys": keys}), encoding="utf-8")
    return len(batches)


def ingest(folder, model: str) -> tuple[int, int]:
    """Cache every answer file's valid answers not cached yet: `(words cached, lost)`."""
    cache = read_cache()
    added, lost = 0, 0
    with td.data_file(FEATURES).open("a", encoding="utf-8") as out:
        for keys_path in sorted(folder.glob("batch_*.keys.json")):
            answer_path = keys_path.with_name(keys_path.name.replace(".keys.json",
                                                                       ".answer.json"))
            if not answer_path.exists():
                continue
            meta = json.loads(keys_path.read_text(encoding="utf-8"))
            try:
                result = json.loads(answer_path.read_text(encoding="utf-8"))
            except ValueError:
                result = tc.terminal.last_json_object(answer_path.read_text(encoding="utf-8"))
            answers = {a.get("id"): a for a in (result or {}).get("words", [])
                       if isinstance(a, dict)}
            for k in meta["keys"]:
                if k["key"] in cache:
                    continue
                answer = answers.get(k["id"])
                if answer is None or not valid(answer):
                    lost += 1
                    continue
                row = {"key": k["key"], "nid": k["nid"], "model": meta["model"],
                       "effort": "subagent", "version": PROMPT_VERSION,
                       "answer": {x: v for x, v in answer.items() if x != "id"}}
                cache[k["key"]] = row
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                added += 1
    return added, lost


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["pilot", "run", "status", "export", "ingest"])
    parser.add_argument("--dir", type=Path, default=None,
                        help="export/ingest: the batch folder (default: opus_batches/ beside"
                             " the cache)")
    parser.add_argument("--model", default=tc.MODEL)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="at most this many batches")
    args = parser.parse_args()

    folder = args.dir or td.data_file("opus_batches")
    if args.command == "ingest":
        added, lost = ingest(folder, args.model)
        print(f"cached {added} answers, {lost} missing or invalid", file=sys.stderr)
        return 0
    rows = td.read_jsonl(td.data_file("words.jsonl"))
    if not rows:
        sys.exit("no words.jsonl: run triage_extract.py words first")
    cache = read_cache()
    todo = pending(rows, args.model, cache)
    print(f"{len(rows)} words, {len(rows) - len(todo)} answered, {len(todo)} to ask",
          file=sys.stderr)
    if args.command == "status" or not todo:
        return 0
    batches = [todo[i : i + args.batch] for i in range(0, len(todo), args.batch)]
    if args.command == "export":
        n = export(batches[: args.limit] if args.limit else batches, folder, args.model)
        print(f"wrote {n} batch files to {folder}", file=sys.stderr)
        return 0
    if args.command == "pilot":
        run_batches(batches[:1], args.model, args.effort, 1, echo=True)
        return 0
    if args.limit:
        batches = batches[: args.limit]
    run_batches(batches, args.model, args.effort, args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
