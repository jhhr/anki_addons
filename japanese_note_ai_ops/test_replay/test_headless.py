"""dev/headless.py's session: what it opens and replaces, it closes and puts back, a constructor
that raises included."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import headless
from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import capture, run_errors
from japanese_note_ai_ops.async_api_ops.base_ops import RunResult


def collection_file(tmp_path: Path) -> Path:
    path = tmp_path / "collection.anki2"
    real_anki.open_collection(path).close()
    return path


def test_a_capture_store_that_does_not_open_leaves_the_collection_closed(tmp_path):
    # The raise came after the collection was open, and nothing closed it: the file stayed
    # locked for the rest of the process
    path = collection_file(tmp_path)
    store = tmp_path / "a_directory"
    store.mkdir()
    stub = real_anki.install()
    before = (stub.col, stub.progress)

    with pytest.raises(RuntimeError, match="did not open"):
        headless.Headless(path, tmp_path / "profile", {}, capture_path=store)

    real_anki.open_collection(path).close()
    assert (stub.col, stub.progress) == before


def test_a_run_that_wrote_no_run_row_reports_no_run(tmp_path):
    # The store's newest run was reported whatever it was: a run that failed before its start
    # got an earlier capture run's id, and capture_run.py printed that run's summary as its own
    store = tmp_path / "capture.sqlite3"
    with headless.Headless(
        collection_file(tmp_path), tmp_path / "profile", {}, capture_path=store
    ) as session:
        earlier = capture.begin_run("an earlier capture run")
        capture.end_run(earlier, "completed")

        def fails_at_once(col) -> None:
            raise RuntimeError("failed before its run began")

        spec = SimpleNamespace(notes_run=lambda nids: (fails_at_once, RunResult()))
        report = session.run(spec, [], "no_run")

    assert isinstance(report.error, RuntimeError)
    assert earlier is not None and report.run_id is None


def test_a_session_puts_back_what_it_replaced(tmp_path):
    stub = real_anki.install()
    before = (stub.col, stub.progress, stub.pm._profile_folder)
    delivered: list = []
    run_errors.deliver_with(lambda title, text: delivered.append(text))
    try:
        with headless.Headless(collection_file(tmp_path), tmp_path / "profile", {}) as session:
            assert stub.col is session.col
            run_errors.report_error("printed by the session")
        run_errors.report_error("delivered as before it")

        assert (stub.col, stub.progress, stub.pm._profile_folder) == before
        assert delivered == ["delivered as before it"]
        session.close()
    finally:
        run_errors.deliver_with(None)


def test_a_two_type_layout_s_notes_to_match_are_its_sentence_notes(tmp_path):
    # The vocab type keeps its old sentence fields, under the sentence block's names, until the
    # user deletes them: the array a vocab note still holds is not one a match run works on
    col = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(col, "Sentences", ["Sentence", "Words"])
        real_anki.make_note_type(col, "Vocab", ["Word", "Words"])
        array = json.dumps([["本", "名詞", "本", "ほん", ["match"], []]], ensure_ascii=False)
        sentence = real_anki.add_note(col, "Sentences", {"Sentence": "本", "Words": array})
        real_anki.add_note(col, "Vocab", {"Word": "本", "Words": array})
        config = {
            "Sentences": {"vocab_note_type": "Vocab", "word_list_field": "Words"},
            "Vocab": {"sentence_note_type": "Sentences", "word_sort_field": "Word"},
        }

        assert headless.notes_to_match(col, config) == [sentence.id]
    finally:
        col.close()
