"""The practice run of "Move sentences to sentence notes": the op run headlessly over every note of
the vocab note type in a copy of a collection, as selecting them all in the browser and choosing
the menu entry would, so that its report can be read before the real collection is touched.

    python dev/sentence_migration_run.py --collection COPY.anki2

Run from the addon's directory, with the interpreter that has anki and aqt (the tests' one).
**It writes to the collection**: point it at a copy, never at a profile's collection.anki2, and
never while Anki has that file open. Make the copy after the sentence note type exists and the
config names it: the config is the user's (meta.json over config.json, secrets removed), the
same the menu's run reads. The vocab note type is the one block of the config naming a
`sentence_note_type`, or `--note-type`. `--set key=json` overrides a config value.

Checks what the menu's run checks before it starts, and refuses with its message. Prints a
summary as JSON on stdout, the report's path in it (user_files/sentence_migration_<timestamp>.txt,
as a run from the menu writes); progress and errors go to stderr. Ctrl+C cancels as the
dialog's Cancel does: the sentence notes registered so far are added, with their vocab notes,
and a second run does the rest.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, TextIO

import headless  # noqa: I001 - first: it prepares the imports of everything below

from anki.notes import NoteId  # noqa: E402
from japanese_note_ai_ops.note_roles import note_type_search  # noqa: E402
from japanese_note_ai_ops.sync_local_ops.migrate_to_sentence_notes import (  # noqa: E402
    SentenceMigration,
    collection_preflight_error,
)

KEY = "migrate_to_sentence_notes"
# The op asks no model anything, but user_config refuses a config whose models could reach an
# API provider: every model is set to a terminal- name nothing is ever sent to
UNUSED_MODEL = f"{headless.TERMINAL_PREFIX}unused-by-the-migration"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--collection", type=Path, required=True, help="a copy, never the real one")
    parser.add_argument("--note-type", help="the vocab note type; by default the configured one")
    parser.add_argument(
        "--profile-dir", type=Path, help="the profile folder's stand-in; by default a temporary one"
    )
    parser.add_argument("--set", action="append", default=[], dest="overrides", metavar="KEY=JSON")
    return parser.parse_args(argv)


def overrides_from(pairs: list[str]) -> dict:
    overrides: dict = {
        # A notes run would copy the whole collection into a capture store
        "capture_calls": False,
        "capture_notes": False,
        "log_to_console": False,
    }
    for pair in pairs:
        key, _, value = pair.partition("=")
        overrides[key] = json.loads(value)
    return overrides


def vocab_type_of(config: Mapping[str, Any], named: Optional[str]) -> Optional[str]:
    """`named`, else the one block naming a sentence_note_type; None when that is not one."""
    if named:
        return named
    vocab_types = [
        name
        for name, block in config.items()
        if isinstance(block, Mapping) and block.get("sentence_note_type") not in (None, "", name)
    ]
    return vocab_types[0] if len(vocab_types) == 1 else None


def main(argv: list[str]) -> int:
    # The summary is the only thing on stdout: what the addon prints goes to stderr
    out, sys.stdout = sys.stdout, sys.stderr
    try:
        return run(argv, out)
    finally:
        sys.stdout = out


def run(argv: list[str], out: TextIO) -> int:
    args = parse_args(argv)
    config = headless.user_config(overrides_from(args.overrides), UNUSED_MODEL)
    vocab_type = vocab_type_of(config, args.note_type)
    if vocab_type is None:
        print(
            "Refusing to run: no one note type of the config names a sentence_note_type; give"
            " --note-type",
            file=sys.stderr,
        )
        return 2
    with tempfile.TemporaryDirectory() as scratch:
        profile_dir = args.profile_dir or Path(scratch) / "profile"
        with headless.Headless(args.collection, profile_dir, config) as session:
            nids = [NoteId(nid) for nid in session.col.find_notes(note_type_search(vocab_type))]
            error = collection_preflight_error(session.col, config, nids)
            if error:
                print(f"Refusing to run: {error}", file=sys.stderr)
                return 2
            print(f"Moving the sentences of {len(nids)} {vocab_type} notes", file=sys.stderr)
            migration = SentenceMigration()
            report = session.run(migration.spec(), nids, KEY)
    result = report.result
    summary = {
        "report": migration.report_path if Path(migration.report_path).exists() else None,
        "vocab_notes": len(nids),
        "edited_notes": len(result.edited_nids),
        "edited_other_notes": len(result.edited_other_nids),
        "new_notes": result.new_notes._asdict(),
        "cancelled": result.cancelled,
        "errors_reported": report.error_count,
        "failed": None if report.error is None else repr(report.error),
        "wall_seconds": round(report.seconds, 1),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), file=out)
    return 1 if report.error is not None else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
