"""The multi-link cleanup: which of a note's links are its word, and what happens to the rest."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import vocab_unlink  # noqa: E402


def word(raw: str, dict_form: str, reading: str, match_data: list, sub_words=None) -> list:
    return [raw, "noun", dict_form, reading, match_data, sub_words or []]


def array(*words) -> str:
    return json.dumps(list(words), ensure_ascii=False)


def note(nid: int, kanjified: str, kana: str, links: str = "", **extra) -> dict:
    row = {
        "nid": nid,
        "vocab-key": kanjified,
        "vocab": kanjified,
        "vocab-kanjified": kanjified,
        "vocab-kana": kana,
        "sentence-vocab-list": links,
    }
    row.update(extra)
    return row


def sentence(nid: int, *words) -> dict:
    return note(nid, "", "", array(*words))


def actions(edits: list) -> list:
    return [(e.action, e.form, e.reading) for e in edits]


class FakeAnki:
    """notes_info/updateNoteFields over a dict of {nid: {field: value}}."""

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


class TestFindingTheAnchor(unittest.TestCase):
    def test_a_note_linked_by_one_word_is_not_touched(self):
        rows = [note(1, "達", "たち"), sentence(9, word("達", "達", "たち", [1]))]
        self.assertEqual(vocab_unlink.plan(rows), ([], {}))

    def test_the_link_that_reads_and_spells_like_the_note_is_the_anchor(self):
        rows = [
            note(1, "人", "ひと"),
            sentence(9, word("人", "人", "ひと", [1]), word("人", "人", "にん", [1])),
        ]
        edits, held = vocab_unlink.plan(rows)
        self.assertEqual((actions(edits), dict(held)), ([("unlink", "人", "にん")], {}))

    def test_a_note_no_link_spells_is_held_back(self):
        # 良く linked only by 良い [よい] and 浴 [よく]: 浴 reads right but is another word, and
        # picking it would go on to respell the note 良く -> 浴.
        rows = [
            note(1, "良く", "よく"),
            sentence(9, word("良い", "良い", "よい", [1]), word("浴", "浴", "よく", [1])),
        ]
        edits, held = vocab_unlink.plan(rows)
        self.assertEqual(edits, [])
        self.assertIn("no link both reads and spells", " | ".join(held))

    def test_a_note_linked_by_two_of_its_own_spellings_is_held_back(self):
        rows = [
            note(1, "珈琲", "こーひー", **{"vocab": "コーヒー"}),
            sentence(9, word("珈琲", "珈琲", "こーひー", [1]), word("コーヒー", "コーヒー", "こーひー", [1])),
        ]
        edits, held = vocab_unlink.plan(rows)
        self.assertEqual(edits, [])
        self.assertIn("two of its own spellings", " | ".join(held))

    def test_the_vocab_key_base_form_can_be_the_anchor(self):
        rows = [
            note(1, "皆", "みな", **{"vocab-key": "皆 (m1)"}),
            sentence(9, word("皆", "皆", "みな", [1]), word("皆", "皆", "みんな", [1])),
        ]
        edits, _ = vocab_unlink.plan(rows)
        self.assertEqual(actions(edits), [("unlink", "皆", "みんな")])


class TestDecidingWhatToDo(unittest.TestCase):
    def test_a_word_another_note_owns_is_unlinked(self):
        rows = [
            note(1, "或る", "ある"),
            note(2, "有る", "ある"),
            sentence(9, word("或る", "或る", "ある", [1]), word("有る", "有る", "ある", [1])),
        ]
        edits, _ = vocab_unlink.plan(rows)
        self.assertEqual(actions(edits), [("unlink", "有る", "ある")])
        self.assertIn("note 2's own word", edits[0].why)

    def test_a_spelling_no_note_owns_is_respelled_in_the_array(self):
        rows = [
            note(1, "どうぞ", "どうぞ"),
            sentence(9, word("どうぞ", "どうぞ", "どうぞ", [1]), word("如何ぞ", "如何ぞ", "どうぞ", [1])),
        ]
        edits, _ = vocab_unlink.plan(rows)
        self.assertEqual(actions(edits), [("respell", "如何ぞ", "どうぞ")])
        self.assertEqual(edits[0].spelling, "どうぞ")

    def test_a_phrase_no_note_owns_goes_back_to_the_judge(self):
        rows = [
            note(1, "七", "なな"),
            sentence(9, word("七", "七", "なな", [1]), word("七番組寮", "七番組寮", "ななばんぐみりょう", [1])),
        ]
        edits, _ = vocab_unlink.plan(rows)
        self.assertEqual(actions(edits), [("unjudge", "七番組寮", "ななばんぐみりょう")])

    def test_a_phrase_another_note_owns_goes_to_the_matcher_not_the_judge(self):
        rows = [
            note(1, "青", "あお"),
            note(2, "青い", "あおい"),
            sentence(9, word("青", "青", "あお", [1]), word("青い", "青い", "あおい", [1])),
        ]
        edits, _ = vocab_unlink.plan(rows)
        self.assertEqual(actions(edits), [("unlink", "青い", "あおい")])

    def test_a_katakana_reading_still_counts_as_the_note_s(self):
        rows = [
            note(1, "タバコ", "たばこ"),
            sentence(9, word("タバコ", "タバコ", "タバコ", [1]), word("煙草", "煙草", "たばこ", [1])),
        ]
        edits, _ = vocab_unlink.plan(rows)
        self.assertEqual(actions(edits), [("respell", "煙草", "たばこ")])

    def test_a_sub_word_link_is_counted_like_a_top_level_one(self):
        rows = [
            note(1, "持つ", "もつ"),
            sentence(
                9,
                word("持つ", "持つ", "もつ", [1]),
                word(" 持[も]って", "持って来る", "もってくる", [], [word("持つ", "持つ", "もち", [1])]),
            ),
        ]
        edits, _ = vocab_unlink.plan(rows)
        self.assertEqual(actions(edits), [("unlink", "持つ", "もち")])

    def test_a_placeholder_id_is_not_a_link(self):
        rows = [note(1, "犬", "いぬ"), sentence(9, word("犬", "犬", "いぬ", [-7]))]
        self.assertEqual(vocab_unlink.plan(rows), ([], {}))

    def test_a_dump_without_the_array_field_is_refused(self):
        with self.assertRaises(vocab_unlink.anki_connect.AnkiConnectError):
            vocab_unlink.plan([{"nid": 1, "vocab-kanjified": "達"}])


class TestRewritingAnArray(unittest.TestCase):
    def setUp(self):
        self.rows = [
            note(1, "人", "ひと"),
            note(2, "七", "なな"),
            note(3, "どうぞ", "どうぞ"),
            sentence(
                9,
                word("人", "人", "ひと", [1, 4]),
                word("人", "人", "にん", [1, 3]),
                word("七", "七", "なな", [2]),
                word("七番組寮", "七番組寮", "ななばんぐみりょう", [2]),
                word("どうぞ", "どうぞ", "どうぞ", [3]),
                word("如何ぞ", "如何ぞ", "どうぞ", [3, 5]),
            ),
        ]
        self.edits, _ = vocab_unlink.plan(self.rows)

    def rewritten(self):
        array = vocab_unlink.match_flags.decode_word_array(self.rows[-1]["sentence-vocab-list"])
        changed, stale = vocab_unlink.rewrite(array, self.edits, 9)
        return array, changed, stale

    def test_each_action_writes_its_own_match_data(self):
        array, changed, stale = self.rewritten()
        self.assertEqual((changed, stale), (3, []))
        self.assertEqual([w[4] for w in array], [[1, 4], ["match"], [2], [], [3], [3, 5]])

    def test_a_respell_rewrites_the_dict_form_and_leaves_the_raw_text(self):
        array, _, _ = self.rewritten()
        self.assertEqual((array[5][0], array[5][2]), ("如何ぞ", "どうぞ"))

    def test_the_anchor_elements_are_left_alone(self):
        array, _, _ = self.rewritten()
        self.assertEqual([w[2] for w in array[:1]], ["人"])
        self.assertEqual(array[0][4], [1, 4])

    def test_an_array_that_changed_since_the_dump_is_refused(self):
        array = vocab_unlink.match_flags.decode_word_array(self.rows[-1]["sentence-vocab-list"])
        array.pop(1)  # the 人 [にん] element is gone
        changed, stale = vocab_unlink.rewrite(array, self.edits, 9)
        self.assertEqual((changed, [(e.form, e.reading) for e in stale]), (0, [("人", "にん")]))


class TestWriting(unittest.TestCase):
    def setUp(self):
        self.rows = [
            note(1, "人", "ひと"),
            sentence(9, word("人", "人", "ひと", [1]), word("人", "人", "にん", [1])),
        ]
        self.edits, _ = vocab_unlink.plan(self.rows)
        self.anki = FakeAnki({9: {"sentence-vocab-list": self.rows[-1]["sentence-vocab-list"]}})

    def test_apply_writes_the_array_one_word_per_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            written, refused = vocab_unlink.apply(self.anki, self.edits, Path(tmp) / "undo.jsonl")
        self.assertEqual((written, refused), (1, []))
        field = self.anki.fields[9]["sentence-vocab-list"]
        self.assertEqual([w[4] for w in json.loads(field)], [[1], ["match"]])
        self.assertIn('],\n', field)

    def test_revert_puts_the_field_text_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            undo = Path(tmp) / "undo.jsonl"
            vocab_unlink.apply(self.anki, self.edits, undo)
            reverted, refused = vocab_unlink.revert(self.anki, undo)
            self.assertEqual(undo.read_text(encoding="utf-8"), "")
        self.assertEqual((reverted, refused), (1, []))
        self.assertEqual(
            self.anki.fields[9]["sentence-vocab-list"], self.rows[-1]["sentence-vocab-list"]
        )

    def test_a_note_whose_array_moved_on_is_refused_and_nothing_is_written(self):
        before = array(word("人", "人", "ひと", [1]))
        self.anki.fields[9]["sentence-vocab-list"] = before
        with tempfile.TemporaryDirectory() as tmp:
            written, refused = vocab_unlink.apply(self.anki, self.edits, Path(tmp) / "undo.jsonl")
        self.assertEqual(written, 0)
        self.assertIn("no longer there as the dump saw them", refused[0])
        self.assertEqual(self.anki.fields[9]["sentence-vocab-list"], before)


if __name__ == "__main__":
    unittest.main()
