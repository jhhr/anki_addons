#!/usr/bin/env python3
"""Score a code-review-fix run against the findings document that drove it.

Given the branch state before a run (``--base``) and after it (``--head``), report
for each numbered finding whether the cited file was touched and whether any hunk
landed near the cited line. That is a mechanical signal, not a verdict: a fix can
be correct and land far from the cited line, and a file can be touched without the
finding being addressed. The point is to make the human/LLM judgement step cheap
and consistent across arms, not to replace it.

Usage:

    python eval/score_findings.py \
        --findings docs/code-review-jnaio-auto-rate-limit.md \
        --base  <pre-run sha> \
        --head  <post-run ref> \
        --label baseline

Compare two arms by running it twice with the same --findings and diffing the
JSON, or pass --json to feed the output somewhere else.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# "## 3. `vendor_path.py:103` — stale user_files/lib permanently shadows ..."
FINDING_RE = re.compile(r"^##\s+(\d+)\.\s+`([^`]+?):(\d+)`\s*[—–-]\s*(.*)$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0 and proc.stderr.strip():
        print(f"warning: git {' '.join(args)}: {proc.stderr.strip()}", file=sys.stderr)
    return proc.stdout


def parse_findings(text: str) -> list[dict]:
    findings = []
    for line in text.splitlines():
        m = FINDING_RE.match(line)
        if m:
            findings.append(
                {
                    "n": int(m.group(1)),
                    "basename": m.group(2),
                    "line": int(m.group(3)),
                    "title": m.group(4).strip(),
                }
            )
    return findings


def resolve_path(repo: Path, refs: tuple[str, ...], basename: str) -> list[str]:
    """The findings doc cites bare filenames; map them onto real repo paths.

    Resolve across both ends of the range: a file created by the run exists only
    at head, and one deleted by it only at base.
    """
    tracked: set[str] = set()
    for ref in refs:
        tracked.update(git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())
    return sorted(p for p in tracked if p == basename or p.endswith("/" + basename))


def touched_lines(repo: Path, base: str, head: str, path: str) -> list[tuple[int, int]]:
    """Old-file line ranges touched by the diff, from -U0 hunk headers."""
    diff = git(repo, "diff", "-U0", f"{base}..{head}", "--", path)
    spans = []
    for line in diff.splitlines():
        m = HUNK_RE.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            # A pure insertion reports count 0 at the line it follows.
            spans.append((start, start + max(count, 1) - 1))
    return spans


def score(repo: Path, findings: list[dict], base: str, head: str, window: int) -> list[dict]:
    changed = set(git(repo, "diff", "--name-only", f"{base}..{head}").splitlines())
    results = []
    for f in findings:
        paths = resolve_path(repo, (base, head), f["basename"])
        hit_paths = [p for p in paths if p in changed]
        spans: list[tuple[int, int]] = []
        for p in hit_paths:
            spans.extend(touched_lines(repo, base, head, p))
        near = [
            (s, e)
            for (s, e) in spans
            if s - window <= f["line"] <= e + window
        ]
        results.append(
            {
                **f,
                "resolved_paths": paths,
                "ambiguous_path": len(paths) > 1,
                "file_touched": bool(hit_paths),
                "touched_paths": hit_paths,
                "hunks_in_file": len(spans),
                "hunks_near_cited_line": len(near),
                "nearest_hunks": near[:3],
            }
        )
    return results


def render(results: list[dict], label: str, base: str, head: str, window: int) -> str:
    lines = [
        f"# Finding coverage — {label}",
        "",
        f"`{base[:12]}` → `{head}`, proximity window ±{window} lines",
        "",
        "| # | Finding | File touched | Hunks near cited line |",
        "|---|---|---|---|",
    ]
    for r in results:
        title = r["title"] if len(r["title"]) <= 60 else r["title"][:57] + "…"
        touched = "yes" if r["file_touched"] else "**no**"
        near = r["hunks_near_cited_line"]
        near_cell = str(near) if near else ("— (file touched elsewhere)" if r["file_touched"] else "—")
        lines.append(
            f"| {r['n']} | `{r['basename']}:{r['line']}` {title} | {touched} | {near_cell} |"
        )

    touched_n = sum(1 for r in results if r["file_touched"])
    near_n = sum(1 for r in results if r["hunks_near_cited_line"])
    lines += [
        "",
        f"**Files touched:** {touched_n}/{len(results)}  ",
        f"**Changed near the cited line:** {near_n}/{len(results)}",
        "",
        "> Both numbers are proxies. Read the diff for any finding marked touched but "
        "not near, and for any finding the author may have fixed somewhere else "
        "entirely. A finding can also be correctly declined — check the run's own "
        "notes before counting it as a miss.",
    ]

    ambiguous = [r for r in results if r["ambiguous_path"]]
    if ambiguous:
        lines += ["", "**Ambiguous filenames** (cited basename matches several paths):"]
        for r in ambiguous:
            lines.append(f"- finding {r['n']}: `{r['basename']}` → {', '.join(r['resolved_paths'])}")

    missing = [r for r in results if not r["resolved_paths"]]
    if missing:
        lines += ["", "**Unresolved filenames** (not found at base — check the findings doc):"]
        for r in missing:
            lines.append(f"- finding {r['n']}: `{r['basename']}`")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--findings", required=True, help="path to the findings markdown")
    ap.add_argument("--base", required=True, help="sha/ref of the branch BEFORE the run")
    ap.add_argument("--head", required=True, help="sha/ref of the branch AFTER the run")
    ap.add_argument("--label", default="run", help="name for this arm in the report")
    ap.add_argument("--window", type=int, default=40, help="proximity window in lines")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = ap.parse_args()

    repo = args.repo.resolve()
    findings_path = Path(args.findings)
    if not findings_path.is_absolute():
        findings_path = repo / findings_path
    if not findings_path.exists():
        print(f"error: findings doc not found: {findings_path}", file=sys.stderr)
        return 2

    findings = parse_findings(findings_path.read_text(encoding="utf-8"))
    if not findings:
        print(
            "error: no findings parsed. Expected headings like:\n"
            "  ## 1. `file.py:123` — description",
            file=sys.stderr,
        )
        return 2

    results = score(repo, findings, args.base, args.head, args.window)

    if args.json:
        print(json.dumps({"label": args.label, "base": args.base, "head": args.head,
                          "findings": results}, indent=2))
    else:
        print(render(results, args.label, args.base, args.head, args.window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
