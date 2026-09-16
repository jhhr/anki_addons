"""Numbers and the five `match_data` states, both of which are plain data handling.

Neither needs SudachiPy, JMdict or a network, so these run wherever the suite does.
"""

import json
import re
import unittest

from addon_modules import load_ops_module

numbers = load_ops_module("numbers", subdir="word_array")
match_flags = load_ops_module("match_flags", subdir="word_array")
State = match_flags.MatchState


def word(text="X", pos="noun", form="X", reading="x", match_data=None, subs=None):
    return [text, pos, form, reading, match_data if match_data is not None else [], subs or []]


class NumberValueTests(unittest.TestCase):
    def test_digits_of_either_width(self):
        self.assertEqual(numbers.parse_number("1"), 1)
        self.assertEqual(numbers.parse_number("１０"), 10)
        self.assertEqual(numbers.parse_number("1935"), 1935)

    def test_kanji_numerals_with_units_and_written_out_digit_by_digit(self):
        self.assertEqual(numbers.parse_number("千九百三十五"), 1935)
        self.assertEqual(numbers.parse_number("一九三五"), 1935)
        self.assertEqual(numbers.parse_number("二十"), 20)
        self.assertEqual(numbers.parse_number("一万二千"), 12000)
        self.assertEqual(numbers.parse_number("1万"), 10000)

    def test_what_is_not_a_number(self):
        for text in ["", "幾", "0.5", "十分に"]:
            self.assertIsNone(numbers.parse_number(text), text)


class NumeralAndReadingTests(unittest.TestCase):
    def test_the_dictionary_form_is_the_japanese_numeral(self):
        cases = {1: "一", 10: "十", 20: "二十", 28: "二十八", 1935: "千九百三十五", 10000: "一万"}
        for value, expected in cases.items():
            self.assertEqual(numbers.numeral(value), expected, value)

    def test_readings(self):
        cases = {
            0: "ぜろ",
            10: "じゅう",
            28: "にじゅうはち",
            300: "さんびゃく",
            1935: "せんきゅうひゃくさんじゅうご",
            10000: "いちまん",
        }
        for value, expected in cases.items():
            self.assertEqual(numbers.number_reading(value), expected, value)

    def test_large_numbers_do_not_raise(self):
        self.assertEqual(numbers.numeral(12345678), "千二百三十四万五千六百七十八")


class DefaultFlagTests(unittest.TestCase):
    def test_a_numeral_that_is_a_word_of_its_own_is_left_unjudged(self):
        for form in ["一", "九", "十", "二十", "百", "千", "万"]:
            self.assertEqual(match_flags.default_match_data("number", form, []), [], form)

    def test_any_other_number_starts_out_judged_dontmatch(self):
        for form in ["二十八", "千九百三十五", "0.5"]:
            self.assertEqual(
                match_flags.default_match_data("number", form, []), ["dontmatch"], form
            )

    def test_a_word_built_on_a_flagged_number_is_flagged_too(self):
        subs = [word(form="二十八", pos="number", match_data=["dontmatch"]), word(form="日")]
        self.assertEqual(match_flags.default_match_data("noun", "二十八日", subs), ["dontmatch"])

    def test_a_word_built_on_a_matched_number_is_not(self):
        subs = [word(form="三", pos="number"), word(form="月")]
        self.assertEqual(match_flags.default_match_data("noun", "三月", subs), [])


class MatchStateTests(unittest.TestCase):
    def test_the_five_states(self):
        cases = [
            ([], State.UNJUDGED),
            (["dontmatch"], State.DONT_MATCH),
            (["match"], State.MATCH),
            ([1674931277303], State.LINKED),
            ([1674931277303, 4], State.RATED),
        ]
        for data, state in cases:
            self.assertEqual(match_flags.match_state(word(match_data=data)), state, data)

    def test_anything_else_is_refused(self):
        for data in [["dont_match"], [True], [1, "4"], [1, 2, 3], ["match", 1]]:
            with self.assertRaises(ValueError, msg=data):
                match_flags.match_state(word(match_data=data))


class FlagHelperTests(unittest.TestCase):
    def test_words_are_walked_parents_before_their_sub_words(self):
        arr = [["<k>"], word(form="A", subs=[word(form="B"), word(form="C")]), word(form="D")]
        self.assertEqual(
            [(depth, e[2]) for depth, e in match_flags.iter_words(arr)],
            [(0, "A"), (1, "B"), (1, "C"), (0, "D")],
        )

    def test_flagging_a_matched_word_reports_the_note_it_unlinks(self):
        elem = word(match_data=[1674931277303, 2])
        self.assertEqual(match_flags.set_dont_match(elem), 1674931277303)
        self.assertEqual(elem[4], ["dontmatch"])
        self.assertTrue(match_flags.is_flagged(elem))

    def test_judging_a_linked_word_worth_a_note_keeps_its_link(self):
        linked, flagged = word(match_data=[123]), word(match_data=["dontmatch"])
        match_flags.set_match(linked)
        match_flags.set_match(flagged)
        self.assertEqual((linked[4], flagged[4]), ([123], ["match"]))

    def test_each_matching_prompt_gets_its_own_state(self):
        words = {
            data: word(form=data, match_data=value)
            for data, value in [
                ("unjudged", []),
                ("dontmatch", ["dontmatch"]),
                ("match", ["match"]),
                ("linked", [123]),
                ("rated", [123, 5]),
            ]
        }
        arr = [["。"], *words.values()]
        self.assertEqual(match_flags.elements_to_match(arr), [words["match"]])
        self.assertEqual(match_flags.elements_to_rate(arr), [words["linked"]])

    def test_a_note_id_is_not_confused_with_a_flag(self):
        self.assertIsNone(match_flags.matched_note_id(word(match_data=["dontmatch"])))
        self.assertIsNone(match_flags.matched_note_id(word(match_data=["match"])))
        self.assertEqual(match_flags.matched_note_id(word(match_data=[123])), 123)


class HighlightTests(unittest.TestCase):
    def test_every_word_is_marked_in_the_sentence_sub_words_too(self):
        arr = [
            ["<k>"],
            word(" 一[ひと]つ", "noun", "一つ", "ひとつ", subs=[word("一"), word("つ")]),
            ["</k>"],
            word("は", "particle", "は", "は"),
            word("28", "number", "二十八", "にじゅうはち"),
        ]
        expected = [
            (0, "<b>一つ</b>は28"),
            (1, "<b>一</b>つは28"),
            (1, "一<b>つ</b>は28"),
            (0, "一つ<b>は</b>28"),
            (0, "一つは<b>28</b>"),
        ]
        self.assertEqual(
            [(depth, sentence) for depth, _, sentence in match_flags.iter_highlighted(arr)],
            expected,
        )

    def test_each_occurrence_of_a_repeated_word_is_marked_where_it_is(self):
        arr = [word("本", form="本"), word("と"), word(" 本[ほん]", form="本")]
        sentences = [sentence for _, _, sentence in match_flags.iter_highlighted(arr)]
        self.assertEqual(sentences, ["<b>本</b>と本", "本<b>と</b>本", "本と<b>本</b>"])


class DecodeWordArrayTests(unittest.TestCase):
    def test_only_an_array_is_decoded(self):
        arr = [word("A")]
        self.assertEqual(match_flags.decode_word_array(json.dumps(arr)), arr)
        for value in ["", '{"nouns": []}', "[not json"]:
            with self.subTest(value=value):
                self.assertIsNone(match_flags.decode_word_array(value))

    def test_a_field_the_editor_turned_into_html_is_read_anyway(self):
        # What Anki's editor makes of the rows format_word_array writes, once anyone edits
        # the note by hand. Without this the note looks to every op like it holds no array.
        arr = [word("本", form="本", reading="ほん", match_data=["match"]), word("。")]
        mangled = (
            match_flags.format_word_array(arr).replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
        )
        self.assertEqual(match_flags.decode_word_array(mangled), arr)

    def test_an_entity_inside_a_raw_text_is_left_alone(self):
        # The repair only runs on a field that did not parse as it stands, so a raw text
        # that holds `&nbsp;` itself keeps it.
        arr = [word("&nbsp;", form="本", reading="ほん")]
        self.assertEqual(match_flags.decode_word_array(json.dumps(arr)), arr)


class FormatWordArrayTests(unittest.TestCase):
    def test_one_top_level_word_per_row_with_sub_words_indented(self):
        arr = [
            ["<k>"],
            word(
                " 一[ひと]つ",
                pos="adverb",
                form="一つ",
                reading="ひとつ",
                match_data=["match"],
                subs=[
                    word(" 一[ひと]", pos="number", form="一", reading="ひと", match_data=["dontmatch"]),
                    word("つ", pos="counter", form="つ", reading="つ", match_data=[1674931277303, 4]),
                ],
            ),
            ["。"],
        ]
        self.assertEqual(
            match_flags.format_word_array(arr),
            "[\n"
            '  ["<k>"],\n'
            '  [" 一[ひと]つ", "adverb", "一つ", "ひとつ", ["match"], [\n'
            '    [" 一[ひと]", "number", "一", "ひと", ["dontmatch"], []],\n'
            '    ["つ", "counter", "つ", "つ", [1674931277303, 4], []]\n'
            "  ]],\n"
            '  ["。"]\n'
            "]",
        )

    def test_an_empty_array_is_one_pair_of_brackets(self):
        self.assertEqual(match_flags.format_word_array([]), "[]")

    def test_it_round_trips_through_decode(self):
        arr = [
            word("本", form="本", reading="ほん", match_data=[1674931277303]),
            word("を", pos="particle", form="を", reading="を", match_data=["dontmatch"]),
            word(
                " 読[よ]む",
                pos="verb",
                form="読む",
                reading="よむ",
                subs=[word("読", form="読", reading="よ", subs=[word("読")])],
            ),
        ]
        self.assertEqual(match_flags.decode_word_array(match_flags.format_word_array(arr)), arr)

    def test_the_search_regexes_still_find_a_word_in_the_rows(self):
        # The separators within a word stay ", ", which is the whole of what
        # word_array_query_regex assumes; only the rows between words are new.
        arr = [
            word(
                "本を",
                pos="expression",
                form="本を",
                reading="ほんを",
                match_data=["dontmatch"],
                subs=[word("本", form="本", reading="ほん", match_data=["match"])],
            )
        ]
        text = match_flags.format_word_array(arr)
        regex = match_flags.word_array_query_regex("本", "ほん", [State.MATCH])
        self.assertIsNotNone(re.search(regex, text))


class WordArrayQueryRegexTests(unittest.TestCase):
    def search(self, arr, states, form="本", reading="ほん"):
        regex = match_flags.word_array_query_regex(form, reading, states)
        return re.search(regex, json.dumps(arr, ensure_ascii=False)) is not None

    def test_finds_the_word_in_the_states_asked_sub_words_too(self):
        linked = [State.LINKED, State.RATED]
        cases = [
            ([word("本", form="本", reading="ほん", match_data=[1674931277303])], linked, True),
            ([word("本", form="本", reading="ほん", match_data=[1674931277303, 4])], linked, True),
            ([word("本", form="本", reading="ほん", match_data=[-1234567])], linked, True),
            ([word("本", form="本", reading="ほん", match_data=["match"])], linked, False),
            ([word("本", form="本", reading="ほん", match_data=["match"])], [State.MATCH], True),
            ([word("本", form="本", reading="ほん", match_data=[])], [State.MATCH], False),
            ([word("本", form="本", reading="もと", match_data=["match"])], [State.MATCH], False),
            (
                [
                    word(
                        "本屋",
                        form="本屋",
                        subs=[word("本", form="本", reading="ほん", match_data=[5])],
                    )
                ],
                linked,
                True,
            ),
        ]
        for arr, states, found in cases:
            with self.subTest(arr=arr, states=states):
                self.assertEqual(self.search(arr, states), found)

    def test_raw_text_and_part_of_speech_are_not_the_word(self):
        arr = [word("本", pos="ほん", form="別", reading="べつ", match_data=["match"])]
        self.assertFalse(self.search(arr, [State.MATCH]))


if __name__ == "__main__":
    unittest.main()
