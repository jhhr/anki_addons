"""The vocab re-key: one sort field per duplicate group, keeper lowest, tags and undo."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import vocab_dupes  # noqa: E402
import vocab_rekey  # noqa: E402

CONFIG = {"Vocab": {"word_sort_field": "vocab-key"}}
# 未だ is the canonical spelling of まだ, 気 of 氣; every other key is its own.
CANON = {"まだ": "未だ", "氣": "気"}


def row(nid: int, key: str, reading: str, studied: bool = False, links: str = "") -> dict:
    return {
        "nid": nid,
        "vocab-key": key,
        "vocab-kana": reading,
        "sentence-vocab-list": links,
        "studied": studied,
    }


class FakeAnki:
    def __init__(self, fields: dict, model: str = "Vocab"):
        self.fields = fields
        self.model = model
        self.tags: dict = {}
        self.written: list = []

    def notes_info(self, nids):
        return [
            (
                {
                    "noteId": n,
                    "modelName": self.model,
                    "fields": {"vocab-key": {"value": self.fields[n]}},
                }
                if n in self.fields
                else {}
            )
            for n in nids
        ]

    def update_note_fields(self, nid, fields):
        self.written.append((nid, fields))
        self.fields[nid] = fields["vocab-key"]

    def add_tags(self, nids, tags):
        for nid in nids:
            self.tags.setdefault(nid, set()).update(tags.split())

    def remove_tags(self, nids, tags):
        for nid in nids:
            self.tags.get(nid, set()).difference_update(tags.split())


class KeyPartTests(unittest.TestCase):
    def test_a_trailing_meaning_number_is_the_only_one_split_off(self):
        self.assertEqual(vocab_rekey.split_key("一 (kun)(r2)(m3)"), ("一", ["kun", "r2"], 3))
        self.assertEqual(vocab_rekey.split_key("一 (kun)(r2)"), ("一", ["kun", "r2"], None))
        self.assertEqual(vocab_rekey.split_key("気 (m1)(x2)"), ("気", ["m1", "x2"], None))
        self.assertEqual(vocab_rekey.split_key("という"), ("という", [], None))

    def test_built_keys_share_the_prefix_the_dedupe_groups_by(self):
        self.assertEqual(vocab_rekey.build_key("一つ", [], 4), "一つ (m4)")
        self.assertEqual(vocab_rekey.build_key("気", ["r1"], 2), "気 (r1)(m2)")
        keys = [vocab_rekey.build_key("気", ["r1"], n) for n in (1, 2)]
        self.assertEqual({k[: k.rindex("(m")] for k in keys}, {"気 (r1)"})


class NumberingTests(unittest.TestCase):
    def test_numbers_rise_with_the_rank_and_keep_free_existing_ones(self):
        ranked = [row(1, "来る (m1)", "くる"), row(2, "來る", "くる"), row(3, "来る (m3)", "くる")]
        self.assertEqual(vocab_rekey.assign_numbers(ranked, set()), [1, 2, 3])

    def test_a_number_below_the_one_before_it_is_moved_up(self):
        ranked = [row(1, "渡る (m1)", "わたる"), row(2, "亘る (m1)", "わたる")]
        self.assertEqual(vocab_rekey.assign_numbers(ranked, set()), [1, 2])

    def test_numbers_of_notes_outside_the_group_are_left_alone(self):
        ranked = [row(1, "応える", "こたえる"), row(2, "応える", "こたえる")]
        self.assertEqual(vocab_rekey.assign_numbers(ranked, {1, 3}), [2, 4])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.canonical_key = vocab_dupes.canonical_key
        vocab_dupes.canonical_key = lambda key, reading: CANON.get(
            vocab_dupes.base_form(key), vocab_dupes.base_form(key)
        )

    def tearDown(self):
        vocab_dupes.canonical_key = self.canonical_key

    def test_the_studied_oldest_note_keeps_the_lowest_number(self):
        rows = [
            row(3, "まだ", "まだ", studied=True),
            row(1, "未だ (m1)", "まだ"),
            row(2, "未だ (m2)", "まだ", studied=True),
        ]
        changes = {c.nid: c for c in vocab_rekey.plan(rows)}
        # The studied notes rank first, oldest first: #2 keeps its own number, #3 takes the next
        # free one, and the unstudied #1 cannot keep (m1) below them.
        self.assertEqual(
            [changes[n].after for n in (1, 2, 3)], ["未だ (m4)", "未だ (m2)", "未だ (m3)"]
        )
        self.assertEqual({c.keeper for c in changes.values()}, {2})
        self.assertEqual(changes[3].before, "まだ")

    def test_a_respelled_note_drops_its_reading_markers_and_takes_the_groups(self):
        rows = [
            row(1, "気 (r1)(m1)", "き", studied=True),
            row(2, "氣 (kun)", "き"),
        ]
        changes = {c.nid: c for c in vocab_rekey.plan(rows)}
        self.assertEqual(changes[1].after, "気 (r1)(m1)")
        self.assertEqual(changes[2].after, "気 (r1)(m2)")
        self.assertEqual(changes[2].dropped, ["kun"])

    def test_one_sort_field_on_two_notes_is_numbered_but_not_respelled(self):
        rows = [
            row(1, "応える", "こたえる"),
            row(2, "応える", "こたえる"),
            row(3, "答える", "こたえる"),
        ]
        changes = {c.nid: c for c in vocab_rekey.plan(rows)}
        self.assertEqual([changes[1].after, changes[2].after], ["応える (m1)", "応える (m2)"])
        self.assertNotIn(3, changes)

    def test_a_note_of_the_same_prefix_outside_the_group_keeps_its_number(self):
        rows = [
            row(1, "未だ (m2)", "まだ"),
            row(2, "まだ", "まだ"),
            row(3, "未だ (m1)", "いまだ"),  # another reading: not of the group, but of the prefix
        ]
        changes = {c.nid: c for c in vocab_rekey.plan(rows)}
        self.assertEqual([changes[1].after, changes[2].after], ["未だ (m2)", "未だ (m3)"])
        self.assertNotIn(3, changes)

    def test_every_note_is_tagged_and_only_the_variants_name_their_keeper(self):
        rows = [row(1, "未だ (m1)", "まだ", studied=True), row(2, "まだ", "まだ")]
        changes = {c.nid: c for c in vocab_rekey.plan(rows)}
        self.assertEqual(vocab_rekey.tags_for(changes[1]), "word-array-duplicate")
        self.assertEqual(
            vocab_rekey.tags_for(changes[2]), "word-array-duplicate word-array-keeper::1"
        )


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.undo = Path(self.tmp.name) / "undo.jsonl"
        self.changes = [
            vocab_rekey.Change(1, "未だ (m1)", "未だ (m1)", 1, "未だ [まだ]", []),
            vocab_rekey.Change(2, "まだ", "未だ (m2)", 1, "未だ [まだ]", []),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_the_changed_fields_are_written_but_every_note_is_tagged(self):
        anki = FakeAnki({1: "未だ (m1)", 2: "まだ"})
        written, tagged, refused = vocab_rekey.apply(anki, self.changes, CONFIG, self.undo)
        self.assertEqual((written, tagged, refused), (1, 2, []))
        self.assertEqual(anki.fields[2], "未だ (m2)")
        self.assertEqual(anki.tags[1], {"word-array-duplicate"})
        self.assertEqual(anki.tags[2], {"word-array-duplicate", "word-array-keeper::1"})

    def test_a_note_edited_since_the_dump_is_refused(self):
        anki = FakeAnki({1: "未だ (m1)", 2: "まだまだ"})
        written, tagged, refused = vocab_rekey.apply(anki, self.changes, CONFIG, self.undo)
        self.assertEqual((written, tagged), (0, 1))
        self.assertEqual(refused, ["nid 2: sort field changed since the dump"])
        self.assertNotIn(2, anki.tags)

    def test_a_note_already_re_keyed_is_tagged_and_written_no_more(self):
        anki = FakeAnki({1: "未だ (m1)", 2: "未だ (m2)"})
        written, tagged, _ = vocab_rekey.apply(anki, self.changes, CONFIG, self.undo)
        self.assertEqual((written, tagged), (0, 2))
        self.assertEqual(anki.written, [])
        self.assertIn("word-array-keeper::1", anki.tags[2])

    def test_revert_puts_the_fields_back_and_removes_the_tags(self):
        anki = FakeAnki({1: "未だ (m1)", 2: "まだ"})
        vocab_rekey.apply(anki, self.changes, CONFIG, self.undo)
        reverted, refused = vocab_rekey.revert(anki, CONFIG, self.undo)
        self.assertEqual((reverted, refused), (1, []))
        self.assertEqual(anki.fields, {1: "未だ (m1)", 2: "まだ"})
        self.assertEqual(anki.tags[1] | anki.tags[2], set())
        self.assertEqual(self.undo.read_text(encoding="utf-8"), "")

    def test_revert_leaves_a_note_edited_since_the_re_key(self):
        anki = FakeAnki({1: "未だ (m1)", 2: "まだ"})
        vocab_rekey.apply(anki, self.changes, CONFIG, self.undo)
        anki.fields[2] = "未だ (m5)"
        reverted, refused = vocab_rekey.revert(anki, CONFIG, self.undo)
        self.assertEqual((reverted, len(refused)), (0, 1))
        self.assertEqual(anki.fields[2], "未だ (m5)")
        self.assertIn("word-array-keeper::1", anki.tags[2])
        left = [json.loads(line) for line in self.undo.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["nid"] for e in left], [2])


if __name__ == "__main__":
    unittest.main()
