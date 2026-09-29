"""The async benchmark (issue #11, stage 4): a fixture's run replayed with timed answers, its
throughput and phases reported as JSON.

    python dev/benchmark.py --fixture DIR [--latency recorded|none|fixed:MS] [--scale F]
        [--memory free:GB] [--repeat N] [--label NAME] [--history FILE]

What is real: the planning, the thread pool and the event loop, the caches and the word index,
the collection reads and writes of a real collection, the concurrency gate. What is not: every
`get_response` is answered by the fixture's cassette after a latency (`--latency`): the one the
answer took in the capture run times `--scale`, a fixed one, or none. The wait is on the pool
worker that asked, as a provider's request blocks it. The cassette is lenient: a request the
capture run never made (notes contending for a word ask in another order) gets an answer of its
kind, counted under `answered`, so the run does the capture run's work. `--memory free:GB`
answers the gate's memory reads with that much free memory, so a run on a loaded machine and
one on an idle one decide their concurrency alike.

Each run's summary holds the wall time, the calls by kind and per minute, the phase table, the
gate's and the caches' figures (`run_metrics`), what the cassette answered how, the new notes,
and where it ran: the commit, the machine, the Python. `--history` appends the summaries, one
JSON line each, to that file (by default `user_files/benchmarks/<fixture>.jsonl`, gitignored):
comparing runs across commits is stage 6's. Absolute times are for this machine only; a test
asserts the counts, never the seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import replay

ADDON_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ADDON_DIR.parent


def latency_profile(spec: str, scale: float) -> Optional[Callable[[dict], float]]:
    """`recorded` (the answer's latency_ms times `scale`), `none`, or `fixed:MS`."""
    if spec == "none":
        return None
    if spec == "recorded":
        return lambda answer: scale * answer.get("latency_ms", 0) / 1000.0
    if spec.startswith("fixed:"):
        seconds = float(spec.split(":", 1)[1]) / 1000.0
        return lambda answer: seconds
    raise ValueError(f"unknown latency profile {spec!r}")


def store_metrics(store: Path) -> dict[str, Any]:
    """What the replay's own capture store says of its run: calls by kind with their latency,
    the phase table from the `phase` events, the decisions, and the run's own metrics events."""
    with closing(sqlite3.connect(str(store))) as connection:
        calls = connection.execute(
            "SELECT kind, COUNT(*), ROUND(AVG(latency_ms)), MAX(started) FROM calls"
            " GROUP BY kind ORDER BY kind"
        ).fetchall()
        phases = [
            json.loads(payload)
            for (payload,) in connection.execute(
                "SELECT payload_json FROM events WHERE kind = 'phase' ORDER BY event_id"
            )
        ]
        metrics = [
            {"kind": kind, **json.loads(payload)}
            for kind, payload in connection.execute(
                "SELECT kind, payload_json FROM events WHERE kind LIKE 'metrics.%'"
                " ORDER BY event_id"
            )
        ]
        decisions = connection.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'match.decision'"
        ).fetchone()[0]
    return {
        "calls": {
            kind: {"count": count, "avg_latency_ms": avg} for kind, count, avg, _ in calls
        },
        "calls_total": sum(count for _, count, _, _ in calls),
        "phases": [
            {"label": phase["label"], "seconds": round(phase["seconds"], 3),
             **{k: v for k, v in phase.items() if k not in ("label", "seconds")}}
            for phase in phases
        ],
        "metrics": metrics,
        "decisions": decisions,
    }


def where_it_ran() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    memory_gb = None
    try:
        import psutil  # type: ignore

        memory_gb = round(psutil.virtual_memory().total / 2**30, 1)
    except ImportError:
        pass
    return {
        "commit": commit,
        "dirty": dirty,
        "machine": platform.node(),
        "processor": platform.processor(),
        "cpus": os.cpu_count(),
        "memory_gb": memory_gb,
        "python": platform.python_version(),
        "platform": sys.platform,
    }


def run_once(
    fixture: replay.Fixture,
    args: argparse.Namespace,
    estimates: Optional[dict] = None,
) -> dict[str, Any]:
    """One timed replay. `estimates` is the gate's learned per-task cost, kept in
    memory: empty is a cold run, the dict an earlier run left is a warm one."""
    estimates = {} if estimates is None else estimates
    cold = not estimates
    cassette = replay.Cassette(
        fixture.cassette["entries"], lenient=True, latency=latency_profile(args.latency, args.scale)
    )
    memory = memory_profile(args.memory)
    with_copy_anywhere = getattr(args, "copy_anywhere", False)

    @contextmanager
    def around_run() -> Iterator[None]:
        with memory() if memory is not None else nullcontext():
            if not with_copy_anywhere:
                yield
                return
            # After the corpus is built, around the run only: its add definitions run on the
            # notes the run adds, not on the corpus being put in
            import headless
            from anki_shared.testing import real_anki

            remove = headless.copy_anywhere_on_add(real_anki.install())
            try:
                yield
            finally:
                remove()

    background = getattr(args, "background", 0)
    result = replay.replay(
        fixture,
        cassette=cassette,
        around_run=around_run,
        read_store=store_metrics,
        background=background,
        estimates=estimates,
    )
    data = result.store_data or {}
    seconds = result.seconds
    return {
        "seconds": round(seconds, 2),
        "calls_per_minute": round(60.0 * data.get("calls_total", 0) / seconds, 1) if seconds else None,
        "notes_per_second": round(
            sum(n["selected"] for n in fixture.corpus["notes"]) / seconds, 3
        ) if seconds else None,
        "answered": result.answered,
        "new_notes": result.new_notes,
        "errors": len(result.errors),
        "copy_anywhere": with_copy_anywhere,
        "background_notes": background,
        # The gate's learned per-task cost: none yet on a cold run
        "cache": "cold" if cold else "warm",
        "fields_differing": replay.fields_differing(result.notes, fixture.expected["notes"]),
        "new_note_fields": replay.new_note_fields(result.notes),
        **data,
    }


def memory_profile(spec: Optional[str]) -> Optional[Callable[[], Any]]:
    """None, or `free:GB`: the gate reads that much free memory for the whole run, whatever
    else the machine is doing, so its concurrency is decided alike from run to run. The gate
    reads its probes as module attributes at each call (concurrency.system_memory), which is
    what test_concurrency's stubs replace too."""
    if spec is None:
        return None
    if not spec.startswith("free:"):
        raise ValueError(f"unknown memory profile {spec!r}")
    free_bytes = int(float(spec.split(":", 1)[1]) * 2**30)
    return lambda: fixed_free_memory(free_bytes)


@contextmanager
def fixed_free_memory(free_bytes: int) -> Iterator[None]:
    from japanese_note_ai_ops.async_api_ops import concurrency

    real = concurrency.system_memory

    def probe() -> Optional[tuple[int, int]]:
        measured = real()
        total = measured[0] if measured else 2 * free_bytes
        return total, free_bytes

    concurrency.system_memory = probe  # type: ignore[assignment]
    try:
        yield
    finally:
        concurrency.system_memory = real  # type: ignore[assignment]


def corpus_version(directory: Path, fixture: replay.Fixture) -> dict[str, Any]:
    """Which corpus a summary is of: the capture run it came from, and a hash of its
    files, so two summaries of a re-exported corpus are not taken for one corpus's."""
    import hashlib

    digest = hashlib.sha1()
    for name in replay.Fixture.FILES:
        digest.update((directory / f"{name}.json").read_bytes())
    return {"source": fixture.corpus.get("source"), "sha1": digest.hexdigest()[:12]}


def main(argv: list[str]) -> int:
    # As a script only: it installs the stub mw, which a test running inside Anki must not get
    import headless  # noqa: F401

    # The summary is the only thing on stdout: what the addon prints goes to stderr
    out, sys.stdout = sys.stdout, sys.stderr
    try:
        summary = benchmark(argv)
    finally:
        sys.stdout = out
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def benchmark(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--latency", default="recorded")
    parser.add_argument("--scale", type=float, default=0.1)
    parser.add_argument("--memory")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--label")
    parser.add_argument("--history", type=Path)
    parser.add_argument(
        "--copy-anywhere",
        action="store_true",
        help="the user's CopyAnywhere add definitions on the notes the run adds",
    )
    parser.add_argument(
        "--background",
        type=int,
        default=0,
        help="notes no request finds, to bring the collection to a real one's size",
    )
    args = parser.parse_args(argv)

    fixture = replay.Fixture.read(args.fixture)
    runs = []
    # One store of learned costs for the invocation: the first run is cold, the rest warm
    estimates: dict = {}
    for index in range(args.repeat):
        print(f"run {index + 1}/{args.repeat}", file=sys.stderr, flush=True)
        runs.append(run_once(fixture, args, estimates))
    summary = {
        "fixture": args.fixture.name,
        "label": args.label,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "where": where_it_ran(),
        "profile": {
            "latency": args.latency,
            "scale": args.scale,
            "memory": args.memory,
            "copy_anywhere": args.copy_anywhere,
            "background": args.background,
        },
        "corpus": corpus_version(args.fixture, fixture),
        "source_notes": sum(n["selected"] for n in fixture.corpus["notes"]),
        "corpus_notes": len(fixture.corpus["notes"]),
        "median_seconds": statistics.median(run["seconds"] for run in runs),
        "runs": runs,
    }
    history = args.history or ADDON_DIR / "user_files" / "benchmarks" / (
        f"{args.fixture.name}.jsonl"
    )
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as file:
        file.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
