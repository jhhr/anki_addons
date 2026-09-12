"""Which words of a word array match_words_to_notes gathers to match. Plain data handling."""

import asyncio
import json
import unittest
from unittest import mock

from addon_modules import load_ops_module

match_flags = load_ops_module("match_flags", subdir="word_array")
match_targets = load_ops_module("match_targets", subdir="word_array")
State = match_flags.MatchState


def word(form, match_data=None, subs=None, pos="noun", reading="よみ"):
    return [form, pos, form, reading, match_data if match_data is not None else [], subs or []]


class GatherTargetsTests(unittest.TestCase):
    def test_only_words_judged_match_by_default(self):
        arr = [
            word("一", []),
            word("二", ["dontmatch"]),
            word("三", ["match"]),
            word("四", [1674931277303]),
            word("五", [1674931277303, 4]),
            ["、"],
        ]
        self.assertEqual([t.word for t in match_targets.gather_targets(arr)], ["三"])

    def test_sub_words_at_any_depth_follow_their_parent(self):
        inner = word("様に", ["match"], [word("様", ["match"]), word("に", ["dontmatch"])])
        arr = [
            word("様に成る", ["match"], [inner, word("成る", ["match"], pos="verb")]),
            word("本", ["match"]),
        ]
        targets = match_targets.gather_targets(arr)
        self.assertEqual([t.word for t in targets], ["様に成る", "様に", "様", "成る", "本"])

    def test_a_target_holds_its_element_so_a_result_lands_in_the_array(self):
        sub = word("様", ["match"])
        arr = [word("様に", ["dontmatch"], [sub, word("に", ["dontmatch"])])]
        (target,) = match_targets.gather_targets(arr)
        target.elem[4] = [1674931277303, 5]
        self.assertEqual(arr[0][5][0][4], [1674931277303, 5])

    def test_each_occurrence_of_a_word_is_its_own_target(self):
        arr = [word("為る", ["match"], pos="verb"), word("と"), word("為る", ["match"], pos="verb")]
        targets = match_targets.gather_targets(arr)
        self.assertEqual(len(targets), 2)
        self.assertIsNot(targets[0].elem, targets[1].elem)

    def test_other_states_on_request(self):
        arr = [word("三", ["match"]), word("四", [1674931277303])]
        targets = match_targets.gather_targets(arr, states=[State.LINKED])
        self.assertEqual([t.word for t in targets], ["四"])

    def test_limit_to_words_and_readings(self):
        arr = [word("本", ["match"], reading="ほん"), word("本", ["match"], reading="もと")]
        targets = match_targets.gather_targets(arr, limit=[("本", "もと")])
        self.assertEqual([t.reading for t in targets], ["もと"])

    def test_words_that_cannot_be_matched_are_left_out(self):
        arr = [word("ABC", ["match"]), word("本", ["match"], reading="")]
        self.assertEqual(match_targets.gather_targets(arr), [])

    def test_part_of_speech_as_the_notes_have_it(self):
        arr = [word("走る", ["match"], pos="verb"), word("静か", ["match"], pos="na-adjective")]
        targets = match_targets.gather_targets(arr)
        self.assertEqual([t.part_of_speech for t in targets], ["Verb", "Adjective"])

    def test_unknown_match_data_raises(self):
        with self.assertRaises(ValueError):
            match_targets.gather_targets([word("本", ["maybe"])])


class SaveResultsTests(unittest.TestCase):
    def test_note_ids_land_in_nested_elements(self):
        sub = word("様", ["match"])
        arr = [word("様に", ["match"], [sub, word("に", ["dontmatch"])]), word("本", ["match"])]
        targets = match_targets.gather_targets(arr)
        results = {1: ("様", "さま", "様 (m2)", "-1234567"), 2: None}
        self.assertEqual(match_targets.save_results(targets, results), 1)
        # a new note's placeholder id is saved as an int, like a real one
        self.assertEqual(sub[4], [-1234567])
        self.assertEqual(arr[0][4], ["match"])
        self.assertEqual(arr[1][4], ["match"])


class Progress:
    def __init__(self):
        self.notes_done = 0

    def increment_counts(self, notes_done=0):
        self.notes_done += notes_done

    def update_new_note_processing_progress(self, **_):
        pass


class FakeNote:
    def __init__(self, fields, note_id=1):
        self.id = note_id
        self.fields = fields
        self.tags = []

    def note_type(self):
        return {"name": "Word"}

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def __setitem__(self, field, value):
        self.fields[field] = value

    def add_tag(self, tag):
        self.tags.append(tag)


class WordIndexCache:
    async def get(self, _):
        return None


class MatchWordsToNotesArrayTests(unittest.TestCase):
    """The op reads a word array rather than taking it for a broken old word list."""

    def setUp(self):
        self.mwtn = load_ops_module("match_words_to_notes")
        self.config = {
            "Word": {key: key for key in self.mwtn.MATCH_FIELD_KEYS},
            "match_words_model": "model",
            "word_lists_to_process": {"nouns": True},
        }

    def plan(self, note, arr, progress, updates, edited_nids, config=None):
        return self.mwtn.plan_word_array_matching(
            config=config or self.config,
            note=note,
            arr=arr,
            sentence="本を",
            edited_nids=edited_nids,
            notes_to_add_dict={},
            notes_to_update_dict=updates,
            progress_updater=progress,
            cancel_state=None,
            gate=None,
            all_generated_meanings_dict={},
            word_locks_dict={},
            word_lock=None,
            word_note_index_cache=WordIndexCache(),
            note_cache=None,
            sentence_cache=None,
            limit_words_and_readings=None,
            log_prefix="",
        )

    def test_an_array_note_is_not_taken_for_a_broken_word_list(self):
        arr = [word("本", ["dontmatch"]), word("を", ["dontmatch"])]
        fields = {"furigana_sentence_field": "本を", "word_list_field": json.dumps(arr)}
        note = FakeNote(fields)
        progress = Progress()
        updates = {}
        plan = self.mwtn.match_words_to_notes_for_note(
            config=self.config,
            note=note,
            edited_nids=[],
            notes_to_add_dict={},
            notes_to_update_dict=updates,
            progress_updater=progress,
            cancel_state=None,
            gate=None,
            all_generated_meanings_dict={},
            word_locks_dict={},
            word_lock=None,
            word_note_index_cache=None,
            note_cache=None,
            sentence_cache=None,
        )
        self.assertIsNone(plan)
        self.assertEqual(note.tags, [])
        self.assertEqual(updates, {})
        self.assertEqual(progress.notes_done, 1)

    def test_nothing_to_match_or_no_model_counts_the_note_done(self):
        progress = Progress()
        note = FakeNote({})
        # match_data in no known state is logged, and the note still counted done
        self.assertIsNone(self.plan(note, [word("棚", ["maybe"])], progress, {}, []))
        no_model = {**self.config, "match_words_model": ""}
        self.assertIsNone(self.plan(note, [word("本", ["match"])], progress, {}, [], no_model))
        self.assertEqual(progress.notes_done, 2)

    def test_matched_ids_are_saved_into_the_array_field(self):
        sub = word("様", ["match"], pos="suffix")
        arr = [word("様に", ["dontmatch"], [sub]), word("本", ["match"])]
        note = FakeNote({"word_list_field": json.dumps(arr, ensure_ascii=False)})
        seen = []

        async def match_word(config, word_lock, word_locks_dict, log_prefix, match_op_args):
            args = match_op_args
            seen.append((args["word"], args["part_of_speech"], args["word_list_field"]))
            if args["word"] == "様":
                results = args["processed_word_tuples"]
                results[args["word_index"]] = ("様", "よみ", "様", -1234567)
            return True

        def inner_bulk_op(config, op, **_):
            async def process(**op_args):
                return await op(config, **op_args)

            return process

        progress = Progress()
        updates = {}
        edited_nids = []
        plan = self.plan(note, arr, progress, updates, edited_nids)
        self.assertEqual(plan.task_count, 2)

        async def run():
            tasks = []
            plan.spawn(tasks)
            await asyncio.gather(*tasks)

        with (
            mock.patch.object(self.mwtn, "match_single_word_in_word_tuple", match_word),
            mock.patch.object(self.mwtn, "make_inner_bulk_op", inner_bulk_op),
        ):
            asyncio.run(run())

        self.assertEqual(
            seen, [("様", "Suffix", "word_list_field"), ("本", "Noun", "word_list_field")]
        )
        saved = json.loads(note["word_list_field"])
        self.assertEqual(saved[0][5][0][4], [-1234567])
        self.assertEqual(saved[1][4], ["match"])
        self.assertEqual(updates, {1: note})
        self.assertEqual(edited_nids, [1])
        self.assertEqual(progress.notes_done, 1)

    def test_a_new_notes_placeholder_id_is_replaced_in_an_array_field(self):
        arr = [word("様", [-1234567]), word("本", [1674931277303, 4])]
        fields = {"word_list_field": json.dumps(arr), "new_note_id_field": ""}
        referencing = FakeNote(fields, note_id=2)
        new_note = FakeNote({"word_list_field": "", "new_note_id_field": "-1234567"}, 99)
        with (
            mock.patch.object(self.mwtn, "col_find_notes", lambda _: [2]),
            mock.patch.object(self.mwtn, "col_get_notes", lambda _: [referencing]),
        ):
            updated = self.mwtn.update_fake_note_ids([new_note], self.config, Progress())
        saved = json.loads(referencing["word_list_field"])
        self.assertEqual([saved[0][4], saved[1][4]], [[99], [1674931277303, 4]])
        self.assertEqual(set(updated), {2, 99})


if __name__ == "__main__":
    unittest.main()
