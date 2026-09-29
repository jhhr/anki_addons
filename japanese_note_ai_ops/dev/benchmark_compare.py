"""Compare benchmark summaries by where the time went (issue #11, stage 6).

    python dev/benchmark_compare.py HISTORY [--base SELECTOR] [--head SELECTOR] [--phases]

HISTORY is a benchmark.py history file (`user_files/benchmarks/<fixture>.jsonl`, one summary per
line). A selector picks summaries by label or by commit prefix; `--head` defaults to the last
summary, `--base` to the last one before the head's first. Only summaries with the head's profile (latency, scale,
memory, background, CopyAnywhere) and corpus are compared: a change of profile is not a change
of the code.

The time is attributed to the four places issue #11 names, each a median over the selected
runs:

- async: planning and running the plans (the `nested op` phases of the async body);
- collection: the seconds the run held the collection (the gate's collection share of its
  time), and how many turns;
- writes: the cleanup's writes, the undo merges of its adding included, without the adds;
- hooks: the adds themselves, which is where every addon's note_will_be_added runs.

`--phases` adds every phase's median. A difference within a few percent between runs of one
commit is the machine's noise; the history holds the runs to judge that by.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

ASYNC_PHASES = ("nested op: plan notes", "nested op: run plans")
ADD_LOOP = "cleanup: add_note loop"


def load(history: Path) -> list[dict]:
    return [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line]


def matches(summary: dict, selector: str) -> bool:
    commit = (summary.get("where") or {}).get("commit") or ""
    return summary.get("label") == selector or (bool(commit) and commit.startswith(selector))


# What a summary written before a profile field existed ran with
PROFILE_DEFAULTS = {"background": 0, "copy_anywhere": False, "memory": None}


def profile(summary: dict) -> dict:
    return {**PROFILE_DEFAULTS, **(summary.get("profile") or {})}


# The corpus hashes summaries have held: of what a run is given (inputs_sha1), and before it of
# the files (sha1), which gzipping or trimming them changed
CORPUS_HASHES = ("inputs_sha1", "sha1")


def comparable(summary: dict, head: dict) -> bool:
    """The same profile, and the same corpus: by a hash both summaries have, else by the
    fixture's name (a summary from before the hash was recorded, or of another kind)."""
    if profile(summary) != profile(head) or summary.get("fixture") != head.get("fixture"):
        return False
    corpora = [summary.get("corpus") or {}, head.get("corpus") or {}]
    for key in CORPUS_HASHES:
        if all(key in corpus for corpus in corpora):
            return corpora[0][key] == corpora[1][key]
    return True


def phase_seconds(run: dict) -> dict[str, float]:
    totals: dict[str, float] = {}
    for phase in run.get("phases") or []:
        totals[phase["label"]] = totals.get(phase["label"], 0.0) + phase["seconds"]
    return totals


def add_loop(run: dict) -> dict:
    for phase in run.get("phases") or []:
        if phase["label"] == ADD_LOOP:
            return phase
    return {}


def gate(run: dict) -> dict:
    for metric in run.get("metrics") or []:
        if metric.get("kind") == "metrics.gate":
            return metric
    return {}


def figures(run: dict) -> dict[str, float]:
    """One run's time by where it went."""
    phases = phase_seconds(run)
    loop = add_loop(run)
    gate_figures = gate(run)
    writes = sum(
        seconds
        for label, seconds in phases.items()
        if label.startswith("cleanup:") and label not in (ADD_LOOP, "cleanup: finished")
    )
    share = gate_figures.get("collection_share") or 0.0
    return {
        "total": run["seconds"],
        "async": sum(phases.get(label, 0.0) for label in ASYNC_PHASES),
        "collection": share * (gate_figures.get("seconds") or 0.0),
        "collection turns": float(gate_figures.get("collection_turns") or 0),
        "writes": writes + (loop.get("merge_seconds") or 0.0),
        "hooks": loop.get("add_seconds") or 0.0,
        "calls per minute": run.get("calls_per_minute") or 0.0,
    }


def medians(runs: Iterable[dict], key: Callable[[dict], dict[str, float]]) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for run in runs:
        for name, value in key(run).items():
            values.setdefault(name, []).append(value)
    return {name: statistics.median(found) for name, found in values.items()}


def table(base: dict[str, float], head: dict[str, float]) -> list[str]:
    lines = [f"{'':32} {'base':>10} {'head':>10} {'change':>10} {'%':>7}"]
    for name in list(dict.fromkeys([*base, *head])):
        b, h = base.get(name), head.get(name)
        if b is None or h is None:
            lines.append(f"{name:32} {b!s:>10} {h!s:>10}")
            continue
        change = h - b
        percent = f"{100 * change / b:+.1f}" if b else ""
        lines.append(f"{name:32} {b:10.3f} {h:10.3f} {change:+10.3f} {percent:>7}")
    return lines


def select(
    summaries: list[dict], head_selector: Optional[str], base_selector: Optional[str]
) -> tuple[list[dict], list[dict]]:
    """The head summaries and the base ones, of the last head summary's profile and corpus.

    By position in the history: a head selector picks every summary it matches, and the base
    by default is the last summary before the first of them. Taking "everything but the last
    head summary" made the base another summary of the head (the head compared with itself), or
    one recorded after it (every change's sign turned over); a base selector matching a head
    summary took it for a base too.
    """
    if head_selector is None:
        head_at = [len(summaries) - 1] if summaries else []
    else:
        head_at = [i for i, summary in enumerate(summaries) if matches(summary, head_selector)]
    if not head_at:
        return [], []
    head = summaries[head_at[-1]]
    head_at = [i for i in head_at if comparable(summaries[i], head)]
    heads = [summaries[i] for i in head_at]
    others = [
        (i, summary)
        for i, summary in enumerate(summaries)
        if i not in set(head_at) and comparable(summary, head)
    ]
    if base_selector is None:
        bases = [summary for i, summary in others if i < head_at[0]][-1:]
    else:
        bases = [summary for _, summary in others if matches(summary, base_selector)]
    return heads, bases


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("history", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--phases", action="store_true")
    args = parser.parse_args(argv)

    summaries = load(args.history)
    if not summaries:
        print("no summaries", file=sys.stderr)
        return 2
    heads, bases = select(summaries, args.head, args.base)
    if not heads:
        print(f"no summary matches {args.head!r}", file=sys.stderr)
        return 2
    if not bases:
        print("nothing to compare with: no other summary of the same profile and corpus",
              file=sys.stderr)
        return 2
    base_runs = [run for summary in bases for run in summary["runs"]]
    head_runs = [run for summary in heads for run in summary["runs"]]

    def described(group: list[dict]) -> str:
        commits = sorted({(s.get("where") or {}).get("commit") or "?" for s in group})
        labels = sorted({s.get("label") or "-" for s in group})
        return f"{', '.join(commits)} ({', '.join(labels)})"

    print(f"base: {described(bases)}, {len(base_runs)} runs")
    print(f"head: {described(heads)}, {len(head_runs)} runs")
    print(f"profile: {json.dumps(profile(heads[-1]))}")
    print("\n".join(table(medians(base_runs, figures), medians(head_runs, figures))))
    if args.phases:
        print()
        print("\n".join(table(medians(base_runs, phase_seconds), medians(head_runs, phase_seconds))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
