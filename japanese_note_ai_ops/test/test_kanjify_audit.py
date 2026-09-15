"""The kanjify audit's pure helpers: partial un-kanjify, the reverse check, policy uses."""

import sys
import unittest
from types import SimpleNamespace

from addon_modules import ADDON_ROOT, load_ops_module

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import note_edits  # noqa: E402

resources = load_ops_module("resources", subdir="word_array")


def m(surface, lemma, norm, *pos):
    pos = tuple(pos) + ("*",) * (6 - len(pos))
    return SimpleNamespace(surface=surface, lemma=lemma, norm=norm, pos=pos)


TE = m("て", "て", "て", "助詞", "接続助詞")
HELPER_KURU = m("き", "くる", "来る", "動詞", "非自立可能", "*", "*", "カ行変格", "連用形-一般")


class SpanKanaTests(unittest.TestCase):
    def test_only_the_given_groups_turn_kana_inside_the_tags(self):
        field = "本を<k> 為[し]て 来[き]た</k>。"
        self.assertEqual(note_edits.unkanjify_groups(field, 0, {1}), "本を<k> 為[し]てきた</k>。")

    def test_a_span_left_without_furigana_loses_its_tags(self):
        field = "見て<k> 来[き]た</k>、<k> 此[こ]の</k>"
        self.assertEqual(note_edits.unkanjify_groups(field, 0, {0}), "見てきた、<k> 此[こ]の</k>")

    def test_a_group_the_span_lacks_is_refused(self):
        with self.assertRaises(note_edits.NoteEditError):
            note_edits.unkanjify_groups("<k> 来[き]た</k>", 0, {1})


@unittest.skipUnless(resources.is_ready(), "needs SudachiPy, a Sudachi dictionary and JMdict")
class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import kanjify_audit

        cls.audit = kanjify_audit

    def test_reverse_check(self):
        rev = self.audit.mismatch
        self.assertIsNone(rev("<b>本</b>をしてきた。", "本を<k> 為[し]て 来[き]た</k>。"))
        self.assertIsNone(rev("1 年[ねん]", "1[いち] 年[ねん]"))
        self.assertIsNone(rev("のぼりきった", "<k> 登[のぼ]り切[き]った</k>"))
        self.assertEqual(rev("凄味[すごみ]", " 凄[すご] 味[み]"), "furigana")
        self.assertEqual(rev("本をしてきた。", "本を<k> 為[し]て 来[き]る</k>。"), "text")

    def test_policy_uses(self):
        use = self.audit.policy_use
        self.assertEqual(use([TE, HELPER_KURU], 1), "て-helper")
        self.assertIsNone(use([m("を", "を", "を", "助詞"), HELPER_KURU], 1))
        de = m("で", "だ", "だ", "助動詞")
        aru = m("ある", "ある", "有る", "動詞", "非自立可能")
        self.assertEqual(use([de, aru], 1), "である")
        self.assertIsNone(use([m("で", "で", "で", "助詞", "格助詞"), aru], 1))
        self.assertIsNone(use([m("に", "だ", "だ", "助動詞"), aru], 1))  # 十分にある
        nai = m("ない", "ない", "無い", "形容詞", "非自立可能")
        self.assertEqual(use([m("悪く", "悪い", "悪い", "形容詞"), nai], 1), "negation ない")
        self.assertIsNone(use([m("が", "が", "が", "助詞", "格助詞"), nai], 1))
        ii = m("いい", "いい", "良い", "形容詞", "非自立可能")
        self.assertEqual(use([TE, m("も", "も", "も", "助詞", "係助詞"), ii], 2), "て-pattern")
        naru = m("なら", "なる", "成る", "動詞", "非自立可能")
        self.assertEqual(use([TE, m("は", "は", "は", "助詞", "係助詞"), naru], 2), "て-pattern")
        self.assertIsNone(use([m("に", "に", "に", "助詞", "格助詞"), naru], 1))

    def test_row_fix_keeps_the_verb_and_takes_the_common_cut(self):
        audit = self.audit
        rows = [
            "<k> 此[こ]の</k> 本[ほん]",
            "<k> 此[こ]の</k> 犬[いぬ]",
            "<k> 此[こ]の</k> 鳥[とり]",
        ]
        rows.append("<b>猫</b>を<k> 此[この]</k> 所[ところ]")
        rows.append("<k> 此[この]</k> 花[はな]を 見[み]て<k> 来[き]た</k>")
        toks = [audit.analyze_row(i, label) for i, label in enumerate(rows)]
        targets = audit.format_targets([t for ts in toks for t in ts])
        after, fixes = audit.row_fix(rows[4], toks[4], targets)
        self.assertEqual(after, "<k> 此[こ]の</k> 花[はな]を 見[み]てきた")
        self.assertEqual({f["class"] for f in fixes}, {"format", "て-helper"})
        after, _ = audit.row_fix(rows[3], toks[3], targets)
        self.assertEqual(after, "<b>猫</b>を<k> 此[こ]の</k> 所[ところ]")


if __name__ == "__main__":
    unittest.main()
