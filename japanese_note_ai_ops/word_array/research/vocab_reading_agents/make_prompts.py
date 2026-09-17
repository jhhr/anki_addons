"""Renders one agent prompt per batch of reading questions `vocab_reading_judge` could not settle.

The judge asks a model one word per request, with no way to look anything up: it sees the note,
the two readings and up to three sentences, and answers with one of four verdicts. The cases
that survive that are the ones where the answer is not in the prompt - whether another note
already holds the other reading, how the element is used across every sentence rather than
three, what the note's own history is - so they go to an agent that can look those up instead,
and that writes a plan to read rather than a verdict to apply.

Both models' answers travel with each case, disagreements marked, because where
`claude-sonnet-5` and `claude-opus-5` differ is exactly where the extra evidence is worth
fetching.

    py -3.10 word_array/research/vocab_reading_agents/make_prompts.py [--batch N] [--out DIR]

Writes `<out>/NN_<key>.md`, one per batch, and `<out>/cases.jsonl` for the record.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
RESEARCH = HERE.parent
sys.path.insert(0, str(RESEARCH))

import vocab_dupes  # noqa: E402
import vocab_reading_judge as judge  # noqa: E402

ADDON_ROOT = RESEARCH.parents[1]
MODELS = ["claude-sonnet-5", "claude-opus-5"]


def verdicts_by_note(cases: list, cached: dict) -> dict:
    """`{note id: {model: (verdict, reading, why)}}` for every model that has answered."""
    out: dict = {case.note_id: {} for case in cases}
    for model in MODELS:
        for case, verdict, reading, why in judge.verdicts(cases, model, cached):
            out[case.note_id][model] = (verdict, reading, why)
    return out


def case_block(n: int, case, answers: dict) -> str:
    """One case as the agent sees it: the question, the evidence, and what each model said."""
    lines = [
        "### Case %d - %s  (nid %d)" % (n, case.key or case.spelling, case.note_id),
        "",
        "| | |",
        "|---|---|",
        "| spelling | %s |" % case.spelling,
        "| the note reads it | %s |" % case.note_reading,
        "| the sentences read it | %s |" % case.array_reading,
        "| how the readings differ | %s |" % case.family,
        "| links | %d |" % case.links,
    ]
    if case.meaning:
        lines.append("| the note's meaning | %s |" % case.meaning.replace("|", "/"))
    if case.owned_by:
        lines.append(
            "| another note owns it | %s read %s is already note %s |"
            % (case.spelling, case.array_reading, ", ".join(str(n) for n in case.owned_by))
        )
    lines.append("")
    if case.sentences:
        lines.append("Sentences (up to three; `links NID` shows them all):")
        lines += ["- %s" % s for s in case.sentences]
        lines.append("")
    said = answers.get(case.note_id, {})
    pair = {m: said.get(m) for m in MODELS}
    agree = len({v[0] for v in pair.values() if v}) <= 1
    lines.append("What the two models answered %s:" % ("(they agree)" if agree else "(THEY DISAGREE)"))
    for model in MODELS:
        got = pair.get(model)
        if got:
            lines.append(
                "- **%s**: `%s` %s - %s" % (model, got[0], got[1] or "", got[2])
            )
        else:
            lines.append("- **%s**: did not answer" % model)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ADDON_ROOT / "output" / "vocab_reading_agents")
    parser.add_argument("--batch", type=int, default=6, help="cases per agent")
    parser.add_argument(
        "--only",
        choices=["all", "disagreed"],
        default="all",
        help="'disagreed' keeps only the cases the two models answered differently",
    )
    args = parser.parse_args()

    cases = judge.collect(vocab_dupes.read_dump())
    cached = judge.read_results()
    answers = verdicts_by_note(cases, cached)
    if args.only == "disagreed":
        cases = [
            c
            for c in cases
            if len({v[0] for v in answers.get(c.note_id, {}).values()}) > 1
        ]

    template = (HERE / "agent_template.md").read_text(encoding="utf-8")
    args.out.mkdir(parents=True, exist_ok=True)
    plans = args.out / "plans"
    plans.mkdir(exist_ok=True)

    with (args.out / "cases.jsonl").open("w", encoding="utf-8") as fh:
        for case in cases:
            row = case._asdict()
            row["answers"] = {m: list(v) for m, v in answers.get(case.note_id, {}).items()}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    batches = [cases[i : i + args.batch] for i in range(0, len(cases), args.batch)]
    for n, batch in enumerate(batches, 1):
        blocks = [case_block(i, c, answers) for i, c in enumerate(batch, 1)]
        # An ASCII name: a Japanese one is mangled the moment an agent puts it on a command line.
        slug = "batch_%02d" % n
        # Its own scratch directory per batch: the agents run at the same time, and sharing one
        # made them overwrite each other's evidence files half way through a run.
        work = args.out / "work" / slug
        work.mkdir(parents=True, exist_ok=True)
        prompt = (
            template.replace("{BATCH}", str(n))
            .replace("{COUNT}", str(len(batch)))
            .replace("{NIDS}", ", ".join(str(c.note_id) for c in batch))
            .replace("{CASES}", "\n\n".join(blocks))
            .replace("{PLANS}", str(plans))
            .replace("{WORK}", str(work))
            .replace("{SLUG}", slug)
        )
        (args.out / ("%s.md" % slug)).write_text(prompt, encoding="utf-8")
        print("%2d cases  %s" % (len(batch), slug))
    print("\n%d cases in %d batches -> %s" % (len(cases), len(batches), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
