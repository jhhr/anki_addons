"""What a word list entry has to look like before a word can be read out of it.

The word list field holds JSON an LLM wrote, read back through `repair_json`, so the entries
are not always the tuples the rest of the module expects. Reading them positionally raised on
two of the shapes that turn up in practice and - the part that mattered - silently mis-read a
third. The cases here are the ones taken off real run logs; see `normalize_word_tuple`.
"""

import unittest

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
from addon_modules import load_ops_module

mwtn = load_ops_module("match_words_to_notes")
normalize = mwtn.normalize_word_tuple


class WellFormedEntriesTests(unittest.TestCase):
    """Every shape in FinalWordTuple comes back as itself."""

    def test_a_word_and_a_reading(self):
        self.assertEqual(normalize(["引く", "ひく"]), ("引く", "ひく"))

    def test_a_multi_meaning_word_keeps_its_meaning_index(self):
        self.assertEqual(normalize(["掛かる", "かかる", 2]), ("掛かる", "かかる", 2))

    def test_a_matched_word_keeps_its_sort_value_and_note_id(self):
        entry = ["引く", "ひく", "ひく(m1)", 1674931277303]
        self.assertEqual(normalize(entry), tuple(entry))

    def test_a_tuple_is_accepted_as_readily_as_a_list(self):
        self.assertEqual(normalize(("引く", "ひく")), ("引く", "ひく"))


class RecoverableEntriesTests(unittest.TestCase):
    """A word with no reading, where the reading can only be the word itself."""

    def test_a_kana_word_alone_in_a_list_is_its_own_reading(self):
        self.assertEqual(normalize(["なんと"]), ("なんと", "なんと"))

    def test_a_bare_kana_string_is_not_indexed_character_by_character(self):
        """The failure that never raised, and so was never noticed.

        `"なんと"[0]` and `"なんと"[1]` are perfectly good subscripts, so the entry used to be
        read as the word な with the reading ん and processed as if that were a word.
        """
        self.assertEqual(normalize("なんと"), ("なんと", "なんと"))

    def test_katakana_and_the_long_vowel_mark_count_as_kana(self):
        self.assertEqual(normalize(["コーヒー"]), ("コーヒー", "コーヒー"))


class UnreadableEntriesTests(unittest.TestCase):
    """Refused rather than guessed at. Each of these was logged by a real run."""

    def test_a_bare_note_id(self):
        self.assertIsNone(normalize(1378555076170))

    def test_an_empty_list(self):
        self.assertIsNone(normalize([]))

    def test_a_kanji_word_with_no_reading(self):
        # 三 reads さん, which is not something this function can know
        self.assertIsNone(normalize("三"))
        self.assertIsNone(normalize(["三"]))

    def test_a_reading_that_is_not_a_string(self):
        self.assertIsNone(normalize(["漢字", 1]))

    def test_an_empty_word_or_reading(self):
        self.assertIsNone(normalize(["", "ひく"]))
        self.assertIsNone(normalize(["引く", ""]))

    def test_none_and_dicts_and_numbers(self):
        for entry in (None, {"word": "引く"}, 3.5, True):
            self.assertIsNone(normalize(entry), entry)


if __name__ == "__main__":
    unittest.main()


class DropDuplicateWordTuplesTests(unittest.TestCase):
    """Dedup that rewrites a note's word lists, and must not skip over what it rewrites."""

    KEYS = ["words", "extra_words"]

    def drop(self, word_list_dict):
        mwtn.drop_duplicate_word_tuples(word_list_dict, self.KEYS)
        return word_list_dict

    def test_three_equal_entries_leave_one(self):
        """The case `word_tuples.remove(wt)` during iteration got wrong.

        `remove` deletes the first equal element - index 0, not necessarily `wt` - and every
        entry past it shifts left, so the iterator went from index 1 to index 2, which was
        now past the end. The third duplicate was never examined and stayed in the note.
        """
        lists = {"words": [["あ", "ア"], ["あ", "ア"], ["あ", "ア"]]}
        self.assertEqual(self.drop(lists), {"words": [["あ", "ア"]]})

    def test_the_list_object_is_rewritten_in_place(self):
        words = [["引く", "ひく"], ["引く", "ひく"]]
        lists = {"words": words}
        self.drop(lists)
        self.assertIs(lists["words"], words)
        self.assertEqual(words, [["引く", "ひく"]])

    def test_different_words_and_readings_are_all_kept(self):
        lists = {"words": [["引く", "ひく"], ["引く", "びく"], ["退く", "ひく"]]}
        self.assertEqual(
            self.drop(lists), {"words": [["引く", "ひく"], ["引く", "びく"], ["退く", "ひく"]]}
        )

    def test_multi_meaning_entries_are_meant_to_repeat(self):
        lists = {"words": [["掛かる", "かかる", 1], ["掛かる", "かかる", 2]]}
        self.assertEqual(
            self.drop(lists), {"words": [["掛かる", "かかる", 1], ["掛かる", "かかる", 2]]}
        )

    def test_a_word_already_seen_in_another_list_is_a_duplicate(self):
        lists = {"words": [["引く", "ひく"]], "extra_words": [["引く", "ひく"], ["押す", "おす"]]}
        self.assertEqual(
            self.drop(lists), {"words": [["引く", "ひく"]], "extra_words": [["押す", "おす"]]}
        )

    def test_an_unreadable_entry_is_kept_rather_than_dropped(self):
        # Rewriting the note's word list without an entry nobody understands is not this
        # function's call to make
        lists = {"words": [1378555076170, ["引く", "ひく"], ["引く", "ひく"]]}
        self.assertEqual(self.drop(lists), {"words": [1378555076170, ["引く", "ひく"]]})

    def test_a_key_holding_something_other_than_a_list_is_left_alone(self):
        lists = {"words": "not a list"}
        self.assertEqual(self.drop(lists), {"words": "not a list"})

    def test_a_missing_key_is_not_an_error(self):
        self.assertEqual(self.drop({}), {})
