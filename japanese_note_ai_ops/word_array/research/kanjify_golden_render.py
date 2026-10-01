"""Renders the kanjify golden set's agent prompts into queues for `agent_queue.py`.

    py -3.10 word_array/research/kanjify_golden_render.py words [--effort xhigh] [--wids ...]
    py -3.10 word_array/research/kanjify_golden_render.py batches [--effort high] [--bids ...]
    py -3.10 word_array/research/kanjify_golden_render.py relabel SIDS_FILE [--size 30]
    py -3.10 word_array/research/kanjify_golden_render.py readings [--size 20]
    py -3.10 word_array/research/kanjify_golden_render.py pilot

`words` (step 1): one prompt per inventory word, `kanjify_agents/word_template.md` filled with
the word, its JMdict entries and its uses (at most `--max-uses`, taken evenly from each way the
collection writes it; `kanjify_lookup.py uses` shows an agent the rest), plus the words step 2
handed back (`handed_back.jsonl`, with the labellers' reasons). Under a use or a sentence, in
both steps, the note's translation (`kanjify_golden.translations`). The pilot's words come first,
so the pilot's batch agent has their decisions early; then the inventory's order, then the
handed-back words. -> `queues/words.jsonl`.

`batches` (step 2): one prompt per inventory batch, `batch_template.md` filled with its input
sentences and the decisions (`decisions.jsonl`, written by the collate step) of the words they
hold, handed-back words included. A batch never says which of its sentences are hand-fixed or
labelled twice.
-> `queues/batches.jsonl`.

`relabel SIDS_FILE`: the same prompts for only the sentences a newer policy or decision table
changed, in new batches of `--size`. Their rows replace the batch rows of those sentences in the
collate step. -> `queues/relabel.jsonl`.

`readings`: the reading pass, for the notes rather than the labels: the sentences of the
collate step's furigana fix list with anything a program can't repair (a kanji with no reading,
a labeller's fix), and those a labeller held back for a typo, in batches of `--size`,
`kanjify_agents/reading_template.md` filled with each one's findings. An agent writes each
sentence with its furigana fixed; the collate step puts that on the fix list, and
`furigana_fix.py --readings` writes it into the note. -> `queues/readings.jsonl`.

`pilot`: the pilot's sentences three ways, to compare cost and agreement before scaling step 2:
`pilot_single` (one agent per sentence, deciding words itself, with web search), `pilot_batch`
(batch agents with the decision table, in batches of 10 and of 30; written only once every
pilot word has its decision) and `pilot_op` (kanjify_sentence's own prompt, no
tools, as the op sends it).

Every item records the policy and decision table version it was rendered with. Prompts hold
`{PYTHON}` for the command agents run the lookups with; the driver fills it on the machine that
runs them, so a queue is the same on every machine and names none of its folders.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import kanjify_golden as golden
import kanjify_lookup as lookup

MODEL = "claude-opus-5-5"


def template(name: str) -> str:
    return (golden.AGENTS_DIR / name).read_text(encoding="utf-8")


def read(name: str) -> list[dict]:
    return golden.read_jsonl(golden.INVENTORY / name)


def sample_uses(uses: list[dict], cap: int) -> list[dict]:
    """At most `cap` uses, taken in turn from each way the word is written now (kana, each
    kanji), so a rare spelling is never crowded out by the common one."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for u in uses:
        groups[(u["kind"], u["kanji"])].append(u)
    queues = sorted(groups.values(), key=len)
    out: list[dict] = []
    while len(out) < cap and any(queues):
        for q in queues:
            if q and len(out) < cap:
                out.append(q.pop(0))
    return out


def translation_line(sid: str, translations: dict[str, str]) -> str:
    """The note's translation, on its own line under the sentence's: never read as part of it."""
    return f'\n  translation: "{translations[sid]}"' if translations.get(sid) else ""


def use_line(u: dict, translations: dict[str, str]) -> str:
    how = f"kanjified {u['kanji']}" if u["kind"] == "kanjified" else u["kind"]
    return f"- {u['sid']} {how}: {u['text']}" + translation_line(u["sid"], translations)


def jmdict_block(word: dict) -> str:
    forms = [word["kana"]]
    if word["word"] != word["kana"]:
        forms.append(word["word"])
    text = "\n\n".join(lookup.jmdict_text(f) for f in forms)
    return "```\n" + text + "\n```"


def handed_word(entry: dict, sentences: dict[str, dict]) -> dict:
    """A word step 2 handed back (`golden.HANDED`), shaped as an inventory word for
    `word_prompt`. It has no survey counts, so the prompt gives the labellers' reasons."""
    word = lookup.plain(entry["word"])
    uses = []
    for sid in entry["sids"]:
        text = lookup.plain(sentences[sid]["sentence"]) if sid in sentences else ""
        at = text.find(word) if word else -1
        if at >= 0:
            text = f"{text[:at]}【{word}】{text[at + len(word):]}"
        uses.append({"sid": sid, "kind": "kana", "kanji": "", "text": text})
    return {"wid": entry["wid"], "word": word, "kana": entry["kana"] or word,
            "pos": "as a step 2 labeller wrote it", "uses": uses, "why": entry["why"]}  # fmt: skip


def word_prompt(word: dict, policy: str, max_uses: int,
                translations: Optional[dict[str, str]] = None) -> str:  # fmt: skip
    uses = sample_uses(word["uses"], max_uses)
    if "why" in word:
        counts = "Step 2's sentence labellers handed it back undecided: " + " / ".join(
            dict.fromkeys(word["why"][:4])
        )
    else:
        kanjified = ", ".join(f"{k} ×{n}" for k, n in word["kanjified"].items()) or "never"
        counts = (
            f"In the collection's current labels it is kanjified {kanjified}; kana"
            f" ×{word['kana_uses']}; already in kanji in the input ×{word['kanji_uses']}"
            f" (those are not shown and not yours to change)."
        )
    note = (
        f"All {len(uses)} uses are listed."
        if len(uses) == len(word["uses"])
        else f"{len(uses)} of its {len(word['uses'])} uses are listed, taken evenly from each"
        f" way it is written now; `kanjify_lookup.py uses {word['wid']}` shows every one."
    )
    return (
        template("word_template.md")
        .replace("{WORD}", word["word"])
        .replace("{KANA}", word["kana"])
        .replace("{POS}", word["pos"])
        .replace("{WID}", word["wid"])
        .replace("{COUNTS}", counts)
        .replace("{JMDICT}", jmdict_block(word))
        .replace("{USES_NOTE}", note)
        .replace("{USES}", "\n".join(use_line(u, translations or {}) for u in uses))
        .replace("{POLICY}", policy)
    )


def read_decisions() -> dict[str, dict]:
    return {d["wid"]: d for d in golden.read_jsonl(golden.DECISIONS)}


def decision_block(d: dict) -> str:
    lines = [f"### {d['kana']} ({d['word']}, {d['pos']}): `{d['wid']}`"]
    for n, u in enumerate(d["uses"], 1):
        if u["write"] == "kanji":
            how = f"kanji {u['spelling']}"
        elif u["write"] == "kana":
            how = "kana"
        else:
            how = "undecided: leave as the input has it and list it under pending"
        lines.append(f"{n}. {u['use']}: {how}. Example: {u['example']} ({u['rule']})")
    for n in d.get("not_this_word", [])[:3]:
        lines.append(f"- not this word: {n['note']}")
    return "\n".join(lines)


SINGLE_STEPS = """3. No word decisions are given: decide each word yourself from the policy and the
   dictionaries (the JMdict lookup, and WebSearch / WebFetch on jisho.org, weblio.jp,
   kotobank.jp). The policy's rules still bind you: never invent a split or a rule.
4. A use that turns on a pending question (listed at the end of the policy) is written as the
   policy's draft answer says and listed under `pending` with the question id. A word the
   policy leaves truly open is left as the input has it and listed under `pending`."""


def batch_prompt(sentences: list[dict], decisions: dict[str, dict], policy: str,
                 single: bool = False, translations: Optional[dict[str, str]] = None,
                 ) -> str:  # fmt: skip
    words = sorted({w for s in sentences for w in s["words"]})
    blocks = [decision_block(decisions[w]) for w in words if w in decisions]
    text = template("batch_template.md")
    if single:
        start = text.index("3. A word listed under")
        end = text.index("5. Write the sentence")
        text = text[:start] + SINGLE_STEPS + "\n" + text[end:]
        text = text.replace(
            "- `{PYTHON} word_array/research/kanjify_lookup.py jmdict WORD`",
            "- WebSearch and WebFetch for the dictionaries.\n"
            "- `{PYTHON} word_array/research/kanjify_lookup.py jmdict WORD`",
        )
        blocks = ["(none: decide each word yourself, see step 3)"]
    return (
        text.replace("{COUNT}", str(len(sentences)))
        .replace("{DECISIONS}", "\n\n".join(blocks) or "(none of these sentences' words)")
        .replace("{SENTENCES}", "\n".join(
            f"- {s['sid']}: {s['sentence']}" + translation_line(s["sid"], translations or {})
            for s in sentences
        ))  # fmt: skip
        .replace("{POLICY}", policy)
    )


def item(id_: str, prompt: str, effort: str, tools: str, schema: str, **meta) -> dict:
    return {
        "id": id_,
        "prompt": prompt,
        "model": MODEL,
        "effort": effort,
        "tools": tools,
        "schema": schema,
        "meta": {
            "policy_version": golden.policy_version(),
            "decisions_version": golden.decisions_version(),
            **meta,
        },
    }


def relabel_items(rows: list[dict], decisions: dict[str, dict], policy: str,
                  translations: dict[str, str], args) -> list[dict]:  # fmt: skip
    """Batches of just the sentences a policy or decision change touched. Named by when they
    were rendered, so ids never repeat across rounds (a saved id is never run again) and the
    collate step can take the latest round's row of a sentence by name."""
    stamp = time.strftime("%y%m%d%H%M")
    return [
        item(f"r{stamp}-{n:03d}", batch_prompt(rows[i : i + args.size], decisions, policy,
                                               translations=translations),
             args.effort, "lookup", "batch_schema.json", kind="relabel",
             sids=[s["sid"] for s in rows[i : i + args.size]])  # fmt: skip
        for n, i in enumerate(range(0, len(rows), args.size), 1)
    ]


def reading_rows(fixes: list[dict], pending: list[dict]) -> list[dict]:
    """The sentences for the reading pass: those on the furigana fix list with anything
    `kanjify_golden.fix_groups` does not repair, and those a labeller held back for a typo in
    the text, which the note has to be fixed for too."""
    rows = [{**r, "pending": []} for r in fixes if r["labeller"]
            or any(not p.startswith(golden.REPAIRED) for p in r["program"])]  # fmt: skip
    listed = {r["sid"] for r in rows}
    for r in pending:
        typos = [p for p in r["pending"] if "typo" in p.get("why", "")]
        if typos and r["sid"] not in listed:
            listed.add(r["sid"])
            rows.append({"sid": r["sid"], "sentence": r["sentence"], "program": [],
                         "labeller": [], "pending": typos})  # fmt: skip
    return rows


def reading_block(row: dict, translations: dict[str, str]) -> str:
    lines = [f"- {row['sid']}: {row['sentence']}" + translation_line(row["sid"], translations)]
    lines += [f"  found by a program: {p}" for p in row["program"]]
    lines += [f"  a labeller ({f['confidence']}): `{f['group']}` -> `{f['fix']}`: {f['problem']}"
              for f in row["labeller"]]  # fmt: skip
    lines += [f"  held back by a labeller: {p['word']}: {p['why']}" for p in row["pending"]]
    return "\n".join(lines)


def reading_items(rows: list[dict], translations: dict[str, str], size: int,
                  effort: str) -> list[dict]:  # fmt: skip
    """The reading pass's prompts, named like relabel rounds by when they were rendered."""
    stamp = time.strftime("%y%m%d%H%M")
    out = []
    for n, i in enumerate(range(0, len(rows), size), 1):
        chunk = rows[i : i + size]
        prompt = (template("reading_template.md").replace("{COUNT}", str(len(chunk)))
                  .replace("{SENTENCES}", "\n".join(reading_block(r, translations)
                                                    for r in chunk)))  # fmt: skip
        out.append(item(f"f{stamp}-{n:03d}", prompt, effort, "lookup", "reading_schema.json",
                        kind="readings", sids=[r["sid"] for r in chunk]))  # fmt: skip
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("words")
    p.add_argument("--effort", default="xhigh")
    p.add_argument("--wids", nargs="*", default=[])
    p.add_argument("--max-uses", type=int, default=120)
    p = sub.add_parser("batches")
    p.add_argument("--effort", default="high")
    p.add_argument("--bids", nargs="*", default=[])
    p = sub.add_parser("relabel")
    p.add_argument("sids", type=Path, help="a file of sentence ids, one per line")
    p.add_argument("--effort", default="high")
    p.add_argument("--size", type=int, default=30)
    p = sub.add_parser("readings")
    p.add_argument("--effort", default="high")
    p.add_argument("--size", type=int, default=20)
    p = sub.add_parser("pilot")
    p.add_argument("--single-effort", default="xhigh")
    p.add_argument("--batch-effort", default="high")
    p.add_argument("--batch-sizes", type=int, nargs="+", default=[10, 30])
    args = parser.parse_args()

    policy = golden.policy_text()
    translations = golden.translations()
    golden.QUEUES.mkdir(parents=True, exist_ok=True)
    if args.command == "readings":
        rows = reading_rows(golden.read_jsonl(golden.COLLATED / "furigana_fixes.jsonl"),
                            golden.read_jsonl(golden.COLLATED / "pending.jsonl"))  # fmt: skip
        n = golden.write_jsonl(golden.QUEUES / "readings.jsonl",
                               reading_items(rows, translations, args.size, args.effort))  # fmt: skip
        print(f"{n} reading prompts for {len(rows)} sentences -> {golden.QUEUES}")
        return 0
    if args.command == "words":
        words = read("words.jsonl")
        pilot_sids = set(json.loads((golden.INVENTORY / "pilot.json").read_text())["sids"])
        pilot_words = {w for s in read("sentences.jsonl") if s["sid"] in pilot_sids
                       for w in s["words"]}  # fmt: skip
        words.sort(key=lambda w: w["wid"] not in pilot_words)
        sentences = {s["sid"]: s for s in read("sentences.jsonl")}
        words += [handed_word(h, sentences) for h in golden.read_jsonl(golden.HANDED)]
        if args.wids:
            words = [w for w in words if w["wid"] in args.wids]
        items = [
            item(w["wid"], word_prompt(w, policy, args.max_uses, translations), args.effort,
                 "lookup+web", "word_schema.json", kind="word", word=w["word"],
                 kana=w["kana"])  # fmt: skip
            for w in words
        ]
        n = golden.write_jsonl(golden.QUEUES / "words.jsonl", items)
        print(f"{n} word prompts -> {golden.QUEUES / 'words.jsonl'}")
        return 0

    sentences = {s["sid"]: s for s in read("sentences.jsonl")}
    for h in golden.read_jsonl(golden.HANDED):
        for sid in h["sids"]:
            if sid in sentences:
                sentences[sid] = {**sentences[sid], "words": sentences[sid]["words"] + [h["wid"]]}
    decisions = read_decisions()
    if args.command == "batches":
        batches = read("batches.jsonl")
        if args.bids:
            batches = [b for b in batches if b["bid"] in args.bids]
        items = []
        for b in batches:
            rows = [sentences[sid] for sid in b["sids"]]
            items.append(
                item(b["bid"], batch_prompt(rows, decisions, policy, translations=translations),
                     args.effort, "lookup", "batch_schema.json", kind="batch",
                     sids=b["sids"])  # fmt: skip
            )
        n = golden.write_jsonl(golden.QUEUES / "batches.jsonl", items)
        print(f"{n} batch prompts ({len(decisions)} decisions) -> {golden.QUEUES}")
        return 0
    if args.command == "relabel":
        wanted = dict.fromkeys(line.strip() for line in args.sids.read_text().splitlines())
        rows = [sentences[sid] for sid in wanted if sid in sentences]
        n = golden.write_jsonl(golden.QUEUES / "relabel.jsonl",
                               relabel_items(rows, decisions, policy, translations, args))  # fmt: skip
        print(f"{n} relabel prompts for {len(rows)} sentences -> {golden.QUEUES}")
        return 0

    pilot = json.loads((golden.INVENTORY / "pilot.json").read_text())["sids"]
    rows = [sentences[sid] for sid in pilot]
    single = [
        item(f"single_{s['sid']}", batch_prompt([s], {}, policy, single=True), args.single_effort,
             "lookup+web", "batch_schema.json", kind="pilot_single", sids=[s["sid"]])  # fmt: skip
        for s in rows
    ]
    batched = [
        item(f"batch{size}_{n:02d}", batch_prompt(rows[i : i + size], decisions, policy),
             args.batch_effort, "lookup", "batch_schema.json", kind="pilot_batch",
             sids=[s["sid"] for s in rows[i : i + size]])  # fmt: skip
        for size in args.batch_sizes
        for n, i in enumerate(range(0, len(rows), size))
    ]
    op, _ = __import__("kanjify_eval").load_op()
    op_items = []
    for s in rows:
        it = item(f"op_{s['sid']}", op.get_kanjify_sentence_prompt(s["sentence"]), "", "none",
                  "", kind="pilot_op", sids=[s["sid"]])  # fmt: skip
        op_items.append(it)
    queues = [("pilot_single", single), ("pilot_op", op_items)]
    # The batch way is the one with the decision table: rendered before its words are decided
    # it would measure a batch agent without one, and a running driver takes a queue file as
    # soon as it is written
    missing = sorted({w for s in rows for w in s["words"]} - set(decisions))
    if missing:
        print(f"pilot_batch not written: {len(missing)} of its words have no decision yet")
    else:
        queues.append(("pilot_batch", batched))
    for name, items in queues:
        golden.write_jsonl(golden.QUEUES / f"{name}.jsonl", items)
        print(f"{len(items)} -> {name}.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
