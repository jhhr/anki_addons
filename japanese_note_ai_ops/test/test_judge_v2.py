"""Word matching judge v2: which words it asks about, what each prompt says, what it writes back."""

import unittest

from addon_modules import load_ops_module

judge_v2 = load_ops_module("judge_v2", subdir="word_array")
match_flags = load_ops_module("match_flags", subdir="word_array")


def word(text, pos="noun", match_data=None, subs=None):
    return [text, pos, text, "x", match_data if match_data is not None else [], subs or []]


class PlanTests(unittest.TestCase):
    def test_particles_and_the_copula_are_not_asked_about(self):
        arr = [word("A"), word("は", "particle"), word("B", "verb"), word("だ", "copula")]
        plan = judge_v2.plan_judgements(arr)
        self.assertEqual([e[2] for e in plan.auto], ["は", "だ"])
        self.assertEqual([(a.elem[2], a.group) for a in plan.asks], [("A", "noun"), ("B", "verb")])
        self.assertEqual(judge_v2.set_auto(plan), [])
        self.assertEqual([e[4] for e in arr], [[], ["dontmatch"], [], ["dontmatch"]])

    def test_only_words_in_the_states_are_planned(self):
        arr = [word("A", match_data=["match"]), word("B", match_data=[5]), word("C")]
        plan = judge_v2.plan_judgements(arr, match_flags.REJUDGE_MATCHED)
        self.assertEqual([a.elem[2] for a in plan.asks], ["B"])

    def test_a_linked_particle_is_unlinked_when_rejudged(self):
        arr = [word("の", "particle", [9])]
        plan = judge_v2.plan_judgements(arr, match_flags.REJUDGE_ALL)
        self.assertEqual(judge_v2.set_auto(plan), [9])

    def test_the_prompt_has_the_word_its_parent_and_components(self):
        subs = [word("B", "noun"), word("C", "suffix")]
        arr = [word("X"), word("BC", "expression", subs=subs), word("D", "mystery")]
        asks = {a.elem[2]: a for a in judge_v2.plan_judgements(arr).asks}
        self.assertIn("Sentence: X<b>BC</b>D", asks["BC"].prompt)
        self.assertIn("Made of: B [x] + C [x]", asks["BC"].prompt)
        self.assertIn(judge_v2.POS_RULES["expression"], asks["BC"].prompt)
        self.assertIn("Sentence: XB<b>C</b>D", asks["C"].prompt)
        self.assertIn("Part of: BC [x], expression", asks["C"].prompt)
        self.assertIn(judge_v2.POS_RULES["affix"], asks["C"].prompt)
        self.assertNotIn("\nPart of:", asks["D"].prompt)
        self.assertEqual(asks["D"].group, judge_v2.OTHER_GROUP)

    def test_every_group_has_rules(self):
        for group in set(judge_v2.POS_GROUPS.values()) | {judge_v2.OTHER_GROUP}:
            with self.subTest(group=group):
                self.assertIn(group, judge_v2.POS_RULES)


class ApplyTests(unittest.TestCase):
    def test_the_decision_is_applied(self):
        elem = word("A")
        self.assertIsNone(judge_v2.apply_word_response(elem, {"decision": "match"}))
        self.assertEqual(elem[4], ["match"])
        linked = word("B", match_data=[7])
        self.assertEqual(judge_v2.apply_word_response(linked, {"decision": "dontmatch"}), 7)
        self.assertEqual(linked[4], ["dontmatch"])

    def test_a_malformed_response_changes_nothing(self):
        for response in [None, {"decision": "maybe"}, {"reason": "x"}, ["match"]]:
            elem = word("A")
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    judge_v2.apply_word_response(elem, response)
                self.assertEqual(elem[4], [])


class ModelConfigTests(unittest.TestCase):
    def test_the_judge_model_falls_back_to_extract_words(self):
        op = load_ops_module("word_matching_judgev2")
        self.assertEqual(op.judge_model({"word_matching_judge_model": "a"}), "a")
        self.assertEqual(op.judge_model({"extract_words_model": "b"}), "b")
        self.assertEqual(
            op.judge_model({"word_matching_judge_model": "", "extract_words_model": "b"}), "b"
        )


if __name__ == "__main__":
    unittest.main()
