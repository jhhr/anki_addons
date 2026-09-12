"""Numbers and the "dont_match" flag, both of which are plain data handling.

Neither needs SudachiPy, JMdict or a network, so these run wherever the suite does.
"""

import unittest

from addon_modules import load_ops_module

numbers = load_ops_module("numbers", subdir="word_array")
match_flags = load_ops_module("match_flags", subdir="word_array")


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
    def test_a_numeral_that_is_a_word_of_its_own_is_matched(self):
        for form in ["一", "九", "十", "二十", "百", "千", "万"]:
            self.assertEqual(match_flags.default_match_data("number", form, []), [], form)

    def test_any_other_number_starts_out_flagged(self):
        for form in ["二十八", "千九百三十五", "0.5"]:
            self.assertEqual(
                match_flags.default_match_data("number", form, []), ["dont_match"], form
            )

    def test_a_word_built_on_a_flagged_number_is_flagged_too(self):
        subs = [word(form="二十八", pos="number", match_data=["dont_match"]), word(form="日")]
        self.assertEqual(match_flags.default_match_data("noun", "二十八日", subs), ["dont_match"])

    def test_a_word_built_on_a_matched_number_is_not(self):
        subs = [word(form="三", pos="number"), word(form="月")]
        self.assertEqual(match_flags.default_match_data("noun", "三月", subs), [])


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
        self.assertEqual(elem[4], ["dont_match"])
        self.assertTrue(match_flags.is_flagged(elem))

    def test_only_words_neither_matched_nor_flagged_are_left_to_match(self):
        todo, matched, flagged = (
            word(form="todo"),
            word(form="matched", match_data=[123]),
            word(form="flagged", match_data=["dont_match"]),
        )
        arr = [["。"], todo, matched, flagged]
        self.assertEqual(match_flags.elements_to_match(arr), [todo])

    def test_a_note_id_is_not_confused_with_a_flag(self):
        self.assertIsNone(match_flags.matched_note_id(word(match_data=["dont_match"])))
        self.assertEqual(match_flags.matched_note_id(word(match_data=[123])), 123)


class FlagPromptTests(unittest.TestCase):
    def setUp(self):
        self.arr = [
            ["<k>"],
            word(" 一[ひと]つ", "noun", "一つ", "ひとつ", subs=[word("一"), word("つ")]),
            ["</k>"],
            word("は", "particle", "は", "は"),
            word("28", "number", "二十八", "にじゅうはち", match_data=["dont_match"]),
        ]
        self.prompt, self.elements = match_flags.flag_prompt("sentence", self.arr)

    def test_every_word_is_numbered_and_sub_words_are_indented(self):
        self.assertEqual([e[2] for e in self.elements], ["一つ", "X", "X", "は", "二十八"])
        self.assertIn("0. 一つ: 一つ [ひとつ], noun", self.prompt)
        self.assertIn("    1. 一: X [x], noun", self.prompt)

    def test_an_already_flagged_word_says_so(self):
        self.assertIn("4. 28: 二十八 [にじゅうはち], number (already not matched)", self.prompt)

    def test_the_picked_words_are_flagged(self):
        match_flags.apply_flag_response(self.elements, {"dont_match": [0, 3]})
        self.assertEqual(
            [match_flags.is_flagged(e) for e in self.elements], [True, False, False, True, True]
        )

    def test_numbers_that_name_no_word_are_ignored(self):
        self.assertEqual(
            match_flags.apply_flag_response(self.elements, {"dont_match": [99, -1, "0"]}), []
        )
        self.assertFalse(any(match_flags.is_flagged(e) for e in self.elements[:4]))

    def test_a_response_without_the_key_is_refused(self):
        for response in [{}, {"dont_match": "0"}, [], None]:
            with self.assertRaises(ValueError):
                match_flags.apply_flag_response(self.elements, response)


if __name__ == "__main__":
    unittest.main()
