"""Repairing a damaged vocab-kana, and refusing to guess when nothing shows which side is wrong."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import vocab_reading_fix as rf  # noqa: E402


def word(dict_form: str, reading: str, match_data: list) -> list:
    return ["", "noun", dict_form, reading, match_data, []]


def note(nid: int, spelling: str, reading: str, links: str = "", **extra) -> dict:
    row = {
        "nid": nid,
        "vocab-key": spelling,
        "vocab": spelling,
        "vocab-kanjified": spelling,
        "vocab-kana": reading,
        "sentence-vocab-list": links,
    }
    row.update(extra)
    return row


def sentence(nid: int, *words) -> dict:
    return note(nid, "", "", json.dumps(list(words), ensure_ascii=False))


class FakeAnki:
    def __init__(self, fields: dict):
        self.fields = fields

    def notes_info(self, nids):
        return [
            (
                {"noteId": n, "fields": {k: {"value": v} for k, v in self.fields[n].items()}}
                if n in self.fields
                else {}
            )
            for n in nids
        ]

    def update_note_fields(self, nid, fields):
        self.fields[nid].update(fields)


class TestDecidingWhichReadingIsWrong(unittest.TestCase):
    def test_a_katakana_spelling_decides_against_the_note(self):
        reading, why = rf.decide("カーネーション", "かーのーしょん", "かーねーしょん")
        self.assertEqual(reading, "かーねーしょん")
        self.assertIn("transliterates", why)

    def test_a_katakana_spelling_that_backs_the_note_writes_nothing(self):
        reading, why = rf.decide("カーネーション", "かーねーしょん", "かーのーしょん")
        self.assertIsNone(reading)
        self.assertIn("the note already has", why)

    def test_a_katakana_spelling_matching_neither_reading_writes_nothing(self):
        self.assertIsNone(rf.decide("カーネーション", "あ", "い")[0])

    def test_a_latin_note_reading_is_damage(self):
        reading, why = rf.decide("ＩＴ", "it", "あいてぃー")
        self.assertEqual(reading, "あいてぃー")
        self.assertIn("not a Japanese reading", why)

    def test_stray_whitespace_is_simply_removed(self):
        self.assertEqual(rf.decide("幾つか", "いくつ か", "いくつか")[0], "いくつか")

    def test_two_plausible_readings_of_a_kanji_word_are_refused(self):
        # Voicing runs both ways and any reading draws onto kanji, so nothing here decides.
        self.assertIsNone(rf.decide("狡賢い", "ずるがしこい", "ずるかしこい")[0])
        self.assertIsNone(rf.decide("砂埃", "すなほこり", "すなぼこり")[0])
        self.assertIsNone(rf.decide("一段落", "ひとだんらく", "いちだんらく")[0])

    def test_a_missing_reading_is_refused(self):
        self.assertIsNone(rf.decide("犬", "", "いぬ")[0])


class TestPlanning(unittest.TestCase):
    def test_a_katakana_typo_is_planned(self):
        rows = [
            note(1, "カーネーション", "かーのーしょん"),
            sentence(9, word("カーネーション", "かーねーしょん", [1])),
        ]
        fixes, _ = rf.plan(rows)
        self.assertEqual([(f.note_id, f.was, f.now) for f in fixes],
                         [(1, "かーのーしょん", "かーねーしょん")])

    def test_a_word_another_note_owns_says_nothing_about_this_reading(self):
        rows = [
            note(1, "カーネーション", "かーのーしょん"),
            note(2, "カーネーション", "かーねーしょん"),
            sentence(9, word("カーネーション", "かーねーしょん", [1])),
        ]
        fixes, held = rf.plan(rows)
        self.assertEqual(fixes, [])
        self.assertIn("another note owns", " | ".join(held))

    def test_a_kanji_word_with_two_plausible_readings_is_held(self):
        rows = [note(1, "狡賢い", "ずるがしこい"), sentence(9, word("狡賢い", "ずるかしこい", [1]))]
        fixes, held = rf.plan(rows)
        self.assertEqual(fixes, [])
        self.assertIn("voicing", " | ".join(held))

    def test_a_dump_without_the_array_field_is_refused(self):
        with self.assertRaises(rf.anki_connect.AnkiConnectError):
            rf.plan([{"nid": 1, "vocab-kanjified": "犬"}])


class TestWriting(unittest.TestCase):
    def setUp(self):
        self.rows = [
            note(1, "カーネーション", "かーのーしょん"),
            sentence(9, word("カーネーション", "かーねーしょん", [1])),
        ]
        self.fixes, _ = rf.plan(self.rows)
        self.anki = FakeAnki(
            {
                1: {
                    "vocab-kana": "かーのーしょん",
                    "vocab-furigana": "old",
                    "vocab-processed-furigana": "old",
                }
            }
        )

    def test_apply_writes_the_reading_and_redraws_the_furigana(self):
        with tempfile.TemporaryDirectory() as tmp:
            written, refused = rf.apply(self.anki, self.fixes, Path(tmp) / "undo.jsonl")
        self.assertEqual((written, refused), (1, []))
        self.assertEqual(self.anki.fields[1]["vocab-kana"], "かーねーしょん")
        self.assertNotEqual(self.anki.fields[1]["vocab-furigana"], "old")

    def test_a_note_whose_reading_moved_on_is_refused(self):
        self.anki.fields[1]["vocab-kana"] = "something else"
        with tempfile.TemporaryDirectory() as tmp:
            written, refused = rf.apply(self.anki, self.fixes, Path(tmp) / "undo.jsonl")
        self.assertEqual(written, 0)
        self.assertIn("not", refused[0])
        self.assertEqual(self.anki.fields[1]["vocab-kana"], "something else")

    def test_revert_puts_the_fields_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            undo = Path(tmp) / "undo.jsonl"
            rf.apply(self.anki, self.fixes, undo)
            reverted, refused = rf.revert(self.anki, undo)
            self.assertEqual(undo.read_text(encoding="utf-8"), "")
        self.assertEqual((reverted, refused), (1, []))
        self.assertEqual(self.anki.fields[1]["vocab-kana"], "かーのーしょん")
        self.assertEqual(self.anki.fields[1]["vocab-furigana"], "old")

    def test_revert_leaves_a_note_edited_since_the_repair_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            undo = Path(tmp) / "undo.jsonl"
            rf.apply(self.anki, self.fixes, undo)
            self.anki.fields[1]["vocab-kana"] = "edited by hand"
            reverted, refused = rf.revert(self.anki, undo)
            self.assertNotEqual(undo.read_text(encoding="utf-8"), "")
        self.assertEqual(reverted, 0)
        self.assertIn("edited since the repair", refused[0])
        self.assertEqual(self.anki.fields[1]["vocab-kana"], "edited by hand")


if __name__ == "__main__":
    unittest.main()
