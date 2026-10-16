"""Fill link IDs (sync_local_ops/fill_link_ids.py) in a real collection of the two-type layout,
run through its NotesRunSpec as a script runs it, over every note of both types.

The notes, all made up here: Create Copy-like duplicates of a vocab note holding its own note id
(the copy holds the original's, while an array written before link IDs links the copy by its
own) and of notes holding generated IDs, one whose own note id a third note holds, an empty ID
field, a legacy placeholder in a field and in an array. Afterwards every note holds an ID of its
own, so the migration's pre-flight and find_missing, which refuse duplicates, pass; one undo
takes it all back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from anki.notes import NoteId

from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops.link_lookup import link_index
from japanese_note_ai_ops.link_ids import DuplicateLinkIdsError, new_link_id
from japanese_note_ai_ops.sync_local_ops.fill_link_ids import (
    FillLinkIdsError,
    LinkIdFill,
    fill_preflight_error,
)
from japanese_note_ai_ops.sync_local_ops.find_missing_matched_note_ids import link_indexes
from japanese_note_ai_ops.sync_local_ops.migrate_to_sentence_notes import (
    collection_preflight_error,
)
from japanese_note_ai_ops.word_array.match_flags import format_word_array, read_word_array

PACKAGE = "japanese_note_ai_ops"
VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"
# Configured, with no link ID field: its notes are left out
KANJI = "Kanji note"

CONFIG = {
    SENTENCE: {
        "vocab_note_type": VOCAB,
        "sentence_field": "Sentence",
        "word_list_field": "Word array",
        "sentence_id_field": "Sentence ID",
    },
    VOCAB: {
        "sentence_note_type": SENTENCE,
        "example_sentence_id_field": "Example sentence id",
        "word_kanjified_field": "Word kanjified",
        "word_normal_field": "Word",
        "word_reading_field": "Word reading",
        "word_sort_field": "Word sort key",
        "new_note_id_field": "Word ID",
    },
    KANJI: {"kanji_field": "Kanji"},
    "log_to_console": False,
}
# The vocab type as it is before the migration: its own fields, then the old sentence fields
# under the sentence type's names
VOCAB_FIELDS = [
    "Word sort key",
    "Word",
    "Word kanjified",
    "Word reading",
    "Word ID",
    "Example sentence id",
    "Sentence",
    "Word array",
]
SENTENCE_FIELDS = ["Sentence", "Word array", "Sentence ID"]

# Link IDs no note has as its note id, and legacy placeholders: the negative ids a match op from
# before link IDs linked a new note by until it was added
GENERATED = 1160697660039337
SENTENCE_GENERATED = 4000000000000001
PLACEHOLDER = -777
UNHELD = -999


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Any]:
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs))
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(collection, VOCAB, VOCAB_FIELDS)
        real_anki.make_note_type(collection, SENTENCE, SENTENCE_FIELDS)
        real_anki.make_note_type(collection, KANJI, ["Kanji"])
        stub.col = collection
        stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(CONFIG))
        stub.progress.set_cancel(False)
        yield collection
    finally:
        stub.progress.set_cancel(False)
        stub.col, stub.addonManager.configs = saved
        collection.close()


def word(form: str, match_data: list) -> list:
    return [form, "noun", form, form, match_data, []]


def vocab(col: Any, form: str, word_id: str = "") -> Any:
    fields = {"Word sort key": form, "Word": form, "Word kanjified": form, "Word reading": form}
    return real_anki.add_note(col, VOCAB, {**fields, "Word ID": word_id})


def copy_of(col: Any, note: Any) -> Any:
    """Anki's Create Copy: a new note with every field of `note`."""
    return real_anki.add_note(col, note.note_type()["name"], dict(note.items()))


def set_field(col: Any, note: Any, field: str, value: str) -> None:
    """For a value naming a note id, which is known only once the note is added."""
    note[field] = value
    col.update_note(note)


VOCAB_NAMES = ("empty", "placeholder", "original", "copy", "generated", "generated copy", "third")
SENTENCE_NAMES = ("sentence", "sentence original", "sentence copy")


def build(col: Any) -> dict[str, int]:
    """The notes by name: VOCAB_NAMES, SENTENCE_NAMES, and "kanji"."""
    notes: dict[str, Any] = {}
    notes["empty"] = vocab(col, "cat")
    notes["placeholder"] = vocab(col, "eat", str(PLACEHOLDER))
    notes["original"] = vocab(col, "fish")
    # As the match op's resolving or a CopyAnywhere definition left the notes it saved
    set_field(col, notes["original"], "Word ID", str(notes["original"].id))
    notes["copy"] = copy_of(col, notes["original"])
    notes["generated"] = vocab(col, "dog", str(GENERATED))
    notes["generated copy"] = copy_of(col, notes["generated"])
    # Holds the note id of the generated note's copy, which therefore cannot have it
    notes["third"] = vocab(col, "bird")
    set_field(col, notes["third"], "Word ID", str(notes["generated copy"].id))
    array = [
        word("fish", [notes["original"].id]),
        # Written before link IDs: the copy by its own note id
        word("fish", [notes["copy"].id, 3]),
        word("eat", [PLACEHOLDER, 4]),
        word("dog", [GENERATED]),
        word("run", [UNHELD]),
    ]
    notes["sentence"] = real_anki.add_note(
        col, SENTENCE, {"Sentence": "s1", "Word array": format_word_array(array)}
    )
    notes["sentence original"] = real_anki.add_note(
        col, SENTENCE, {"Sentence": "s2", "Sentence ID": str(SENTENCE_GENERATED)}
    )
    notes["sentence copy"] = copy_of(col, notes["sentence original"])
    notes["kanji"] = real_anki.add_note(col, KANJI, {"Kanji": "k"})
    return {name: note.id for name, note in notes.items()}


def state(col: Any, nids: Any) -> dict[int, dict[str, str]]:
    return {nid: dict(col.get_note(nid).items()) for nid in nids}


def fill(col: Any, nids: Any, report_dir: Path) -> tuple[LinkIdFill, Any]:
    """One run over `nids`, as a script runs it: its run object (the report's path) and result."""
    run_fill = LinkIdFill(report_dir=str(report_dir))
    run, result = run_fill.spec().notes_run([NoteId(nid) for nid in nids])
    run(col)
    return run_fill, result


def array_of(col: Any, nid: int) -> list:
    array, problem = read_word_array(col.get_note(nid)["Word array"])
    assert problem is None and array is not None
    return [element[4] for element in array]


def test_every_note_of_both_types_gets_an_id_of_its_own_and_one_undo_takes_it_back(
    col, tmp_path
):
    ids = build(col)
    every = list(ids.values())
    before = state(col, every)
    undo_before = col.undo_status().undo
    # The copy holds the original's note id, so the older array's link to it leads nowhere
    assert link_index(CONFIG, VOCAB, col=col).note_id(ids["copy"]) is None
    # Every vocab ID held before the run, which a generated one must not be
    vocab_held = {ids[name] for name in ("empty", "placeholder", "original", "generated copy")}
    vocab_held.add(GENERATED)

    run_fill, result = fill(col, every, tmp_path / "reports")

    assert {name: col.get_note(ids[name])["Word ID"] for name in VOCAB_NAMES} == {
        "empty": str(ids["empty"]),
        "placeholder": str(ids["placeholder"]),
        "original": str(ids["original"]),
        # Its own note id, which the older array links it by
        "copy": str(ids["copy"]),
        "generated": str(GENERATED),
        # Its own note id is the third note's ID
        "generated copy": str(new_link_id(f"dup|{ids['generated copy']}", vocab_held)),
        "third": str(ids["generated copy"]),
    }
    assert {name: col.get_note(ids[name])["Sentence ID"] for name in SENTENCE_NAMES} == {
        "sentence": str(ids["sentence"]),
        "sentence original": str(SENTENCE_GENERATED),
        "sentence copy": str(ids["sentence copy"]),
    }
    assert array_of(col, ids["sentence"]) == [
        [ids["original"]],
        [ids["copy"], 3],
        # The note whose field held the placeholder, by its note id: its link ID
        [ids["placeholder"], 4],
        [GENERATED],
        ["match"],
    ]
    assert dict(col.get_note(ids["kanji"]).items()) == before[ids["kanji"]]
    # Each link resolves to the note it named before link IDs: the copy is its own again
    index = link_index(CONFIG, VOCAB, col=col)
    assert index.duplicates == {}
    assert index.note_id(ids["copy"]) == ids["copy"]
    assert index.note_id(ids["original"]) == ids["original"]
    assert sorted(result.edited_nids) == sorted(
        ids[name]
        for name in ("empty", "placeholder", "copy", "generated copy", "sentence", "sentence copy")
    )
    report = Path(run_fill.report_path).read_text(encoding="utf-8")
    for line in (
        "notes selected: 11",
        "notes written: 6",
        "empty ID fields given their note id: 2",
        "ID fields holding something but an ID given their note id: 1",
        "IDs more than one note held: 3",
        "holders given their own note id: 2",
        "holders given a generated ID: 1",
        "legacy placeholders linked to the note holding them: 1",
        "legacy placeholders no note holds, set to match: 1",
        "notes of a note type with no link ID field, left out: 1",
        f"its own note id is the ID of note {ids['third']}",
    ):
        assert line in report, line

    assert col.undo_status().undo == "Filling link IDs for 6 notes."
    col.undo()
    assert state(col, every) == before
    assert col.undo_status().undo == undo_before


def test_a_cancel_between_notes_leaves_no_placeholder_without_its_holder(
    col, tmp_path, monkeypatch
):
    ids = build(col)
    stub = real_anki.install()
    note_op = LinkIdFill.note_op
    processed: list = []

    def cancelling(self: LinkIdFill, **kwargs: Any) -> bool:
        done = note_op(self, **kwargs)
        processed.append(kwargs["note"].id)
        stub.progress.set_cancel(True)
        return done

    monkeypatch.setattr(LinkIdFill, "note_op", cancelling)
    # The note whose field holds the placeholder selected first, the array holding it last
    _, result = fill(col, list(ids.values()), tmp_path / "cancelled")
    monkeypatch.undo()
    stub.progress.set_cancel(False)

    # The array was written first: had the field been, the rerun would find no note holding the
    # placeholder and set the word to match
    assert processed == [ids["sentence"]]
    assert result.cancelled
    assert col.get_note(ids["placeholder"])["Word ID"] == str(PLACEHOLDER)
    assert array_of(col, ids["sentence"])[2] == [ids["placeholder"], 4]

    fill(col, list(ids.values()), tmp_path / "rerun")

    assert col.get_note(ids["placeholder"])["Word ID"] == str(ids["placeholder"])
    assert array_of(col, ids["sentence"])[2] == [ids["placeholder"], 4]
    link_indexes(CONFIG, col)


def test_after_it_the_ops_refusing_duplicates_run(col, tmp_path):
    """The migration's pre-flight and find_missing read the same notes' link IDs and refuse
    while two notes of a type hold one; after Fill link IDs over them, both pass."""
    ids = build(col)
    vocab_ids = [NoteId(ids[name]) for name in VOCAB_NAMES]
    refusal = collection_preflight_error(col, CONFIG, vocab_ids)
    assert refusal is not None and 'Run "Fill link IDs" on these notes first' in refusal
    with pytest.raises(DuplicateLinkIdsError):
        link_indexes(CONFIG, col)

    fill(col, list(ids.values()), tmp_path / "reports")

    assert collection_preflight_error(col, CONFIG, vocab_ids) is None
    link_indexes(CONFIG, col)


def test_a_second_run_writes_nothing_and_says_so(col, tmp_path):
    ids = build(col)
    fill(col, list(ids.values()), tmp_path / "first")
    after_first = state(col, ids.values())

    run_fill, result = fill(col, list(ids.values()), tmp_path / "second")

    assert state(col, ids.values()) == after_first
    assert result.edited_nids == []
    report = Path(run_fill.report_path).read_text(encoding="utf-8")
    assert "Nothing to write: no selected note was changed." in report


def test_the_run_refuses_notes_with_no_field_to_fill_and_writes_nothing(col, tmp_path):
    ids = build(col)
    before = state(col, ids.values())
    config = real_anki.install().addonManager.configs[PACKAGE]
    error = fill_preflight_error(col, config, [NoteId(ids["kanji"])])
    assert error is not None and "no selected note is of a note type whose settings" in error

    with pytest.raises(FillLinkIdsError) as refused:
        fill(col, [ids["kanji"]], tmp_path / "refused")

    assert str(refused.value) == error
    assert state(col, ids.values()) == before
    assert not (tmp_path / "refused").exists()
    # A field the settings name that the note type lacks
    config[VOCAB]["new_note_id_field"] = "Missing"
    error = fill_preflight_error(col, config, [NoteId(ids["empty"])])
    assert error is not None and 'has no field "Missing"' in error
