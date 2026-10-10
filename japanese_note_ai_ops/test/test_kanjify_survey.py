"""The kanjify survey's pure helpers: kanji choices of a kana word, meaning tags, policy counts."""

import sys
import unittest
from collections import Counter
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # What the real jmdict_index.spellings returns, which `spellings` below stands in for
    from japanese_note_ai_ops.word_array.jmdict_index import Spellings

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

YORU = [
    (("夜",), ("よる",), frozenset({"n"})),
    (("寄る", "依る"), ("よる",), frozenset({"v5r"})),
    (("因る", "由る", "依る", "拠る"), ("よる",), frozenset({"v5r", "vi"})),
    ((), ("よる",), frozenset({"prt"})),
]
SPELLINGS: dict[tuple[tuple[str, ...], tuple[str, ...]], "Spellings"] = {
    (("因る", "由る", "依る", "拠る"), ("よる",)): ({"拠る": frozenset({"oK"})}, {}, True)
}


def lookup(kana):
    return YORU if kana == "よる" else []


def spellings(kebs, rebs):
    return SPELLINGS.get((kebs, rebs), ({}, {}, False))


class SurveyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import kanjify_survey

        cls.survey = kanjify_survey

    def test_kanji_choices_filter_by_pos_and_drop_outdated_kanji(self):
        choices = self.survey.kanji_choices
        self.assertEqual(
            choices("よる", "動詞", lookup, spellings),
            [["寄る", "依る"], ["因る", "由る", "依る"]],
        )
        self.assertEqual(choices("よる", "名詞", lookup, spellings), [["夜"]])
        self.assertEqual(choices("よる", "助詞", lookup, spellings), [])

    def test_one_set_of_kanji_is_one_choice(self):
        entry = [(("積り", "積もり"), ("つもり",), frozenset({"n"}))]
        got = self.survey.kanji_choices("つもり", "名詞", lambda k: entry, spellings)
        self.assertEqual(got, [["積り"]])

    def test_meaning_tag(self):
        tag = self.survey.meaning_tag
        self.assertEqual(tag([["夜"], ["寄る"]]), "HOMOPHONES")
        self.assertEqual(tag([["因る", "依る"]]), "CHOICES")
        self.assertEqual(tag([["積り"]]), "")
        self.assertEqual(tag([]), "")

    def test_policy_breakdown_counts_kinds_per_word(self):
        def tok(policy, norm, kind):
            return SimpleNamespace(policy=policy, kind=kind, morph=SimpleNamespace(norm=norm))

        toks = [
            tok("て-helper", "見る", "kanjified"),
            tok("て-helper", "見る", "kana"),
            tok("て-helper", "見る", "kana"),
            tok(None, "見る", "kanjified"),
        ]
        self.assertEqual(
            self.survey.policy_breakdown(toks),
            {("て-helper", "見る"): Counter(kanjified=1, kana=2)},
        )


if __name__ == "__main__":
    unittest.main()
