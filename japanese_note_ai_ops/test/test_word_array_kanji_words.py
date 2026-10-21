"""The kanji words of a spoken line: names, numbers and cut runs taken whole, the captions' own
readings as a word's caption.

Nothing here tokenizes with Sudachi: a stand-in segments the few lines the tests use the way
Sudachi does, so the suite runs without SudachiPy or the downloaded dictionaries."""

import unittest

from addon_modules import load_ops_module

captions = load_ops_module("captions", subdir="word_array")
kanji_words = load_ops_module("kanji_words", subdir="word_array")

NOUN = ("名詞", "普通名詞", "一般", "*", "*", "*")
PROPER = ("名詞", "固有名詞", "人名", "一般", "*", "*")
PARTICLE = ("助詞", "格助詞", "*", "*", "*", "*")
NUMERAL = ("名詞", "数詞", "*", "*", "*", "*")
COUNTER = ("接尾辞", "名詞的", "助数詞", "*", "*", "*")
COUNTER_NOUN = ("名詞", "普通名詞", "助数詞可能", "*", "*", "*")
ADVERB = ("副詞", "*", "*", "*", "*", "*")


class Morpheme:
    def __init__(self, surface, reading, pos, begin):
        self._surface, self._reading, self._pos, self._begin = surface, reading, pos, begin

    def surface(self):
        return self._surface

    def reading_form(self):
        return self._reading

    def part_of_speech(self):
        return self._pos

    def begin(self):
        return self._begin

    def end(self):
        return self._begin + len(self._surface)


# Sudachi's take on the lines below: names cut up and read as common words, digits read one by
# one, a compound cut into words it knows
LEXICON = {
    "猫": ("ネコ", NOUN),
    "馬": ("ウマ", NOUN),
    "高順": ("タカノブ", PROPER),
    "10": ("イチレイ", NUMERAL),
    "175": ("イチナナゴ", NUMERAL),
    "百七十五": ("ヒャクナナジュウゴ", NUMERAL),
    "三つ": ("ミッツ", NUMERAL),
    "km": ("キロメートル", COUNTER_NOUN),
    "七": ("ナナ", NUMERAL),
    "三": ("サン", NUMERAL),
    "3": ("サン", NUMERAL),
    "つ": ("ツ", COUNTER),
    "日": ("ニチ", COUNTER_NOUN),
    "十日": ("トオカ", NOUN),
    "響": ("ヒビキ", NOUN),
    "神": ("カミ", NOUN),
    "美": ("ビ", NOUN),
    "物置": ("モノオキ", NOUN),
    "小屋": ("コヤ", NOUN),
}


def tokenize(text):
    """Longest match over LEXICON; any other character is a morpheme of its own, read as
    itself (Sudachi reads a kanji it does not know so)."""
    out, i = [], 0
    while i < len(text):
        for n in range(len(text) - i, 0, -1):
            piece = text[i : i + n]
            if piece in LEXICON:
                reading, pos = LEXICON[piece]
                out.append(Morpheme(piece, reading, pos, i))
                i += n
                break
        else:
            out.append(Morpheme(text[i], text[i], PARTICLE, i))
            i += 1
    return out


class KanjiWordsTest(unittest.TestCase):
    def words(self, line, names=(), read=None):
        text, readings = captions.split_readings(line)
        return kanji_words.kanji_words(0, text, tokenize(text), readings, names, read)

    def test_a_number_and_its_counter_are_one_word_its_digits_too(self):
        def read(text):
            return kanji_words.numeral_reading(text, tokenize)

        words = self.words("10日に七日", read=read)
        self.assertEqual(
            [(w.surface, w.start, w.end, w.sudachi, w.kinds) for w in words],
            [("10日", 0, 3, "とおか", ["number"]), ("七日", 4, 6, "ななにち", ["number"])],
        )

    def test_a_run_sudachi_cut_into_single_kanji_or_could_not_read_is_one_word(self):
        # 迂 is not in the stand-in's lexicon: its reading stays the kanji, as Sudachi's does
        words = self.words("響迂は神美に物置小屋")
        self.assertEqual(
            [(w.surface, w.sudachi, w.kinds) for w in words],
            [
                ("響迂", "ひびき迂", ["split"]),
                ("神美", "かみび", ["split"]),
                ("物置", "ものおき", []),
                ("小屋", "こや", []),
            ],
        )

    def test_a_number_sudachi_cut_into_a_numeral_and_a_word_is_read_by_itself(self):
        # In a line Sudachi cuts 三十分 into 三|十分 and reads 十分 as "enough"
        LEXICON["十分"] = ("ジュウブン", ADVERB)

        def read(text):
            self.assertEqual(text, "三十分")
            return "さんじゅっぷん"

        try:
            words = self.words("三十分で", read=read)
        finally:
            del LEXICON["十分"]
        self.assertEqual(
            [(w.surface, w.sudachi, w.kinds) for w in words],
            [("三十分", "さんじゅっぷん", ["number"])],
        )

    def test_digits_alone_or_with_a_kana_counter_are_a_number_too(self):
        def read(text):
            return kanji_words.numeral_reading(text, tokenize)

        words = self.words("175と3つ", read=read)
        self.assertEqual(
            [(w.surface, w.sudachi, w.kinds) for w in words],
            [("175", "ひゃくななじゅうご", ["number"]), ("3つ", "みっつ", ["number"])],
        )
        # A number in kana needs no furigana
        self.assertEqual(self.words("みっつ"), [])

    def test_a_counter_in_latin_letters_is_no_part_of_the_number(self):
        def read(text):
            return kanji_words.numeral_reading(text, tokenize)

        words = self.words("175km", read=read)
        self.assertEqual([(w.surface, w.sudachi) for w in words], [("175", "ひゃくななじゅうご")])

    def test_a_name_the_tokenizer_cut_is_one_word(self):
        words = self.words("あら 猫猫", names={"猫猫"})
        self.assertEqual(
            [(w.surface, w.sudachi, w.kinds) for w in words], [("猫猫", "ねこねこ", ["name"])]
        )

    def test_an_empty_name_is_no_name(self):
        # Found at every place, it hung the search for the next one
        words = self.words("あら 猫猫", names={"", "猫猫"})
        self.assertEqual([(w.surface, w.kinds) for w in words], [("猫猫", ["name"])])

    def test_an_inline_reading_is_the_words_caption(self):
        words = self.words("猫猫(マオマオ)")
        self.assertEqual(
            [(w.surface, w.caption, w.kinds) for w in words], [("猫猫", "まおまお", ["name"])]
        )

    def test_readings_of_kanji_the_tokenizer_cut_across_are_one_words(self):
        # A title and a name, each read by the captions, which the tokenizer cut 春|妃夏|樹:
        # two words would share 妃夏
        LEXICON.update({"春": ("ハル", PROPER), "妃夏": ("ヒナツ", PROPER), "樹": ("キ", NOUN)})
        try:
            words = self.words("春妃(しゅんひ)夏樹(ナツキ)は")
        finally:
            for piece in ("春", "妃夏", "樹"):
                del LEXICON[piece]
        self.assertEqual(
            [(w.surface, w.start, w.end, w.caption, w.kinds) for w in words],
            [("春妃夏樹", 0, 4, "しゅんひなつき", ["name"])],
        )

    def test_a_reading_of_a_words_kanji_takes_in_its_okurigana(self):
        LEXICON["噛みつい"] = ("カミツイ", NOUN)
        try:
            words = self.words("噛(か)みついた")
        finally:
            del LEXICON["噛みつい"]
        self.assertEqual([(w.surface, w.caption) for w in words], [("噛みつい", "かみつい")])

    def test_a_one_kanji_name_only_where_it_is_a_proper_noun(self):
        # 馬 is the horse here: the stand-in tags it a common noun
        self.assertEqual([w.kinds for w in self.words("馬", names={"馬"})], [[]])

    def test_a_proper_noun_of_sudachis_is_a_name(self):
        self.assertEqual([(w.surface, w.kinds) for w in self.words("高順")], [("高順", ["name"])])


if __name__ == "__main__":
    unittest.main()
