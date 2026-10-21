"""A sentence's furigana from its audio (word_array/audio_furigana.py): which reading each kanji
word is written with, which go to review, and the furigana line written, a word in review with
the readings to choose between.

Nothing here tokenizes with Sudachi: a stand-in segments the few lines the tests use the way
Sudachi does, so the suite runs without SudachiPy or the downloaded dictionaries. The
transcripts are written as the kana models write them, particles as pronounced (ひとわ)."""

import unittest

from addon_modules import load_ops_module

af = load_ops_module("audio_furigana", subdir="word_array")

NOUN = ("名詞", "普通名詞", "一般", "*", "*", "*")
PROPER = ("名詞", "固有名詞", "人名", "一般", "*", "*")
PARTICLE = ("助詞", "格助詞", "*", "*", "*", "*")
NUMERAL = ("名詞", "数詞", "*", "*", "*", "*")
COUNTER_NOUN = ("名詞", "普通名詞", "助数詞可能", "*", "*", "*")


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


# Sudachi's take on the lines below: a name cut up and read as common words or not at all,
# digits read one by one, 婆 read ばあ where it was said ばばあ
LEXICON = {
    "他": ("ホカ", NOUN),
    "人": ("ヒト", NOUN),
    "猫": ("ネコ", NOUN),
    "響": ("ヒビキ", NOUN),
    "婆": ("バア", NOUN),
    "薬師": ("ヤクシ", PROPER),
    "10": ("イチレイ", NUMERAL),
    "日": ("ニチ", COUNTER_NOUN),
    "十日": ("トオカ", NOUN),
}

# JMdict's readings of the spellings the lines hold
DICTIONARY = {
    "他": {"ほか", "た"},
    "人": {"ひと", "にん", "じん"},
    "婆": {"ばば", "ばばあ"},
    "十日": {"とおか", "じゅうにち"},
}


def tokenize(text):
    """Longest match over LEXICON; any other character is a morpheme of its own, read as
    itself (Sudachi reads a kanji it does not know so) and a particle (は read ワ)."""
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


def dictionary(form):
    return DICTIONARY.get(form, set())


# A line long enough that a word heard otherwise leaves its transcript close to it
LINE = "他の人はどこにいるの"


def furigana(caption, *transcripts, names=None, **kwargs):
    return af.audio_furigana(caption, transcripts, tokenize, names or {}, dictionary, **kwargs)


def why(result):
    return {r.surface: r.why for r in result.readings}


class WrittenTest(unittest.TestCase):
    def test_a_draft_both_models_hear_and_the_captions_own_reading(self):
        result = furigana("（白鈴）他に猫猫(マオマオ)", "ほかにまおまお", "ホカニマオマオ")
        self.assertEqual(result.text, "他[ほか]に 猫猫[まおまお]")
        self.assertEqual(why(result), {"他": "draft", "猫猫": "caption"})
        self.assertEqual((result.review, result.mismatched), ([], False))

    def test_another_reading_the_dictionary_has_where_the_audio_says_it(self):
        result = furigana(LINE, "たのひとわどこにいるの", "タノヒトワドコニイルノ")
        self.assertEqual(result.text, "他[た]の 人[ひと]はどこにいるの")
        self.assertEqual(why(result), {"他": "correction", "人": "draft"})

    def test_one_model_hearing_it_is_enough_where_the_other_hears_neither(self):
        result = furigana(LINE, "たのひとわどこにいるの", "そのひとわどこにいるの")
        self.assertEqual(result.text, "他[た]の 人[ひと]はどこにいるの")

    def test_a_name_read_as_the_shows_list_has_it(self):
        # Sudachi cuts 響迂 and cannot read 迂; the clip says the list's きょうう
        line = "響迂はどこにいるのかしら"
        heard = "きょううわどこにいるのかしら"
        result = furigana(line, heard, heard, names={"響迂": "きょうう"})
        self.assertEqual(result.text, "響迂[きょうう]はどこにいるのかしら")
        self.assertEqual(why(result), {"響迂": "correction"})

    def test_a_number_is_read_as_its_kanji_numerals_are(self):
        line = "10日にはどこにいるのかしら"
        result = furigana(line, "とおかにわどこにいるのかしら", "トーカニワドコニイルノカシラ")
        self.assertEqual(result.text, "10日[とおか]にはどこにいるのかしら")
        self.assertEqual(why(result), {"10日": "draft"})
        # JMdict has 十日 and no 10日, so its other reading is found under the numerals
        heard = "じゅうにちにわどこにいるのかしら"
        result = furigana(line, heard, heard)
        self.assertEqual(result.text, "10日[じゅうにち]にはどこにいるのかしら")

    def test_lines_are_joined_and_what_is_not_spoken_left_out(self):
        caption = "（赤ん坊の泣き声）<br>（猫猫）他の人は<br>猫猫(マオマオ)"
        result = furigana(caption, "ほかのひとわまおまお", "ホカノヒトワマオマオ")
        self.assertEqual(result.text, "他[ほか]の 人[ひと]は<br>猫猫[まおまお]")
        self.assertEqual([r.line for r in result.readings], [0, 0, 1])


    def test_html_is_left_out_but_its_line_breaks_and_the_text_written_escaped(self):
        # The tokenizer is given the text alone, so no tag's name is read as speech
        caption = '<font color="#ffff00">（猫猫）他の人は</font><br />猫猫(マオマオ)'
        result = furigana(caption, "ほかのひとわまおまお", "ホカノヒトワマオマオ")
        self.assertEqual(result.texts, ["他の人は", "猫猫"])
        self.assertEqual(result.text, "他[ほか]の 人[ひと]は<br>猫猫[まおまお]")
        heard = "ほかのひとわどこにいるの"
        result = furigana("他の人は&lt;どこ&gt;&amp;にいるの", heard, heard)
        self.assertEqual(result.texts, ["他の人は<どこ>&にいるの"])
        self.assertEqual(result.text, "他[ほか]の 人[ひと]は&lt;どこ&gt;&amp;にいるの")


class ReviewTest(unittest.TestCase):
    def test_models_hearing_two_readings_leave_the_word_with_both(self):
        result = furigana(LINE, "ほかのひとわどこにいるの", "たのひとわどこにいるの")
        self.assertEqual(result.text, "他[ほか？た]の 人[ひと]はどこにいるの")
        self.assertEqual(why(result), {"他": "review", "人": "draft"})
        self.assertEqual([r.surface for r in result.review], ["他"])
        self.assertEqual(result.review[0].heard, ["ホカ", "タ"])

    def test_a_reading_nothing_licenses(self):
        heard = "そのひとわどこにいるの"
        result = furigana(LINE, heard, heard)
        self.assertEqual(why(result), {"他": "review", "人": "draft"})
        # Both heard it alike, so it is offered once
        self.assertEqual(result.text, "他[ほか？そ]の 人[ひと]はどこにいるの")

    def test_no_correction_a_vowels_length_tells_from_another_licensed_reading(self):
        # 婆 drafted ばあ: JMdict reads it ばば and ばばあ, and the models drop a final long
        # vowel, so a clip heard as either is no evidence for it
        for said, offered in (("ばば", "ばあ？ばば"), ("ばばあ", "ばあ？ばばあ")):
            heard = said + "わどこにいるのかしら"
            result = furigana("婆はどこにいるのかしら", heard, heard)
            self.assertEqual(why(result), {"婆": "review"}, heard)
            self.assertEqual(result.text, f"婆[{offered}]はどこにいるのかしら")

    def test_a_transcript_far_from_the_caption_is_no_evidence_for_any_word(self):
        result = furigana("他の人は猫猫(マオマオ)", "ほかのひとわまおまお", "まったくちがうせりふだ")
        self.assertTrue(result.mismatched)
        self.assertEqual(why(result), {"他": "mismatch", "人": "mismatch", "猫猫": "caption"})
        # Each word offers its draft alone: what the alignment puts at a word of a mismatched
        # line may be another line's speech
        self.assertEqual(result.text, "他[ほか？]の 人[ひと？]は 猫猫[まおまお]")

    def test_no_transcripts_are_a_mismatch(self):
        result = furigana(LINE)
        self.assertTrue(result.mismatched)
        self.assertEqual(result.text, "他[ほか？]の 人[ひと？]はどこにいるの")

    def test_an_ass_tag_is_formatting(self):
        # A line drawn at the top of the screen, said as written
        heard = "ほかのひとわどこにいるの"
        result = furigana("{\\an8}" + LINE, heard, heard)
        self.assertFalse(result.mismatched)
        self.assertEqual(result.text, "他[ほか]の 人[ひと]はどこにいるの")

    def test_a_draft_sudachi_could_not_read_is_offered_no_reading(self):
        result = furigana("響迂はどこにいるのかしら")
        self.assertEqual(why(result), {"響迂": "mismatch"})
        self.assertEqual(result.text, "響迂[？]はどこにいるのかしら")

    def test_a_rare_kanji_form_sudachi_cannot_read_is_marked_too(self):
        # 𠮟, of a supplementary plane, was taken for punctuation: no furigana and no mark
        heard = "しかられたのわどこなのかしら"
        result = furigana("𠮟られたのはどこなのかしら", heard, heard)
        self.assertEqual(result.text, "𠮟[？]られたのはどこなのかしら")

    def test_a_spelling_the_captions_read_in_hiragana_somewhere_is_no_name(self):
        # The show's list reads 薬師 as a name, but the captions read it in hiragana elsewhere:
        # a word then, whose readings only the dictionary licenses
        line = "薬師ならどこにいるのかしら"
        names = {"薬師": "くすし"}
        heard = "くすしならどこにいるのかしら"
        result = furigana(line, heard, names=names)
        self.assertEqual(result.text, "薬師[くすし]ならどこにいるのかしら")
        result = furigana(line, heard, names=names, show_words={"薬師"})
        self.assertEqual(why(result), {"薬師": "review"})
        self.assertEqual(result.text, "薬師[やくし？くすし]ならどこにいるのかしら")


class DoubtTest(unittest.TestCase):
    def test_choices_split_as_the_draft_where_each_leaves_the_same_kana(self):
        self.assertEqual(af.doubt_furigana("開か", ["ひらか", "あか"]), " 開[ひら？あ]か")
        self.assertEqual(af.doubt_furigana("お茶", ["おちゃ"]), "お 茶[ちゃ？]")

    def test_else_one_group_over_the_whole_word(self):
        self.assertEqual(
            af.doubt_furigana("泣き明かし", ["なきあかし", "なきかし"]),
            " 泣き明かし[なきあかし？なきかし]",
        )
        self.assertEqual(af.doubt_furigana("響迂", []), " 響迂[？]")

    def test_a_doubt_is_a_question_mark_inside_furigana_only(self):
        self.assertTrue(af.has_doubt("他[ほか？た]の 人[ひと]は"))
        self.assertTrue(af.has_doubt("響迂[？]は"))
        self.assertFalse(af.has_doubt("他[ほか]の 人[ひと]は？ どこ？"))


class LicensedTest(unittest.TestCase):
    def test_the_dictionarys_readings_and_a_names(self):
        word = af.Word(0, 0, 3, "10日", "とおか", None, ["number"])
        self.assertEqual(af.licensed_readings(word, dictionary, {}), ["じゅうにち", "とおか"])
        name = af.Word(0, 0, 2, "響迂", "ひびき迂", None, ["name"])
        self.assertEqual(af.licensed_readings(name, dictionary, {"響迂": "きょうう"}), ["きょうう"])
        word = af.Word(0, 0, 2, "響迂", "ひびき迂", None, ["split"])
        self.assertEqual(af.licensed_readings(word, dictionary, {"響迂": "きょうう"}), [])


if __name__ == "__main__":
    unittest.main()
