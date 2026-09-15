"""The kanjify batch fix: writes only unchanged notes, records old values, reverts them."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import kanjify_fix  # noqa: E402

CONFIG = {"Note": {"kanjified_sentence_field": "k"}}
BEFORE = "本を<k> 為[し]て 来[き]た</k>。"
AFTER = "本を<k> 為[し]てきた</k>。"


class FakeAnki:
    def __init__(self, fields: dict[int, str], model: str = "Note"):
        self.fields = fields
        self.model = model
        self.written: list[tuple[int, dict]] = []

    def notes_info(self, nids):
        return [
            (
                {"noteId": n, "modelName": self.model, "fields": {"k": {"value": self.fields[n]}}}
                if n in self.fields
                else {}
            )
            for n in nids
        ]

    def update_note_fields(self, nid, fields):
        self.written.append((nid, fields))
        self.fields[nid] = fields["k"]


def fix_row(nids, before=BEFORE, after=AFTER) -> dict:
    fixes = [{"class": "て-helper", "k": 0, "word": "来[き]"}]
    return {"row": 0, "nids": nids, "before": before, "after": after, "fixes": fixes}


class KanjifyFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.undo = Path(self.tmp.name) / "undo.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_notes_still_as_exported_are_written(self):
        anki = FakeAnki({1: BEFORE, 2: "本を見た。", 3: AFTER})
        rows = [fix_row([1, 2, 3, 4]), fix_row([])]
        written, refused = kanjify_fix.apply(anki, rows, CONFIG, self.undo)
        self.assertEqual(written, 1)
        self.assertEqual(anki.written, [(1, {"k": AFTER})])
        self.assertEqual(
            refused,
            [
                "nid 2: field changed since the export",
                "nid 3: already fixed",
                "nid 4: no such note",
            ],
        )
        self.assertEqual(
            kanjify_fix.read_jsonl(self.undo),
            [{"nid": 1, "field": "k", "before": BEFORE, "after": AFTER}],
        )

    def test_padding_the_export_stripped_is_kept(self):
        anki = FakeAnki({1: f" {BEFORE}\n", 2: f" {AFTER}"})
        written, refused = kanjify_fix.apply(anki, [fix_row([1, 2])], CONFIG, self.undo)
        self.assertEqual(written, 1)
        self.assertEqual(anki.written, [(1, {"k": f" {AFTER}\n"})])
        self.assertEqual(refused, ["nid 2: already fixed"])
        self.assertEqual(
            kanjify_fix.read_jsonl(self.undo),
            [{"nid": 1, "field": "k", "before": f" {BEFORE}\n", "after": f" {AFTER}\n"}],
        )

    def test_note_type_without_the_field_config_is_refused(self):
        anki = FakeAnki({1: BEFORE}, model="Other")
        written, refused = kanjify_fix.apply(anki, [fix_row([1])], CONFIG, self.undo)
        self.assertEqual(written, 0)
        self.assertIn("kanjified_sentence_field", refused[0])

    def test_revert_puts_back_what_is_unchanged_and_keeps_the_rest(self):
        anki = FakeAnki({1: BEFORE, 2: BEFORE})
        kanjify_fix.apply(anki, [fix_row([1, 2])], CONFIG, self.undo)
        anki.fields[2] = "本を見た。"  # edited in Anki after the fix
        reverted, refused = kanjify_fix.revert(anki, CONFIG, self.undo)
        self.assertEqual((reverted, len(refused)), (1, 1))
        self.assertEqual(anki.fields, {1: BEFORE, 2: "本を見た。"})
        self.assertEqual([e["nid"] for e in kanjify_fix.read_jsonl(self.undo)], [2])

    def test_revert_twice_written_note_goes_back_to_the_first_value(self):
        anki = FakeAnki({1: BEFORE})
        middle = "本を<k> 為[し]て 来[き]ました</k>。"
        kanjify_fix.apply(anki, [fix_row([1], BEFORE, middle)], CONFIG, self.undo)
        kanjify_fix.apply(anki, [fix_row([1], middle, AFTER)], CONFIG, self.undo)
        self.assertEqual(kanjify_fix.revert(anki, CONFIG, self.undo), (2, []))
        self.assertEqual(anki.fields[1], BEFORE)
        self.assertEqual(kanjify_fix.read_jsonl(self.undo), [])

    def test_change_list_names_rows_without_nids(self):
        lines = kanjify_fix.change_list([fix_row([]), fix_row([5, 6])])
        self.assertIn("no nids", lines[0])
        self.assertEqual(lines[4], "[0] nid 5,6")
        self.assertEqual(lines[5], "    fixes  て-helper 来[き]")


if __name__ == "__main__":
    unittest.main()
