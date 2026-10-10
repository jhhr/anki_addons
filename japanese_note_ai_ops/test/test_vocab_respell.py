"""The vocab respell: vocab-kanjified taken from the array's dict_form, furigana redrawn."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import vocab_respell  # noqa: E402


def word(raw: str, dict_form: str, reading: str, match_data: list, sub_words=None) -> list:
    return [raw, "noun", dict_form, reading, match_data, sub_words or []]


def array(*words) -> str:
    return json.dumps(list(words), ensure_ascii=False)


def row(nid: int, kanjified: str, kana: str, links: str = "", **extra) -> dict:
    out = {
        "nid": nid,
        "vocab-key": kanjified,
        "vocab": kanjified,
        "vocab-kanjified": kanjified,
        "vocab-kana": kana,
        "vocab-furigana": "",
        "vocab-processed-furigana": "",
        "ignore-kanjified-form": "",
        "sentence-vocab-list": links,
    }
    out.update(extra)
    return out


def reasons(held: dict) -> str:
    return " | ".join(held)


class FakeAnki:
    """notes_info/updateNoteFields over a dict of {nid: {field: value}}."""

    def __init__(self, fields: dict):
        self.fields = fields
        self.tags: dict = {}
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

    def add_tags(self, nids, tags):
        for nid in nids:
            self.tags.setdefault(nid, []).append(tags)

    def remove_tags(self, nids, tags):
        self.untagged.append((list(nids), tags))


class TestReadingTheArrays(unittest.TestCase):
    def test_a_sub_word_link_counts_like_a_top_level_one(self):
        sentence = array(
            word(" 持[も]って", "持って来る", "もってくる", [], [word("持つ", "持つ", "もつ", [11])]),
        )
        links = vocab_respell.links_by_note([{"sentence-vocab-list": sentence}])
        self.assertEqual(links[11][("持つ", "もつ")], 1)

    def test_a_placeholder_id_is_not_a_link(self):
        sentence = array(word("犬", "犬", "いぬ", [-7]))
        self.assertEqual(vocab_respell.links_by_note([{"sentence-vocab-list": sentence}]), {})

    def test_an_unjudged_or_rated_element_is_read_correctly(self):
        sentence = array(
            word("猫", "猫", "ねこ", []),
            word("犬", "犬", "いぬ", [12, 3]),
            word("鳥", "鳥", "とり", ["dontmatch"]),
        )
        links = vocab_respell.links_by_note([{"sentence-vocab-list": sentence}])
        self.assertEqual(dict(links), {12: {("犬", "いぬ"): 1}})

    def test_a_field_that_is_not_an_array_is_skipped(self):
        rows = [{"sentence-vocab-list": "not json"}, {"sentence-vocab-list": ""}]
        self.assertEqual(vocab_respell.links_by_note(rows), {})


class TestPlanning(unittest.TestCase):
    def test_the_kanjified_form_becomes_the_dict_form(self):
        links = array(word("たち", "達", "たち", [1]))
        changes, _ = vocab_respell.plan([row(1, "たち", "たち", links, **{"vocab": "たち"})])
        self.assertEqual([(c.before, c.after) for c in changes], [("たち", "達")])

    def test_the_furigana_is_redrawn_from_the_new_spelling(self):
        links = array(word("たち", "達", "たち", [1]))
        changes, _ = vocab_respell.plan([row(1, "たち", "たち", links, **{"vocab": "たち"})])
        self.assertEqual(changes[0].furigana_after, " 達[たち]")
        self.assertIn("達", changes[0].processed_after)

    def test_a_note_the_arrays_already_agree_with_is_left_alone(self):
        links = array(word("達", "達", "たち", [1]))
        changes, held = vocab_respell.plan([row(1, "達", "たち", links)])
        self.assertEqual((changes, dict(held)), ([], {}))

    def test_a_reading_that_differs_holds_the_note_back(self):
        links = array(word("様", "様", "よう", [1]))
        changes, held = vocab_respell.plan([row(1, "樣", "さま", links)])
        self.assertEqual(changes, [])
        self.assertIn("the reading differs", reasons(held))

    def test_two_arrays_that_disagree_hold_the_note_back(self):
        rows = [
            row(1, "こと", "こと"),
            row(2, "x", "x", array(word("こと", "事", "こと", [1]))),
            row(3, "y", "y", array(word("こと", "毎", "こと", [1]))),
        ]
        changes, held = vocab_respell.plan(rows)
        self.assertEqual([c.nid for c in changes], [])
        self.assertIn("more than one word", reasons(held))

    def test_a_note_that_opts_out_is_skipped(self):
        links = array(word("たち", "達", "たち", [1]))
        rows = [row(1, "たち", "たち", links, **{"ignore-kanjified-form": "y"})]
        changes, held = vocab_respell.plan(rows)
        self.assertEqual(changes, [])
        self.assertIn("ignore-kanjified-form", reasons(held))

    def test_respelling_to_the_notes_own_vocab_is_held_back(self):
        # `vocab` already matches through the definitions' {{vocab}} alternative, and writing it
        # into vocab-kanjified would throw the kanjified spelling away.
        links = array(word("お前", "お前", "おまえ", [1]))
        rows = [row(1, "御前", "おまえ", links, **{"vocab": "お前"})]
        changes, held = vocab_respell.plan(rows)
        self.assertEqual(changes, [])
        self.assertIn("already matches", reasons(held))

    def test_furigana_that_does_not_read_back_holds_the_note_back(self):
        # お金 read as かね: the reading leaves out the word's own お, so no furigana can be
        # drawn that reads back as both. The note is held back rather than written with one
        # that would read おかね.
        links = array(word("お金", "お金", "かね", [1]))
        rows = [row(1, "御金", "かね", links, **{"vocab": "おかね"})]
        changes, held = vocab_respell.plan(rows)
        self.assertEqual(changes, [])
        self.assertIn("cannot be drawn as furigana", reasons(held))

    def test_a_dump_without_the_new_fields_is_refused(self):
        with self.assertRaises(vocab_respell.anki_connect.AnkiConnectError):
            vocab_respell.plan([{"nid": 1, "vocab-kanjified": "達"}])


class TestFuriganaCheck(unittest.TestCase):
    def test_a_furigana_that_reads_back_passes(self):
        self.assertTrue(vocab_respell.furigana_reads_back(" 気持[きも]ち", "気持ち", "きもち"))

    def test_a_doubled_reading_fails(self):
        self.assertFalse(
            vocab_respell.furigana_reads_back("かも 知[かもし]れない", "かも知れない", "かもしれない")
        )


class TestWriting(unittest.TestCase):
    def setUp(self):
        links = array(word("たち", "達", "たち", [1]))
        self.changes, _ = vocab_respell.plan([row(1, "たち", "たち", links, **{"vocab": "たち"})])
        self.anki = FakeAnki(
            {
                1: {
                    "vocab-kanjified": "たち",
                    "vocab-furigana": "",
                    "vocab-processed-furigana": "",
                }
            }
        )

    def test_apply_writes_the_three_fields_and_tags_the_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            undo = Path(tmp) / "undo.jsonl"
            written, tagged, refused = vocab_respell.apply(self.anki, self.changes, undo)
        self.assertEqual((written, tagged, refused), (1, 1, []))
        self.assertEqual(self.anki.fields[1]["vocab-kanjified"], "達")
        self.assertEqual(self.anki.fields[1]["vocab-furigana"], " 達[たち]")
        self.assertEqual(self.anki.tags[1], [vocab_respell.TAG])

    def test_a_note_changed_since_the_dump_is_refused(self):
        self.anki.fields[1]["vocab-kanjified"] = "達人"
        with tempfile.TemporaryDirectory() as tmp:
            written, _, refused = vocab_respell.apply(self.anki, self.changes, Path(tmp) / "u")
        self.assertEqual(written, 0)
        self.assertIn("the dump said", refused[0])

    def test_revert_puts_the_old_values_back_and_removes_the_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            undo = Path(tmp) / "undo.jsonl"
            vocab_respell.apply(self.anki, self.changes, undo)
            reverted, refused = vocab_respell.revert(self.anki, undo)
            self.assertEqual(undo.read_text(encoding="utf-8"), "")
        self.assertEqual((reverted, refused), (1, []))
        self.assertEqual(self.anki.fields[1]["vocab-kanjified"], "たち")
        self.assertEqual(self.anki.untagged, [([1], vocab_respell.TAG)])


if __name__ == "__main__":
    unittest.main()
