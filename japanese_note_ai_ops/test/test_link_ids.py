"""Link IDs: what a field's value holds, generated IDs, a type's index and the search terms.

A link ID becomes the identity of every note in the collection and in the decks shared from it,
so each rule here is pinned: what counts as an ID (exactly what Anki's field search finds), the
fallback to the note id, the generator's exact numbers, which of two notes holding one ID
answers, and the escaping of a field's name in a search. What the terms find in a real
collection, and the lookups that read one, are test_replay/test_link_lookup.py's.
"""

import hashlib
import threading
import unittest

from addon_modules import load_addon_module

link_ids = load_addon_module("link_ids", subdir="")

NOTE_ID = 1700000000000
LIMIT = 2**53


def by_formula(text: str) -> int:
    """The generator's mapping as its docstring states it, worked out here independently."""
    number = int.from_bytes(hashlib.sha1(text.encode("utf-8")).digest(), "big")
    return 10**15 + number % (2**53 - 10**15)


class ParseTests(unittest.TestCase):
    def test_a_positive_integer_is_an_id(self):
        for value in ["1", "123", str(NOTE_ID), "1160697660039337", str(LIMIT - 1)]:
            with self.subTest(value):
                self.assertEqual(link_ids.parse_link_id(value), int(value))

    def test_no_value_zero_and_negative_placeholders_are_none(self):
        for value in ["", "0", "-5", "-1700000000000", "00"]:
            with self.subTest(value):
                self.assertIsNone(link_ids.parse_link_id(value))

    def test_text_is_none(self):
        for value in ["abc", "12a", "1e5", "12.0", "0x1f", "one"]:
            with self.subTest(value):
                self.assertIsNone(link_ids.parse_link_id(value))

    def test_only_the_plain_spelling_counts(self):
        # Anki's field search finds none of these as 123 (test_link_lookup), so an index
        # reading them as 123 would answer otherwise than a lookup
        for value in [" 123", "123 ", "1 23", "123\n", "\t123", "0123", "+123", "123<br>",
                      "<b>123</b>", "&nbsp;123", "１２３"]:
            with self.subTest(value):
                self.assertIsNone(link_ids.parse_link_id(value))

    def test_a_number_a_card_script_cannot_hold_exactly_is_none(self):
        for value in [str(LIMIT), str(LIMIT + 1), "99999999999999999", "9" * 30]:
            with self.subTest(value):
                self.assertIsNone(link_ids.parse_link_id(value))

    def test_a_huge_run_of_digits_is_none_without_converting_it(self):
        # Python 3.11+ refuses int() of more than 4300 digits
        self.assertIsNone(link_ids.parse_link_id("1" * 5000))

    def test_is_link_id(self):
        for number in [1, NOTE_ID, LIMIT - 1]:
            self.assertTrue(link_ids.is_link_id(number), number)
        for number in [0, -5, LIMIT, "123", None, 1.5]:
            self.assertFalse(link_ids.is_link_id(number), number)


class LinkIdTests(unittest.TestCase):
    def test_the_fields_id_wins(self):
        self.assertEqual(link_ids.link_id("1160697660039337", NOTE_ID), 1160697660039337)
        # Even a note id other than the note's own: it is what the field says
        self.assertEqual(link_ids.link_id(str(NOTE_ID + 1), NOTE_ID), NOTE_ID + 1)

    def test_a_field_holding_no_id_gives_the_note_id(self):
        for value in ["", "0", "-5", "text", " 123"]:
            with self.subTest(value):
                self.assertEqual(link_ids.link_id(value, NOTE_ID), NOTE_ID)

    def test_a_note_not_added_yet_has_only_its_fields_id(self):
        self.assertEqual(link_ids.link_id("1160697660039337", 0), 1160697660039337)
        for value in ["", "-5"]:
            with self.subTest(value), self.assertRaises(ValueError):
                link_ids.link_id(value, 0)


class Note:
    """A note in hand: its id and its fields."""

    def __init__(self, note_id: int, fields: dict) -> None:
        self.id = note_id
        self.fields = fields

    def __contains__(self, key: str) -> bool:
        return key in self.fields

    def __getitem__(self, key: str) -> str:
        return self.fields[key]


class NoteLinkIdTests(unittest.TestCase):
    """The link ID of a note in hand, read off the field its type names (or none)."""

    def test_the_fields_id_wins_over_the_note_id(self):
        note = Note(NOTE_ID, {"Word ID": "1160697660039337"})
        self.assertEqual(link_ids.note_link_id(note, "Word ID"), 1160697660039337)

    def test_no_id_in_the_field_or_no_field_gives_the_note_id(self):
        for fields, field in [
            ({"Word ID": ""}, "Word ID"),
            ({"Word ID": "0"}, "Word ID"),
            ({"Word ID": "-1234567"}, "Word ID"),
            ({}, "Word ID"),
            ({"Word ID": "1160697660039337"}, None),
        ]:
            with self.subTest(fields=fields, field=field):
                self.assertEqual(link_ids.note_link_id(Note(NOTE_ID, fields), field), NOTE_ID)

    def test_a_note_not_added_yet_has_one_only_when_its_field_holds_one(self):
        pending = Note(0, {"Word ID": "1160697660039337"})
        self.assertEqual(link_ids.note_link_id(pending, "Word ID"), 1160697660039337)
        # Nothing can link these yet: an answer rather than link_id's raise
        for value in ["", "-1234567"]:
            with self.subTest(value):
                self.assertIsNone(link_ids.note_link_id(Note(0, {"Word ID": value}), "Word ID"))
        self.assertIsNone(link_ids.note_link_id(Note(0, {}), None))


class NewLinkIdTests(unittest.TestCase):
    KEY = "vocab|1700000000000|[3]|word|reading"

    def test_the_exact_numbers(self):
        # Pinned: the IDs already in collections came from this mapping, and a replay expects
        # the same ones again
        self.assertEqual(link_ids.new_link_id(self.KEY, set()), 1160697660039337)
        self.assertEqual(link_ids.new_link_id("sentence|1700000000000", set()), 1841288010442579)
        self.assertEqual(link_ids.new_link_id("dup|1700000000001", set()), 8846100906101570)

    def test_the_number_is_the_sha1_of_the_key_in_the_range(self):
        for key in [self.KEY, "", "日本語|よみ", "dup|1"]:
            with self.subTest(key):
                self.assertEqual(link_ids.new_link_id(key, set()), by_formula(key))

    def test_every_id_has_16_digits_and_fits_a_javascript_number(self):
        for n in range(2000):
            number = link_ids.new_link_id(f"key|{n}", set())
            self.assertTrue(10**15 <= number < LIMIT, number)
            self.assertEqual(len(str(number)), 16)
            self.assertEqual(link_ids.parse_link_id(str(number)), number)

    def test_the_same_key_gives_the_same_id(self):
        taken = {NOTE_ID, 1234}
        first = link_ids.new_link_id(self.KEY, taken)
        self.assertEqual(link_ids.new_link_id(self.KEY, set(taken)), first)
        self.assertNotEqual(link_ids.new_link_id(self.KEY + "x", taken), first)

    def test_a_taken_id_is_rehashed_with_a_counter(self):
        first = by_formula(self.KEY)
        second = by_formula(self.KEY + "#1")
        third = by_formula(self.KEY + "#2")
        self.assertEqual(link_ids.new_link_id(self.KEY, {first}), second)
        self.assertEqual(link_ids.new_link_id(self.KEY, {first, second}), third)
        # Only what `taken` holds counts: the second taken alone leaves the first free
        self.assertEqual(link_ids.new_link_id(self.KEY, {second}), first)

    def test_ids_handed_out_into_taken_never_repeat(self):
        taken: set = set()
        for _ in range(5):
            taken.add(link_ids.new_link_id(self.KEY, taken))
        self.assertEqual(len(taken), 5)

    def test_an_index_can_be_taken(self):
        first = by_formula(self.KEY)
        index = link_ids.LinkIndex.from_rows([(NOTE_ID, str(first))])
        self.assertEqual(link_ids.new_link_id(self.KEY, index), by_formula(self.KEY + "#1"))

    def test_a_taken_holding_everything_raises_rather_than_hangs(self):
        class Everything:
            def __contains__(self, item):
                return True

        with self.assertRaises(RuntimeError):
            link_ids.new_link_id(self.KEY, Everything())


A, B, C = NOTE_ID, NOTE_ID + 1, NOTE_ID + 2
GENERATED = 1160697660039337


class LinkIndexTests(unittest.TestCase):
    def test_a_field_holding_no_id_falls_back_to_the_note_id(self):
        index = link_ids.LinkIndex.from_rows([(A, ""), (B, "0"), (C, "-5")])
        for note_id in (A, B, C):
            self.assertEqual(index.note_id(note_id), note_id)
        self.assertEqual(index.duplicates, {})

    def test_a_fields_id_is_held_and_its_note_id_is_not(self):
        index = link_ids.LinkIndex.from_rows([(A, str(GENERATED)), (B, str(B))])
        self.assertEqual(index.note_id(GENERATED), A)
        self.assertIsNone(index.note_id(A))
        self.assertEqual(index.note_id(B), B)

    def test_a_copied_field_is_a_duplicate_answered_by_the_older_note(self):
        for rows in ([(A, str(GENERATED)), (C, str(GENERATED))],
                     [(C, str(GENERATED)), (A, str(GENERATED))]):
            with self.subTest(rows):
                index = link_ids.LinkIndex.from_rows(rows)
                self.assertEqual(index.note_id(GENERATED), A)
                self.assertEqual(index.duplicates, {GENERATED: [A, C]})

    def test_a_field_holding_another_notes_id_while_its_field_is_empty_is_a_duplicate(self):
        # A holds A by its note id (empty field), and C's field holds A too
        for rows in ([(A, ""), (C, str(A))], [(C, str(A)), (A, "")]):
            with self.subTest(rows):
                index = link_ids.LinkIndex.from_rows(rows)
                self.assertEqual(index.note_id(A), A)
                self.assertEqual(index.duplicates, {A: [A, C]})
                # C's own note id is nobody's ID now
                self.assertIsNone(index.note_id(C))

    def test_the_older_answers_whichever_holds_it_by_field(self):
        # B, the older, holds C's note id in its field while C's field is empty
        index = link_ids.LinkIndex.from_rows([(B, str(C)), (C, "")])
        self.assertEqual(index.note_id(C), B)
        self.assertEqual(index.duplicates, {C: [B, C]})

    def test_a_field_holding_another_notes_id_is_no_duplicate_when_that_ones_field_holds_one(self):
        index = link_ids.LinkIndex.from_rows([(A, str(GENERATED)), (C, str(A))])
        self.assertEqual(index.duplicates, {})
        self.assertEqual(index.note_id(A), C)
        self.assertEqual(index.note_id(GENERATED), A)

    def test_three_holders(self):
        index = link_ids.LinkIndex.from_rows(
            [(B, str(GENERATED)), (C, str(GENERATED)), (A, str(GENERATED))]
        )
        self.assertEqual(index.duplicates, {GENERATED: [A, B, C]})
        self.assertEqual(index.note_id(GENERATED), A)

    def test_the_values_that_are_no_id_are_listed(self):
        rows = [(A, "0"), (B, "-1700000000123"), (C, " 123"), (C + 1, "abc"), (C + 2, ""),
                (C + 3, "123<br>"), (C + 4, str(GENERATED))]
        index = link_ids.LinkIndex.from_rows(rows)
        self.assertEqual(
            index.invalid,
            {A: "0", B: "-1700000000123", C: " 123", C + 1: "abc", C + 3: "123<br>"},
        )
        # And each falls back to its note id
        self.assertEqual(index.note_id(C + 3), C + 3)
        self.assertIsNone(index.note_id(123))

    def test_membership_and_many_ids(self):
        index = link_ids.LinkIndex.from_rows([(A, str(GENERATED)), (B, "")])
        self.assertIn(GENERATED, index)
        self.assertIn(B, index)
        self.assertNotIn(A, index)
        self.assertNotIn("not a number", index)
        self.assertEqual(index.note_ids([GENERATED, A, B, 5]), {GENERATED: A, B: B})

    def test_every_answer_naming_a_note_is_reported_found(self):
        found: list = []
        index = link_ids.LinkIndex.from_rows(
            [(A, str(GENERATED)), (B, "")], on_found=lambda ids: found.append(sorted(ids))
        )
        self.assertEqual(found, [])
        index.note_id(GENERATED)
        index.note_id(A)  # held by no note
        index.note_ids([B, 5])
        index.note_ids([5])
        self.assertIn(B, index)
        self.assertEqual(found, [[A], [B], [B]])


class IndexNewLinkIdTests(unittest.TestCase):
    """The IDs a run hands its new notes, from its index of their type: none a note held when
    the index was read, none handed out twice (SPEC 10, "Uniqueness")."""

    def test_a_free_key_gets_its_plain_id(self):
        index = link_ids.LinkIndex.from_rows([(A, "")])

        self.assertEqual(index.new_link_id("vocab|1|2|本|ほん"), by_formula("vocab|1|2|本|ほん"))

    def test_an_id_a_note_holds_is_skipped(self):
        held = by_formula("vocab|1|2|本|ほん")
        index = link_ids.LinkIndex.from_rows([(A, str(held))])

        self.assertEqual(index.new_link_id("vocab|1|2|本|ほん"), by_formula("vocab|1|2|本|ほん#1"))

    def test_one_key_twice_gets_two_ids_and_the_note_ids_stay_free_to_hold(self):
        index = link_ids.LinkIndex.from_rows([(A, "")])

        first, second = (index.new_link_id("same") for _ in range(2))

        self.assertEqual((first, second), (by_formula("same"), by_formula("same#1")))
        # Handed out, not held: no note holds them until the run adds one
        self.assertNotIn(first, index)
        self.assertEqual(index.note_ids([first, second]), {})

    def test_asking_what_is_taken_tells_on_found_nothing(self):
        # Only answers a replay must find too are told: no note is looked up here
        found: list = []
        held = by_formula("key")
        index = link_ids.LinkIndex.from_rows([(A, str(held))], on_found=found.append)

        index.new_link_id("key")

        self.assertEqual(found, [])

    def test_threads_making_notes_at_once_never_get_one_id(self):
        index = link_ids.LinkIndex.from_rows([])
        start = threading.Barrier(8)
        handed: list = []

        def make(worker: int) -> None:
            start.wait()
            # The same keys from every thread: each would get the same IDs unguarded
            handed.extend(index.new_link_id(f"key|{n}") for n in range(50))

        threads = [threading.Thread(target=make, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(handed), 400)
        self.assertEqual(len(set(handed)), 400)


class DuplicatesErrorTests(unittest.TestCase):
    """The refusal of an op that writes links while two notes of a type hold one link ID."""

    def test_none_without_duplicates(self):
        index = link_ids.LinkIndex.from_rows([(A, str(GENERATED)), (B, "")])

        self.assertIsNone(link_ids.duplicates_error("Word", "Word ID", index))

    def test_it_names_the_type_the_field_each_id_and_its_notes_and_the_fix(self):
        D = NOTE_ID + 3
        index = link_ids.LinkIndex.from_rows(
            [(A, str(GENERATED)), (B, str(GENERATED)), (C, ""), (D, str(C))]
        )

        error = link_ids.duplicates_error("Word", "Word ID", index)

        assert error is not None
        self.assertIn('Notes of "Word" hold the same link ID in their field "Word ID"', error)
        # Each ID with its holders, the oldest first: a field copied, and a field naming another
        # note's id while that one's holds none
        self.assertIn(f"{C} by notes {C}, {D}; {GENERATED} by notes {A}, {B}", error)
        self.assertIn(f'Run "{link_ids.FILL_LINK_IDS}" on these notes first', error)
        self.assertEqual(link_ids.FILL_LINK_IDS, "Fill link IDs")
        self.assertTrue(error.endswith(f"search: nid:{A},{B},{C},{D}"))

    def test_a_long_list_is_cut_short(self):
        rows = [(NOTE_ID + n, str(GENERATED + n // 2)) for n in range(30)]
        index = link_ids.LinkIndex.from_rows(rows)

        error = link_ids.duplicates_error("Word", None, index)

        assert error is not None
        self.assertIn("; and 5 more IDs.", error)
        self.assertNotIn("in their field", error)
        self.assertEqual(error.count(" by notes "), 10)


class FieldSearchTests(unittest.TestCase):
    def test_a_plain_name_is_quoted(self):
        self.assertEqual(link_ids.field_search("Word ID", "123"), '"Word ID:123"')

    def test_what_anki_reads_as_syntax_in_a_field_name_is_escaped(self):
        # As Anki's build_search_string escapes them; unescaped, `a_b:` also searched a field
        # "axb" (test_link_lookup)
        cases = {
            "a_b": '"a\\_b:1"',
            "star*name": '"star\\*name:1"',
            "back\\slash": '"back\\\\slash:1"',
            'Say "hi"': '"Say \\"hi\\":1"',
        }
        for name, term in cases.items():
            with self.subTest(name):
                self.assertEqual(link_ids.field_search(name, "1"), term)

    def test_the_text_is_left_as_search_syntax(self):
        self.assertEqual(link_ids.field_search("Words", "*123*"), '"Words:*123*"')

    def test_parentheses_hyphens_and_other_scripts_are_left_alone(self):
        self.assertEqual(link_ids.field_search("-id (x) 番号", "1"), '"-id (x) 番号:1"')

    def test_a_field_named_like_a_search_keyword_cannot_be_searched(self):
        for name in ["nid", "Nid", "NOTE", "tag", "re", "has-cd", "w"]:
            with self.subTest(name), self.assertRaises(ValueError):
                link_ids.field_search(name, "1")

    def test_names_close_to_a_keyword_are_fields(self):
        for name in ["id", "nids", "Note ID", "tags", "word"]:
            with self.subTest(name):
                self.assertEqual(link_ids.field_search(name, "1"), f'"{name}:1"')


class LinkIdsSearchTests(unittest.TestCase):
    def test_an_anchored_regex_of_the_ids_in_order(self):
        self.assertEqual(
            link_ids.link_ids_search("Word ID", [GENERATED, A, A]),
            f'"Word ID:re:^(?:{A}|{GENERATED})$"',
        )

    def test_one_id(self):
        self.assertEqual(link_ids.link_ids_search("ID", [7]), '"ID:re:^(?:7)$"')

    def test_the_field_name_is_escaped(self):
        self.assertEqual(link_ids.link_ids_search("a_b", [7]), '"a\\_b:re:^(?:7)$"')

    def test_no_ids_raise_rather_than_find_every_empty_field(self):
        with self.assertRaises(ValueError):
            link_ids.link_ids_search("ID", [])

    def test_numbers_no_note_can_hold_raise(self):
        for numbers in ([0], [-5, 7], [LIMIT]):
            with self.subTest(numbers), self.assertRaises(ValueError):
                link_ids.link_ids_search("ID", numbers)


if __name__ == "__main__":
    unittest.main()
