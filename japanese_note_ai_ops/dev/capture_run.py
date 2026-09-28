"""A capture run: an op run headlessly over notes of a collection file, recording its calls and
its notes into a capture store of its own (issue #11, stage 2).

    python dev/capture_run.py --collection COPY.anki2 --profile-dir DIR --capture STORE \\
        --meanings-from PROFILE/collection.media/_all_meanings_dict.json \\
        --model terminal-claude-haiku-4-5 --limit 10

Run from the addon's directory, with the interpreter that has anki and aqt (the tests' one).
**It writes to the collection**: point it at a copy, never at a profile's collection.anki2,
and never while Anki has that file open. `--dry-run` picks and lists the notes and runs
nothing.

The notes are those of the configured note types whose word arrays have a word to match
(`headless.notes_to_match`), narrowed by `--search` when given, then the first `--limit` of
them in id order, or a `--seed`ed random sample. A run matches those words, so the next run
over the same collection picks other notes. `--model` sets every model the config names; a run
refuses to start while any model would reach an API provider. `--set key=json` overrides a
config value (`--set log_level='"INFO"'`).

Prints the run's summary from the store at the end, as JSON on stdout; progress and errors go
to stderr.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import headless  # noqa: I001 - first: it prepares the imports of everything below

from anki.notes import NoteId  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--op", default="match_words", choices=sorted(headless.op_specs()))
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True, help="the capture store to write")
    parser.add_argument("--meanings-from", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--seed", type=int, help="a random sample instead of the first ones")
    parser.add_argument("--search", help="an Anki search the notes must also match")
    parser.add_argument("--note-type", action="append", dest="note_types")
    parser.add_argument("--set", action="append", default=[], dest="overrides", metavar="KEY=JSON")
    parser.add_argument("--no-notes", action="store_true", help="record the calls only")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def overrides_from(pairs: list[str], record_notes: bool) -> dict:
    overrides: dict = {
        "capture_calls": True,
        "capture_notes": record_notes,
        # The log's call lines name each call's row; the default ERROR hides them
        "log_level": "INFO",
        "log_to_console": False,
    }
    for pair in pairs:
        key, _, value = pair.partition("=")
        overrides[key] = json.loads(value)
    return overrides


def pick(candidates: list[NoteId], limit: int, seed: int | None) -> list[NoteId]:
    if seed is None:
        return candidates[:limit]
    chosen = random.Random(seed).sample(candidates, min(limit, len(candidates)))
    return sorted(chosen)


def main(argv: list[str]) -> int:
    # The summary is the only thing on stdout: what the addon prints (the MDX loader's banner)
    # goes to stderr with the progress
    out, sys.stdout = sys.stdout, sys.stderr
    try:
        return run(argv, out)
    finally:
        sys.stdout = out


def run(argv: list[str], out) -> int:
    args = parse_args(argv)
    try:
        config = headless.user_config(overrides_from(args.overrides, not args.no_notes), args.model)
    except ValueError as e:
        print(f"Refusing to run: {e}", file=sys.stderr)
        return 2
    spec = headless.op_specs()[args.op]()
    with headless.Headless(
        args.collection,
        args.profile_dir,
        config,
        capture_path=None if args.dry_run else args.capture,
        meanings_from=args.meanings_from,
    ) as session:
        candidates = headless.notes_to_match(session.col, config, args.note_types)
        if args.search:
            allowed = set(session.col.find_notes(args.search))
            candidates = [nid for nid in candidates if nid in allowed]
        nids = pick(candidates, args.limit, args.seed)
        models = sorted({str(config.get(key)) for key in headless.model_keys(config)})
        print(
            f"{len(candidates)} notes have words to match; running {args.op} over {len(nids)}"
            f" with {models}",
            file=sys.stderr,
        )
        if args.dry_run:
            print(json.dumps({"note_ids": nids}), file=out)
            return 0
        if not nids:
            return 0
        report = session.run(spec, nids, args.op)
    summary = headless.capture_summary(args.capture, report.run_id) if report.run_id else {}
    summary.update(
        {
            "edited_notes": len(report.result.edited_nids),
            "edited_other_notes": len(report.result.edited_other_nids),
            "new_notes": report.result.new_notes._asdict(),
            "cancelled": report.result.cancelled,
            "errors_reported": report.error_count,
            "failed": None if report.error is None else repr(report.error),
            "wall_seconds": round(report.seconds, 1),
        }
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), file=out)
    return 1 if report.error is not None else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
