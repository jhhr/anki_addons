"""The check of a draft's readings against a transcript of the audio: morae as pronounced, the
alignment, what a word is heard as, and the rule that picks the reading to write.

Nothing here tokenizes with Sudachi: a stand-in segments the few lines the tests use the way
Sudachi does, so the suite runs without SudachiPy or the downloaded dictionaries."""

import unittest

from addon_modules import load_ops_module

check = load_ops_module("reading_check", subdir="word_array")

NOUN = ("名詞", "普通名詞", "一般", "*", "*", "*")
PARTICLE = ("助詞", "係助詞", "*", "*", "*", "*")
NUMERAL = ("名詞", "数詞", "*", "*", "*", "*")
SPACE = ("空白", "*", "*", "*", "*", "*")
STOP = ("補助記号", "句点", "*", "*", "*", "*")


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


LEXICON = {
    "猫": ("ネコ", NOUN),
    "薬": ("クスリ", NOUN),
    "は": ("ハ", PARTICLE),
    "175": ("イチナナゴ", NUMERAL),
    "百七十五": ("ヒャクナナジュウゴ", NUMERAL),
    # Sudachi's reading of a space
    "\u3000": ("キゴウ", SPACE),
}


def tokenize(text):
    """Longest match over LEXICON; any other character is a morpheme of its own."""
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
            out.append(Morpheme(text[i], text[i], NOUN, i))
            i += 1
    return out


class MoraeTest(unittest.TestCase):
    """The comparison of a transcript with a reading, in morae as pronounced."""

    def test_morae_join_small_kana_and_lengthen_vowels(self):
        self.assertEqual(check.morae("きょうはいい"), ["キョ", "ウ", "ハ", "イ", "イ"])
        self.assertEqual(check.morae("キョーワ"), ["キョ", "オ", "ワ"])
        self.assertEqual(check.morae("ぢづを、！"), ["ジ", "ズ", "オ"])

    def test_one_sound_spelled_two_ways_agrees(self):
        self.assertTrue(check.agrees("とうきょう", "トーキョー"))
        self.assertTrue(check.agrees("せんせい", "センセー"))

    def test_speech_dropping_a_long_vowel_or_geminate_agrees(self):
        self.assertTrue(check.agrees("いじょう", "イジョ"))
        self.assertTrue(check.agrees("いった", "イタ"))

    def test_one_reading_as_furigana_counts_a_vowels_length(self):
        self.assertTrue(check.same_reading("せんせい", "センセー"))
        self.assertTrue(check.same_reading("とうきょう", "トーキョー"))
        # A transcript that drops a long vowel agrees with the reading, but as furigana ばば is
        # another word than ばばあ
        self.assertTrue(check.agrees("ばばあ", "ババ"))
        self.assertFalse(check.same_reading("ばばあ", "ばば"))
        self.assertFalse(check.same_reading("ほんとう", "ほんと"))

    def test_another_reading_does_not(self):
        self.assertFalse(check.agrees("ねこねこ", "まおまお"))
        self.assertFalse(check.agrees("ほか", "た"))
        # rendaku is a reading of its own: 洗濯係 is せんたくがかり
        self.assertFalse(check.agrees("かかり", "がかり"))

    def test_a_word_heard_as_another_of_its_length_lines_up_mora_by_mora(self):
        ref = check.morae("ですおうかです")
        hyp = check.morae("ですいんふぁです")
        _, pairs = check.align(ref, hyp)
        self.assertEqual([hyp[j] for j in pairs[2:5] if j is not None], ["イ", "ン", "ファ"])


class HearsTest(unittest.TestCase):
    def test_the_reading_covering_what_was_heard_wins(self):
        hyp = check.morae("ババア")
        verdicts = check.hears({"draft": "ばあ", "caption": "ばばあ"}, hyp, 0, len(hyp))
        self.assertEqual(verdicts, {"draft": False, "caption": True})

    def test_a_correction_needs_a_closer_fit_than_the_draft(self):
        # イッ is a long vowel and an edge move from イー: close enough to confirm a draft read
        # so, not to write it instead of one
        hyp = check.morae("イッ")
        self.assertEqual(check.hears({"draft": "いー"}, hyp, 0, 2), {"draft": True})
        self.assertEqual(check.hears({"x": "いー"}, hyp, 0, 2, strict=["x"]), {"x": False})

    def test_a_correction_explains_all_that_was_heard_in_its_place(self):
        # 匹 of 一匹 heard ピキ: き fits once ピ is dropped, which only the draft may do
        hyp = check.morae("イッピキ")
        readings = {"draft": "ひき", "x": "き"}
        self.assertEqual(check.hears(readings, hyp, 2, 4), {"draft": False, "x": True})
        verdicts = check.hears(readings, hyp, 2, 4, strict=["x"])
        self.assertEqual(verdicts, {"draft": False, "x": False})

    def test_a_reading_may_move_an_edge_by_a_mora(self):
        # the alignment gave 猫猫 only オマオ of the マオマオ heard
        hyp = check.morae("ノマオマオガ")
        verdicts = check.hears({"draft": "ねこねこ", "show_name": "まおまお"}, hyp, 2, 5)
        self.assertEqual(verdicts, {"draft": False, "show_name": True})


class ReferenceTest(unittest.TestCase):
    def test_a_word_is_read_as_its_reading_and_the_rest_as_pronounced(self):
        spans = [check.Span(0, 0, 2, "まおまお")]
        ref, owner, placed = check.reference(["猫猫は薬"], spans, tokenize)
        self.assertEqual(ref, ["マ", "オ", "マ", "オ", "ワ", "ク", "ス", "リ"])
        self.assertEqual(owner, [0, 0, 0, 0, -1, -1, -1, -1])
        self.assertEqual(placed, [0])

    def test_digits_no_word_took_are_a_number_and_a_space_no_sound(self):
        ref, owner, _ = check.reference(["175\u3000猫"], [], tokenize)
        self.assertEqual("".join(ref), "ヒャクナナジュウゴネコ")
        self.assertEqual(owner, [-1] * len(ref))

    def test_a_word_not_starting_at_a_morpheme_is_not_found(self):
        # 175 is one morpheme: a word from its second digit starts inside it
        spans = [check.Span(0, 1, 3, "ななじゅうご")]
        _, owner, placed = check.reference(["175"], spans, tokenize)
        self.assertEqual(placed, [])
        self.assertNotIn(0, owner)

    def test_a_morpheme_of_no_text_places_no_word_again(self):
        # Sudachi reads … as ... and puts the two dots it adds where the next word starts
        def tokenize_ellipsis(text):
            out = []
            for mo in tokenize(text):
                out.append(mo)
                if mo.surface() == "…":
                    out += [Morpheme("", "．", STOP, mo.end()) for _ in range(2)]
            return out

        spans = [check.Span(0, 2, 4, "まおまお")]
        ref, owner, placed = check.reference(["ん…猫猫"], spans, tokenize_ellipsis)
        self.assertEqual("".join(ref), "ンマオマオ")
        self.assertEqual(owner, [-1, 0, 0, 0, 0])
        self.assertEqual(placed, [0])

    def test_words_are_placed_in_line_order(self):
        spans = [check.Span(1, 0, 1, "くすり"), check.Span(0, 0, 1, "ねこ")]
        _, _, placed = check.reference(["猫", "薬"], spans, tokenize)
        self.assertEqual(placed, [1, 0])

    def test_a_words_stretch_is_between_its_aligned_neighbours(self):
        # mora 2 of the word lines up with nothing; its neighbours with hyp morae 0 and 3
        self.assertEqual(check.neighbours([-1, 0, 0, -1], [0, 1, None, 3], 0, 5), (1, 3))
        self.assertEqual(check.neighbours([-1, 0], [0, None], 0, 4), (1, 4))
        self.assertEqual(check.neighbours([-1], [0], 0, 4), (0, 0))


class RuleTest(unittest.TestCase):
    def test_the_choices_are_the_draft_and_the_other_readings_that_do_not_sound_like_it(self):
        options = check.choices("ほか", ["た", "ほか", "ほかあ"])
        self.assertEqual(options, {"draft": "ほか", "た": "た"})

    def test_a_draft_holding_a_kanji_sudachi_could_not_read_is_no_choice(self):
        self.assertEqual(check.choices("すい苓", ["すいれい"]), {"すいれい": "すいれい"})

    def test_a_transcript_hears_the_choices_the_others_held_to_a_corrections_limits(self):
        options = check.choices("ほか", ["た"])
        hyp = check.morae("タ")
        self.assertEqual(check.hears_choices(options, hyp, 0, 1), {"draft": False, "た": True})
        self.assertEqual(check.hears_choices({}, hyp, 0, 1), {"draft": False})

    def test_the_draft_where_a_quorum_hears_it_and_no_other(self):
        draft = {"draft": True, "た": False}
        self.assertEqual(check.decide([draft, draft], "ほか", 2), "ほか")
        self.assertEqual(check.decide([draft, {"draft": False, "た": False}], "ほか", 1), "ほか")
        self.assertIsNone(check.decide([draft, {"draft": False, "た": False}], "ほか", 2))

    def test_another_reading_where_a_quorum_hears_it_and_not_the_draft(self):
        other = {"draft": False, "た": True}
        self.assertEqual(check.decide([other, other], "ほか", 2), "た")

    def test_no_correction_a_vowels_length_tells_from_another_licensed_reading(self):
        # 婆 said ばばあ, heard ババ by both: JMdict reads it ばば too
        heard = {"draft": False, "ばば": True, "ばばあ": False}
        self.assertEqual(check.decide([heard, heard], "ばあ", 2), "ばば")
        self.assertIsNone(check.decide([heard, heard], "ばあ", 2, licensed=["ばば", "ばばあ"]))
        # A reading spelled two ways is no sibling of itself
        self.assertEqual(check.decide([heard, heard], "ばあ", 2, licensed=["ばば", "ババ"]), "ばば")

    def test_a_draft_is_confirmed_whatever_its_siblings(self):
        heard = {"draft": True}
        self.assertEqual(check.decide([heard], "ほんとう", 1, licensed=["ほんと"]), "ほんとう")

    def test_a_quorum_hearing_two_readings_is_a_review(self):
        hearings = [{"draft": True, "た": False}, {"draft": False, "た": True}]
        self.assertIsNone(check.decide(hearings, "ほか", 1))
        self.assertIsNone(check.decide(hearings, "ほか", 2))


if __name__ == "__main__":
    unittest.main()
