"""Kanjify sentence's per-note op (async_api_ops/kanjify_sentence.py): the notes it leaves
alone. Its prompt and its call are pinned in test_capture_kinds.py."""

from __future__ import annotations

import unittest
from unittest import mock

from addon_modules import load_ops_module

kanjify = load_ops_module("kanjify_sentence")

SENTENCE = "Sentence note"
CONFIG = {
    SENTENCE: {"furigana_sentence_field": "Furigana", "kanjified_sentence_field": "Kanjified"},
}
BEFORE = "お 箸[はし]、一膳しかないの。"
AFTER = "お 箸[はし]、一膳しか<k> 無[な]い</k>の。"


class FakeNote:
    def __init__(self, fields: dict[str, str], tags=(), note_id: int = 7) -> None:
        self.id = note_id
        self.fields = dict(fields)
        self.tags = list(tags)

    def note_type(self):
        return {"name": SENTENCE}

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def add_tag(self, tag: str) -> None:
        if tag not in self.tags:
            self.tags.append(tag)

    def remove_tag(self, tag: str) -> None:
        self.tags = [t for t in self.tags if t != tag]

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]

    def __setitem__(self, field: str, value: str) -> None:
        self.fields[field] = value


class KanjifyInNoteTests(unittest.TestCase):
    def kanjify_note(self, note: FakeNote) -> tuple[bool, dict, mock.Mock, mock.Mock]:
        updates: dict = {}
        with mock.patch.object(
            kanjify, "get_kanjified_sentence_from_model", return_value=[AFTER]
        ) as model, mock.patch.object(kanjify, "report_error") as report:
            changed = kanjify.kanjify_sentence_in_note(CONFIG, note, {}, updates)
        return changed, updates, model, report

    def test_a_checked_note_is_kanjified(self):
        note = FakeNote({"Furigana": BEFORE, "Kanjified": ""})
        changed, updates, _, report = self.kanjify_note(note)
        self.assertTrue(changed)
        self.assertEqual((note["Kanjified"], updates), (AFTER, {7: note}))
        report.assert_not_called()

    def test_a_note_whose_furigana_awaits_review_is_left_alone(self):
        # Set furigana from audio left a reading unsettled; kanjified, it would be taken as said
        note = FakeNote({"Furigana": BEFORE, "Kanjified": ""}, tags=["furigana-review"])
        changed, updates, model, report = self.kanjify_note(note)
        self.assertFalse(changed)
        self.assertEqual((note["Kanjified"], updates), ("", {}))
        model.assert_not_called()
        self.assertIn('tagged "furigana-review"', report.call_args.args[0])

    def test_a_word_still_marked_for_review_keeps_the_note_alone_without_its_tag(self):
        # The tag taken off before every word in doubt was fixed
        note = FakeNote({"Furigana": "お 箸[はし？ばし]、一膳しかないの。", "Kanjified": ""})
        changed, updates, model, report = self.kanjify_note(note)
        self.assertFalse(changed)
        self.assertEqual((note["Kanjified"], updates), ("", {}))
        model.assert_not_called()
        self.assertIn('"？"', report.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
