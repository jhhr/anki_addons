"""A fixture's run replayed in a running Anki (issue #11, stage 5): opt-in, by environment.

    JNAIO_REAL_ANKI_REPLAY=<fixture or corpus: a name in the test data checkout, or a path>
    JNAIO_REAL_ANKI_SCALE=<factor>        answers after their recorded latency times this, and
                                          a lenient cassette (benchmark.py's); else instant and
                                          strict
    JNAIO_REAL_ANKI_COPY_ANYWHERE=1       the corpus's CopyAnywhere definitions (export
                                          --copy-anywhere) on the add hook

The run goes the way the menu's does: `selected_notes_op`, a real `CollectionOp` on Anki's
background thread, the progress manager, and whatever the add hook has attached; the replay
machinery is dev/replay.py's (`replay_in_anki`). Each run's figures (benchmark.py's, and the
add phase's per-note records) are appended to `user_files/benchmarks/<fixture>-real-anki.jsonl`.

Strict, the run must leave the notes as the capture run did, or with CopyAnywhere as the
fixture's expected_copy_anywhere.json has them, which a headless replay recorded: the running
Anki and the headless replay are the same run. Timed, the run must add every note without a
failed add or merge; comparing its CopyAnywhere fields with a headless replay's is reading the
two JSON lines side by side.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import aqt.operations
import pytest

from japanese_note_ai_ops.async_api_ops import base_ops

ADDON = Path(__file__).resolve().parents[1]
FIXTURE = os.environ.get("JNAIO_REAL_ANKI_REPLAY")
SCALE = os.environ.get("JNAIO_REAL_ANKI_SCALE")
WITH_COPY_ANYWHERE = bool(os.environ.get("JNAIO_REAL_ANKI_COPY_ANYWHERE"))
# A 500-note run with timed answers takes minutes
WAIT = 2 * 60 * 60 * 1000

pytestmark = pytest.mark.skipif(
    not FIXTURE, reason="opt-in: set JNAIO_REAL_ANKI_REPLAY to a fixture directory"
)


def test_a_fixture_replays_in_a_running_anki(
    anki_session, anki_mw, config, copy_anywhere_config, tmp_path, monkeypatch
):
    sys.path.insert(0, str(ADDON / "dev"))
    import benchmark  # noqa: E402 - dev/ is on sys.path only now
    import replay  # noqa: E402

    assert FIXTURE is not None
    directory = replay.find_fixture(FIXTURE)
    fixture = replay.Fixture.read(directory)
    definitions = fixture.corpus.get("copy_anywhere")
    if WITH_COPY_ANYWHERE and definitions is None:
        pytest.fail(f"{directory.name} holds no CopyAnywhere definitions (export --copy-anywhere)")
    exceptions: list[BaseException] = []
    monkeypatch.setattr(base_ops, "tooltip", lambda *a, **k: None)
    monkeypatch.setattr(base_ops, "showWarning", lambda *a, **k: None)
    monkeypatch.setattr(
        aqt.operations, "show_exception", lambda **k: exceptions.append(k["exception"])
    )
    def attach_copy_anywhere() -> None:
        from copy_anywhere.hooks import note_hooks

        copy_anywhere_config(**(definitions or {}))
        note_hooks.init_note_hooks()

    cassette = replay.Cassette(
        fixture.cassette["entries"],
        lenient=SCALE is not None,
        latency=benchmark.latency_profile("recorded", float(SCALE)) if SCALE else None,
    )

    def set_config(values: dict) -> None:
        config(**values)

    def run_op(spec: Any, nids: list) -> None:
        base_ops.selected_notes_op(
            spec.done_text,
            spec.bulk_op,
            nids,
            anki_mw,
            base_ops.AsyncTaskProgressUpdater(title=spec.title),
            spec.new_notes_op,
            spec.filter_new_notes_op,
            unadded_notes_op=spec.unadded_notes_op,
            tidy_markers_op=spec.tidy_markers_op,
        )
        anki_session.qtbot.waitUntil(lambda: anki_mw._background_op_count == 0, timeout=WAIT)

    result = replay.replay_in_anki(
        anki_mw,
        fixture,
        tmp_path,
        set_config,
        run_op,
        cassette=cassette,
        read_store=lambda store: {
            **benchmark.store_metrics(store),
            "note_adds": note_adds(store),
        },
        before_run=attach_copy_anywhere if WITH_COPY_ANYWHERE else None,
    )

    data = result.store_data or {}
    adds = data.get("note_adds", [])
    summary = {
        "fixture": directory.name,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "where": benchmark.where_it_ran(),
        "real_anki": True,
        "copy_anywhere": WITH_COPY_ANYWHERE,
        "profile": {"scale": SCALE},
        "seconds": round(result.seconds, 2),
        "answered": result.answered,
        "new_notes": result.new_notes,
        "add_seconds": round(sum(add["seconds"] for add in adds), 3),
        "merge_seconds": round(sum(add["merge_seconds"] or 0 for add in adds), 3),
        "add_errors": sum(1 for add in adds if add["add_error"]),
        "merge_errors": sum(1 for add in adds if add["merge_error"]),
        # Which fields of which notes differ from the capture run's: with CopyAnywhere, what
        # its definitions wrote; without it, what a running Anki does differently
        "fields_differing": replay.fields_differing(result.notes, fixture.expected["notes"]),
        "new_note_fields": replay.new_note_fields(result.notes),
        **{key: data.get(key) for key in ("calls_total", "phases", "metrics", "decisions")},
    }
    history = ADDON / "user_files" / "benchmarks" / f"{directory.name}-real-anki.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as file:
        file.write(json.dumps(summary, ensure_ascii=False) + "\n")

    assert exceptions == []
    assert (summary["add_errors"], summary["merge_errors"]) == (0, 0)
    expected: Optional[dict] = (
        fixture.expected_copy_anywhere if WITH_COPY_ANYWHERE else fixture.expected
    )
    if SCALE is None and expected is not None:
        differences = result.differences(expected)
        assert not differences, "\n".join(differences)


def note_adds(store: Path) -> list[dict]:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(str(store))) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE kind = 'note.add' ORDER BY event_id"
        ).fetchall()
    return [json.loads(payload) for (payload,) in rows]
