"""Which notes have their word only inside a word the generator leaves whole: what the config's
list of compounds to split is decided from."""

import sys
import unittest

from addon_modules import ADDON_ROOT, load_ops_module

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

resources = load_ops_module("resources", subdir="word_array")


@unittest.skipUnless(resources.is_ready(), "needs SudachiPy, a Sudachi dictionary and JMdict")
class WordsInsideWordsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import words_inside_words

        cls.script = words_inside_words

    def inside(self, sentence, word):
        return [tuple(m) for m in self.script.words_inside(sentence, word)]

    def test_a_notes_word_inside_a_compound_is_found_on_either_side(self):
        verb = self.script.INFLECTING
        self.assertEqual(
            self.inside("表面[ひょうめん]の 毛羽[けば]立[た]ちと", "毛羽"),
            [(verb, "毛羽立つ", "毛羽", "立つ", "first", "splits")],
        )
        self.assertEqual(
            self.inside("仲[なか] 睦[むつ]まじい 夫婦[ふうふ]", "睦まじい"),
            [(verb, "仲睦まじい", "仲", "睦まじい", "second", "splits")],
        )

    def test_a_compound_the_config_lists_already_is_reported_all_the_same(self):
        # The report is the whole basis of the list, so the analysis splits no compound for it,
        # and only marks the ones a regeneration splits by now
        (found,) = self.script.words_inside("表面[ひょうめん]の 毛羽[けば]立[た]ちと", "毛羽")
        self.assertEqual(found.verdict, "splits")
        mark = "on split_compound_words"
        self.assertIn(mark, self.script.describe(found, frozenset({"毛羽立つ"})))
        self.assertNotIn(mark, self.script.describe(found, frozenset({"心地良い"})))
        self.assertNotIn(mark, self.script.describe(found, frozenset()))

    def test_a_compound_goes_by_the_form_an_array_shows(self):
        # ...which is the form the config lists it by: one word however a note spells it
        found = self.inside("もの凄[すご]い 音[おと]", "凄い")
        self.assertEqual([m[1:4] for m in found], [("物凄い", "もの", "凄い")])

    def test_a_word_the_sentence_has_as_a_word_is_not_inside_one(self):
        sentence = "表面[ひょうめん]の 毛羽[けば]立[た]ちと 目立[めだ]った"
        for word in ("表面", "毛羽立つ", "目立つ"):
            with self.subTest(word=word):
                self.assertEqual(self.inside(sentence, word), [])

    def test_the_verdict_says_whether_the_generator_finds_two_words(self):
        # 目 + 立つ are two words by the checks: whether 目立つ should split is what the set
        # decides, not the report
        self.assertEqual([m[5] for m in self.inside("目立[めだ]った", "目")], ["splits"])
        # 損 is a lone on'yomi kanji, and so is 涙 beside the 腺 a note is about
        for sentence, word, side in [
            ("損[そん]<k> 為[し]ました</k>", "損", "first"),
            ("涙腺[るいせん]が 緩[ゆる]む", "腺", "second"),
        ]:
            with self.subTest(sentence=sentence):
                (found,) = self.inside(sentence, word)
                self.assertEqual(found[4], side)
                self.assertTrue(found[5].startswith("the first is no word"), found[5])

    def test_a_kanjified_group_is_one_place_in_the_written_text(self):
        # <k> 狡[ずる]</k> goes to the tokenizer as ずる, two characters for one kanji
        found = self.inside("<k> 狡[ずる]</k> 賢[かしこ]いし", "賢い")
        self.assertEqual([m[1:] for m in found], [("狡賢い", "狡", "賢い", "second", "splits")])


if __name__ == "__main__":
    unittest.main()
