"""Renders the kanjify golden set's agent prompts into queues for `agent_queue.py`.

    py -3.10 word_array/research/kanjify_golden_render.py words [--effort xhigh] [--wids ...]
    py -3.10 word_array/research/kanjify_golden_render.py batches [--effort high] [--bids ...]
    py -3.10 word_array/research/kanjify_golden_render.py pilot

`words` (step 1): one prompt per inventory word, `kanjify_agents/word_template.md` filled with
the word, its JMdict entries and its uses (at most `--max-uses`, taken evenly from each way the
collection writes it; `kanjify_lookup.py uses` shows an agent the rest), plus the words step 2
handed back (`collated/queue.jsonl`). The pilot's words come first, so the pilot's batch agent
has their decisions early; then the inventory's order. -> `queues/words.jsonl`.

`batches` (step 2): one prompt per inventory batch, `batch_template.md` filled with its input
sentences and the decisions (`decisions.jsonl`, written by the collate step) of the words they
hold. A batch never says which of its sentences are hand-fixed or labelled twice.
-> `queues/batches.jsonl`.

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
from collections import defaultdict
from pathlib import Path

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


def use_line(u: dict) -> str:
    how = f"kanjified {u['kanji']}" if u["kind"] == "kanjified" else u["kind"]
    return f"- {u['sid']} {how}: {u['text']}"


def jmdict_block(word: dict) -> str:
    forms = [word["kana"]]
    if word["word"] != word["kana"]:
        forms.append(word["word"])
    text = "\n\n".join(lookup.jmdict_text(f) for f in forms)
    return "```\n" + text + "\n```"


def word_prompt(word: dict, policy: str, max_uses: int) -> str:
    uses = sample_uses(word["uses"], max_uses)
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
        .replace("{USES}", "\n".join(use_line(u) for u in uses))
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
                 single: bool = False) -> str:  # fmt: skip
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
        .replace("{SENTENCES}", "\n".join(f"- {s['sid']}: {s['sentence']}" for s in sentences))
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
    p = sub.add_parser("pilot")
    p.add_argument("--single-effort", default="xhigh")
    p.add_argument("--batch-effort", default="high")
    p.add_argument("--batch-sizes", type=int, nargs="+", default=[10, 30])
    args = parser.parse_args()

    policy = golden.policy_text()
    golden.QUEUES.mkdir(parents=True, exist_ok=True)
    if args.command == "words":
        words = read("words.jsonl")
        pilot_sids = set(json.loads((golden.INVENTORY / "pilot.json").read_text())["sids"])
        pilot_words = {w for s in read("sentences.jsonl") if s["sid"] in pilot_sids
                       for w in s["words"]}  # fmt: skip
        words.sort(key=lambda w: w["wid"] not in pilot_words)
        if args.wids:
            words = [w for w in words if w["wid"] in args.wids]
        items = [
            item(w["wid"], word_prompt(w, policy, args.max_uses), args.effort, "lookup+web",
                 "word_schema.json", kind="word", word=w["word"], kana=w["kana"])  # fmt: skip
            for w in words
        ]
        n = golden.write_jsonl(golden.QUEUES / "words.jsonl", items)
        print(f"{n} word prompts -> {golden.QUEUES / 'words.jsonl'}")
        return 0

    sentences = {s["sid"]: s for s in read("sentences.jsonl")}
    decisions = read_decisions()
    if args.command == "batches":
        batches = read("batches.jsonl")
        if args.bids:
            batches = [b for b in batches if b["bid"] in args.bids]
        items = []
        for b in batches:
            rows = [sentences[sid] for sid in b["sids"]]
            items.append(
                item(b["bid"], batch_prompt(rows, decisions, policy), args.effort, "lookup",
                     "batch_schema.json", kind="batch", sids=b["sids"])  # fmt: skip
            )
        n = golden.write_jsonl(golden.QUEUES / "batches.jsonl", items)
        print(f"{n} batch prompts ({len(decisions)} decisions) -> {golden.QUEUES}")
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
