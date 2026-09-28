"""Replays of the fixtures made from real capture runs (dev/export_fixture.py).

Two places hold fixtures: `test_replay/fixtures/`, committed, and the addon's gitignored
`user_files/fixtures/`, for fixtures of a collection its owner has not chosen to publish. Each
fixture is replayed twice: the run must leave every note as the capture run did, ask for exactly
the answers it was given and every one of them, and look up exactly the dictionary entries it
did, both times. No fixture anywhere is a skip, not a pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import replay

ADDON = Path(__file__).resolve().parents[1]
FIXTURE_DIRS = (Path(__file__).resolve().parent / "fixtures", ADDON / "user_files" / "fixtures")


def fixture_paths() -> list[Path]:
    return sorted(
        path
        for directory in FIXTURE_DIRS
        if directory.is_dir()
        for path in directory.iterdir()
        if (path / "corpus.json").is_file()
    )


PATHS = fixture_paths()


@pytest.mark.skipif(not PATHS, reason="no replay fixtures, committed or in user_files/fixtures")
@pytest.mark.parametrize("path", PATHS, ids=[path.name for path in PATHS])
def test_a_fixture_replays_as_it_was_captured(path: Path):
    fixture = replay.Fixture.read(path)

    for attempt in (1, 2):
        result = replay.replay(fixture)
        differences = result.differences(fixture.expected)
        assert not differences, f"replay {attempt}:\n" + "\n".join(differences)
