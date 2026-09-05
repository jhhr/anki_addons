"""get_matching_notes_for_word_and_reading, now that it reads the index instead of searching.

The index's own behaviour is covered in test_word_index. What is left here is the translation
that used to be a query string: which spellings of the word get looked up, that the reading
filter still drops what it dropped, and that the fetch afterwards asks for only what survived
both - which is the point of moving the reading filter ahead of it.

Notes are faked down to an id, because nothing in this function reads a field off one any
more: the word, the reading and the sort marker all come out of the index.
"""

import asyncio
import unittest

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
import addon_modules  # noqa: F401
from anki_stubs import load_ops_module
from test_word_index import FIELDS, VOCAB_MID, VOCAB_ORDS, vocab_row

wi = load_ops_module("word_index")
mwtn = load_ops_module("match_words_to_notes")


class FakeNote:
    """All the function does with a fetched note is put it in the list it returns."""

    def __init__(self, note_id):
        self.id = note_id


class LookupTestCase(unittest.TestCase):
    def setUp(self):
        self.fetched: "list[list[int]]" = []
        self.real_get_notes = mwtn.col_get_notes_async

        async def fake_get_notes_async(note_ids):
            ids = list(note_ids)
            self.fetched.append(ids)
            return [FakeNote(note_id) for note_id in ids]

        mwtn.col_get_notes_async = fake_get_notes_async

    def tearDown(self):
        mwtn.col_get_notes_async = self.real_get_notes

    def lookup(self, word, reading, *rows, notes_to_update_dict=None):
        index = wi.WordIndex.from_rows(FIELDS, {VOCAB_MID: VOCAB_ORDS}, list(rows))
        notes = asyncio.run(
            mwtn.get_matching_notes_for_word_and_reading(
                word=word,
                reading=reading,
                notes_to_update_dict=notes_to_update_dict or {},
                log_prefix="test--",
                word_note_index=index,
            )
        )
        return [note.id for note in notes]


class WordSpellingTests(LookupTestCase):
    def test_a_word_with_a_matching_reading_is_found(self):
        self.assertEqual(
            self.lookup("私", "わたし", vocab_row(1, "私", "私", "わたし", "私")), [1]
        )

    def test_the_suru_form_of_the_word_is_looked_up_too(self):
        self.assertEqual(
            self.lookup(
                "勉強", "べんきょう", vocab_row(1, "勉強する", "勉強する", "べんきょうする", "勉強する")
            ),
            [1],
        )

    def test_an_honorific_written_with_kanji_finds_the_kana_spelling(self):
        # 御茶/お茶 are the same entry, and which one a note uses is not knowable up front
        self.assertEqual(
            self.lookup("御茶", "おちゃ", vocab_row(1, "お茶", "お茶", "おちゃ", "お茶")), [1]
        )
        self.assertEqual(
            self.lookup("御飯", "ごはん", vocab_row(1, "ご飯", "ご飯", "ごはん", "ご飯")), [1]
        )

    def test_the_wrong_honorific_kana_is_not_looked_up(self):
        # The reading says which of the two it is, so only that one is tried
        self.assertEqual(
            self.lookup("御茶", "おちゃ", vocab_row(1, "ご茶", "ご茶", "おちゃ", "ご茶")), []
        )

    def test_a_kana_only_word_is_found_by_its_reading_alone(self):
        # No kanji to match on, so the reading in the plain word field identifies it
        self.assertEqual(
            self.lookup("ください", "ください", vocab_row(1, "", "ください", "ください", "ください")),
            [1],
        )

    def test_a_word_with_kanji_is_not_found_by_its_reading_alone(self):
        self.assertEqual(
            self.lookup("私", "わたし", vocab_row(1, "", "わたし", "わたし", "わたし")), []
        )


class ReadingFilterTests(LookupTestCase):
    def test_a_note_with_a_different_reading_is_dropped(self):
        rows = [
            vocab_row(1, "私", "私", "わたし", "私 (kun)"),
            vocab_row(2, "私", "私", "わたくし", "私 (on)"),
        ]
        self.assertEqual(self.lookup("私", "わたし", *rows), [1])

    def test_a_katakana_reading_matches_its_hiragana(self):
        self.assertEqual(
            self.lookup("珈琲", "コーヒー", vocab_row(1, "珈琲", "珈琲", "こーひー", "珈琲")), [1]
        )

    def test_the_suru_reading_matches_the_plain_one(self):
        self.assertEqual(
            self.lookup(
                "勉強", "べんきょう", vocab_row(1, "勉強する", "勉強する", "べんきょうする", "勉強する")
            ),
            [1],
        )

    def test_a_note_marked_x_never_reaches_the_reading_filter(self):
        self.assertEqual(
            self.lookup("私", "わたし", vocab_row(1, "私", "私", "わたし", "私 (x1)")), []
        )


class FetchingTests(LookupTestCase):
    def test_only_the_notes_that_survived_the_reading_filter_are_fetched(self):
        # The whole gain of filtering by reading first: the ones that cannot match are never
        # pulled out of the collection
        rows = [
            vocab_row(1, "私", "私", "わたし", "私 (kun)"),
            vocab_row(2, "私", "私", "わたくし", "私 (on)"),
            vocab_row(3, "私", "私", "わたし", "私 (r2)"),
        ]
        self.assertEqual(self.lookup("私", "わたし", *rows), [1, 3])
        self.assertEqual(self.fetched, [[1, 3]])

    def test_nothing_is_fetched_when_no_note_matches(self):
        self.assertEqual(self.lookup("彼女", "かのじょ", vocab_row(1, "私", "私", "わたし", "私")), [])
        # get_notes_async short-circuits an empty list, but it should not even be asked
        self.assertEqual(self.fetched, [[]])

    def test_an_already_edited_note_is_used_rather_than_fetched_again(self):
        edited = FakeNote(1)
        rows = [
            vocab_row(1, "私", "私", "わたし", "私"),
            vocab_row(2, "私", "私", "わたし", "私 (r2)"),
        ]
        notes = asyncio.run(
            mwtn.get_matching_notes_for_word_and_reading(
                word="私",
                reading="わたし",
                notes_to_update_dict={1: edited},
                log_prefix="test--",
                word_note_index=wi.WordIndex.from_rows(
                    FIELDS, {VOCAB_MID: VOCAB_ORDS}, rows
                ),
            )
        )
        self.assertEqual(self.fetched, [[2]])
        self.assertIs(notes[0], edited)
        self.assertEqual([note.id for note in notes], [1, 2])


if __name__ == "__main__":
    unittest.main()
