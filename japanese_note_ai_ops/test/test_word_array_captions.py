"""A subtitle caption as spoken: what is taken out, the readings the captions give and where
each belongs, and numbers spelt in kanji numerals."""

import unittest

from addon_modules import load_ops_module

captions = load_ops_module("captions", subdir="word_array")


class CleaningTest(unittest.TestCase):
    def test_labels_sound_descriptions_and_ass_tags_are_not_spoken(self):
        jp = (
            "（羅門(ルォメン)）猫猫(マオマオ)<br>（猫猫）ん？<br>（赤ん坊のはしゃぎ声）"
            "<br>{\\an8}（客）おお～っ"
        )
        self.assertEqual(captions.spoken_lines(jp), ["猫猫(マオマオ)", "ん？", "おお～っ"])

    def test_a_label_in_the_middle_of_a_line(self):
        self.assertEqual(
            captions.spoken_lines("（白鈴）あら 猫猫 （猫猫）ん？"), ["あら 猫猫 ん？"]
        )

    def test_noisy_cards_have_ass_tags(self):
        self.assertTrue(captions.is_noisy("これじゃ {\\an8}（客）おお～っ これじゃ"))
        self.assertFalse(captions.is_noisy("（猫猫）ん？"))

    def test_readings_are_taken_out_and_kept_by_place(self):
        text, readings = captions.split_readings("あら 猫猫(マオマオ) 緑青館(ろくしょうかん)に")
        self.assertEqual(text, "あら 猫猫 緑青館に")
        self.assertEqual(
            readings,
            [
                captions.InlineReading("猫猫", "まおまお", True, 3, 5),
                captions.InlineReading("緑青館", "ろくしょうかん", False, 6, 9),
            ],
        )

    def test_a_reading_after_other_kanji_takes_the_tail_base_gives(self):
        text, readings = captions.split_readings("白鈴姐(ねえ)ちゃん", base=lambda run, reading: 1)
        self.assertEqual(text, "白鈴姐ちゃん")
        self.assertEqual(readings, [captions.InlineReading("姐", "ねえ", False, 2, 3)])

    def test_labels_readings_count_for_the_show(self):
        readings = captions.all_inline_readings("（羅門(ルォメン)）ん？")
        self.assertEqual([(r.surface, r.reading) for r in readings], [("羅門", "るぉめん")])


class ReadingBaseTest(unittest.TestCase):
    def base(self, texts=(), dictionary=None):
        table = dictionary or {}
        return captions.ReadingBase(texts, lambda form: table.get(form, set()))

    def test_a_tail_the_dictionary_reads_so(self):
        base = self.base(dictionary={"妓楼": {"ぎろう"}})
        self.assertEqual(base("高級妓楼", "ぎろう"), 2)

    def test_a_tail_the_captions_read_so_on_its_own(self):
        base = self.base(texts=["公主(ひめ)"])
        self.assertEqual(base("鈴麗公主", "ひめ"), 2)

    def test_what_follows_a_head_the_captions_read_on_its_own(self):
        base = self.base(texts=["白鈴(パイリン)"])
        self.assertEqual(base("白鈴姐", "ねえ"), 1)

    def test_the_whole_run_when_the_reading_can_cover_it(self):
        self.assertEqual(self.base()("緑青館", "ろくしょうかん"), 3)

    def test_no_more_kanji_than_the_reading_has_kana(self):
        self.assertEqual(self.base()("白鈴姐", "ね"), 1)


class ShowReadingsTest(unittest.TestCase):
    def test_names_by_the_reading_given_most_and_words_read_in_hiragana(self):
        texts = ["猫猫(ネコネコ)が", "（猫猫(マオマオ)）妃(きさき)", "猫猫(マオマオ)", "高順(ガオシュン)"]
        names, words = captions.show_readings(texts)
        self.assertEqual(names, {"猫猫": "まおまお", "高順": "がおしゅん"})
        self.assertEqual(words, {"妃"})

    def test_of_two_readings_given_as_often_the_first(self):
        names, _ = captions.show_readings(["猫猫(ネコネコ)", "猫猫(マオマオ)"])
        self.assertEqual(names, {"猫猫": "ねこねこ"})

    def test_a_reading_goes_to_the_tail_base_gives(self):
        names, words = captions.show_readings(["白鈴姐(ねえ)ちゃん"], base=lambda run, r: 1)
        self.assertEqual((names, words), ({}, {"姐"}))


class NumeralsTest(unittest.TestCase):
    def test_a_number_in_kanji_numerals(self):
        cases = {0: "〇", 10: "十", 11: "十一", 175: "百七十五", 1000: "千", 2010: "二千十"}
        cases.update({10000: "一万", 12345: "一万二千三百四十五", 100000000: "一億"})
        self.assertEqual({n: captions.kanji_number(n) for n in cases}, cases)

    def test_digits_of_either_width_in_a_text(self):
        self.assertEqual(captions.kanji_numerals("船で10日、１万人"), "船で十日、一万人")


if __name__ == "__main__":
    unittest.main()
