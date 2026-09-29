"""dev/headless.py's session: what it opens and replaces, it closes and puts back, a constructor
that raises included."""

from __future__ import annotations

from pathlib import Path

import pytest

import headless
from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import run_errors


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
