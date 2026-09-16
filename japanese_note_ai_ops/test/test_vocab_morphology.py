"""Naming how a note's reading and a linked word's reading differ."""

import sys
import unittest

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import vocab_morphology as m  # noqa: E402


class TestVerbForms(unittest.TestCase):
    def test_a_godan_renyoukei_moves_to_the_i_row(self):
        self.assertIn("もち", m.renyoukei("もつ"))
        self.assertIn("ゆき", m.renyoukei("ゆく"))

    def test_an_ichidan_renyoukei_drops_the_ru(self):
        self.assertIn("はれ", m.renyoukei("はれる"))

    def test_the_te_form_of_a_godan_verb_takes_its_onbin(self):
        self.assertIn("あいて", m.inflections("あく"))
        self.assertIn("あいた", m.inflections("あく"))
        self.assertIn("つつしんで", m.inflections("つつしむ"))

    def test_the_potential_and_passive_are_inflections(self):
        self.assertIn("つかえる", m.inflections("つかう"))
        self.assertIn("いけない", m.inflections("いく"))
        self.assertIn("おもわれる", m.inflections("おもう"))

    def test_a_one_kana_verb_has_no_forms(self):
        self.assertEqual(m.renyoukei("る"), set())
        self.assertEqual(m.inflections("る"), set())


class TestReadingHelpers(unittest.TestCase):
    def test_devoicing_drops_the_marks(self):
        self.assertEqual(m.devoice("がいしゃ"), "かいしゃ")
        self.assertEqual(m.devoice("ぶんぽう"), "ふんほう")

    def test_the_distance_counts_kana(self):
        self.assertEqual(m.kana_distance("こみゅぬけーしょん", "こみゅにけーしょん"), 1)
        self.assertEqual(m.kana_distance("ひとだんらく", "いちだんらく"), 2)


class TestFamiliesOfDifferentWords(unittest.TestCase):
    def test_a_deverbal_noun_against_its_verb(self):
        self.assertEqual(m.family("行き", "ゆき", "行く", "ゆく"), m.DEVERBAL)
        self.assertEqual(m.family("晴れ", "はれ", "晴れる", "はれる"), m.DEVERBAL)

    def test_a_verb_against_its_deverbal_noun(self):
        self.assertEqual(m.family("持つ", "もつ", "持ち", "もち"), m.VERB_OF_DEVERBAL)

    def test_an_inflected_form_against_its_plain_one(self):
        self.assertEqual(m.family("空いた", "あいた", "空く", "あく"), m.INFLECTED)
        self.assertEqual(m.family("行けない", "いけない", "行く", "いく"), m.INFLECTED)

    def test_a_phrase_built_around_the_word(self):
        self.assertEqual(m.family("陵", "みささぎ", "嵯峨山上陵", "さがのみささぎ"), m.PHRASE_AROUND)

    def test_the_word_inside_the_notes_phrase(self):
        self.assertEqual(m.family("読み書き", "よみかき", "読み", "よみ"), m.WORD_INSIDE)


class TestFamiliesAwaitingADecision(unittest.TestCase):
    def test_an_adverbial_ku_form(self):
        self.assertEqual(m.family("多く", "おおく", "多い", "おおい"), m.KU_ADVERB)

    def test_a_suru_compound(self):
        self.assertEqual(m.family("期する", "きする", "期", "き"), m.SURU_COMPOUND)
        self.assertEqual(m.family("愛する", "あいする", "愛", "あい"), m.SURU_COMPOUND)

    def test_a_zuru_jiru_pair(self):
        self.assertEqual(m.family("通ずる", "つうずる", "通じる", "つうじる"), m.ZURU_JIRU)
        self.assertEqual(m.family("奉じる", "ほうじる", "奉ずる", "ほうずる"), m.ZURU_JIRU)

    def test_two_real_readings_of_one_spelling(self):
        self.assertEqual(m.family("一段落", "ひとだんらく", "一段落", "いちだんらく"), m.TWO_READINGS)


class TestFamiliesOfADamagedReading(unittest.TestCase):
    def test_readings_that_differ_only_by_voicing(self):
        self.assertEqual(m.family("会社", "がいしゃ", "会社", "かいしゃ"), m.RENDAKU)

    def test_voicing_is_named_whichever_side_holds_it(self):
        # 狡賢い keeps its rendaku on the note, 砂埃 on the array: the family says they differ
        # by voicing and nothing about which one is right.
        self.assertEqual(m.family("狡賢い", "ずるがしこい", "狡賢い", "ずるかしこい"), m.RENDAKU)
        self.assertEqual(m.family("砂埃", "すなほこり", "砂埃", "すなぼこり"), m.RENDAKU)

    def test_rendaku_is_named_before_the_typo_it_also_looks_like(self):
        # がいしゃ is one kana from かいしゃ as well; the voicing is the better explanation.
        self.assertEqual(m.family("階", "がい", "階", "かい"), m.RENDAKU)

    def test_one_kana_apart_is_a_typo(self):
        self.assertEqual(
            m.family("コミュニケーション", "こみゅぬけーしょん", "コミュニケーション", "こみゅにけーしょん"),
            m.TYPO,
        )

    def test_stray_whitespace(self):
        self.assertEqual(m.family("幾つか", "いくつ か", "幾つか", "いくつか"), m.WHITESPACE)

    def test_full_width_against_half_width(self):
        self.assertEqual(m.family("ＩＴ", "あいてぃー", "IT", "it"), m.WIDTH)


class TestNoFamily(unittest.TestCase):
    def test_a_link_that_already_agrees_has_no_family(self):
        # Stripping spaces from two equal readings trivially matches, which once made this
        # look like stray whitespace and would have driven a repair of nothing.
        self.assertIsNone(m.family("此れ", "これ", "此れ", "これ"))

    def test_two_unrelated_words_have_none(self):
        self.assertIsNone(m.family("犬", "いぬ", "猫", "ねこ"))

    def test_a_note_with_no_spelling_has_none(self):
        self.assertIsNone(m.family("", "いぬ", "猫", "ねこ"))


if __name__ == "__main__":
    unittest.main()
