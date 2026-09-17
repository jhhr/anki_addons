"""The one-note kanjified field tool: base hash, reads-the-same check, undo."""

import sys
import tempfile
import unittest
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import anki_connect  # noqa: E402
import kanjify_fix  # noqa: E402
import kanjify_note  # noqa: E402
from test_kanjify_fix import CONFIG, FakeAnki  # noqa: E402

BEFORE = "お 箸[はし]、一膳しかないの。"
AFTER = "お 箸[はし]、一膳しか<k> 無[な]い</k>の。"


class KanjifyNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.undo = Path(self.tmp.name) / "undo.jsonl"
        self.anki = FakeAnki({1: BEFORE})

    def tearDown(self):
        self.tmp.cleanup()

    def set(self, value, base=None):
        base = base or kanjify_note.field_hash(BEFORE)
        return kanjify_note.set_field(self.anki, CONFIG, 1, base, value, self.undo)

    def test_writes_and_records_the_old_value(self):
        self.assertEqual(kanjify_note.get(self.anki, CONFIG, 1), ("k", BEFORE))
        self.assertIn("wrote note 1", self.set(AFTER))
        self.assertEqual(self.anki.fields[1], AFTER)
        self.assertEqual(
            kanjify_fix.read_jsonl(self.undo),
            [{"nid": 1, "field": "k", "before": BEFORE, "after": AFTER}],
        )
        self.assertEqual(kanjify_fix.revert(self.anki, CONFIG, self.undo), (1, []))
        self.assertEqual(self.anki.fields[1], BEFORE)

    def test_refusals(self):
        for value, base, why in (
            (AFTER, "stale", "changed since"),
            ("お 箸[はし]、一膳しか<k> 無[な]かった</k>の。", None, "doesn't read"),
            ("お 箸[はし]、一膳しか<k> 無[な]いの。", None, "pair up"),
            ("お 箸[はし]、一膳しか 無いの。", None, "doesn't read"),
            (BEFORE, None, "nothing changed"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(anki_connect.AnkiConnectError) as cm:
                    self.set(value, base)
                self.assertIn(why, str(cm.exception))
        self.assertEqual(self.anki.written, [])
        self.assertFalse(self.undo.exists())

    def test_unkanjifying_reads_the_same(self):
        self.assertIsNone(kanjify_note.edit_problem("<k> 此[こ]の</k> 本[ほん]", "この 本[ほん]"))


if __name__ == "__main__":
    unittest.main()
