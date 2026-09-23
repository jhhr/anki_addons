"""Tidying a word's sort field markers, which the cleanup does last in a match run.

The match op numbers a word's meanings (mN) and readings (rN), and marks its readings (kun)
or (on), as it prepares each new note, from the notes it can see then. A note that is not
added after all, or a new reading whose meaning could not be made, leaves the numbering with
a gap, or a marker on a note that has nothing left to be told apart from. These tests give
`tidy_word_markers` a word's notes and check what it says their sort fields should be.

Every fixture also goes through the checks in PropertyTests: tidying is order independent,
tidying twice changes nothing more, and no two sort fields that differed come out the same.
"""

import itertools
import unittest

from addon_modules import load_ops_module

sfm = load_ops_module("sort_field_markers")

W = "言葉"


def notes(*sort_fields, types: dict | None = None) -> list:
    """The notes of a word, ids 1, 2, ... in the order given, each of the reading type in
    `types` (by id) or of none."""
    types = types or {}
    return [
        sfm.WordNote(nid, sort_field, types.get(nid, ""))
        for nid, sort_field in enumerate(sort_fields, start=1)
    ]


def tidied(word_notes: list) -> dict:
    """Every note's sort field after tidying, by id."""
    renamed = sfm.tidy_word_markers(word_notes)
    return {note.nid: renamed.get(note.nid, note.sort_field) for note in word_notes}


# Every fixture of the tests below, for PropertyTests
FIXTURES: list = []


def fixture(*sort_fields, types: dict | None = None) -> list:
    word_notes = notes(*sort_fields, types=types)
    FIXTURES.append(word_notes)
    return word_notes


class ParseTests(unittest.TestCase):
    def test_the_markers_are_read_apart_from_the_word(self):
        self.assertEqual(
            sfm.parse_sort_field(f"{W} (kun)(r2)(m3)"), sfm.SortMarkers(W, "kun", 2, 3)
        )
        self.assertEqual(sfm.parse_sort_field(W), sfm.SortMarkers(W))
        self.assertEqual(sfm.parse_sort_field(f"{W}(m1)"), sfm.SortMarkers(W, "", None, 1))

    def test_in_any_order_and_spacing(self):
        self.assertEqual(
            sfm.parse_sort_field(f" {W} (m1) (on)(r2) "), sfm.SortMarkers(W, "on", 2, 1)
        )

    def test_another_marker_or_two_of_a_kind_leaves_the_note_out(self):
        for value in (
            f"{W} (x1)",
            f"{W} (m1)(x1)",
            f"{W} (m1)(m2)",
            f"{W} (kun)(on)",
            f"{W} (r1)(r1)",
            f"{W} (M1)",
            f"{W} (m1) etc",
            "(m1)",
            "",
        ):
            with self.subTest(value=value):
                self.assertIsNone(sfm.parse_sort_field(value))

    def test_written_back_in_the_match_op_s_order(self):
        self.assertEqual(sfm.SortMarkers(W, "on", 2, 1).format(), f"{W} (on)(r2)(m1)")
        self.assertEqual(sfm.SortMarkers(W, "", None, 2).format(), f"{W} (m2)")
        self.assertEqual(sfm.SortMarkers(W).format(), W)


class MeaningTests(unittest.TestCase):
    def test_a_gap_is_closed(self):
        # The case a second meaning not added leaves, when a third was added
        word = fixture(f"{W} (m1)", f"{W} (m3)")
        self.assertEqual(tidied(word), {1: f"{W} (m1)", 2: f"{W} (m2)"})

    def test_a_numbering_starting_above_1_starts_at_1(self):
        word = fixture(f"{W} (m2)", f"{W} (m3)")
        self.assertEqual(tidied(word), {1: f"{W} (m1)", 2: f"{W} (m2)"})

    def test_a_meaning_left_alone_loses_its_number(self):
        # The copied note, when the second meaning made from it was not added
        self.assertEqual(tidied(fixture(f"{W} (m1)")), {1: W})
        self.assertEqual(tidied(fixture(f"{W} (m3)")), {1: W})

    def test_a_numbering_without_gaps_is_left_as_it_is(self):
        word = fixture(f"{W} (m1)", f"{W} (m2)", f"{W} (m3)")
        self.assertEqual(sfm.tidy_word_markers(word), {})

    def test_the_order_is_kept(self):
        word = fixture(f"{W} (m4)", f"{W} (m2)", f"{W} (m7)")
        self.assertEqual(tidied(word), {1: f"{W} (m2)", 2: f"{W} (m1)", 3: f"{W} (m3)"})

    def test_an_unnumbered_meaning_comes_first(self):
        # The note the others were copied from, whose (m1) was taken back
        word = fixture(f"{W} (m2)", W)
        self.assertEqual(tidied(word), {1: f"{W} (m2)", 2: f"{W} (m1)"})

    def test_two_notes_of_one_number_are_numbered_by_id(self):
        word = fixture(f"{W} (m1)", f"{W} (m1)", f"{W} (m2)")
        self.assertEqual(tidied(word), {1: f"{W} (m1)", 2: f"{W} (m2)", 3: f"{W} (m3)"})

    def test_unnumbered_notes_of_one_reading_are_not_numbered(self):
        # Two notes of one sort field: no numbering of the match op's
        self.assertEqual(sfm.tidy_word_markers(fixture(W, W)), {})

    def test_meanings_are_numbered_per_reading(self):
        word = fixture(f"{W} (r1)(m1)", f"{W} (r1)(m3)", f"{W} (r2)(m2)")
        self.assertEqual(
            tidied(word), {1: f"{W} (r1)(m1)", 2: f"{W} (r1)(m2)", 3: f"{W} (r2)"}
        )

    def test_a_kun_reading_s_meanings_are_not_numbered_with_the_on_reading_s(self):
        word = fixture(f"{W} (kun)(m1)", f"{W} (kun)(m2)", f"{W} (on)(m3)")
        self.assertEqual(
            tidied(word), {1: f"{W} (kun)(m1)", 2: f"{W} (kun)(m2)", 3: f"{W} (on)"}
        )


class ReadingTests(unittest.TestCase):
    def test_a_gap_is_closed(self):
        word = fixture(f"{W} (r1)", f"{W} (r3)")
        self.assertEqual(tidied(word), {1: f"{W} (r1)", 2: f"{W} (r2)"})

    def test_a_reading_left_alone_loses_its_number(self):
        # The note a new reading numbered, when the new reading's meaning could not be made
        self.assertEqual(tidied(fixture(f"{W} (r1)")), {1: W})

    def test_a_reading_alone_keeps_its_meanings(self):
        word = fixture(f"{W} (r2)(m1)", f"{W} (r2)(m2)")
        self.assertEqual(tidied(word), {1: f"{W} (m1)", 2: f"{W} (m2)"})

    def test_a_reading_numbering_without_gaps_is_left_as_it_is(self):
        word = fixture(f"{W} (r1)(m1)", f"{W} (r1)(m2)", f"{W} (r2)", f"{W} (r3)")
        self.assertEqual(sfm.tidy_word_markers(word), {})

    def test_an_unnumbered_reading_comes_first(self):
        word = fixture(f"{W} (r2)", W, f"{W} (r4)")
        self.assertEqual(tidied(word), {1: f"{W} (r2)", 2: f"{W} (r1)", 3: f"{W} (r3)"})

    def test_meanings_first_then_readings(self):
        # The reading r1 is down to one meaning, and r2 is gone: both numbers go
        word = fixture(f"{W} (r1)(m2)", f"{W} (r3)")
        self.assertEqual(tidied(word), {1: f"{W} (r1)", 2: f"{W} (r2)"})
        self.assertEqual(tidied(fixture(f"{W} (kun)(r2)(m1)")), {1: W})

    def test_readings_are_numbered_per_kind(self):
        word = fixture(f"{W} (kun)(r1)", f"{W} (kun)(r3)", f"{W} (on)(r2)")
        self.assertEqual(
            tidied(word), {1: f"{W} (kun)(r1)", 2: f"{W} (kun)(r2)", 3: f"{W} (on)"}
        )

    def test_kun_and_on_marked_apart_need_no_numbers(self):
        word = fixture(f"{W} (kun)(r1)", f"{W} (on)(r1)")
        self.assertEqual(tidied(word), {1: f"{W} (kun)", 2: f"{W} (on)"})

    def test_an_unmarked_reading_of_a_word_marked_apart_is_a_kind_of_its_own(self):
        # What a reading of no kind made of a word marked apart (the match op numbers it
        # across the word): the kinds need no number, and it is the only unmarked reading
        word = fixture(f"{W} (kun)(r1)", f"{W} (on)(r1)", f"{W} (r2)")
        self.assertEqual(tidied(word), {1: f"{W} (kun)", 2: f"{W} (on)", 3: W})


class KunOnTests(unittest.TestCase):
    def test_a_kind_alone_loses_its_marker(self):
        # A kun note marked for an on reading whose meaning could not be made
        self.assertEqual(tidied(fixture(f"{W} (kun)", types={1: "kun"})), {1: W})
        word = fixture(f"{W} (on)(m1)", f"{W} (on)(m2)")
        self.assertEqual(tidied(word), {1: f"{W} (m1)", 2: f"{W} (m2)"})

    def test_without_the_other_kind_the_readings_are_numbered_as_one(self):
        word = fixture(f"{W} (kun)(r1)", f"{W} (kun)(r2)", types={1: "kun", 2: "kun"})
        self.assertEqual(tidied(word), {1: f"{W} (r1)", 2: f"{W} (r2)"})

    def test_both_kinds_marked_are_kept(self):
        word = fixture(f"{W} (kun)", f"{W} (on)", types={1: "kun", 2: "on"})
        self.assertEqual(sfm.tidy_word_markers(word), {})

    def test_an_unmarked_reading_of_the_other_kind_keeps_the_markers(self):
        # A partly marked word: its unmarked on reading is an on reading all the same, and
        # nothing adds the (on) it lacks
        word = fixture(f"{W} (kun)", W, types={1: "kun", 2: "on"})
        self.assertEqual(sfm.tidy_word_markers(word), {})

    def test_an_unmarked_reading_of_the_same_kind_does_not(self):
        # Two readings once told apart only by (kun) are numbered instead
        word = fixture(f"{W} (kun)", W, types={1: "kun", 2: "kun"})
        self.assertEqual(tidied(word), {1: f"{W} (r1)", 2: f"{W} (r2)"})

    def test_a_reading_of_no_kind_is_neither(self):
        word = fixture(f"{W} (on)", W, types={1: "on"})
        self.assertEqual(tidied(word), {1: f"{W} (r1)", 2: f"{W} (r2)"})

    def test_the_marker_says_the_kind_over_the_furigana(self):
        word = fixture(f"{W} (kun)", f"{W} (on)", types={1: "on", 2: "on"})
        self.assertEqual(sfm.tidy_word_markers(word), {})

    def test_a_reading_s_kind_is_its_first_note_s_that_tells_one(self):
        word = fixture(f"{W} (kun)", f"{W} (m1)", f"{W} (m2)", types={1: "kun", 3: "on"})
        self.assertEqual(sfm.tidy_word_markers(word), {})


class LeftOutTests(unittest.TestCase):
    def test_a_note_with_another_marker_is_neither_counted_nor_renamed(self):
        # As the match op numbers a word's notes, which leaves an (xN) note out
        word = fixture(f"{W} (m1)", f"{W} (m2)(x1)", f"{W} (x1)")
        self.assertEqual(tidied(word), {1: W, 2: f"{W} (m2)(x1)", 3: f"{W} (x1)"})

    def test_notes_of_other_words_are_tidied_apart(self):
        word = fixture(f"{W} (m1)", f"{W}遣い (m2)", f"{W}遣い (m3)")
        self.assertEqual(tidied(word), {1: W, 2: f"{W}遣い (m1)", 3: f"{W}遣い (m2)"})

    def test_a_word_is_compared_as_the_match_op_finds_it(self):
        word = fixture("ok (m1)", "OK (m3)")
        self.assertEqual(tidied(word), {1: "ok (m1)", 2: "OK (m2)"})

    def test_a_note_left_as_it_is_is_not_rewritten_into_the_usual_order(self):
        word = fixture(f"{W} (m1) (r1)", f"{W}(r1)(m2)", f"{W} (r2)")
        self.assertEqual(sfm.tidy_word_markers(word), {})

    def test_a_note_renamed_is(self):
        word = fixture(f"{W} (m3) (kun)", f"{W} (kun)(m1)", f"{W} (on)", types={3: "on"})
        self.assertEqual(sfm.tidy_word_markers(word), {1: f"{W} (kun)(m2)"})


class PropertyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The fixtures are made by the tests above, which a loader may run after these
        FIXTURES.clear()
        loader = unittest.TestLoader()
        unittest.TestSuite(
            loader.loadTestsFromTestCase(case)
            for case in (MeaningTests, ReadingTests, KunOnTests, LeftOutTests)
        ).run(unittest.TestResult())

    def test_every_fixture_comes_out_the_same_in_any_order(self):
        for word_notes in FIXTURES:
            expected = sfm.tidy_word_markers(word_notes)
            orders = itertools.islice(itertools.permutations(word_notes), 24)
            for order in orders:
                with self.subTest(order=[note.sort_field for note in order]):
                    self.assertEqual(sfm.tidy_word_markers(list(order)), expected)

    def test_tidying_twice_changes_nothing_more(self):
        for word_notes in FIXTURES:
            once = tidied(word_notes)
            again = [note._replace(sort_field=once[note.nid]) for note in word_notes]
            with self.subTest(notes=[note.sort_field for note in word_notes]):
                self.assertEqual(sfm.tidy_word_markers(again), {})

    def test_sort_fields_that_differed_still_differ(self):
        for word_notes in FIXTURES:
            after = tidied(word_notes)
            for a, b in itertools.combinations(word_notes, 2):
                if a.sort_field.strip() != b.sort_field.strip():
                    with self.subTest(a=a.sort_field, b=b.sort_field):
                        self.assertNotEqual(after[a.nid], after[b.nid])

    def test_the_fixtures_were_collected(self):
        self.assertGreater(len(FIXTURES), 30)


if __name__ == "__main__":
    unittest.main()
