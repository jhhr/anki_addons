"""A second opinion on the notes the model is least sure of: agents that read what the user knows.

    py -3.10 word_array/research/triage_agents.py export [--lo 0.35] [--hi 0.85] [--labelled 400]
    py -3.10 word_array/research/triage_agents.py ingest
    py -3.10 word_array/research/triage_agents.py status

The model sees a word through its features; an agent can read it, and read it against this
user: each batch comes with a profile of words the user has judged, by what they said (knows it
well, thinks they know it, does not know it) and the word's Jiten rank, and the agent gives, for
each word in the batch, the chance the user would judge it known (Schedule or Suspend rather than
Learn). A profile never holds a word its batch judges, in any sense or reading: the profiles are
drawn afresh for each batch from the labels, leaving out every note of the same base form.

Which notes: the unjudged ones whose P(known) (`predictions.jsonl`) lies between `--lo` and `--hi`
(the middle, where the decision is made), every note of the judging page's random holdout (so
that the stacked model is scored on it honestly), and up to `--labelled` labelled notes in the
same band, which triage_model.py stacks the answers on: a second stage fitted on the labelled
notes that have an answer. The notes go in a fixed random order, `--batch` to a batch.

`export` writes each batch as `agent_batches/batch_NNNN.md` (instructions, profile, items, schema
and the path to write the answer to) with `batch_NNNN.keys.json` beside it, for subagents of a
Claude session to answer, one batch each; `ingest` caches every answer found in
`agent_judgements.jsonl` under a key of the prompt's version and the item's text, so a note is
answered once whatever batch or profile it lands with. Run with `claude -p` it would cost the same
as the Opus features per word; there is no `run` here, since the batches are answered in cloud
sessions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

import triage_data as td

PROMPT_VERSION = 1
BATCH = 40
SEED = 20260930
LO, HI = 0.35, 0.85
LABELLED = 400
PROFILE = {"known_well": 45, "thinks_known": 30, "unknown": 45}
SENSE_CHARS = 160
ANSWERS = "agent_judgements.jsonl"
FOLDER = "agent_batches"

INSTRUCTIONS = """You are judging, for one particular adult learner of Japanese, whether they \
already know each of a list of vocabulary items. The learner has studied Japanese for years with \
Anki and by reading and watching native material (novels, manga, anime, games), but studied \
poorly for about two years, so their memory of words studied back then has decayed. Each item is \
one sense (and reading) of a word; judge THAT sense.

For each item they will choose one of: Suspend (so easy there is nothing to learn), Schedule (I \
think I know it; test me in a year or more), or Learn (I don't know it). Their choice is a \
feeling ("I've heard this, I've read it, I know what it means"), not a rule. A word appearing in \
a sentence they studied does not by itself mean they know it.

Below is a profile of words this learner has already judged, grouped by their verdict, each with \
its Jiten frequency rank (#1 is the commonest word in anime, drama, novels and games; blank when \
unranked). Use it to place their knowledge: which ranks, registers, kinds of word and kanji they \
know and which they don't. Then, for each item, give p_known: the probability (0 to 1) that they \
would choose Suspend or Schedule rather than Learn. Think about each item: how common this sense \
is where this learner reads and watches; whether its kanji or parts make it guessable to someone \
at their level; whether it is a new sense or reading of a word they studied (marked "studied") \
and whether that sense follows from the one they know; whether it is a set phrase of words they \
know. Spread your probabilities: use values near 0 or 1 when the profile makes you sure, and \
near 0.5 only when it does not. Also give `why`: at most twelve words.

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
                    "p_known": {"type": "number", "minimum": 0, "maximum": 1},
                    "why": {"type": "string"},
                },
                "required": ["id", "p_known", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["words"],
    "additionalProperties": False,
}


def short(text: str, limit: int = SENSE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def rank_text(freq: dict) -> str:
    rank = (freq or {}).get("freq_jiten_rank")
    return f"#{rank}" if rank else "unranked"


def item_text(w: dict, freq: dict) -> str:
    """How one note reads in the prompt, without its number: the cache key is made of this."""
    word = w["word"] or w["kanjified"]
    spelling = f", also written {w['kanjified']}" if w["kanjified"] and w["kanjified"] != word else ""
    lines = [f"{word} ({w['reading']}{spelling}), {w['pos'] or 'unknown part of speech'},"
             f" Jiten {rank_text(freq)}",
             f"   this sense: {short(w['meaning'], 300)}"]
    for s in w["siblings"][:6]:
        if s["relation"] == td.MEANING and s["meaning"] and s["meaning"] != w["meaning"]:
            lines.append(f"   other sense{' (studied)' if s['reviewed'] else ''}: {short(s['meaning'])}")
        elif s["relation"] != td.MEANING:
            lines.append(f"   also {s['key']} read {s['reading']}"
                         f"{' (studied)' if s['reviewed'] else ''}")
    return "\n".join(lines)


def cache_key(text: str) -> str:
    return hashlib.sha1(f"agents\n{PROMPT_VERSION}\n{text}".encode("utf-8")).hexdigest()


def verdict_group(level: tuple[int, int]) -> str:
    lo, hi = level
    if hi == 0:
        return "unknown"
    if lo >= 2 or (lo, hi) == (1, 2):
        return "known_well" if lo >= 2 else "thinks_known"
    return "thinks_known"


def profile_pool(words: dict, preds: dict) -> dict[str, list[int]]:
    """The labelled notes (training labels only, never the holdout), by verdict group."""
    pool: dict[str, list[int]] = {g: [] for g in PROFILE}
    for nid, p in preds.items():
        if p.get("label_source") in ("anki", "hand") and p.get("label_level") and nid in words:
            pool[verdict_group(tuple(p["label_level"]))].append(nid)
    for g in pool:
        pool[g].sort()
    return pool


def profile_text(pool, words, freq, exclude_bases: set[str], rng: random.Random) -> str:
    parts = []
    names = {"known_well": "Knows well (suspended, or scheduled two and a half years or more out)",
             "thinks_known": "Thinks they know (scheduled sooner)",
             "unknown": "Does not know (chose Learn)"}
    for group, n in PROFILE.items():
        allowed = [nid for nid in pool[group] if words[nid]["base"] not in exclude_bases]
        chosen = rng.sample(allowed, min(n, len(allowed)))
        chosen.sort(key=lambda nid: (freq.get(nid, {}).get("freq_jiten_rank") or 10**9, nid))
        rows = [f"- {words[nid]['word'] or words[nid]['kanjified']} ({words[nid]['reading']})"
                f" {rank_text(freq.get(nid, {}))}: {short(words[nid]['meaning'], 60)}"
                for nid in chosen]
        parts.append(f"{names[group]}:\n" + "\n".join(rows))
    return "\n\n".join(parts)


def read_cache() -> dict[str, dict]:
    return {r["key"]: r for r in td.read_jsonl(td.data_file(ANSWERS))}


def select(words: dict, preds: dict, holdout: set[int], lo: float, hi: float,
           labelled: int, wrong: frozenset[int] = frozenset()) -> list[int]:
    """The notes to ask about; none the user marked Wrong data, which the triage drops."""
    words = {nid: w for nid, w in words.items() if nid not in wrong}
    middle = [nid for nid, p in preds.items() if nid in words and not p["labelled"]
              and lo <= p["p_known"] <= hi]
    held = [nid for nid in holdout if nid in words]
    train = sorted(nid for nid, p in preds.items() if nid in words
                   and p.get("label_source") in ("anki", "hand") and lo <= p["p_known"] <= hi)
    rng = random.Random(SEED)
    train = rng.sample(train, min(labelled, len(train)))
    chosen = sorted(set(middle) | set(held) | set(train))
    random.Random(SEED + 1).shuffle(chosen)
    return chosen


def valid(answer: dict) -> bool:
    p = answer.get("p_known")
    return isinstance(p, (int, float)) and 0 <= p <= 1


def export(args) -> int:
    words = {w["nid"]: w for w in td.read_jsonl(td.data_file("words.jsonl"))}
    preds = {r["nid"]: r for r in td.read_jsonl(td.data_file("predictions.jsonl"))}
    freq = {r["nid"]: r for r in td.read_jsonl(td.data_file("frequency.jsonl"))}
    if not preds:
        sys.exit("no predictions.jsonl: run triage_model.py first")
    holdout = {r["nid"] for r in td.read_jsonl(td.data_file("judge_queue.jsonl"))
               if r["kind"] == "random"}
    cache = read_cache()
    wrong = frozenset(r["nid"] for r in td.read_jsonl(td.data_file("hand_labels.jsonl"))
                      if r.get("label") == "invalid")
    chosen = select(words, preds, holdout, args.lo, args.hi, args.labelled, wrong)
    todo = []
    for nid in chosen:
        text = item_text(words[nid], freq.get(nid, {}))
        key = cache_key(text)
        if key not in cache:
            todo.append((nid, text, key))
    print(f"{len(chosen)} notes chosen, {len(chosen) - len(todo)} answered, {len(todo)} to ask",
          file=sys.stderr)
    folder = args.dir or td.data_file(FOLDER)
    folder.mkdir(parents=True, exist_ok=True)
    pool = profile_pool(words, preds)
    batches = [todo[i : i + args.batch] for i in range(0, len(todo), args.batch)]
    for i, batch in enumerate(batches, 1):
        name = f"batch_{i:04d}"
        bases = {words[nid]["base"] for nid, _, _ in batch}
        profile = profile_text(pool, words, freq, bases, random.Random(SEED + i))
        answer = (folder / f"{name}.answer.json").resolve()
        items = "\n\n".join(f"[{j}] {text}" for j, (_, text, _) in enumerate(batch, 1))
        text = "\n\n".join([
            INSTRUCTIONS,
            "Answer with one JSON object matching this JSON Schema, and write it (only the JSON)"
            f" with your Write tool to:\n{answer}",
            json.dumps(SCHEMA),
            "THE LEARNER'S PROFILE\n\n" + profile,
            f"THE {len(batch)} ITEMS TO JUDGE\n\n" + items,
        ])
        (folder / f"{name}.md").write_text(text + "\n", encoding="utf-8")
        keys = [{"id": j, "nid": nid, "key": key} for j, (nid, _, key) in enumerate(batch, 1)]
        (folder / f"{name}.keys.json").write_text(json.dumps({"keys": keys}), encoding="utf-8")
    print(f"wrote {len(batches)} batch files to {folder}", file=sys.stderr)
    return 0


def ingest(args) -> int:
    folder = args.dir or td.data_file(FOLDER)
    cache = read_cache()
    added, lost = 0, 0
    with td.data_file(ANSWERS).open("a", encoding="utf-8") as out:
        for keys_path in sorted(folder.glob("batch_*.keys.json")):
            answer_path = keys_path.with_name(keys_path.name.replace(".keys.json", ".answer.json"))
            if not answer_path.exists():
                continue
            meta = json.loads(keys_path.read_text(encoding="utf-8"))
            try:
                result = json.loads(answer_path.read_text(encoding="utf-8"))
            except ValueError:
                lost += len(meta["keys"])
                continue
            answers = {a.get("id"): a for a in (result or {}).get("words", []) if isinstance(a, dict)}
            for k in meta["keys"]:
                if k["key"] in cache:
                    continue
                a = answers.get(k["id"])
                if a is None or not valid(a):
                    lost += 1
                    continue
                row = {"key": k["key"], "nid": k["nid"], "score": round(float(a["p_known"]), 4),
                       "why": str(a.get("why", ""))[:200], "version": PROMPT_VERSION,
                       "batch": keys_path.name.split(".")[0]}
                cache[k["key"]] = row
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                added += 1
    print(f"cached {added} answers, {lost} missing or invalid", file=sys.stderr)
    return 0


def status(args) -> int:
    rows = td.read_jsonl(td.data_file(ANSWERS))
    preds = {r["nid"]: r for r in td.read_jsonl(td.data_file("predictions.jsonl"))}
    by = Counter(preds.get(r["nid"], {}).get("label_source") or "unlabelled" for r in rows)
    print(f"{len(rows)} answers: " + ", ".join(f"{k} {v}" for k, v in by.most_common()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["export", "ingest", "status"])
    parser.add_argument("--dir", type=Path, default=None,
                        help=f"the batch folder (default: {FOLDER}/ beside the answers)")
    parser.add_argument("--lo", type=float, default=LO)
    parser.add_argument("--hi", type=float, default=HI)
    parser.add_argument("--labelled", type=int, default=LABELLED)
    parser.add_argument("--batch", type=int, default=BATCH)
    args = parser.parse_args()
    return {"export": export, "ingest": ingest, "status": status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
