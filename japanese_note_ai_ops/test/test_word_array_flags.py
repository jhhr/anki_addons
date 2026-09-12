"""Numbers and the five `match_data` states, both of which are plain data handling.

Neither needs SudachiPy, JMdict or a network, so these run wherever the suite does.
"""

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


class JudgePromptTests(unittest.TestCase):
    def setUp(self):
        self.arr = [
            ["<k>"],
            word(" 一[ひと]つ", "noun", "一つ", "ひとつ", subs=[word("一"), word("つ")]),
            ["</k>"],
            word("は", "particle", "は", "は"),
            word("28", "number", "二十八", "にじゅうはち", match_data=["dontmatch"]),
            word("本", "noun", "本", "ほん", match_data=[123, 4]),
        ]
        self.prompt, self.elements = match_flags.judge_prompt("sentence", self.arr)

    def test_only_unjudged_words_are_numbered_and_sub_words_are_indented(self):
        self.assertEqual([e[2] for e in self.elements], ["一つ", "X", "X", "は"])
        self.assertIn("0. 一つ: 一つ [ひとつ], noun", self.prompt)
        self.assertIn("    1. 一: X [x], noun", self.prompt)

    def test_decided_words_are_shown_for_context(self):
        self.assertIn("- 28: 二十八 [にじゅうはち], number (no note)", self.prompt)
        self.assertIn("- 本: 本 [ほん], noun (has a note)", self.prompt)

    def test_a_rejudging_mode_numbers_the_states_it_is_given(self):
        _, elements = match_flags.judge_prompt("s", self.arr, match_flags.REJUDGE_MATCHED)
        self.assertEqual([e[2] for e in elements], ["本"])
        _, elements = match_flags.judge_prompt("s", self.arr, match_flags.REJUDGE_ALL)
        self.assertEqual([e[2] for e in elements], ["二十八", "本"])

    def test_picked_words_are_dontmatch_and_the_rest_match(self):
        self.assertEqual(match_flags.apply_judge_response(self.elements, {"dontmatch": [0]}), [])
        self.assertEqual([e[4] for e in self.elements], [["dontmatch"]] + [["match"]] * 3)

    def test_rejudging_reports_the_links_it_takes_away(self):
        _, elements = match_flags.judge_prompt("s", self.arr, match_flags.REJUDGE_ALL)
        self.assertEqual(match_flags.apply_judge_response(elements, {"dontmatch": [1]}), [123])
        self.assertEqual([e[4] for e in elements], [["match"], ["dontmatch"]])

    def test_numbers_that_name_no_word_are_ignored(self):
        response = {"dontmatch": [99, -1, "0", True]}
        self.assertEqual(match_flags.apply_judge_response(self.elements, response), [])
        self.assertFalse(any(match_flags.is_flagged(e) for e in self.elements))

    def test_a_response_without_the_key_is_refused_and_changes_nothing(self):
        for response in [{}, {"dontmatch": "0"}, [], None]:
            with self.assertRaises(ValueError):
                match_flags.apply_judge_response(self.elements, response)
        self.assertEqual([e[4] for e in self.elements], [[]] * 4)


if __name__ == "__main__":
    unittest.main()
