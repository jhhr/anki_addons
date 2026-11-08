"""Run all ops for new notes (async_api_ops/new_note_all_ops.py) under the two note layouts.

In the two-type layout the new notes of a day are sentence notes added by hand and vocab notes
the match op made, one selection: each note gets the steps of its type's role (meanings and
their cleaning for a vocab note, kanjify and extract for a sentence note, the judge on sentence
notes only), never the other role's, which used to fail per note on fields its type does not
have. In the one-type layout every note gets every step, as before.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from addon_modules import load_ops_module

ops = load_ops_module("new_note_all_ops")
gate = load_ops_module("role_gate")
roles = load_ops_module("note_roles", subdir="")

VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"

TWO_TYPE = {
    SENTENCE: {"vocab_note_type": VOCAB, "word_list_field": "Words"},
    VOCAB: {"sentence_note_type": SENTENCE, "word_sort_field": "Sort"},
}
ONE_TYPE = {VOCAB: {"word_list_field": "Words", "word_sort_field": "Sort"}}


class Note:
    def __init__(self, note_id: int, type_name: str) -> None:
        self.id = note_id
        self.type_name = type_name

    def note_type(self):
        return {"name": self.type_name}


class StepsByRoleTests(unittest.TestCase):
    """new_note_all_ops_in_note runs the steps of the roles it is given, in today's order."""

    def setUp(self):
        self.calls: list[str] = []

        def step(name, result=True):
            def run(*args, **kwargs):
                self.calls.append(name)
                return result

            return run

        clean_result = mock.Mock(changed=True)
        for name, replacement in (
            ("make_meanings_in_note", step("meanings")),
            ("clean_meaning_in_note", step("clean", clean_result)),
            ("kanjify_sentence_in_note", step("kanjify")),
        ):
            patcher = mock.patch.object(ops, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.extract = step("extract")

    def run_steps(self, **kwargs):
        return ops.new_note_all_ops_in_note(
            {}, Note(1, VOCAB), {}, {}, set(), {}, self.extract, **kwargs
        )

    def test_both_roles_run_every_step_in_order(self):
        self.assertTrue(self.run_steps())
        self.assertEqual(self.calls, ["meanings", "clean", "kanjify", "extract"])

    def test_a_vocab_note_gets_the_meanings_and_their_cleaning(self):
        self.run_steps(roles=(roles.VOCAB_ROLE,))
        self.assertEqual(self.calls, ["meanings", "clean"])

    def test_a_sentence_note_gets_kanjify_and_extract(self):
        self.run_steps(roles=(roles.SENTENCE_ROLE,))
        self.assertEqual(self.calls, ["kanjify", "extract"])

    def test_no_role_runs_nothing(self):
        self.assertFalse(self.run_steps(roles=()))
        self.assertEqual(self.calls, [])


class BulkOpRolesTests(unittest.TestCase):
    """The bulk op hands each note the roles of its type, and leaves out a broken layout's."""

    def run_bulk(self, config, notes):
        given_roles: dict[int, tuple] = {}

        def in_note(config, note, *args):
            given_roles[note.id] = args[-1]
            return False

        async def bulk_notes_op(message, config, op, col, notes, *args, **kwargs):
            for note in notes:
                op(config, note, {}, {})
            return [note.id for note in notes]

        with mock.patch.object(ops, "mw") as mw, mock.patch.multiple(
            ops,
            load_meanings_dict_from_file=mock.Mock(return_value={}),
            extract_words_op=mock.Mock(),
            new_note_all_ops_in_note=in_note,
            bulk_notes_op=bulk_notes_op,
        ), mock.patch.object(gate, "report_error") as report:
            mw.addonManager.getConfig.return_value = config
            ran = asyncio.run(ops.bulk_new_note_all_ops(None, notes, [], mock.Mock(), {}, {}))
        return ran, given_roles, report

    def test_two_type_each_note_gets_its_own_role(self):
        notes = [Note(1, SENTENCE), Note(2, VOCAB), Note(3, "Kanji draw")]
        ran, given, report = self.run_bulk(TWO_TYPE, notes)
        self.assertEqual(ran, [1, 2, 3])
        self.assertEqual(
            given,
            {1: (roles.SENTENCE_ROLE,), 2: (roles.VOCAB_ROLE,), 3: roles.ROLES},
        )
        report.assert_not_called()

    def test_one_type_every_note_gets_every_step(self):
        ran, given, report = self.run_bulk(ONE_TYPE, [Note(1, VOCAB), Note(2, VOCAB)])
        self.assertEqual(given, {1: roles.ROLES, 2: roles.ROLES})
        report.assert_not_called()

    def test_a_broken_layout_s_notes_are_left_out_and_reported_once_per_type(self):
        config = {name: dict(block) for name, block in TWO_TYPE.items()}
        del config[VOCAB]["sentence_note_type"]
        notes = [Note(1, SENTENCE), Note(2, VOCAB), Note(3, SENTENCE), Note(4, "Kanji draw")]
        ran, given, report = self.run_bulk(config, notes)
        self.assertEqual(ran, [4])
        self.assertEqual(report.call_count, 2)
        for call in report.call_args_list:
            self.assertIn("must name each other", call.args[0])


class JudgePhaseTests(unittest.TestCase):
    def test_the_judge_gets_the_sentence_notes_and_leaves_the_vocab_notes_silently(self):
        judged: list = []

        async def judge(col, notes, **kwargs):
            judged.extend(note.id for note in notes)
            return "judged"

        with mock.patch.object(
            ops, "make_judge_bulk_op", return_value=judge
        ), mock.patch.object(ops, "mw") as mw, mock.patch.object(gate, "report_error") as report:
            mw.addonManager.getConfig.return_value = TWO_TYPE
            phase = ops.judge_sentence_notes_op()
            notes = [Note(1, SENTENCE), Note(2, VOCAB), Note(3, SENTENCE)]
            result = asyncio.run(
                phase(
                    None,
                    notes=notes,
                    edited_nids=[],
                    progress_updater=None,
                    notes_to_add_dict={},
                    notes_to_update_dict={},
                )
            )
        self.assertEqual(result, "judged")
        self.assertEqual(judged, [1, 3])
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
