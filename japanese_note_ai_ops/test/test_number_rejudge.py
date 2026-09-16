"""Reopening the flagged numbers and counters: which go to the matcher, the judge, or nowhere."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import number_rejudge  # noqa: E402

FLAGGED = ["dontmatch"]


def word(pos: str, form: str, reading: str, match_data=None, sub_words=None) -> list:
    return [form, pos, form, reading, list(FLAGGED if match_data is None else match_data), sub_words or []]


def array(*words) -> str:
    return json.dumps(list(words), ensure_ascii=False)


def note(nid: int, kanjified: str, kana: str, links: str = "") -> dict:
    return {
        "nid": nid,
        "vocab-key": kanjified,
        "vocab": kanjified,
        "vocab-kanjified": kanjified,
        "vocab-kana": kana,
        "sentence-vocab-list": links,
    }


def sentence(nid: int, *words) -> dict:
    return note(nid, "", "", array(*words))


def actions(edits: list) -> list:
    return [(e.action, e.form, e.reading) for e in edits]


class FakeAnki:
    def __init__(self, fields: dict):
        self.fields = fields
        self.tags: list = []
        self.untagged: list = []

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

    def add_tags(self, nids, tag):
        self.tags.append((list(nids), tag))

    def remove_tags(self, nids, tag):
        self.untagged.append((list(nids), tag))


class TestWhatCounts(unittest.TestCase):
    def test_a_word_that_counts_nothing_is_not_looked_at(self):
        rows = [sentence(9, word("noun", "手紙", "てがみ"))]
        self.assertEqual(number_rejudge.plan(rows), ([], {}))

    def test_a_word_built_on_a_number_counts(self):
        rows = [sentence(9, word("noun", "一年", "いちねん", sub_words=[word("number", "一", "いち")]))]
        edits, _ = number_rejudge.plan(rows)
        self.assertIn(("unjudge", "一年", "いちねん"), actions(edits))

    def test_an_element_that_is_not_flagged_is_left_alone(self):
        rows = [sentence(9, word("number", "一", "いち", match_data=[]))]
        self.assertEqual(number_rejudge.plan(rows), ([], {}))


class TestDeciding(unittest.TestCase):
    def test_a_word_with_a_note_goes_straight_to_the_matcher(self):
        rows = [note(1, "一", "いち"), sentence(9, word("number", "一", "いち"))]
        edits, _ = number_rejudge.plan(rows)
        self.assertEqual(actions(edits), [("match", "一", "いち")])

    def test_a_note_found_by_a_katakana_reading_still_counts(self):
        rows = [note(1, "ゼロ", "ゼロ"), sentence(9, word("number", "ゼロ", "ぜろ"))]
        edits, _ = number_rejudge.plan(rows)
        self.assertEqual(actions(edits), [("match", "ゼロ", "ぜろ")])

    def test_a_plain_numeral_that_is_no_word_stays_flagged(self):
        rows = [sentence(9, word("number", "二十八", "にじゅうはち"))]
        edits, held = number_rejudge.plan(rows)
        self.assertEqual(edits, [])
        self.assertIn("written out of numerals", " | ".join(held))

    def test_a_counting_reading_stays_flagged(self):
        # 三[みっ] is the 三 of 三つ and has no reading of its own; sending it to the judge
        # under the new rules would invite a note for it.
        rows = [sentence(9, word("number", "三", "みっ"))]
        edits, held = number_rejudge.plan(rows)
        self.assertEqual(edits, [])
        self.assertIn("counting reading", " | ".join(held))

    def test_a_counting_reading_with_a_note_is_matched_anyway(self):
        # 一[ひと] is not 一's own reading either, but the note says it is a word.
        rows = [note(1, "一", "ひと"), sentence(9, word("number", "一", "ひと"))]
        edits, _ = number_rejudge.plan(rows)
        self.assertEqual(actions(edits), [("match", "一", "ひと")])

    def test_the_counter_tsu_stays_flagged(self):
        rows = [sentence(9, word("counter", "つ", "つ"))]
        edits, held = number_rejudge.plan(rows)
        self.assertEqual(edits, [])
        self.assertIn("dontmatch つ", " | ".join(held))

    def test_a_number_not_written_out_of_numerals_goes_to_the_judge(self):
        rows = [sentence(9, word("number", "幾", "いく"))]
        edits, _ = number_rejudge.plan(rows)
        self.assertEqual(actions(edits), [("unjudge", "幾", "いく")])

    def test_a_date_with_no_note_goes_to_the_judge(self):
        rows = [sentence(9, word("noun", "三月", "さんがつ", sub_words=[word("number", "三", "さん")]))]
        edits, _ = number_rejudge.plan(rows)
        self.assertIn(("unjudge", "三月", "さんがつ"), actions(edits))

    def test_a_dump_without_the_array_field_is_refused(self):
        with self.assertRaises(number_rejudge.anki_connect.AnkiConnectError):
            number_rejudge.plan([{"nid": 1, "vocab-kanjified": "一"}])


class TestWhichNotesNeedTheJudge(unittest.TestCase):
    def test_only_the_notes_with_something_unjudged_are_named(self):
        rows = [
            note(1, "一", "いち"),
            sentence(9, word("number", "一", "いち")),  # a note exists, so nothing to judge
            sentence(8, word("number", "幾", "いく")),  # no note, so the judge decides
        ]
        edits, _ = number_rejudge.plan(rows)
        self.assertEqual(number_rejudge.notes_for_the_judge(edits), [8])


class TestRewriting(unittest.TestCase):
    def setUp(self):
        self.rows = [
            note(1, "一", "いち"),
            sentence(
                9,
                word("number", "一", "いち"),
                word("number", "幾", "いく"),
                word("number", "二十八", "にじゅうはち"),
                word("noun", "手紙", "てがみ"),
            ),
        ]
        self.edits, _ = number_rejudge.plan(self.rows)

    def test_each_action_writes_its_own_match_data(self):
        array_ = number_rejudge.match_flags.decode_word_array(self.rows[-1]["sentence-vocab-list"])
        changed, stale = number_rejudge.rewrite(array_, self.edits, 9)
        self.assertEqual((changed, stale), (2, []))
        self.assertEqual([w[4] for w in array_], [["match"], [], FLAGGED, FLAGGED])

    def test_an_array_that_changed_since_the_dump_is_refused(self):
        array_ = number_rejudge.match_flags.decode_word_array(self.rows[-1]["sentence-vocab-list"])
        array_.pop(1)  # the 幾 element is gone
        changed, stale = number_rejudge.rewrite(array_, self.edits, 9)
        self.assertEqual((changed, [e.form for e in stale]), (0, ["幾"]))


class TestWriting(unittest.TestCase):
    def setUp(self):
        self.rows = [
            note(1, "一", "いち"),
            sentence(9, word("number", "一", "いち"), word("number", "幾", "いく")),
        ]
        self.edits, _ = number_rejudge.plan(self.rows)
        self.anki = FakeAnki({9: {"sentence-vocab-list": self.rows[-1]["sentence-vocab-list"]}})

    def test_apply_writes_the_array_and_tags_what_the_judge_must_see(self):
        with tempfile.TemporaryDirectory() as tmp:
            written, tagged, refused = number_rejudge.apply(
                self.anki, self.edits, Path(tmp) / "undo.jsonl"
            )
        self.assertEqual((written, tagged, refused), (1, 1, []))
        field = self.anki.fields[9]["sentence-vocab-list"]
        self.assertEqual([w[4] for w in json.loads(field)], [["match"], []])
        self.assertEqual(self.anki.tags, [([9], number_rejudge.TAG)])

    def test_revert_puts_the_field_back_and_removes_the_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            undo = Path(tmp) / "undo.jsonl"
            number_rejudge.apply(self.anki, self.edits, undo)
            reverted, refused = number_rejudge.revert(self.anki, undo)
            self.assertEqual(undo.read_text(encoding="utf-8"), "")
        self.assertEqual((reverted, refused), (1, []))
        self.assertEqual(
            self.anki.fields[9]["sentence-vocab-list"], self.rows[-1]["sentence-vocab-list"]
        )
        self.assertEqual(self.anki.untagged, [([9], number_rejudge.TAG)])

    def test_a_note_whose_array_moved_on_is_refused_and_nothing_is_written(self):
        before = array(word("number", "一", "いち"))
        self.anki.fields[9]["sentence-vocab-list"] = before
        with tempfile.TemporaryDirectory() as tmp:
            written, tagged, refused = number_rejudge.apply(
                self.anki, self.edits, Path(tmp) / "undo.jsonl"
            )
        self.assertEqual((written, tagged), (0, 0))
        self.assertIn("no longer flagged as the dump saw them", refused[0])
        self.assertEqual(self.anki.fields[9]["sentence-vocab-list"], before)


if __name__ == "__main__":
    unittest.main()
