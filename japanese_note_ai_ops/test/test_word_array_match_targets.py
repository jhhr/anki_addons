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


class StatesToMatchTests(unittest.TestCase):
    def test_a_run_matches_judged_words_and_linked_ones_when_replacing(self):
        self.assertEqual(match_targets.states_to_match(), {State.MATCH})
        self.assertEqual(
            match_targets.states_to_match(replace_existing=True),
            {State.MATCH, State.LINKED, State.RATED},
        )

    def test_a_single_word_run_takes_its_modes_states(self):
        states = match_targets.states_to_match
        self.assertEqual(states(True, "only_unprocessed"), {State.MATCH})
        self.assertEqual(states(reprocess="only_processed"), {State.LINKED, State.RATED})
        self.assertEqual(states(reprocess="both"), {State.MATCH, State.LINKED, State.RATED})


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

    def test_a_match_quality_is_saved_after_the_note_id(self):
        arr = [word("様", ["match"]), word("本", ["match"])]
        targets = match_targets.gather_targets(arr)
        results = {0: ("様", "さま", "様", 111), 1: ("本", "ほん", "本", 222)}
        self.assertEqual(match_targets.save_results(targets, results, {0: 4}), 2)
        self.assertEqual((arr[0][4], arr[1][4]), ([111, 4], [222]))


class RatingTests(unittest.TestCase):
    def test_linked_words_are_rated_unless_matched_again(self):
        self.assertEqual(match_targets.states_to_rate({State.MATCH}), {State.LINKED})
        self.assertEqual(match_targets.states_to_rate({State.LINKED, State.RATED}), set())

    def test_a_rating_is_saved_after_the_note_id(self):
        arr = [word("様", [111]), word("本", [222]), word("棚", [333])]
        targets = match_targets.gather_targets(arr, states=[State.LINKED])
        self.assertEqual(match_targets.save_ratings(targets, {0: 2, 2: 5}), 2)
        self.assertEqual([w[4] for w in arr], [[111, 2], [222], [333, 5]])

    def test_the_rating_read_from_a_response(self):
        read = match_targets.rating_from_response
        self.assertEqual(read({"match_quality": 3}), 3)
        self.assertEqual(read([{"match_quality": "5"}]), 5)
        for bad in (None, [], {"match_quality": 9}, {}, "3"):
            self.assertIsNone(read(bad))


class MatchQualityTests(unittest.TestCase):
    def test_only_a_whole_number_from_one_to_five(self):
        parse = match_targets.parse_match_quality
        self.assertEqual([parse(v) for v in (1, 5, 3.0, " 4", "2")], [1, 5, 3, 4, 2])
        for bad in (0, 6, 2.5, "x", "", None, True, [3]):
            self.assertIsNone(parse(bad))


class HighlightedSentenceTests(unittest.TestCase):
    def test_the_very_occurrence_is_marked(self):
        first, second = word("為る", pos="verb"), word("為る", pos="verb")
        arr = [first, word("と"), second, ["。"]]
        self.assertEqual(match_targets.highlighted_sentence(arr, second), "為ると<b>為る</b>。")
        sub = word("様", ["match"])
        arr = [word("様に", [], [sub, word("に")]), word("本")]
        self.assertEqual(match_targets.highlighted_sentence(arr, sub), "<b>様</b>に本")
        self.assertIsNone(match_targets.highlighted_sentence(arr, word("本")))

    def test_an_example_sentence_marks_the_notes_own_word(self):
        example = match_targets.example_sentence
        arr = [word("本", [222]), word("と"), word("本", [111], reading="ほん"), ["。"]]
        text = json.dumps(arr, ensure_ascii=False)
        # the occurrence linked to the note, then the word and reading, then the word
        self.assertEqual(example("field", text, "本", "ほん", 111), "本と<b>本</b>。")
        self.assertEqual(example("field", text, "本", "ほん", 333), "本と<b>本</b>。")
        self.assertEqual(example("field", text, "本", "もと", 333), "<b>本</b>と本。")
        # no array, or the word not in it: the field as it is
        self.assertEqual(example("<b>本</b>", "", "本", "ほん", 111), "<b>本</b>")
        self.assertEqual(example("field", text, "棚", "たな", 333), "field")
        self.assertEqual(example("field", '{"nouns": []}', "本", "ほん", 111), "field")


class ResolvePlaceholderIdsTests(unittest.TestCase):
    def test_placeholders_take_the_added_notes_id(self):
        added = word("様", [-111, 4])
        again = word("様", [-111])
        never_added = word("本", [-222])
        ambiguous = word("棚", [-333])
        real = word("為る", [444], pos="verb")
        arr = [word("様に", ["dontmatch"], [added, word("に", ["dontmatch"])]), again]
        arr += [never_added, ambiguous, real]
        holders = {-111: [55], -222: [], -333: [66, 77]}
        asked = []

        def find_notes(fake_id):
            asked.append(fake_id)
            return holders[fake_id]

        self.assertTrue(match_targets.has_placeholder_ids(arr))
        self.assertEqual(match_targets.resolve_placeholder_ids(arr, find_notes), 3)
        self.assertEqual(
            [e[4] for e in (added, again, never_added, ambiguous, real)],
            [[55, 4], [55], ["match"], [-333], [444]],
        )
        self.assertEqual(asked, [-111, -222, -333])
        self.assertFalse(match_targets.has_placeholder_ids([real, word("本", ["match"])]))


class UnlinkMissingNotesTests(unittest.TestCase):
    def test_words_linked_to_missing_notes_are_set_to_be_rematched(self):
        gone = word("様", [111, 4])
        kept = word("本", [222])
        placeholder = word("為る", [-1234567], pos="verb")
        arr = [word("様に", ["dontmatch"], [gone, word("に", ["dontmatch"])]), kept, placeholder]
        asked = []

        def exists(nid):
            asked.append(nid)
            return nid == 222

        self.assertEqual(match_targets.unlink_missing_notes(arr, exists), [111])
        self.assertEqual((gone[4], kept[4], placeholder[4]), (["match"], [222], [-1234567]))
        # a placeholder id is never looked up
        self.assertEqual(asked, [111, 222])


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

    def plan(
        self, note, arr, progress, updates, edited_nids, config=None, note_cache=None, **states
    ):
        return self.mwtn.plan_word_array_matching(
            **states,
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
            note_cache=note_cache,
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
            seen.append((args["word"], args["part_of_speech"], args["prompt_sentence"]))
            if args["word"] == "様":
                results = args["processed_word_tuples"]
                results[args["word_index"]] = ("様", "よみ", "様", -1234567)
                args["match_qualities"][args["word_index"]] = 3
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

        # each word's prompt has its own occurrence in <b>
        self.assertEqual(seen, [("様", "Suffix", "<b>様</b>本"), ("本", "Noun", "様に<b>本</b>")])
        saved = json.loads(note["word_list_field"])
        self.assertEqual(saved[0][5][0][4], [-1234567, 3])
        self.assertEqual(saved[1][4], ["match"])
        self.assertEqual(updates, {1: note})
        self.assertEqual(edited_nids, [1])
        self.assertEqual(progress.notes_done, 1)

    def planned_states(self, config, **single_word):
        arr = [word("本", ["match"])]
        fields = {"furigana_sentence_field": "本", "word_list_field": json.dumps(arr)}
        plans = []
        with mock.patch.object(
            self.mwtn, "plan_word_array_matching", lambda **kwargs: plans.append(kwargs)
        ):
            self.mwtn.match_words_to_notes_for_note(
                config=config,
                note=FakeNote(fields),
                edited_nids=[],
                notes_to_add_dict={},
                notes_to_update_dict={},
                progress_updater=Progress(),
                cancel_state=None,
                gate=None,
                all_generated_meanings_dict={},
                word_locks_dict={},
                word_lock=None,
                word_note_index_cache=None,
                note_cache=None,
                sentence_cache=None,
                **single_word,
            )
        return plans[0]["states"]

    def test_replacing_existing_matches_takes_the_linked_words_too(self):
        self.assertEqual(self.planned_states(self.config), {State.MATCH})
        replace = {**self.config, "replace_existing_matched_words": True}
        self.assertEqual(self.planned_states(replace), {State.MATCH, State.LINKED, State.RATED})
        # a single-word run's mode wins over the config
        mode = {"limit_words_and_readings": [("本", "よみ")], "reprocess_words": "only_unprocessed"}
        self.assertEqual(self.planned_states(replace, **mode), {State.MATCH})

    def test_linked_words_without_a_quality_are_rated(self):
        arr = [
            word("本", [111], reading="ほん"),
            word("と", ["dontmatch"]),
            word("棚", [222]),
            word("様", [333, 5]),
            word("箱", [444]),
        ]
        note = FakeNote({"word_list_field": json.dumps(arr, ensure_ascii=False)})
        meanings = {
            111: FakeNote({"meaning_field": "書物", "english_meaning_field": "book"}, 111),
            222: FakeNote({"meaning_field": "", "english_meaning_field": ""}, 222),
        }

        class Cache:
            async def get_notes(self, ids):
                return {i: meanings[i] for i in ids if i in meanings}

        prompts = []

        def get_response(model, prompt, **kwargs):
            prompts.append((model, prompt, kwargs["instructions"]))
            return {"match_quality": 4}

        def inner_bulk_op(config, op, **_):
            async def process(**op_args):
                return await op(config, **op_args)

            return process

        updates, edited_nids = {}, []
        plan = self.plan(note, arr, Progress(), updates, edited_nids, note_cache=Cache())
        # 本, 棚 and 箱 rated; 様 already has its quality
        self.assertEqual(plan.task_count, 3)

        async def run():
            tasks = []
            plan.spawn(tasks)
            await asyncio.gather(*tasks)

        with (
            mock.patch.object(self.mwtn, "get_response", get_response),
            mock.patch.object(self.mwtn, "make_inner_bulk_op", inner_bulk_op),
        ):
            asyncio.run(run())

        # only 本 had a meaning to rate: 棚's note has none, 箱's is not found
        (prompt,) = prompts
        self.assertEqual(prompt[0], "model")
        self.assertIn("書物", prompt[1])
        self.assertIn("<b>本</b>と棚様箱", prompt[1])
        self.assertEqual(prompt[2], match_targets.RATING_INSTRUCTIONS)
        saved = json.loads(note["word_list_field"])
        self.assertEqual([w[4] for w in saved], [[111, 4], ["dontmatch"], [222], [333, 5], [444]])
        self.assertEqual(edited_nids, [1])

    def test_words_matched_again_are_not_rated_too(self):
        arr = [word("本", ["match"]), word("様", [111])]
        note = FakeNote({"word_list_field": json.dumps(arr, ensure_ascii=False)})
        states = [State.MATCH, State.LINKED, State.RATED]
        self.assertEqual(self.plan(note, arr, Progress(), {}, [], states=states).task_count, 2)
        self.assertEqual(self.plan(note, arr, Progress(), {}, []).task_count, 2)
        only_match = self.plan(note, [word("本", ["match"])], Progress(), {}, [])
        self.assertEqual(only_match.task_count, 1)

    def test_the_planned_states_are_the_ones_gathered(self):
        arr = [word("本", ["match"]), word("様", [111]), word("棚", [222, 4]), word("を", [])]
        note = FakeNote({"word_list_field": json.dumps(arr, ensure_ascii=False)})
        plan = self.plan(note, arr, Progress(), {}, [], states=[State.LINKED, State.RATED])
        self.assertEqual(plan.task_count, 2)

    def test_placeholders_left_by_an_earlier_run_are_resolved_before_gathering(self):
        arr = [word("様", [-111]), word("本", [-222, 4]), word("棚", [-333])]
        fields = {"word_list_field": json.dumps(arr), "new_note_id_field": "-111"}
        note = FakeNote(fields)
        queries = []

        def find_notes(query):
            queries.append(query)
            return [55] if "-222" in query else []

        updates, edited_nids = {}, []
        with mock.patch.object(self.mwtn, "col_find_notes", find_notes):
            plan = self.plan(note, arr, Progress(), updates, edited_nids)
        # the note itself holds -111, -222 was added as note 55, -333 never was: matched again
        saved = json.loads(note["word_list_field"])
        self.assertEqual([w[4] for w in saved], [[1], [55, 4], ["match"]])
        self.assertEqual(len(queries), 2)
        # 棚 matched, 様 now linked without a quality: rated
        self.assertEqual(plan.task_count, 2)
        self.assertEqual((updates, edited_nids), ({1: note}, [1]))

    def test_a_new_notes_placeholder_id_is_replaced_in_an_array_field(self):
        arr = [word("様", [-1234567, 3]), word("本", [1674931277303, 4])]
        fields = {"word_list_field": json.dumps(arr), "new_note_id_field": ""}
        referencing = FakeNote(fields, note_id=2)
        new_note = FakeNote({"word_list_field": "", "new_note_id_field": "-1234567"}, 99)
        with (
            mock.patch.object(self.mwtn, "col_find_notes", lambda _: [2]),
            mock.patch.object(self.mwtn, "col_get_notes", lambda _: [referencing]),
        ):
            updated = self.mwtn.update_fake_note_ids([new_note], self.config, Progress())
        saved = json.loads(referencing["word_list_field"])
        self.assertEqual([saved[0][4], saved[1][4]], [[99, 3], [1674931277303, 4]])
        self.assertEqual(set(updated), {2, 99})
        # and the note is left holding its own id, which is what the card reads to know
        # which word of a sentence is its own
        self.assertEqual(new_note["new_note_id_field"], "99")

    def test_a_new_note_nothing_refers_to_still_gets_its_own_id(self):
        """It used to keep its placeholder for good: the id was only written where a
        reference had been rewritten, and nothing had referred to this note."""
        new_note = FakeNote({"word_list_field": "", "new_note_id_field": "-7654321"}, 42)
        with (
            mock.patch.object(self.mwtn, "col_find_notes", lambda _: []),
            mock.patch.object(self.mwtn, "col_get_notes", lambda _: []),
        ):
            updated = self.mwtn.update_fake_note_ids([new_note], self.config, Progress())
        self.assertEqual(new_note["new_note_id_field"], "42")
        self.assertEqual(set(updated), {42})

    def test_a_note_already_holding_its_own_id_is_not_searched_for(self):
        """The field means 'this is me' once it is not a placeholder, so there is nothing to
        rewrite and no reason to go looking."""
        searched = []
        new_note = FakeNote({"word_list_field": "", "new_note_id_field": "42"}, 42)
        with (
            mock.patch.object(
                self.mwtn, "col_find_notes", lambda query: searched.append(query) or []
            ),
            mock.patch.object(self.mwtn, "col_get_notes", lambda _: []),
        ):
            self.mwtn.update_fake_note_ids([new_note], self.config, Progress())
        self.assertEqual(searched, [])
        self.assertEqual(new_note["new_note_id_field"], "42")


if __name__ == "__main__":
    unittest.main()
