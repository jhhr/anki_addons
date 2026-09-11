"""The word array generator's structural guarantees, on the hand-made gold examples.

Accuracy against the gold is measured by word_array/research/evaluate.py; these tests pin what
must always hold whatever the word choices: the array partitions the sentence, sub-words make
up their parent, <b> can wrap any word, and a few behaviors the design depends on.

Skipped unless SudachiPy (with a dictionary) and JMdict (user_files/jmdict, see
word_array/research/setup_jmdict.py) are available: neither is vendored yet.
"""

import importlib
import importlib.util
import re
import unittest

from addon_modules import ADDON_ROOT, PACKAGE, load_addon_module, load_ops_module

HAS_SUDACHI = importlib.util.find_spec("sudachipy") is not None
JMDICT_DIR = ADDON_ROOT / "user_files" / "jmdict"
HAS_JMDICT = (JMDICT_DIR / "jmdict_index.pkl").exists() or (JMDICT_DIR / "JMdict_e.xml").exists()

TAG_RE = re.compile(r"<(/?)([a-zA-Z]+)[^>]*>")


def balanced(html: str) -> bool:
    stack = []
    for m in TAG_RE.finditer(html):
        if not m.group(1):
            stack.append(m.group(2))
        elif not stack or stack.pop() != m.group(2):
            return False
    return not stack


def find_word(arr: list, dict_form: str) -> list:
    for e in arr:
        if len(e) > 1:
            if e[2] == dict_form:
                return e
            if e[5]:
                found = find_word(e[5], dict_form)
                if found:
                    return found
    return []


@unittest.skipUnless(HAS_SUDACHI and HAS_JMDICT, "needs SudachiPy and JMdict")
class WordArrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = load_ops_module("generator", subdir="word_array")
        cls.gold = load_addon_module("gold", subdir="word_array/research")
        cls.examples = cls.gold.load()
        cls.tag_cleaning = importlib.import_module(
            f"{PACKAGE}.shared.jp_text_processing.word.use_tag_cleaning"
        )

    def test_every_gold_sentence_is_partitioned(self):
        for num, (sentence, _) in self.examples.items():
            with self.subTest(example=num):
                self.assertEqual(
                    self.gold.validate(sentence, self.generator.generate(sentence)), []
                )

    def test_b_can_wrap_every_word(self):
        for num, (sentence, _) in self.examples.items():
            arr = self.generator.generate(sentence)
            for i, e in enumerate(arr):
                if len(e) == 1:
                    continue
                html = (
                    self.gold.concat_raw(arr[:i])
                    + "<b>"
                    + e[0]
                    + "</b>"
                    + self.gold.concat_raw(arr[i + 1 :])
                )
                with self.subTest(example=num, word=e[0]):
                    self.assertTrue(balanced(self.tag_cleaning.apply_tag_fixes(html)), html)

    def test_sub_words_get_their_own_share_of_the_furigana(self):
        arr = self.generator.generate(self.examples[2][0])
        compound = find_word(arr, "見下ろす")
        self.assertEqual([s[0] for s in compound[5]], [" 見[み]", "下[お]ろせた"])

    def test_k_words_are_read_as_their_original_kana(self):
        # <k> 遣[や]っ read as kanji would be 遣う (つかう); as the original やっ it is やる
        arr = self.generator.generate(self.examples[13][0])
        self.assertEqual(find_word(arr, "遣る")[3], "やる")
        # 為[さ]れ is される, not なる
        arr = self.generator.generate(self.examples[12][0])
        self.assertEqual(find_word(arr, "為れる")[3], "される")

    def test_readings_come_from_the_notes_furigana(self):
        # Sudachi reads a bare 私 as わたくし; the note says わたし
        arr = self.generator.generate(self.examples[1][0])
        self.assertEqual(find_word(arr, "私")[3], "わたし")

    def test_a_furigana_group_is_never_split_between_words(self):
        # Sudachi splits 八紘一宇 into 八紘 + 一宇
        arr = self.generator.generate(self.examples[5][0])
        word = find_word(arr, "八紘一宇")
        self.assertEqual(
            (word[0], word[3], word[5]), ("八紘一宇[はっこういちう]", "はっこういちう", [])
        )

    def test_multi_word_expressions_come_from_jmdict(self):
        arr = self.generator.generate(self.examples[11][0])
        expression = find_word(arr, "そう言えば")
        self.assertEqual([s[2] for s in expression[5]], ["そう", "言う"])


if __name__ == "__main__":
    unittest.main()
