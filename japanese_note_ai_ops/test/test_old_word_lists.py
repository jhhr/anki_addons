"""Reading the old extract_words word lists of the research corpora, and finding the word of a
generated array an entry names - the ground truth `judge_eval.py` and the proper noun surveys
score against.

Plain data handling: no SudachiPy, JMdict or network, so these run wherever the suite does.
The arrays here are written by hand rather than generated, which is what lets a case say
exactly what the generator produced for the sentence.
"""

import unittest

from addon_modules import load_ops_module

old_word_lists = load_ops_module("old_word_lists", subdir="word_array/research")


def word(form="X", reading="x", pos="noun", raw="", match_data=None, subs=None):
    return [
        raw or form,
        pos,
        form,
        reading,
        match_data if match_data is not None else [],
        subs or [],
    ]


def entry(value, category="nouns"):
    return old_word_lists.read_entry(category, value)


class ReadEntryTests(unittest.TestCase):
    def test_the_four_shapes_extract_words_and_matching_write(self):
        cases = {
            ("口紅", "くちべに"): None,
            ("口紅", "くちべに", 2): None,
            ("口紅", "くちべに", "sort", 1378555076170): 1378555076170,
            ("口紅", "くちべに", 2, "sort", 1378555076170): 1378555076170,
        }
        for shape, note_id in cases.items():
            read = entry(list(shape))
            self.assertEqual((read.word, read.reading, read.note_id), ("口紅", "くちべに", note_id))

    def test_the_reading_is_compared_in_hiragana(self):
        self.assertEqual(entry(["カプセル", "カプセル"]).reading, "かぷせる")

    def test_a_bare_string_is_wrapped_rather_than_indexed(self):
        # Indexing it read the word "な" with the reading "ん", which is how this shape was found
        read = entry("なんと", "particles")
        self.assertEqual((read.word, read.reading), ("なんと", "なんと"))

    def test_a_word_with_no_reading_is_recovered_only_when_it_is_all_kana(self):
        self.assertEqual(entry(["なんと"], "particles").reading, "なんと")
        self.assertEqual(entry(["三"], "numbers").reading, "")

    def test_a_bare_note_id_keeps_the_id_it_is(self):
        read = entry(1378555076170)
        self.assertEqual((read.word, read.note_id), ("", 1378555076170))

    def test_what_holds_neither_a_word_nor_an_id(self):
        for value in [[], "", None, True, {"word": "x"}, ["", "x"]]:
            self.assertIsNone(entry(value), value)

    def test_a_placeholder_id_for_a_note_that_was_never_made_is_not_a_link(self):
        self.assertIsNone(entry(["口紅", "くちべに", "sort", -12345]).note_id)

    def test_a_meaning_index_is_not_read_as_a_note_id(self):
        self.assertIsNone(entry(["口紅", "くちべに", 2]).note_id)


class ReadWordListsTests(unittest.TestCase):
    def test_every_entry_is_counted_and_the_readable_ones_returned(self):
        entries, total = old_word_lists.read_word_lists(
            {"nouns": [["口紅", "くちべに"], []], "verbs": [["する", "する"]]}
        )
        self.assertEqual(([e.word for e in entries], total), (["口紅", "する"], 3))

    def test_a_category_holding_something_other_than_a_list_is_skipped(self):
        self.assertEqual(old_word_lists.read_word_lists({"nouns": "口紅", "verbs": None}), ([], 0))


class FindElementsTests(unittest.TestCase):
    def find(self, value, arr, category="nouns"):
        return old_word_lists.find_elements(entry(value, category), arr)

    def test_the_form_and_reading_as_they_stand(self):
        arr = [word("口紅", "くちべに")]
        self.assertEqual(self.find(["口紅", "くちべに"], arr), (arr, "form"))

    def test_the_notes_spelling_found_under_jmdicts(self):
        arr = [word("向こう", "むこう")]
        self.assertEqual(self.find(["向う", "むこう"], arr), (arr, "okurigana"))

    def test_okurigana_is_only_ignored_where_the_kanji_agree(self):
        self.assertEqual(self.find(["上げる", "あげる"], [word("上がる", "あがる")]), ([], ""))

    def test_a_colloquial_or_wrong_reading_on_the_same_word(self):
        arr = [word("何", "なに", pos="pronoun")]
        self.assertEqual(self.find(["何", "なん"], arr, "pronouns"), (arr, "written"))

    def test_a_lemma_the_generator_kanjifies(self):
        arr = [word("為る", "する", pos="verb")]
        self.assertEqual(self.find(["する", "する"], arr, "verbs"), (arr, "reading"))

    def test_a_reading_alone_needs_a_part_of_speech_that_fits_the_list(self):
        # 産[うぶ] is the text; the old list called it the adjective 初[うぶ]
        arr = [word("産", "うぶ", pos="noun")]
        self.assertEqual(self.find(["初", "うぶ"], arr, "adjectives"), ([], ""))

    def test_a_pronoun_list_entry_fits_an_adjectival(self):
        arr = [word("此の", "この", pos="adjectival")]
        self.assertEqual(self.find(["この", "この"], arr, "pronouns"), (arr, "reading"))

    def test_a_multi_word_unit_fits_whatever_list_its_words_came_from(self):
        arr = [word("に就いて", "について", pos="expression")]
        self.assertEqual(self.find(["について", "について"], arr, "particles"), (arr, "reading"))

    def test_a_form_the_lemma_hides_is_found_through_the_raw_text(self):
        arr = [word("だ", "だ", pos="copula", raw="です")]
        self.assertEqual(self.find(["です", "です"], arr, "particles"), (arr, "raw"))

    def test_the_raw_text_is_compared_without_furigana_or_tags(self):
        arr = [word("突く", "つく", raw="<k> 突[つ]き</k>")]
        self.assertEqual(self.find(["突き", "つき"], arr), (arr, "raw"))

    def test_an_earlier_step_is_never_widened_by_a_later_one(self):
        # する as written fits 為る only by its reading; 為る found as written decides alone
        written, by_reading = word("為る", "する", pos="verb"), word("刷る", "する", pos="verb")
        self.assertEqual(
            self.find(["為る", "する"], [written, by_reading], "verbs"), ([written], "form")
        )

    def test_every_word_a_step_fits_is_returned(self):
        arr = [word("為る", "する", pos="verb"), word("刷る", "する", pos="verb")]
        self.assertEqual(self.find(["する", "する"], arr, "verbs"), (arr, "reading"))

    def test_a_flagged_word_counts_only_where_nothing_else_fits(self):
        flagged, unjudged = word("一", "いち", match_data=["dontmatch"]), word("一", "いち")
        self.assertEqual(self.find(["一", "いち"], [flagged, unjudged]), ([unjudged], "form"))
        self.assertEqual(self.find(["一", "いち"], [flagged]), ([flagged], "form"))


if __name__ == "__main__":
    unittest.main()
