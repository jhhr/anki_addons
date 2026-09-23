"""Word matching judge: which words it asks about, what each prompt says, what it writes back."""

import asyncio
import json
import threading
import unittest
from unittest import mock

from addon_modules import (
    RunCollection,
    RunProgress,
    load_ops_module,
    mw,
    patch_nested_run,
    wait_until,
)

judge = load_ops_module("judge", subdir="word_array")
match_flags = load_ops_module("match_flags", subdir="word_array")


def word(text, pos="noun", match_data=None, subs=None):
    return [text, pos, text, "x", match_data if match_data is not None else [], subs or []]


class PlanTests(unittest.TestCase):
    def test_particles_and_the_copula_are_not_asked_about(self):
        arr = [word("A"), word("は", "particle"), word("B", "verb"), word("だ", "copula")]
        plan = judge.plan_judgements(arr)
        self.assertEqual([e[2] for e in plan.auto], ["は", "だ"])
        self.assertEqual(
            [(a.elem[2], a.group) for a in plan.asks], [("A", "noun-main"), ("B", "verb")]
        )
        self.assertEqual(judge.set_auto(plan), [])
        self.assertEqual([e[4] for e in arr], [[], ["dontmatch"], [], ["dontmatch"]])

    def test_only_words_in_the_states_are_planned(self):
        arr = [word("A", match_data=["match"]), word("B", match_data=[5]), word("C")]
        plan = judge.plan_judgements(arr, match_flags.REJUDGE_MATCHED)
        self.assertEqual([a.elem[2] for a in plan.asks], ["B"])

    def test_a_linked_particle_is_unlinked_when_rejudged(self):
        arr = [word("の", "particle", [9])]
        plan = judge.plan_judgements(arr, match_flags.REJUDGE_ALL)
        self.assertEqual(judge.set_auto(plan), [9])

    def test_the_prompt_has_the_word_its_parent_and_components(self):
        subs = [word("B", "noun"), word("C", "suffix")]
        arr = [word("X"), word("BC", "expression", subs=subs), word("D", "mystery")]
        asks = {a.elem[2]: a for a in judge.plan_judgements(arr).asks}
        self.assertIn("Sentence: X<b>BC</b>D", asks["BC"].prompt)
        self.assertIn("Made of: B [x] + C [x]", asks["BC"].prompt)
        self.assertIn(judge.POS_RULES["expression"], asks["BC"].prompt)
        self.assertIn("Sentence: XB<b>C</b>D", asks["C"].prompt)
        self.assertIn("Part of: BC [x], expression", asks["C"].prompt)
        self.assertIn(judge.POS_RULES["suffix"], asks["C"].prompt)
        self.assertNotIn("\nPart of:", asks["D"].prompt)
        self.assertEqual(asks["D"].group, judge.OTHER_GROUP)

    def test_nouns_split_by_nesting_and_two_verb_compounds_by_place(self):
        kau, kiru = word("買う", "verb"), word("切る", "verb")
        de, aru = word("で", "particle"), word("有る", "verb")
        arr = [
            word("A", subs=[word("B"), word("C", "pronoun")]),
            word("買い切る", "verb", subs=[kau, kiru]),
            word("で有る", "verb", subs=[de, aru]),
            word("X", "verb", subs=[word("Y", "verb"), word("Z", "verb"), word("W", "verb")]),
        ]
        groups = {a.elem[2]: a.group for a in judge.plan_judgements(arr).asks}
        self.assertEqual(
            groups,
            {
                "A": "noun-main",
                "B": "noun-sub",
                "C": "noun-sub",
                "買い切る": "verb",
                "買う": "prefix-verb",
                "切る": "suffix-verb",
                "で有る": "verb",
                "有る": "verb",
                "X": "verb",
                "Y": "verb",
                "Z": "verb",
                "W": "verb",
            },
        )

    def test_nouns_in_particle_words_and_expressions_split_by_their_words(self):
        arr = [
            word("本当に", "adverb", subs=[word("本当"), word("に", "particle")]),
            word("遣って来る", "expression", subs=[word("遣る", "verb"), word("来る", "verb")]),
            word(
                "気に為る",
                "expression",
                subs=[word("気"), word("に", "particle"), word("為る", "verb")],
            ),
            word("様に", "expression", subs=[word("様", "na-adjective"), word("に", "particle")]),
            word("大きな顔", "expression", subs=[word("大きい", "adjective"), word("顔")]),
        ]
        groups = {a.elem[2]: a.group for a in judge.plan_judgements(arr).asks}
        self.assertEqual(groups["本当"], "noun-phrase")
        self.assertEqual(groups["顔"], "noun-sub")
        self.assertEqual(groups["遣って来る"], "verb-expression")
        self.assertEqual(groups["気に為る"], "collocation")
        self.assertEqual(groups["気"], "noun-phrase")
        self.assertEqual(groups["様に"], "expression")
        self.assertEqual(groups["大きな顔"], "expression")

    def test_every_group_has_rules(self):
        groups = set(judge.POS_GROUPS.values()) | {judge.OTHER_GROUP}
        for group, split in judge.SPLIT_GROUPS.items():
            groups = (groups - {group}) | set(split)
        for group in groups:
            with self.subTest(group=group):
                self.assertIn(group, judge.POS_RULES)


class ApplyTests(unittest.TestCase):
    def test_the_decision_is_applied(self):
        elem = word("A")
        self.assertIsNone(judge.apply_word_response(elem, {"decision": "match"}))
        self.assertEqual(elem[4], ["match"])
        linked = word("B", match_data=[7])
        self.assertEqual(judge.apply_word_response(linked, {"decision": "dontmatch"}), 7)
        self.assertEqual(linked[4], ["dontmatch"])

    def test_a_malformed_response_changes_nothing(self):
        for response in [None, {"decision": "maybe"}, {"reason": "x"}, ["match"]]:
            elem = word("A")
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    judge.apply_word_response(elem, response)
                self.assertEqual(elem[4], [])


class ModelConfigTests(unittest.TestCase):
    def test_the_judge_model_falls_back_to_extract_words(self):
        op = load_ops_module("word_matching_judge")
        self.assertEqual(op.judge_model({"word_matching_judge_model": "a"}), "a")
        self.assertEqual(op.judge_model({"extract_words_model": "b"}), "b")
        self.assertEqual(
            op.judge_model({"word_matching_judge_model": "", "extract_words_model": "b"}), "b"
        )


class JudgedNote:
    """Counts the writes of its word array, so that saving one array twice shows."""

    def __init__(self, note_id, arr):
        self.id = note_id
        self.fields = {"word_list_field": json.dumps(arr, ensure_ascii=False)}
        self.array_writes = 0

    def note_type(self):
        return {"name": "Word"}

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def __setitem__(self, field, value):
        self.array_writes += 1
        self.fields[field] = value


class CancelledJudgeRunTests(unittest.TestCase):
    """A note's word array is written back once all its words are judged, by a task a cancel
    cancels too, so a cancelled run lost every judgment of the notes it was in the middle of."""

    def setUp(self):
        self.op = load_ops_module("word_matching_judge")
        self.base_ops = load_ops_module("base_ops")
        self.config = {
            "Word": {"word_list_field": "word_list_field"},
            "word_matching_judge_model": "model",
        }
        mw.progress.cancel = False
        self.addCleanup(setattr, mw.progress, "cancel", False)

    def test_a_cancelled_run_saves_each_notes_finished_judgments(self):
        """Through the real bulk_nested_notes_op and rolling driver, with a real cancel."""
        # Planned in this order: 箱 finishes before 本 starts, and 机 is never started
        finished = JudgedNote(3, [word("箱")])
        cancelled = JudgedNote(1, [word("本"), word("を", "particle"), word("棚")])
        never_started = JudgedNote(2, [word("机")])
        never_started_field = never_started["word_list_field"]
        updates, edited_nids, progress = {}, [], RunProgress()
        asked, answer = threading.Event(), threading.Event()
        applied = []
        real_apply = self.op.judge.apply_word_response

        def get_response(model, prompt, **_):
            if "<b>棚</b>" in prompt:
                # A request the cancel abandons, answered only after the run has returned
                asked.set()
                answer.wait(5)
                return {"decision": "dontmatch"}
            return {"decision": "match"}

        def apply_word_response(elem, response):
            result = real_apply(elem, response)
            applied.append(elem)
            return result

        fake_mw = mock.MagicMock()
        fake_mw.progress = mw.progress
        fake_mw.addonManager.getConfig.return_value = self.config

        async def run():
            runner = asyncio.ensure_future(
                self.op.make_bulk_op(match_flags.JUDGE_NEW)(
                    col=RunCollection(),
                    notes=[finished, cancelled, never_started],
                    edited_nids=edited_nids,
                    progress_updater=progress,
                    notes_to_add_dict={},
                    notes_to_update_dict=updates,
                )
            )
            # 箱 and 本 judged, 棚 waiting for its answer
            self.assertTrue(await wait_until(lambda: progress.tasks_done == 2 and asked.is_set()))
            mw.progress.cancel = True
            self.assertTrue(await wait_until(runner.done), "the run did not notice the cancel")
            runner.result()
            at_return = (cancelled["word_list_field"], cancelled.array_writes)
            # The abandoned thread judges 棚 after all, and the note's cancelled task unwinds
            answer.set()
            self.assertTrue(await wait_until(lambda: len(applied) == 3))
            for _ in range(50):
                await asyncio.sleep(0)
            return at_return

        with (
            patch_nested_run(self.base_ops),
            mock.patch.object(self.op, "mw", fake_mw),
            mock.patch.object(self.op, "get_response", get_response),
            mock.patch.object(self.op.judge, "apply_word_response", apply_word_response),
        ):
            field_at_return, writes_at_return = asyncio.run(run())

        # 本 judged by its request, を while planning; 棚 unjudged, to be asked again next run
        saved = json.loads(field_at_return)
        self.assertEqual([w[4] for w in saved], [["match"], ["dontmatch"], []])
        self.assertEqual(writes_at_return, 1)
        self.assertIs(updates[1], cancelled)
        # The late judgment changed the element in memory only: the field was written once,
        # before it, and is not written again
        self.assertEqual(applied[-1][2], "棚")
        self.assertEqual(applied[-1][4], ["dontmatch"])
        self.assertEqual(cancelled["word_list_field"], field_at_return)
        self.assertEqual(cancelled.array_writes, 1)
        # the note that finished was saved by its own task, and only then
        self.assertEqual(json.loads(finished["word_list_field"])[0][4], ["match"])
        self.assertEqual(finished.array_writes, 1)
        # and the one never started is untouched
        self.assertEqual(never_started.array_writes, 0)
        self.assertEqual(never_started["word_list_field"], never_started_field)
        self.assertNotIn(2, updates)
        self.assertEqual(edited_nids, [3, 1])


if __name__ == "__main__":
    unittest.main()
