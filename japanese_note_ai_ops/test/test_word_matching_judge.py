"""The word matching judge op: which words it asks about, and what it writes back.

The model is stood in for by a function, so these run without a network.
"""

import json
import unittest

from addon_modules import load_ops_module

judge = load_ops_module("word_matching_judge")
match_flags = load_ops_module("match_flags", subdir="word_array")


def word(text, match_data=None, subs=None):
    return [text, "noun", text, "x", match_data if match_data is not None else [], subs or []]


class Asker:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.response


class JudgeWordArrayTests(unittest.TestCase):
    def test_no_request_when_there_is_nothing_to_judge(self):
        arr = [word("A", ["match"]), word("B", [5, 3])]
        ask = Asker({"dontmatch": []})
        self.assertIsNone(judge.judge_word_array("AB", arr, match_flags.JUDGE_NEW, ask))
        self.assertEqual(ask.prompts, [])

    def test_picks_are_dontmatch_and_the_rest_match(self):
        arr = [word("A"), word("B"), word("C", [7])]
        ask = Asker({"dontmatch": [1]})
        self.assertEqual(judge.judge_word_array("ABC", arr, match_flags.JUDGE_NEW, ask), [])
        self.assertEqual([e[4] for e in arr], [["match"], ["dontmatch"], [7]])
        self.assertEqual(len(ask.prompts), 1)

    def test_rejudging_reports_the_unlinked_note_ids(self):
        arr = [word("A", [7]), word("B", [8, 4]), word("C")]
        ask = Asker({"dontmatch": [0]})
        unlinked = judge.judge_word_array("ABC", arr, match_flags.REJUDGE_MATCHED, ask)
        self.assertEqual(unlinked, [7])
        self.assertEqual([e[4] for e in arr], [["dontmatch"], [8, 4], []])

    def test_a_failed_or_malformed_response_changes_nothing(self):
        for response in [None, {"other": [0]}, ["0"]]:
            arr = [word("A")]
            with self.subTest(response=response):
                ask = Asker(response)
                self.assertIsNone(judge.judge_word_array("A", arr, match_flags.JUDGE_NEW, ask))
                self.assertEqual(arr[0][4], [])


class DecodeWordArrayTests(unittest.TestCase):
    def test_only_an_array_is_decoded(self):
        arr = [word("A")]
        self.assertEqual(judge.decode_word_array(json.dumps(arr)), arr)
        for value in ["", '{"nouns": []}', "[not json"]:
            with self.subTest(value=value):
                self.assertIsNone(judge.decode_word_array(value))


class ModelConfigTests(unittest.TestCase):
    def test_the_judge_model_falls_back_to_extract_words(self):
        self.assertEqual(judge.judge_model({"word_matching_judge_model": "a"}), "a")
        self.assertEqual(judge.judge_model({"extract_words_model": "b"}), "b")
        self.assertEqual(
            judge.judge_model({"word_matching_judge_model": "", "extract_words_model": "b"}), "b"
        )


if __name__ == "__main__":
    unittest.main()
