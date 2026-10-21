"""Run all ops for new notes (async_api_ops/new_note_all_ops.py) under the two note layouts.

In the two-type layout the new notes of a day are sentence notes added by hand and vocab notes
the match op made, one selection: each note gets the steps of its type's role (meanings and
their cleaning for a vocab note, kanjify and extract for a sentence note, the judge on sentence
notes only), never the other role's, which used to fail per note on fields its type does not
have. In the one-type layout every note gets every step, as before.

Where a sentence type keeps transcripts, its notes' furigana is set from their audio first, in
phases of the transcription and furigana ops of their own, over the notes with a clip and no
furigana yet; a note whose furigana awaits a person's check gets no kanjify or extract.
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
    def __init__(self, note_id: int, type_name: str, fields=None, tags=()) -> None:
        self.id = note_id
        self.type_name = type_name
        self.fields = dict(fields or {})
        self.tags = list(tags)

    def note_type(self):
        return {"name": self.type_name}

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]


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

    def test_a_sentence_note_awaiting_a_furigana_check_gets_neither_said_once(self):
        config = {SENTENCE: {"furigana_sentence_field": "Furigana"}}
        tagged = Note(1, SENTENCE, {"Furigana": " 他[ほか]の"}, tags=["furigana-review"])
        marked = Note(2, SENTENCE, {"Furigana": " 他[ほか？た]の"})
        for note in (tagged, marked):
            with mock.patch.object(ops, "report_error") as report:
                ops.new_note_all_ops_in_note(
                    config, note, {}, {}, set(), {}, self.extract, roles=(roles.SENTENCE_ROLE,)
                )
            self.assertEqual(self.calls, [])
            report.assert_called_once()


class BulkOpRolesTests(unittest.TestCase):
    """The bulk op hands each note the roles of its type, and leaves out a broken layout's."""

    def run_bulk(self, config, notes):
        given_roles: dict[int, tuple] = {}
        self.undo_label = None

        def in_note(config, note, *args):
            given_roles[note.id] = args[-1]
            return False

        async def bulk_notes_op(message, config, op, col, notes, *args, **kwargs):
            self.undo_label = kwargs.get("undo_label")
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
        # The run's undo entry counts the selection, as the audio phases do when they come first
        self.assertEqual(self.undo_label, "Running new note all ops for 4 notes.")


FURIGANA_TYPE = {
    SENTENCE: {
        **TWO_TYPE[SENTENCE],
        "sentence_field": "Sentence",
        "furigana_sentence_field": "Furigana",
        "sentence_audio_field": "Audio",
        "sentence_transcription_field": "Heard",
    },
    VOCAB: TWO_TYPE[VOCAB],
}


def sentence(note_id, furigana="", audio="[sound:clip.opus]", sentence="他の人は"):
    fields = {"Sentence": sentence, "Furigana": furigana, "Audio": audio, "Heard": ""}
    return Note(note_id, SENTENCE, fields)


class AudioFuriganaPhaseTests(unittest.TestCase):
    def names(self, config):
        return [phase.name for phase in ops.new_note_phases(config)]

    def test_a_type_keeping_transcripts_has_its_furigana_set_from_audio_first(self):
        self.assertEqual(
            self.names(FURIGANA_TYPE),
            [
                "Transcribing sentence audio",
                "Setting furigana from audio",
                "New note ops",
                "Judging words matchability",
            ],
        )

    def test_the_capture_runs_row_names_each_audio_phase_by_its_op(self):
        base_ops = load_ops_module("base_ops")
        self.assertEqual(
            base_ops._op_names(ops.new_note_phases(FURIGANA_TYPE)),
            [
                "bulk_transcribe_notes_op",
                "bulk_set_furigana_op",
                "bulk_new_note_all_ops",
                "bulk_judge_sentence_notes_op",
            ],
        )

    def test_without_transcripts_the_run_is_as_before(self):
        self.assertEqual(self.names(TWO_TYPE), ["New note ops", "Judging words matchability"])
        # A transcription field named by a list in a hand-edited config is none: it used to
        # fail the run before its first note
        listed = {**FURIGANA_TYPE[SENTENCE], "sentence_transcription_field": ["Heard"]}
        config = {**FURIGANA_TYPE, SENTENCE: listed}
        self.assertEqual(self.names(config), ["New note ops", "Judging words matchability"])
        # Transcripts kept on a vocab type are no sentence type's
        config = {**TWO_TYPE, VOCAB: {**TWO_TYPE[VOCAB], "sentence_transcription_field": "x"}}
        self.assertEqual(self.names(config), ["New note ops", "Judging words matchability"])

    def test_the_notes_with_a_clip_and_no_furigana_yet_and_the_runs_undo_name(self):
        given: dict = {}

        async def bulk_op(col, notes, undo_label=None, **kwargs):
            given.update(notes=[note.id for note in notes], undo_label=undo_label, **kwargs)
            return "ran"

        notes = [
            sentence(1),
            sentence(2, furigana=" 他[ほか]の"),
            sentence(3, audio=""),
            sentence(4, sentence=""),
            Note(5, VOCAB),
        ]
        phase = ops.audio_furigana_phase("Transcribing sentence audio", bulk_op)
        with mock.patch.object(ops, "mw") as mw, mock.patch.object(gate, "report_error") as report:
            mw.addonManager.getConfig.return_value = FURIGANA_TYPE
            result = asyncio.run(phase.bulk_op(None, notes=notes, edited_nids=[]))
        self.assertEqual(result, "ran")
        undo_label = "Running new note all ops for 5 notes."
        self.assertEqual(given, {"notes": [1], "undo_label": undo_label, "edited_nids": []})
        report.assert_not_called()


    def test_the_furigana_phase_leaves_out_quietly_a_note_still_untranscribed(self):
        # The transcription phase could not hear the second note's clip (the server down) and
        # said so once: told to "run Transcribe sentence audio first", it would hear it again
        given: dict = {}

        def recorder(name):
            async def bulk_op(col, notes, undo_label=None, **kwargs):
                given[name] = [note.id for note in notes]
                return "ran"

            return bulk_op

        heard = sentence(1)
        heard.fields["Heard"] = "kana-anime-whisper: たのひとわ<br>ruby-mora: たのひとわ"
        notes = [heard, sentence(2)]
        with mock.patch.multiple(
            ops,
            mw=mock.DEFAULT,
            bulk_transcribe_notes_op=recorder("transcribe"),
            bulk_set_furigana_op=recorder("furigana"),
        ) as patched:
            patched["mw"].addonManager.getConfig.return_value = FURIGANA_TYPE
            for phase in ops.new_note_phases(FURIGANA_TYPE)[:2]:
                asyncio.run(phase.bulk_op(None, notes=notes, edited_nids=[]))
        self.assertEqual(given, {"transcribe": [1, 2], "furigana": [1]})


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
