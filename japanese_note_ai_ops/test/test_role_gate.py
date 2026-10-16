"""The role gate the ops' bulk functions pass their notes through (async_api_ops/role_gate.py).

In the two-type layout an op run over notes of the other role used to fail per note, each with
its traceback, or fail the whole match run as it planned. A selection holding both types is
ordinary (a chain's search, "added today"), so the gate keeps the notes of the op's role and
reports the rest as one run error per note type. Outside the two-type layout it keeps every
note and says nothing, so the one-type layout runs exactly as before.
"""

from __future__ import annotations

import unittest
from unittest import mock

from addon_modules import load_ops_module

gate = load_ops_module("role_gate")
roles = load_ops_module("note_roles", subdir="")

VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"

TWO_TYPE = {
    SENTENCE: {"vocab_note_type": VOCAB, "word_list_field": "Words"},
    VOCAB: {"sentence_note_type": SENTENCE, "word_sort_field": "Sort"},
    "Kanji draw": {"story_field": "Story"},
}
ONE_TYPE = {VOCAB: {"word_list_field": "Words", "word_sort_field": "Sort"}}


class Note:
    def __init__(self, note_id: int, type_name: str) -> None:
        self.id = note_id
        self.type_name = type_name

    def note_type(self):
        return {"name": self.type_name} if self.type_name else None


def mixed_notes() -> list:
    return [
        Note(1, SENTENCE),
        Note(2, VOCAB),
        Note(3, SENTENCE),
        Note(4, VOCAB),
        Note(5, "Kanji draw"),
        Note(6, VOCAB),
        # A note whose type Anki cannot find goes on to the op, to fail there as it always did
        Note(7, ""),
    ]


class RoleGateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(gate, "report_error")
        self.report = patcher.start()
        self.addCleanup(patcher.stop)

    def ids(self, notes):
        return [note.id for note in notes]

    def test_a_sentence_op_keeps_the_sentence_notes_and_reports_the_vocab_type_once(self):
        kept = gate.notes_of_role(TWO_TYPE, mixed_notes(), roles.SENTENCE_ROLE)
        self.assertEqual(self.ids(kept), [1, 3, 5, 7])
        self.assertEqual(
            [call.args for call in self.report.call_args_list],
            [
                (
                    f'{roles.role_error(TWO_TYPE, VOCAB, roles.SENTENCE_ROLE)} Left out of this'
                    " run: 3 notes of it.",
                    f'Notes of "{VOCAB}"',
                )
            ],
        )
        self.assertIn("runs on sentence notes", self.report.call_args.args[0])

    def test_a_vocab_op_keeps_the_vocab_notes(self):
        kept = gate.notes_of_role(TWO_TYPE, mixed_notes(), roles.VOCAB_ROLE)
        self.assertEqual(self.ids(kept), [2, 4, 5, 6, 7])
        [(text, where)] = [call.args for call in self.report.call_args_list]
        self.assertTrue(text.endswith("Left out of this run: 2 notes of it."))
        self.assertIn(f'"{SENTENCE}" is the sentence note type', text)
        self.assertEqual(where, f'Notes of "{SENTENCE}"')

    def test_one_note_left_out_is_one_note(self):
        gate.notes_of_role(TWO_TYPE, [Note(1, SENTENCE)], roles.VOCAB_ROLE)
        self.assertTrue(self.report.call_args.args[0].endswith("1 note of it."))

    def test_the_one_type_layout_keeps_every_note_and_says_nothing(self):
        notes = [Note(1, VOCAB), Note(2, "Kanji draw"), Note(3, "Basic")]
        for role in roles.ROLES:
            with self.subTest(role=role):
                self.assertEqual(self.ids(gate.notes_of_role(ONE_TYPE, notes, role)), [1, 2, 3])
        self.report.assert_not_called()

    def test_without_report_the_notes_are_left_out_silently(self):
        kept = gate.notes_of_role(TWO_TYPE, mixed_notes(), roles.SENTENCE_ROLE, report=False)
        self.assertEqual(self.ids(kept), [1, 3, 5, 7])
        self.report.assert_not_called()

    def test_each_type_is_asked_once(self):
        refusal = mock.Mock(side_effect=lambda name: "no" if name == VOCAB else None)
        gate.notes_passing(mixed_notes(), refusal)
        self.assertEqual(
            sorted(call.args[0] for call in refusal.call_args_list),
            sorted([SENTENCE, VOCAB, "Kanji draw"]),
        )


if __name__ == "__main__":
    unittest.main()
