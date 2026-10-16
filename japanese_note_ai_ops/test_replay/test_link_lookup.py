"""Link IDs in a real collection: what the search terms find, and a type's index and the lookups
by ID (async_api_ops/link_lookup.py) answering alike.

The rules themselves are test/test_link_ids.py's. These pin what only Anki can say: that the
anchored term finds exactly the IDs it names and an escaped field name exactly its field, that
the field names Anki reads as a search of its own are still `link_ids.SEARCH_KEYWORDS`, and
that a lookup by search answers as the index read by SQL does, through collection_access as a
run's worker threads read and with a collection as a sync op or a hook does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest

from anki_shared.testing import real_anki
from japanese_note_ai_ops import link_ids
from japanese_note_ai_ops.async_api_ops import link_lookup
from japanese_note_ai_ops.note_roles import LayoutError

VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"
OTHER = "Other note"

CONFIG = {
    SENTENCE: {
        "vocab_note_type": VOCAB,
        "word_list_field": "Words",
        "sentence_id_field": "Sentence ID",
    },
    VOCAB: {
        "sentence_note_type": SENTENCE,
        "word_sort_field": "Word",
        "new_note_id_field": "Word ID",
    },
    # Has the vocab type's field names but names no link ID field: its notes' IDs are their
    # note ids, whatever the field holds
    OTHER: {"word_sort_field": "Word"},
}

GENERATED = 1160697660039337
OTHER_GENERATED = 1841288010442579
LIMIT = 2**53


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Any]:
    stub = real_anki.install()
    saved = stub.col
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(collection, VOCAB, ["Word", "Word ID", "Meaning"])
        real_anki.make_note_type(collection, SENTENCE, ["Text", "Words", "Sentence ID"])
        real_anki.make_note_type(collection, OTHER, ["Word", "Word ID"])
        # The run's way of reading goes through collection_access, whose worker reads mw.col
        stub.col = collection
        yield collection
    finally:
        stub.col = saved
        collection.close()


def vocab(col: Any, word_id: str, note_type: str = VOCAB) -> int:
    return real_anki.add_note(col, note_type, {"Word": "word", "Word ID": word_id}).id


def set_word_id(col: Any, note_id: int, word_id: str) -> None:
    """For a value naming a note id, which is known only once the note is added."""
    note = col.get_note(note_id)
    note["Word ID"] = word_id
    col.update_note(note)


def found(col: Any, query: str) -> set:
    return set(col.find_notes(query))


def test_the_search_terms_find_exactly_the_ids_they_name(col: Any) -> None:
    # An underscore in the field's name, and a field its unescaped name would also search
    real_anki.make_note_type(col, "Terms", ["Front", "Link_ID", "LinkXID"])
    ids = {}
    for value in ["123", "456", "91234", "1234", "1230", "123<br>", " 123", "0123", ""]:
        ids[value] = real_anki.add_note(col, "Terms", {"Front": "x", "Link_ID": value}).id
    decoy = real_anki.add_note(col, "Terms", {"Front": "x", "LinkXID": "123"}).id

    anchored = link_ids.link_ids_search("Link_ID", [123, 456])
    assert found(col, f'"note:Terms" {anchored}') == {ids["123"], ids["456"]}
    exact = link_ids.field_search("Link_ID", "123")
    assert found(col, f'"note:Terms" {exact}') == {ids["123"]}
    # What the anchoring and the escaping are for
    assert ids["91234"] in found(col, '"note:Terms" "Link\\_ID:re:123"')
    assert decoy in found(col, '"note:Terms" "Link_ID:123"')


def test_the_field_names_anki_reads_as_its_own_search(col: Any) -> None:
    # Each keyword as a field name: the term naming it finds anything but exactly its note, or
    # is no valid search. A name Anki stops reading so fails here, and comes off the list.
    def finds_exactly_its_note(name: str, term: str) -> bool:
        # Numbered: Anki's note type names, like its field names, ignore case
        type_name = f"Keyword {len(col.models.all_names_and_ids())}"
        real_anki.make_note_type(col, type_name, ["Front", name])
        note_id = real_anki.add_note(col, type_name, {"Front": "x", name: "4242"}).id
        # Holding the value elsewhere, for the keywords that search every field
        real_anki.add_note(col, type_name, {"Front": "4242"})
        try:
            return list(col.find_notes(f'"note:{type_name}" {term}')) == [note_id]
        except Exception:  # noqa: BLE001 - an invalid search: no term names the field either
            return False

    for keyword in sorted(link_ids.SEARCH_KEYWORDS):
        assert not finds_exactly_its_note(keyword, f'"{keyword}:4242"'), keyword
        assert not finds_exactly_its_note(keyword.upper(), f'"{keyword.upper()}:4242"'), keyword
    for name in ["id", "nids", "Note ID", "tags", "word", "ord", "guid"]:
        assert finds_exactly_its_note(name, link_ids.field_search(name, "4242")), name


def index_data(col: Any) -> dict[str, int]:
    """Vocab notes in every case of holding an ID, oldest first, and a note of another type."""
    notes = {
        "empty": vocab(col, ""),
        "generated": vocab(col, str(GENERATED)),
        "copy": vocab(col, str(GENERATED)),
        "own id": vocab(col, ""),
        "names empty": vocab(col, ""),
        "names generated": vocab(col, ""),
        "placeholder": vocab(col, "-5"),
        "zero": vocab(col, "0"),
        "other generated": vocab(col, str(OTHER_GENERATED)),
    }
    set_word_id(col, notes["own id"], str(notes["own id"]))
    # The note id of a note whose field is empty: both hold it
    set_word_id(col, notes["names empty"], str(notes["empty"]))
    # The note id of a note whose field holds another ID: only this one holds it
    set_word_id(col, notes["names generated"], str(notes["generated"]))
    notes["of another type"] = vocab(col, str(OTHER_GENERATED + 1), note_type=OTHER)
    notes["sentence"] = real_anki.add_note(
        col, SENTENCE, {"Text": "text", "Sentence ID": str(GENERATED + 1)}
    ).id
    return notes


def test_the_index_reads_each_notes_id(col: Any) -> None:
    notes = index_data(col)
    index = link_lookup.link_index(CONFIG, VOCAB, col=col)
    assert index.note_id(notes["empty"]) == notes["empty"]
    assert index.note_id(GENERATED) == notes["generated"]
    assert index.note_id(notes["own id"]) == notes["own id"]
    assert index.note_id(notes["generated"]) == notes["names generated"]
    assert index.note_id(notes["copy"]) is None
    assert index.note_id(notes["names empty"]) is None
    assert index.note_id(notes["placeholder"]) == notes["placeholder"]
    assert index.note_id(OTHER_GENERATED) == notes["other generated"]
    assert index.duplicates == {
        GENERATED: [notes["generated"], notes["copy"]],
        notes["empty"]: [notes["empty"], notes["names empty"]],
    }
    assert index.invalid == {notes["placeholder"]: "-5", notes["zero"]: "0"}
    # Only the type's own notes, read by the type's own field
    assert index.note_id(OTHER_GENERATED + 1) is None
    assert index.note_id(notes["of another type"]) is None
    sentences = link_lookup.link_index(CONFIG, SENTENCE, col=col)
    assert sentences.note_id(GENERATED + 1) == notes["sentence"]
    assert sentences.note_id(notes["sentence"]) is None


def test_one_id_found_by_its_field(col: Any) -> None:
    holder = vocab(col, str(GENERATED))
    vocab(col, str(OTHER_GENERATED))
    assert link_lookup.find_note_id(CONFIG, VOCAB, GENERATED, col=col) == holder


def test_one_id_found_by_note_id_when_its_field_is_empty(col: Any) -> None:
    holder = vocab(col, "")
    placeholder = vocab(col, "-5")
    assert link_lookup.find_note_id(CONFIG, VOCAB, holder, col=col) == holder
    # A field holding no ID is as good as empty
    assert link_lookup.find_note_id(CONFIG, VOCAB, placeholder, col=col) == placeholder


def test_one_id_not_found_by_note_id_when_its_field_holds_another(col: Any) -> None:
    holder = vocab(col, str(GENERATED))
    assert link_lookup.find_note_id(CONFIG, VOCAB, holder, col=col) is None
    assert link_lookup.find_note_id(CONFIG, VOCAB, GENERATED, col=col) == holder


def test_one_id_held_twice_is_the_older_notes(col: Any) -> None:
    older = vocab(col, "")
    newer = vocab(col, "")
    set_word_id(col, newer, str(older))
    assert link_lookup.find_note_id(CONFIG, VOCAB, older, col=col) == older
    copy = vocab(col, str(GENERATED))
    original = vocab(col, "")
    set_word_id(col, copy, str(GENERATED))
    set_word_id(col, original, str(GENERATED))
    assert link_lookup.find_note_id(CONFIG, VOCAB, GENERATED, col=col) == copy


def test_one_id_held_twice_is_the_notes_whose_note_id_it_is_though_another_is_older(
    col: Any,
) -> None:
    # The older note's field holds the newer one's note id, the newer one's field nothing
    older = vocab(col, "")
    newer = vocab(col, "")
    set_word_id(col, older, str(newer))
    assert link_lookup.find_note_id(CONFIG, VOCAB, newer, col=col) == newer
    index = link_lookup.link_index(CONFIG, VOCAB, col=col)
    assert index.note_id(newer) == newer
    assert index.duplicates == {newer: [newer, older]}


def test_a_note_of_another_type_is_not_found(col: Any) -> None:
    other = vocab(col, str(GENERATED), note_type=OTHER)
    assert link_lookup.find_note_id(CONFIG, VOCAB, GENERATED, col=col) is None
    assert link_lookup.find_note_id(CONFIG, VOCAB, other, col=col) is None


def test_numbers_no_note_can_hold_find_nothing(col: Any) -> None:
    vocab(col, "-5")
    for number in [0, -5, LIMIT, GENERATED]:
        assert link_lookup.find_note_id(CONFIG, VOCAB, number, col=col) is None
    assert link_lookup.find_note_ids(CONFIG, VOCAB, [], col=col) == {}


def test_the_lookups_answer_as_the_index_does(col: Any) -> None:
    notes = index_data(col)
    index = link_lookup.link_index(CONFIG, VOCAB, col=col)
    asked = [*notes.values(), GENERATED, OTHER_GENERATED, GENERATED + 1, 0, -5, LIMIT, 123]
    expected = {number: index.note_id(number) for number in asked}
    expected = {number: note_id for number, note_id in expected.items() if note_id is not None}
    assert len(expected) == 7

    # With the collection, as a sync op or a hook; through collection_access, as a run's
    # worker threads; a search per ID; a regex of several IDs per search
    assert link_lookup.find_note_ids(CONFIG, VOCAB, asked, col=col) == expected
    assert link_lookup.find_note_ids(CONFIG, VOCAB, asked) == expected
    one_by_one = {n: link_lookup.find_note_id(CONFIG, VOCAB, n, col=col) for n in asked}
    assert {n: note_id for n, note_id in one_by_one.items() if note_id is not None} == expected
    with mock.patch.object(link_lookup, "_IDS_PER_SEARCH", 2):
        assert link_lookup.find_note_ids(CONFIG, VOCAB, asked, col=col) == expected
    assert link_lookup.link_index(CONFIG, VOCAB).note_ids(asked) == expected


def test_a_type_naming_no_link_id_field_holds_its_note_ids(col: Any) -> None:
    other = vocab(col, str(GENERATED), note_type=OTHER)
    index = link_lookup.link_index(CONFIG, OTHER, col=col)
    assert index.note_id(other) == other
    assert index.note_id(GENERATED) is None
    assert index.invalid == {}
    assert link_lookup.find_note_id(CONFIG, OTHER, other, col=col) == other
    assert link_lookup.find_note_id(CONFIG, OTHER, GENERATED, col=col) is None


def test_settings_the_collection_cannot_answer_raise_layout_errors(col: Any) -> None:
    real_anki.make_note_type(col, "Keyword field", ["Word", "nid"])
    configs = {
        "not in the collection": ({"Gone": {"new_note_id_field": "Word ID"}}, "Gone"),
        "no such field": ({VOCAB: {"new_note_id_field": "Missing ID"}}, VOCAB),
        "a search keyword": ({"Keyword field": {"new_note_id_field": "nid"}}, "Keyword field"),
    }
    for case, (config, note_type) in configs.items():
        with pytest.raises(LayoutError, match=f'"{note_type}"'):
            link_lookup.link_index(config, note_type, col=col)
        with pytest.raises(LayoutError, match=f'"{note_type}"'):
            link_lookup.find_note_id(config, note_type, GENERATED, col=col)
        with pytest.raises(LayoutError):
            link_lookup.link_index(config, note_type)


def test_a_runs_cache_reads_each_type_once(col: Any) -> None:
    holder = vocab(col, str(GENERATED))

    async def run() -> list:
        cache = link_lookup.LinkIndexCache()
        return await asyncio.gather(
            *[cache.get(CONFIG, VOCAB) for _ in range(5)], cache.get(CONFIG, SENTENCE)
        )

    with mock.patch.object(link_lookup, "_index_rows", wraps=link_lookup._index_rows) as read:
        indexes = asyncio.run(run())
    assert read.call_count == 2
    assert all(index is indexes[0] for index in indexes[:5])
    assert indexes[0].note_id(GENERATED) == holder
    assert indexes[5] is not indexes[0]


def test_every_note_an_answer_names_is_noted_for_the_capture(col: Any) -> None:
    holder = vocab(col, str(GENERATED))
    plain = vocab(col, "")
    with mock.patch.object(link_lookup.capture_notes, "found") as noted:
        link_lookup.find_note_ids(CONFIG, VOCAB, [GENERATED, plain, OTHER_GENERATED], col=col)
        assert sorted(noted.call_args.args[0]) == [holder, plain]
        index = link_lookup.link_index(CONFIG, VOCAB, col=col)
        noted.reset_mock()
        index.note_id(GENERATED)
        assert list(noted.call_args.args[0]) == [holder]
