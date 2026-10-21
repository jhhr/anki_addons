"""The search for a word's other meaning notes, in a real collection: clean_meaning's
`get_other_meaning_notes` without a run's word index (Clean meaning outside a match run,
"Deduplicate existing meaning notes") and make_all_meanings' `make_meanings_in_note` ("Make
meanings").

A match run answers from its word index instead (test/test_word_index.py). These pin what only
Anki can say: that the sort field's two terms find the word's other meaning notes when the
field's name holds spaces, which split each unquoted term in several so that the search found
none, and that inside the quotes the regex is still a regex: `(m2)` is in the group, and a note
the hand-written `(xN)` marker takes out is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest

from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import clean_meaning, make_all_meanings
from japanese_note_ai_ops.configuration import MEANINGS_GENERATED_TAG, MakeMeaningsResult

VOCAB = "Japanese vocab note"
# config.md's example name, and one without spaces, which the unquoted terms found too
SORT_FIELDS = ["Word sort key", "WordSortKey"]
OTHER_FIELDS = ["Word", "Word normal", "Word reading", "Meaning"]


def config(sort_field: str) -> dict:
    return {
        VOCAB: {
            "word_sort_field": sort_field,
            "word_field": "Word",
            "word_normal_field": "Word normal",
            "word_reading_field": "Word reading",
            "meaning_field": "Meaning",
        },
        "log_to_console": False,
    }


@pytest.fixture(params=SORT_FIELDS)
def sort_field(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def col(tmp_path: Path, sort_field: str) -> Iterator[Any]:
    stub = real_anki.install()
    saved = stub.col
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(collection, VOCAB, [sort_field, *OTHER_FIELDS])
        # Both searches go through collection_access, whose worker reads mw.col
        stub.col = collection
        yield collection
    finally:
        stub.col = saved
        collection.close()


def word_notes(col: Any, sort_field: str) -> dict[str, Any]:
    """本 (ほん) by its sort field: three meanings, and two notes taken out by an x marker, one
    of them carrying a meaning marker too, which only the negated term leaves out."""
    return {
        sort_value: real_anki.add_note(
            col,
            VOCAB,
            {sort_field: sort_value, "Word": "本", "Word normal": "本", "Word reading": "ほん"},
        )
        for sort_value in ["本 (m1)", "本 (m2)", "本 (m3)", "本 (x2)", "本 (m4)(x1)"]
    }


def test_clean_meaning_finds_the_other_meaning_notes_by_its_search(
    col: Any, sort_field: str
) -> None:
    notes = word_notes(col, sort_field)
    others = clean_meaning.get_other_meaning_notes(config(sort_field), notes["本 (m1)"])
    assert sorted(note[sort_field] for note in others) == ["本 (m2)", "本 (m3)"]


def test_make_meanings_tags_every_meaning_note_its_search_finds(
    col: Any, sort_field: str
) -> None:
    notes = word_notes(col, sort_field)
    to_update: dict = {}
    # The word's meanings made, which is a dictionary lookup and a request: the search and
    # what is done with the notes it found are what is under test
    with mock.patch.object(
        make_all_meanings, "make_all_meanings_for_word", return_value=MakeMeaningsResult.SUCCESS
    ):
        assert make_all_meanings.make_meanings_in_note(
            config(sort_field), notes["本 (m1)"], set(), {}, {}, to_update
        )
    assert sorted(note[sort_field] for note in to_update.values()) == [
        "本 (m1)",
        "本 (m2)",
        "本 (m3)",
    ]
    assert all(note.has_tag(MEANINGS_GENERATED_TAG) for note in to_update.values())
