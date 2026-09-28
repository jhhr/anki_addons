"""Export a capture run as a replay fixture (issue #11, stage 3), and optionally replay it.

    python dev/export_fixture.py --capture STORE --run RUN_ID --name NAME [--check]

Writes corpus.json, cassette.json and expected.json (see replay.py) to
`user_files/fixtures/NAME/`, which git ignores: a fixture holds the text of the notes it was
captured from, and whether it may be published is the collection owner's decision. `--out`
writes somewhere else, `test_replay/fixtures/NAME` to commit it. `--check` replays it at once
and prints the differences, as test_replay does.

A run that cannot be replayed from what it recorded (no notes recorded, records dropped, a note
written with no state before) is refused with the gap named: fix the capture and record again.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import headless  # noqa: I001 - first: it prepares the imports of everything below

import replay  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--out", type=Path, help="default: user_files/fixtures/NAME")
    parser.add_argument("--check", action="store_true", help="replay it after writing it")
    args = parser.parse_args(argv)

    try:
        fixture = replay.export_fixture(args.capture, args.run)
    except replay.CaptureGap as e:
        print(f"Not exported: {e}", file=sys.stderr)
        return 2
    out = args.out or headless.ADDON_DIR / "user_files" / "fixtures" / args.name
    fixture.write(out)
    corpus = fixture.corpus
    print(
        f"{out}: {len(corpus['notes'])} notes ({sum(n['selected'] for n in corpus['notes'])}"
        f" selected), {sum(len(e['answers']) for e in fixture.cassette['entries'])} answers,"
        f" {len(corpus['dictionary'])} lookups, {fixture.expected['new_notes']} new notes",
        file=sys.stderr,
    )
    if not args.check:
        return 0
    differences = replay.replay(replay.Fixture.read(out)).differences(fixture.expected)
    for line in differences:
        print(line)
    print("replayed as captured" if not differences else f"{len(differences)} differences",
          file=sys.stderr)
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
