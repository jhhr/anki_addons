"""Gathers the agents' per-case plans into one report, with the models' answers beside them.

Each agent writes `plans/nid_<NID>.md`. This reads them all back, pulls the verdict, confidence
and proposed action out of the skeleton, and writes a summary table followed by every plan in
full - so the decision can be made by reading one file rather than seventy-two.

The table's point is the last column: where the agent, which could look things up, ended up
somewhere neither model reached. Those are the ones worth reading first.

    py -3.10 word_array/research/vocab_reading_agents/collate.py [--out FILE]
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
ADDON_ROOT = HERE.parents[2]
AGENTS = ADDON_ROOT / "output" / "vocab_reading_agents"

FIELD = {
    "verdict": re.compile(r"^\*\*Verdict:\*\*\s*(\S+)", re.M),
    "confidence": re.compile(r"^\*\*Confidence:\*\*\s*(\S+)", re.M),
    "action": re.compile(r"^\*\*Proposed action:\*\*\s*(.+?)(?=\n\s*\n|\n##)", re.M | re.S),
}


def parsed(text: str) -> dict:
    out = {}
    for name, pattern in FIELD.items():
        found = pattern.search(text)
        out[name] = " ".join(found.group(1).split()) if found else ""
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plans", type=Path, default=AGENTS / "plans")
    parser.add_argument("--cases", type=Path, default=AGENTS / "cases.jsonl")
    parser.add_argument("--out", type=Path, default=ADDON_ROOT / "output" / "vocab_reading_plans.md")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    cases = {}
    for line in args.cases.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            cases[row["note_id"]] = row

    plans = {}
    for path in sorted(args.plans.glob("nid_*.md")):
        nid = int(path.stem.split("_")[1])
        text = path.read_text(encoding="utf-8")
        plans[nid] = (parsed(text), text)

    missing = [nid for nid in cases if nid not in plans]
    order = sorted(cases, key=lambda n: (plans.get(n, ({},))[0].get("verdict", "zz"), n))

    verdicts = Counter(p[0]["verdict"] for p in plans.values())
    confidence = Counter(p[0]["confidence"] for p in plans.values())

    lines = [
        "# Reading questions: what the agents propose",
        "",
        "%d cases, %d with a plan%s."
        % (len(cases), len(plans), ", %d MISSING" % len(missing) if missing else ""),
        "",
        "Verdicts: %s" % ", ".join("%s %d" % (v or "?", n) for v, n in verdicts.most_common()),
        "Confidence: %s" % ", ".join("%s %d" % (c or "?", n) for c, n in confidence.most_common()),
        "",
        "`sonnet` and `opus` are what the two blind model passes answered; the agent could look"
        " notes and sentences up, so where it differs from both, it saw something they could not.",
        "",
        "| nid | word | note | array | agent | conf | sonnet | opus | new? |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    fresh = []
    for nid in order:
        case = cases[nid]
        plan = plans.get(nid, ({},))[0]
        answers = case.get("answers", {})
        sonnet = (answers.get("claude-sonnet-5") or [""])[0]
        opus = (answers.get("claude-opus-5") or [""])[0]
        agent = plan.get("verdict", "")
        new = agent and agent not in (sonnet, opus)
        if new:
            fresh.append(nid)
        lines.append(
            "| %d | %s | %s | %s | **%s** | %s | %s | %s | %s |"
            % (
                nid,
                case["key"] or case["spelling"],
                case["note_reading"],
                case["array_reading"],
                agent or "-",
                plan.get("confidence", ""),
                sonnet,
                opus,
                "**yes**" if new else "",
            )
        )

    if fresh:
        lines += [
            "",
            "## Where the agent reached a verdict neither model did (%d)" % len(fresh),
            "",
        ]
        for nid in fresh:
            lines.append("- **%d %s** - %s" % (nid, cases[nid]["key"], plans[nid][0]["action"]))

    lines += ["", "## Every proposed action", ""]
    for nid in order:
        plan = plans.get(nid, ({},))[0]
        lines.append("- **%d %s** - `%s` - %s" % (nid, cases[nid]["key"], plan.get("verdict", "-"),
                                                  plan.get("action", "no plan file")))

    if missing:
        lines += ["", "## No plan was written for these", ""]
        lines += ["- %d %s" % (nid, cases[nid]["key"]) for nid in missing]

    lines += ["", "---", "", "# The plans in full", ""]
    for nid in order:
        if nid in plans:
            lines += [plans[nid][1].strip(), "", "---", ""]

    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("%d plans -> %s" % (len(plans), args.out))
    if missing:
        print("missing: %s" % ", ".join(str(n) for n in missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
