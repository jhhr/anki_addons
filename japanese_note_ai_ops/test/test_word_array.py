"""The word array generator's structural guarantees, on the hand-made gold examples.

Accuracy against the gold is measured by word_array/research/evaluate.py; these tests pin what
must always hold whatever the word choices: the array partitions the sentence, sub-words make
up their parent, <b> can wrap any word, and a few behaviors the design depends on.

Skipped unless SudachiPy, a Sudachi dictionary and JMdict are all available. The add-on
downloads the last two on first use; word_array/research/setup_resources.py does the same
from a shell.
"""

import importlib
import re
import unittest

from addon_modules import PACKAGE, load_addon_module, load_ops_module

resources = load_ops_module("resources", subdir="word_array")

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


@unittest.skipUnless(resources.is_ready(), "needs SudachiPy, a Sudachi dictionary and JMdict")
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
        # Sudachi cuts 八紘一宇 into two words, 八紘 + 一宇; they can only be its sub-words
        arr = self.generator.generate(self.examples[5][0])
        word = find_word(arr, "八紘一宇")
        self.assertEqual((word[0], word[3]), ("八紘一宇[はっこういちう]", "はっこういちう"))
        self.assertEqual([s[0] for s in word[5]], ["八紘[はっこう]", "一宇[いちう]"])

    def test_multi_word_expressions_come_from_jmdict(self):
        arr = self.generator.generate(self.examples[11][0])
        expression = find_word(arr, "そう言えば")
        self.assertEqual([s[2] for s in expression[5]], ["そう", "言う"])

    def test_a_jmdict_match_inside_another_nests_in_it(self):
        arr = self.generator.generate(self.examples[5][0])
        outer = find_word(arr, "様に成る")
        self.assertEqual([s[2] for s in outer[5] if len(s) > 1], ["様に", "成る"])
        self.assertEqual([s[2] for s in find_word(arr, "様に")[5]], ["様", "に"])

    def test_of_two_crossing_matches_the_kanji_spelled_one_wins(self):
        # 一つ and つの (角) share the つ; only 一つ is a word here
        arr = self.generator.generate(self.examples[8][0])
        self.assertEqual([s[2] for s in find_word(arr, "一つ")[5]], ["一", "つ"])
        self.assertEqual(find_word(arr, "つの"), [])

    def test_kana_homophones_across_a_particle_are_refused(self):
        # は + 幾つ is also JMdict's はいくつ (背屈), and を + 持って its をもって (を以って)
        for num, form in [(21, "はいくつ"), (17, "をもって")]:
            with self.subTest(form=form):
                arr = self.generator.generate(self.examples[num][0])
                self.assertEqual(find_word(arr, form), [])

    def test_homographs_the_furigana_reads_otherwise_are_refused(self):
        # JMdict's 彼の is あの and its 今日は こんにちは
        arr = self.generator.generate("彼[かれ]の 車[くるま]は 今日[きょう]は 新[あたら]しい。")
        self.assertEqual(find_word(arr, "彼の"), [])
        self.assertEqual(find_word(arr, "今日は"), [])
        # JMdict reads 慈悲深い じひぶかい; furigana split between words has no group to voice
        arr = self.generator.generate("彼女[かのじょ]は 慈悲[じひ] 深[ふか]い 人[ひと]だ。")
        self.assertEqual(find_word(arr, "慈悲深い")[3], "じひふかい")

    def test_an_expression_starting_on_the_copula_keeps_its_whole_reading(self):
        arr = self.generator.generate(
            "彼[かれ]は 作家[さっか]で<k> 有[あ]り</k> 学者[がくしゃ]です。"
        )
        self.assertEqual(find_word(arr, "で有る")[3], "である")

    def test_a_kanjified_expression_matches_through_its_kana(self):
        # JMdict writes だけの事はある; the note kanjified ある to 有る
        arr = self.generator.generate("だけの 事[こと]は<k> 有[あ]って</k>")
        self.assertEqual(find_word(arr, "だけの事は有る")[3], "だけのことはある")

    def test_long_units_are_parents_of_their_short_units(self):
        arr = self.generator.generate(self.examples[3][0])
        self.assertEqual([s[2] for s in find_word(arr, "飛行機")[5]], ["飛行", "機"])
        arr = self.generator.generate(self.examples[14][0])
        self.assertEqual([s[2] for s in find_word(arr, "私達")[5] if len(s) > 1], ["私", "達"])

    def test_words_decompose_into_jmdict_words_read_as_the_furigana_says(self):
        arr = self.generator.generate(self.examples[23][0])
        self.assertEqual([s[0] for s in find_word(arr, "耳元")[5]], [" 耳[みみ]", "元[もと]"])
        # 最 and 近 are JMdict words too, but lone on'yomi kanji don't make sub-words
        arr = self.generator.generate(self.examples[10][0])
        self.assertEqual(find_word(arr, "最近")[5], [])

    def test_sub_word_readings_drop_the_compounds_rendaku(self):
        arr = self.generator.generate(self.examples[20][0])
        self.assertEqual([s[3] for s in find_word(arr, "大空")[5]], ["おお", "そら"])

    def test_a_jukujikun_group_splits_between_words_by_their_own_readings(self):
        # かわせそうば has no per-kanji split, but 為替 and 相場 read it between them, so each
        # sub-word keeps furigana of its own: never " 為替" + "相場[かわせそうば]"
        arr = self.generator.generate("<div> 為替相場[かわせそうば]が 気[き]になる</div>")
        word = find_word(arr, "為替相場")
        self.assertEqual([s[0] for s in word[5]], [" 為替[かわせ]", "相場[そうば]"])
        self.assertEqual([s[3] for s in word[5]], ["かわせ", "そうば"])

    def test_a_character_sudachi_normalizes_into_several_still_partitions(self):
        # … normalizes to ... and comes back as the one morpheme covering it plus empty ones,
        # the last of which starts past the end of the text
        for sentence in ["<div> 嫌[いや]…だ…</div>", "<div> 嫌[いや]だ…</div>"]:
            with self.subTest(sentence=sentence):
                arr = self.generator.generate(sentence)
                self.assertEqual(self.gold.concat_raw(arr), sentence)

    def test_adjective_forms_take_the_adjectives_dictionary_form(self):
        # JMdict lists 大きく as an adverb too; only adverbs of their own keep the く-form
        cases = [
            ("口[くち]を 大[おお]きく 開[あ]けて", " 大[おお]きく", "大きい", "おおきい"),
            ("彼女[かのじょ]は<k> 良[よ]く</k> 喋[しゃべ]る", " 良[よ]く", "良い", "よい"),
            ("大[おお]きな 音[おと]", "大[おお]きな", "大きい", "おおきい"),
            ("毎年[まいとし] 多[おお]くの 人[ひと]が 来[く]る", " 多[おお]く", "多い", "おおい"),
            ("もっと 近[ちか]くに 来[き]て", " 近[ちか]く", "近い", "ちかい"),
        ]
        for sentence, raw, form, reading in cases:
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), form)
                self.assertEqual(word, [raw, "adjective", form, reading, [], []])
        for sentence, form in [
            ("危[あや]うく 死[し]ぬ", "危うく"),
            ("全[まった]く 違[ちが]う", "全く"),
        ]:
            with self.subTest(sentence=sentence):
                self.assertEqual(find_word(self.generator.generate(sentence), form)[1], "adverb")

    def test_a_word_partly_in_k_is_spelled_as_jmdict_spells_it(self):
        # The 為 and the second 当 were kana before kanjify_sentence; JMdict has 私達 as it is
        cases = [
            ("彼[かれ]に 対[たい]<k> 為[し]て</k> 怒[おこ]る", "に対して", "にたいして"),
            ("日当[ひあ]<k> 当[た]り</k>が 良[い]い", "日当たり", "ひあたり"),
            ("私[わたし]<k> 達[たち]</k>が 行[い]く", "私達", "わたしたち"),
        ]
        for sentence, form, reading in cases:
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), form)
                self.assertIsNotNone(word)
                self.assertEqual(word[3], reading)

    def test_an_inflected_compound_keeps_its_rendaku(self):
        for sentence in ("義務[ぎむ]<k> 付[づ]けられる</k>", "義務[ぎむ] 付[づ]けられた"):
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), "義務付ける")
                self.assertEqual(word[3], "ぎむづける")

    def test_only_a_number_makes_the_noun_after_it_a_counter(self):
        self.assertEqual(
            find_word(self.generator.generate("3 年[ねん] 経[た]つ"), "年")[1], "counter"
        )
        # 一日中 starts on a number but is not one
        arr = self.generator.generate("一日中 家[うち]に 居[い]た")
        self.assertNotEqual(find_word(arr, "家")[1], "counter")

    def test_a_suffix_jmdict_has_only_as_a_word_takes_its_label(self):
        # Sudachi has 家 after 一日中 as a suffix, but JMdict's 家[うち] is no suffix
        arr = self.generator.generate("一日中 家[うち]に 居[い]た")
        self.assertEqual(find_word(arr, "家")[1], "noun")
        arr = self.generator.generate(" 少年[しょうねん]<k> 達[たち]</k>が 居[い]る")
        self.assertEqual(find_word(arr, "達")[1], "suffix")
        # inside a compound it stays a suffix, though JMdict's 官[かん] is a noun
        arr = self.generator.generate(" 警察官[けいさつかん]が 来[き]た")
        self.assertEqual(find_word(arr, "官")[1], "suffix")

    def test_a_suffix_jmdict_has_as_one_keeps_its_form(self):
        # 振り read ぶり is a JMdict suffix of its own, not a form of 振る
        word = find_word(self.generator.generate("十 年[ねん]<k> 振[ぶ]り</k>に 会[あ]う"), "振り")
        self.assertEqual((word[1], word[3]), ("suffix", "ぶり"))

    def test_a_reading_put_on_the_last_kanji_is_not_all_its_own(self):
        # The note reads 空域 on 域 alone, and ネット上 on its whole group
        cases = [
            ("<b> 領[りょう] 空</b>域[くういき]です。", "域", "いき"),
            ("ネット上[ねっとじょう]では", "上", "じょう"),
        ]
        for sentence, form, reading in cases:
            with self.subTest(sentence=sentence):
                self.assertEqual(find_word(self.generator.generate(sentence), form)[3], reading)

    def test_sudachis_readings_of_unread_sub_words_must_add_up(self):
        # Sudachi splits 無人島 as 無人[むじん] + 島[むじんとう]: 島 takes JMdict's とう instead
        word = find_word(self.generator.generate("無人島に 行[い]く"), "無人島")
        self.assertEqual([(s[2], s[3]) for s in word[5]], [("無人", "むじん"), ("島", "とう")])
        # 一 + 日中[にっちゅう] can't read いちにちじゅう, so 一日中 keeps no sub-words
        word = find_word(self.generator.generate("一日中 家[うち]に 居[い]た"), "一日中")
        self.assertEqual((word[3], word[5]), ("いちにちじゅう", []))

    def test_a_group_whose_reading_cannot_be_shared_out_is_not_split(self):
        # Sudachi has 土産物 as 土産 + 物, but nothing reads へんなよみ that way
        arr = self.generator.generate("<div> 土産物[へんなよみ]を 買[か]う</div>")
        word = find_word(arr, "土産物")
        self.assertEqual((word[0], word[5]), (" 土産物[へんなよみ]", []))


if __name__ == "__main__":
    unittest.main()
