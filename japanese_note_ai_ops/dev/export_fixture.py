"""Export a capture run as a replay fixture (issue #11, stage 3), and optionally replay it.

    python dev/export_fixture.py --capture STORE --run RUN_ID --name NAME [--check]
        [--corpus] [--copy-anywhere] [--out DIR]

Writes corpus.json, cassette.json and expected.json (see replay.py) to `fixtures/NAME/` of this
addon in the test data checkout (`replay.data_root`: the private repo's clone at
`<repo>/test_data`, or where ANKI_ADDONS_TEST_DATA points), which test_replay replays. A fixture
holds the text of the notes it was captured from and excerpts of the dictionaries the run read,
so it does not go into this public repo; `--out` writes somewhere else, `test_replay/fixtures/
NAME` for one made from a collection its owner has chosen to publish. `--check` replays it at
once and prints the differences, as test_replay does.

`--corpus` writes to `corpora/NAME/` instead, gzipped: a big run for benchmark.py, which
test_replay does not replay strictly. Notes that contend for a word ask in the order their tasks
reach its lock, which is timing, and a run of hundreds of notes has a few requests a strict
replay asks otherwise than the capture run did (7 of 2903 in one 500-note run).

`--copy-anywhere` puts the user's CopyAnywhere config into the corpus (secrets removed), for
replays and benchmarks with its add definitions on. A fixture also gets
expected_copy_anywhere.json: its run replayed with them, once the replay without them has
reproduced the capture. The capture run did not have them on, so that file is a replay's
result, kept to catch a later change in what the two addons do together.

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
    parser.add_argument("--out", type=Path, help="default: fixtures/NAME in the test data")
    parser.add_argument("--corpus", action="store_true", help="to corpora/NAME, gzipped")
    parser.add_argument("--copy-anywhere", action="store_true",
                        help="with the user's CopyAnywhere definitions")
    parser.add_argument("--check", action="store_true", help="replay it after writing it")
    args = parser.parse_args(argv)

    out = args.out
    if out is None:
        directory = replay.data_dir("corpora" if args.corpus else "fixtures")
        if directory is None:
            print(
                f"No test data checkout: clone the test data repo to"
                f" {replay.ADDON_DIR.parent / 'test_data'}, set {replay.DATA_ROOT_ENV}, or"
                " give --out",
                file=sys.stderr,
            )
            return 2
        out = directory / args.name
    copy_anywhere = headless.copy_anywhere_config() if args.copy_anywhere else None
    try:
        fixture = replay.export_fixture(args.capture, args.run, copy_anywhere=copy_anywhere)
    except replay.CaptureGap as e:
        print(f"Not exported: {e}", file=sys.stderr)
        return 2
    if args.copy_anywhere and not args.corpus:
        # Its expected state with CopyAnywhere is a replay's, so the replay must first be one
        # that reproduces the capture
        if report("without CopyAnywhere", replay.replay(fixture), fixture.expected):
            return 1
        with_hooks = replay.replay(fixture, copy_anywhere=True)
        problems = [f"cassette had no answer: {miss}" for miss in with_hooks.misses]
        problems += [f"cassette answer never asked for: {entry}" for entry in with_hooks.unused]
        problems += [f"dictionary had no lookup: {miss}" for miss in with_hooks.dictionary_misses]
        if problems:
            print("\n".join(problems))
            print("with CopyAnywhere: the run asked otherwise, not written", file=sys.stderr)
            return 1
        fixture.expected_copy_anywhere = {
            "notes": with_hooks.notes,
            "meanings": with_hooks.meanings,
            "new_notes": with_hooks.new_notes,
        }
    fixture.write(out, compress=args.corpus)
    corpus = fixture.corpus
    print(
        f"{out}: {len(corpus['notes'])} notes ({sum(n['selected'] for n in corpus['notes'])}"
        f" selected), {sum(len(e['answers']) for e in fixture.cassette['entries'])} answers,"
        f" {len(corpus['dictionary'])} lookups, {fixture.expected['new_notes']} new notes"
        + (", CopyAnywhere definitions" if copy_anywhere is not None else ""),
        file=sys.stderr,
    )
    if not args.check:
        return 0
    written = replay.Fixture.read(out)
    failed = report("replayed", replay.replay(written), written.expected)
    if written.expected_copy_anywhere is not None:
        with_hooks = replay.replay(written, copy_anywhere=True)
        failed |= report("with CopyAnywhere", with_hooks, written.expected_copy_anywhere)
    return 1 if failed else 0


def report(what: str, result: replay.ReplayResult, expected: dict) -> bool:
    """Print a replay's differences from `expected`; True when there are any."""
    differences = result.differences(expected)
    for line in differences:
        print(line)
    print(f"{what}: " + (f"{len(differences)} differences" if differences else "as expected"),
          file=sys.stderr)
    return bool(differences)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
