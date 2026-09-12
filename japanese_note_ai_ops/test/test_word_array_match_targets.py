"""Which words of a word array match_words_to_notes gathers to match. Plain data handling."""

import json
import unittest

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


class Progress:
    def __init__(self):
        self.notes_done = 0

    def increment_counts(self, notes_done=0):
        self.notes_done += notes_done


class FakeNote:
    id = 1

    def __init__(self, fields):
        self.fields = fields
        self.tags = []

    def note_type(self):
        return {"name": "Word"}

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def add_tag(self, tag):
        self.tags.append(tag)


class MatchWordsToNotesArrayTests(unittest.TestCase):
    """The op reads a word array rather than taking it for a broken old word list."""

    def setUp(self):
        self.mwtn = load_ops_module("match_words_to_notes")

    def test_an_array_note_is_gathered_and_counted_done_without_tasks(self):
        arr = [word("本", ["match"]), word("を", ["dontmatch"])]
        note = FakeNote({"Sentence": "本を", "Words": json.dumps(arr, ensure_ascii=False)})
        config = {
            "Word": {"furigana_sentence_field": "Sentence", "word_list_field": "Words"},
            "word_lists_to_process": {"nouns": True},
        }
        progress = Progress()
        updates = {}
        plan = self.mwtn.match_words_to_notes_for_note(
            config=config,
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

    def test_plan_gathers_the_words_to_match(self):
        arr = [word("本", ["match"]), word("棚", ["maybe"])]
        progress = Progress()
        targets = self.mwtn.plan_word_array_matching(None, arr[:1], progress, None, "")
        self.assertEqual([t.word for t in targets], ["本"])
        # match_data in no known state is logged, and the note still counted done
        self.assertEqual(self.mwtn.plan_word_array_matching(None, arr, progress, None, ""), [])
        self.assertEqual(progress.notes_done, 2)


if __name__ == "__main__":
    unittest.main()
