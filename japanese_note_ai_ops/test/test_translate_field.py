"""Translate sentence (async_api_ops/translate_field.py) under the two note layouts.

In the two-type layout a vocab note keeps a copy of its example sentence note's translation
(note_roles.copy_example), so translating a sentence note gives the new translation to the
vocab notes whose example it is, found by the vocab type's example id field and registered for
the run's save. The editor's hook asks for the note it shows only. In the one-type layout a
note's translation is its own, and nothing is searched.
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from addon_modules import load_ops_module

op = load_ops_module("translate_field")

VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"
TRANSLATION = "The cat sleeps."

TWO_TYPE = {
    SENTENCE: {
        "vocab_note_type": VOCAB,
        "word_list_field": "Words",
        "sentence_field": "Text",
        "translated_sentence_field": "Text translation",
        "sentence_audio_field": "Text audio",
    },
    VOCAB: {
        "sentence_note_type": SENTENCE,
        "example_sentence_id_field": "Example id",
        "translated_sentence_field": "Example translation",
        "sentence_audio_field": "Example audio",
        "word_sort_field": "Sort",
    },
}
ONE_TYPE = {
    VOCAB: {
        "word_list_field": "Words",
        "sentence_field": "Sentence",
        "translated_sentence_field": "Sentence translation",
        "word_sort_field": "Sort",
    }
}


class Note:
    def __init__(self, note_id: int, type_name: str, fields: dict[str, str]) -> None:
        self.id = note_id
        self.type_name = type_name
        self.fields = dict(fields)

    def note_type(self):
        return {"name": self.type_name}

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]

    def __setitem__(self, field: str, value: str) -> None:
        self.fields[field] = value


def vocab(note_id: int, example_id: str, translation: str = "old") -> Note:
    return Note(
        note_id,
        VOCAB,
        {"Example id": example_id, "Example translation": translation, "Example audio": "a"},
    )


class Collection:
    """The collection behind translate_field's `col_find_notes` / `col_get_notes`: a search of
    one note type and one exact `"field:value"` term, as the copy makes."""

    def __init__(self, *notes: Note) -> None:
        self.notes = {note.id: note for note in notes}
        self.searches: list[str] = []
        self.fetched: list[int] = []

    def find_notes(self, query: str) -> list[int]:
        self.searches.append(query)
        [type_name] = re.findall(r'"note:([^"]+)"', query)
        [(field, value)] = [
            term for term in re.findall(r'"([^":]+):([^"*]+)"', query) if term[0] != "note"
        ]
        return [
            nid
            for nid, note in self.notes.items()
            if note.type_name == type_name and field in note and note[field] == value
        ]

    def get_notes(self, note_ids) -> list[Note]:
        ids = list(note_ids)
        self.fetched.extend(ids)
        return [self.notes[nid] for nid in ids]


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self.sentence = Note(20, SENTENCE, {"Text": "猫が寝る。", "Text translation": ""})
        self.col = Collection(
            self.sentence,
            vocab(10, "20"),
            vocab(11, "20", translation=TRANSLATION),
            vocab(12, "21"),
            # Another sentence note's example, and an id that only starts like this one's
            vocab(13, "201"),
        )
        for patch in (
            mock.patch.object(op, "col_find_notes", self.col.find_notes),
            mock.patch.object(op, "col_get_notes", self.col.get_notes),
            mock.patch.object(op, "get_translated_field_from_model", return_value=TRANSLATION),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_sentence_note_s_vocab_notes_get_its_translation(self):
        updates: dict = {}
        self.assertTrue(op.translate_sentence_in_note(TWO_TYPE, self.sentence, {}, updates))
        self.assertEqual(self.sentence["Text translation"], TRANSLATION)
        self.assertEqual(self.col.searches, [f'"note:{VOCAB}" "Example id:20"'])
        # The one whose copy already reads so is left as it is
        self.assertEqual(sorted(updates), [10, 20])
        self.assertEqual(updates[10]["Example translation"], TRANSLATION)
        self.assertEqual(self.col.notes[12]["Example translation"], "old")
        self.assertEqual(self.col.notes[13]["Example translation"], "old")

    def test_a_vocab_note_the_run_holds_already_is_changed_in_place(self):
        held = vocab(10, "20")
        updates: dict = {10: held}
        op.translate_sentence_in_note(TWO_TYPE, self.sentence, {}, updates)
        self.assertIs(updates[10], held)
        self.assertEqual(held["Example translation"], TRANSLATION)
        self.assertNotIn(10, self.col.fetched)

    def test_the_editor_s_hook_translates_the_note_it_shows_only(self):
        updates: dict = {}
        op.translate_sentence_in_note(
            TWO_TYPE, self.sentence, {}, updates, copy_to_vocab_notes=False
        )
        self.assertEqual(self.sentence["Text translation"], TRANSLATION)
        self.assertEqual(self.col.searches, [])
        self.assertEqual(sorted(updates), [20])

    def test_a_sentence_note_not_added_yet_has_no_vocab_notes(self):
        self.sentence.id = 0
        op.translate_sentence_in_note(TWO_TYPE, self.sentence, {}, {})
        self.assertEqual(self.col.searches, [])

    def test_a_failed_translation_copies_nothing(self):
        with mock.patch.object(op, "get_translated_field_from_model", return_value=None):
            self.assertFalse(op.translate_sentence_in_note(TWO_TYPE, self.sentence, {}, {}))
        self.assertEqual(self.col.searches, [])

    def test_one_type_searches_nothing(self):
        note = Note(5, VOCAB, {"Sentence": "猫が寝る。", "Sentence translation": ""})
        updates: dict = {}
        self.assertTrue(op.translate_sentence_in_note(ONE_TYPE, note, {}, updates))
        self.assertEqual(note["Sentence translation"], TRANSLATION)
        self.assertEqual(self.col.searches, [])
        self.assertEqual(list(updates), [5])

    def test_a_broken_layout_asks_for_no_translation(self):
        config = {name: dict(block) for name, block in TWO_TYPE.items()}
        del config[VOCAB]["sentence_note_type"]
        with mock.patch.object(op, "get_translated_field_from_model") as model:
            self.assertFalse(op.translate_sentence_in_note(config, self.sentence, {}, {}))
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
