"""Merging a regenerated word array into the one a note already holds.

Plain data handling: no SudachiPy, JMdict or network, so these run wherever the suite does.
The arrays here are written by hand rather than generated, which is what lets a case say
exactly what the generator produced for the corrected sentence.
"""

import unittest
from unittest import mock

from addon_modules import load_ops_module

merge = load_ops_module("merge", subdir="word_array")


def word(text="X", pos="noun", form="X", reading="x", match_data=None, subs=None):
    return [text, pos, form, reading, match_data if match_data is not None else [], subs or []]


def particle(text):
    return [text, "particle", text, text, ["dontmatch"], []]


# The sentence of the case this was built for, before and after ` 小[しょう] 枝[えだ]` was
# corrected to ` 小枝[こえだ]`. Only the rows around the correction differ.
def old_array():
    return [
        word(" 柊[ひいらぎ]", form="柊", reading="ひいらぎ", match_data=[1378555077520]),
        particle("の"),
        word(" 小[しょう] 枝[えだ]", form="小枝", reading="しょうえだ", match_data=["dontmatch"]),
        particle("が"),
        ["。"],
    ]


def corrected_array():
    return [
        word(" 柊[ひいらぎ]", form="柊", reading="ひいらぎ"),
        particle("の"),
        word(" 小枝[こえだ]", form="小枝", reading="こえだ"),
        particle("が"),
        ["。"],
    ]


def raw(arr):
    return "".join(elem[0] for elem in arr)


class UnchangedRowsTests(unittest.TestCase):
    def test_a_regeneration_that_changes_nothing_keeps_every_element(self):
        old = old_array()

        result = merge.merge_arrays(old, old_array())

        self.assertTrue(result.unchanged)
        self.assertEqual(result.kept, len(old))
        self.assertEqual(result.changed, 0)
        self.assertEqual(result.array, old)

    def test_the_old_elements_are_kept_whole_where_nothing_changed(self):
        old = old_array()

        result = merge.merge_arrays(old, corrected_array())

        # The note id of 柊 and the judgement on の are on elements the correction never reached
        self.assertIs(result.array[0], old[0])
        self.assertIs(result.array[1], old[1])
        self.assertEqual(result.array[0][4], [1378555077520])
        self.assertFalse(result.unchanged)

    def test_the_corrected_row_is_taken_from_the_new_array(self):
        result = merge.merge_arrays(old_array(), corrected_array())

        self.assertEqual(result.array[2][0], " 小枝[こえだ]")
        self.assertEqual(result.array[2][3], "こえだ")
        self.assertEqual(result.changed, 1)
        self.assertEqual(result.kept, 4)

    def test_the_merged_array_reconstructs_the_corrected_sentence(self):
        result = merge.merge_arrays(old_array(), corrected_array())

        self.assertEqual(raw(result.array), raw(corrected_array()))

    def test_a_row_the_generator_reads_differently_is_a_changed_row(self):
        # Same raw text, new reading: keeping the old element would leave the field
        # disagreeing with the generator, which is the state the op exists to get out of.
        old = [word("ある", pos="verb", form="有る", reading="ある", match_data=["match"])]
        new = [word("ある", pos="auxiliary", form="有る", reading="ある")]

        result = merge.merge_arrays(old, new)

        self.assertFalse(result.unchanged)
        self.assertEqual(result.array[0][1], "auxiliary")

    def test_a_row_whose_sub_words_changed_is_a_changed_row(self):
        subs = [word("小", form="小", reading="こ"), word("枝", form="枝", reading="えだ")]
        old = [word(" 小枝[こえだ]", form="小枝", reading="こえだ")]
        new = [word(" 小枝[こえだ]", form="小枝", reading="こえだ", subs=subs)]

        result = merge.merge_arrays(old, new)

        self.assertFalse(result.unchanged)
        self.assertEqual(result.array[0][5], subs)


class CarryingJudgementsTests(unittest.TestCase):
    def test_a_word_that_survived_the_correction_keeps_its_judgement(self):
        old = [word(" 小枝[こえだ]", form="小枝", reading="こえだ", match_data=["match"])]
        new = [word("小枝[こえだ]", form="小枝", reading="こえだ")]

        result = merge.merge_arrays(old, new)

        self.assertEqual(result.carried, 1)
        self.assertEqual(result.array[0][4], ["match"])
        self.assertEqual(result.lost, [])

    def test_the_word_the_correction_was_for_keeps_its_link_by_dictionary_form(self):
        # The case this was built for: the reading is what the correction changed, so only
        # the second step - the dictionary form alone - still recognises the word.
        old = [
            word(
                " 小[しょう] 枝[えだ]", form="小枝", reading="しょうえだ", match_data=[1378555077520]
            )
        ]
        new = [word(" 小枝[こえだ]", form="小枝", reading="こえだ")]

        result = merge.merge_arrays(old, new)

        self.assertEqual(result.carried, 1)
        self.assertEqual(result.array[0][4], [1378555077520])
        self.assertEqual(result.lost, [])

    def test_the_reading_step_runs_before_the_dictionary_form_one(self):
        # 同/どう is the same word on both sides and takes its own judgement; 同/おなじ has
        # only the dictionary form to go on and takes what is left.
        old = [
            word("A", form="同", reading="どう", match_data=[111]),
            word("B", form="同", reading="おなじ", match_data=[222]),
        ]
        new = [
            word("C", form="同", reading="どう"),
            word("D", form="同", reading="おなじく"),
        ]

        result = merge.merge_arrays(old, new)

        self.assertEqual([elem[4] for elem in result.array], [[111], [222]])
        self.assertEqual(result.lost, [])

    def test_a_note_id_is_carried_and_the_old_data_is_not_shared_with_it(self):
        data = [1378555077520, 3]
        old = [word("ある", pos="verb", form="有る", reading="ある", match_data=data)]
        new = [word("ある", pos="auxiliary", form="有る", reading="ある")]

        result = merge.merge_arrays(old, new)

        self.assertEqual(result.array[0][4], [1378555077520, 3])
        self.assertIsNot(result.array[0][4], data)
        self.assertEqual(result.lost, [])

    def test_a_sub_word_is_carried_like_any_other_element(self):
        old = [
            word(
                " 小枝[こえだ]",
                form="小枝",
                reading="こえだ",
                subs=[word("枝", form="枝", reading="えだ", match_data=[1378555077520])],
            )
        ]
        new = [
            word(
                " 小枝[こえだ]",
                form="小枝",
                reading="こえだ",
                subs=[
                    word("小", form="小", reading="こ"),
                    word("枝", form="枝", reading="えだ"),
                ],
            )
        ]

        result = merge.merge_arrays(old, new)

        self.assertEqual(result.array[0][5][1][4], [1378555077520])
        self.assertEqual(result.lost, [])

    def test_two_candidates_for_one_key_are_left_to_the_judge(self):
        # Neither step can tell the two apart, and a coin toss here cannot be undone:
        # match_words_to_notes can find the note again from the sentence, with <b> marking
        # which occurrence it is.
        old = [
            word("A", form="同", reading="どう", match_data=[1378555077520]),
            word("B", form="同", reading="どう", match_data=["match"]),
        ]
        new = [word("C", form="同", reading="どう")]

        result = merge.merge_arrays(old, new)

        self.assertEqual(result.carried, 0)
        self.assertEqual(result.array[0][4], [])
        self.assertEqual(result.lost, [1378555077520])

    def test_an_unjudged_word_carries_nothing_and_loses_nothing(self):
        old = [word("A", form="小枝", reading="こえだ")]
        new = [word("B", form="小枝", reading="こえだ")]

        result = merge.merge_arrays(old, new)

        self.assertEqual(result.carried, 0)
        self.assertEqual(result.lost, [])

    def test_a_word_the_correction_removed_reports_its_note_id(self):
        old = old_array()
        old[2][4] = [1378555077520]
        new = [elem for elem in corrected_array() if elem[0] != " 小枝[こえだ]"]

        result = merge.merge_arrays(old, new)

        self.assertEqual(result.lost, [1378555077520])
        self.assertEqual(raw(result.array), raw(new))


class DiffAnchoringTests(unittest.TestCase):
    def test_a_long_sentence_of_repeated_particles_still_anchors_the_diff(self):
        # Above 200 elements difflib's autojunk drops from the matching every element that
        # appears in more than 1% of the sequence - in a sentence, exactly the particles the
        # diff needs as anchors. merge_arrays turns it off.
        old = []
        for n in range(150):
            old.append(word(f"W{n}", form=f"F{n}", reading=f"r{n}", match_data=["match"]))
            old.append(particle("の"))
        new = [elem for elem in old]
        new[200] = word("W100x", form="F100x", reading="r100x")

        result = merge.merge_arrays(old, new)

        self.assertEqual(result.changed, 1)
        self.assertEqual(result.kept, len(old) - 1)
        self.assertEqual(raw(result.array), raw(new))


class InvariantTests(unittest.TestCase):
    def test_a_merge_that_does_not_reconstruct_the_sentence_is_refused(self):
        old = old_array()
        with mock.patch.object(
            merge.difflib, "SequenceMatcher", **{"return_value.get_opcodes.return_value": []}
        ):
            with self.assertRaises(ValueError):
                merge.merge_arrays(old, corrected_array())

    def test_match_data_no_state_can_read_is_reported_rather_than_kept(self):
        old = [word("A", form="小枝", reading="こえだ", match_data=["nonsense", "x"])]
        new = [word("B", form="小枝", reading="こえだ")]

        with self.assertRaises(ValueError):
            merge.merge_arrays(old, new)


if __name__ == "__main__":
    unittest.main()
