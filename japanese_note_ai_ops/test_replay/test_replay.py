"""Replays of the fixtures made from real capture runs (dev/export_fixture.py).

Two places hold fixtures: `test_replay/fixtures/`, committed, and `fixtures/` of this addon in
the test data checkout (`replay.data_root`: a clone of the private test data repo, for
fixtures of a collection its owner has not chosen to publish). Each fixture is replayed twice:
the run must leave every note as the capture run did, ask for exactly the answers it was given
and every one of them, and look up exactly the dictionary entries it did, both times. A fixture
with an expected_copy_anywhere.json is replayed twice more with the corpus's CopyAnywhere
definitions on the add hook, and must leave the notes as that file has them. No fixture
anywhere is a skip, not a pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

import replay

COMMITTED = Path(__file__).resolve().parent / "fixtures"


def fixture_paths() -> list[Path]:
    directories = [COMMITTED, replay.data_dir("fixtures")]
    return sorted(
        path
        for directory in directories
        if directory is not None and directory.is_dir()
        for path in directory.iterdir()
        if replay.Fixture.exists(path)
    )


def cases() -> list:
    found = []
    for path in fixture_paths():
        found.append(pytest.param(path, False, id=path.name))
        if any(path.glob("expected_copy_anywhere.json*")):
            found.append(pytest.param(path, True, id=f"{path.name}-copy_anywhere"))
    return found


CASES = cases()


@pytest.mark.skipif(
    not CASES, reason="no replay fixtures, committed or in the test data checkout"
)
@pytest.mark.parametrize("path, copy_anywhere", CASES)
def test_a_fixture_replays_as_it_was_captured(path: Path, copy_anywhere: bool):
    fixture = replay.Fixture.read(path)
    expected: Optional[dict] = (
        fixture.expected_copy_anywhere if copy_anywhere else fixture.expected
    )
    assert expected is not None

    for attempt in (1, 2):
        result = replay.replay(fixture, copy_anywhere=copy_anywhere)
        differences = result.differences(expected)
        assert not differences, f"replay {attempt}:\n" + "\n".join(differences)
