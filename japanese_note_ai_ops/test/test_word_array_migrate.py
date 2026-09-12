"""Fitting an old extract_words word list into a generated array.

Plain data handling: no SudachiPy, JMdict or network, so these run wherever the suite does.
The arrays here are written by hand rather than generated, which is what lets a case say
exactly what the generator produced for the sentence.
"""

import unittest

from addon_modules import load_ops_module

migrate = load_ops_module("migrate", subdir="word_array")


def word(form="X", reading="x", pos="noun", raw="", match_data=None, subs=None):
    return [
        raw or form,
        pos,
        form,
        reading,
        match_data if match_data is not None else [],
        subs or [],
    ]


def matched(arr):
    """The note id on each word element, parents before sub-words."""
    return [elem[4] for _, elem in migrate.match_flags.iter_words(arr)]


class ReadEntryTests(unittest.TestCase):
    def test_the_four_shapes_extract_words_and_matching_write(self):
        cases = {
            ("口紅", "くちべに"): None,
            ("口紅", "くちべに", 2): None,
            ("口紅", "くちべに", "sort", 1378555076170): 1378555076170,
            ("口紅", "くちべに", 2, "sort", 1378555076170): 1378555076170,
        }
        for shape, note_id in cases.items():
            entry = migrate.read_entry("nouns", list(shape))
            self.assertEqual(
                (entry.word, entry.reading, entry.note_id), ("口紅", "くちべに", note_id)
            )

    def test_the_reading_is_compared_in_hiragana(self):
        self.assertEqual(migrate.read_entry("nouns", ["カプセル", "カプセル"]).reading, "かぷせる")

    def test_a_bare_string_is_wrapped_rather_than_indexed(self):
        # Indexing it read the word "な" with the reading "ん", which is how this shape was found
        entry = migrate.read_entry("particles", "なんと")
        self.assertEqual((entry.word, entry.reading), ("なんと", "なんと"))

    def test_a_word_with_no_reading_is_recovered_only_when_it_is_all_kana(self):
        self.assertEqual(migrate.read_entry("particles", ["なんと"]).reading, "なんと")
        self.assertEqual(migrate.read_entry("numbers", ["三"]).reading, "")

    def test_a_bare_note_id_keeps_the_id_it_is(self):
        entry = migrate.read_entry("nouns", 1378555076170)
        self.assertEqual((entry.word, entry.note_id), ("", 1378555076170))

    def test_what_holds_neither_a_word_nor_an_id(self):
        for value in [[], "", None, True, {"word": "x"}, ["", "x"]]:
            self.assertIsNone(migrate.read_entry("nouns", value), value)

    def test_a_placeholder_id_for_a_note_that_was_never_made_is_not_a_link(self):
        self.assertIsNone(migrate.read_entry("nouns", ["口紅", "くちべに", "sort", -12345]).note_id)

    def test_a_meaning_index_is_not_read_as_a_note_id(self):
        self.assertIsNone(migrate.read_entry("nouns", ["口紅", "くちべに", 2]).note_id)


class MatchStepTests(unittest.TestCase):
    def fit(self, entry, arr, category="nouns"):
        report = migrate.migrate({category: [entry]}, arr)
        return report, matched(arr)

    def test_the_form_and_reading_as_they_stand(self):
        arr = [word("口紅", "くちべに")]
        report, ids = self.fit(["口紅", "くちべに", "sort", 11], arr)
        self.assertEqual((ids, report.by_step["form"], report.leftovers), ([[11]], 1, []))

    def test_the_notes_spelling_carried_over_to_jmdicts(self):
        arr = [word("向こう", "むこう")]
        report, ids = self.fit(["向う", "むこう", "sort", 11], arr)
        self.assertEqual((ids, report.by_step["okurigana"]), ([[11]], 1))

    def test_okurigana_is_only_ignored_where_the_kanji_agree(self):
        arr = [word("上がる", "あがる")]
        report, ids = self.fit(["上げる", "あげる", "sort", 11], arr)
        self.assertEqual((ids, report.leftovers[0].reason), ([[]], migrate.NO_ELEMENT))

    def test_a_colloquial_or_wrong_reading_on_the_same_word(self):
        arr = [word("何", "なに", pos="pronoun")]
        report, ids = self.fit(["何", "なん", "sort", 11], arr, "pronouns")
        self.assertEqual((ids, report.by_step["written"]), ([[11]], 1))

    def test_a_lemma_the_generator_kanjifies(self):
        arr = [word("為る", "する", pos="verb")]
        report, ids = self.fit(["する", "する", "sort", 11], arr, "verbs")
        self.assertEqual((ids, report.by_step["reading"]), ([[11]], 1))

    def test_a_reading_alone_needs_a_part_of_speech_that_fits_the_list(self):
        # 産[うぶ] is the text; the old list called it the adjective 初[うぶ]
        arr = [word("産", "うぶ", pos="noun")]
        report, ids = self.fit(["初", "うぶ", "sort", 11], arr, "adjectives")
        self.assertEqual((ids, report.leftovers[0].reason), ([[]], migrate.NO_ELEMENT))

    def test_a_multi_word_unit_fits_whatever_list_its_words_came_from(self):
        arr = [word("に就いて", "について", pos="expression")]
        report, ids = self.fit(["について", "について", "sort", 11], arr, "particles")
        self.assertEqual((ids, report.by_step["reading"]), ([[11]], 1))

    def test_a_form_the_lemma_hides_is_found_through_the_raw_text(self):
        arr = [word("だ", "だ", pos="copula", raw="です")]
        report, ids = self.fit(["です", "です", "sort", 11], arr, "particles")
        self.assertEqual((ids, report.by_step["raw"]), ([[11]], 1))

    def test_the_raw_text_is_compared_without_furigana_or_tags(self):
        arr = [word("突く", "つく", raw="<k> 突[つ]き</k>")]
        report, ids = self.fit(["突き", "つき", "sort", 11], arr)
        self.assertEqual((ids, report.by_step["raw"]), ([[11]], 1))

    def test_a_sub_word_is_as_good_a_home_as_a_top_level_one(self):
        arr = [word("耳元", "みみもと", subs=[word("耳", "みみ"), word("元", "もと")])]
        report, ids = self.fit(["元", "もと", "sort", 11], arr)
        self.assertEqual((ids, report.linked), ([[], [], [11]], 1))


class LeftoverTests(unittest.TestCase):
    def test_a_content_word_twice_gets_the_one_link_on_both(self):
        arr = [word("為る", "する", pos="verb"), word("為る", "する", pos="verb")]
        report = migrate.migrate({"verbs": [["する", "する", "sort", 11]]}, arr)
        self.assertEqual((matched(arr), report.spread, report.leftovers), ([[11], [11]], 1, []))

    def test_two_different_words_fitting_one_entry_are_ambiguous(self):
        arr = [word("為る", "する", pos="verb"), word("刷る", "する", pos="verb")]
        report = migrate.migrate({"verbs": [["する", "する", "sort", 11]]}, arr)
        self.assertEqual(matched(arr), [[], []])
        self.assertEqual(report.leftovers[0].reason, migrate.AMBIGUOUS)
        self.assertEqual(report.lost_note_ids, [11])

    def test_a_pronoun_list_entry_fits_an_adjectival(self):
        arr = [word("此の", "この", pos="adjectival")]
        report = migrate.migrate({"pronouns": [["この", "この", "sort", 11]]}, arr)
        self.assertEqual((matched(arr), report.by_step["reading"]), ([[11]], 1))

    def test_a_particle_twice_gets_the_one_link_on_both(self):
        arr = [word("は", "は", pos="particle"), word("は", "は", pos="particle")]
        report = migrate.migrate({"particles": [["は", "は", "sort", 11]]}, arr)
        self.assertEqual((matched(arr), report.spread, report.leftovers), ([[11], [11]], 1, []))

    def test_a_spread_link_stops_at_words_that_are_not_the_same_word(self):
        # Only the copula here reads だ; の is a different word that happens to be a particle
        arr = [word("だ", "だ", pos="copula"), word("の", "の", pos="particle")]
        report = migrate.migrate({"particles": [["だ", "だ", "sort", 11]]}, arr)
        self.assertEqual((matched(arr), report.spread), ([[11], []], 0))

    def test_two_links_on_one_repeated_particle_are_still_contested(self):
        arr = [word("は", "は", pos="particle"), word("は", "は", pos="particle")]
        report = migrate.migrate(
            {"particles": [["は", "は", "sort", 11], ["は", "は", "sort", 22]]}, arr
        )
        self.assertEqual(matched(arr), [[], []])
        self.assertEqual({lo.reason for lo in report.leftovers}, {migrate.CONTESTED})

    def test_two_links_wanting_one_word_both_stand_down(self):
        arr = [word("きっと", "きっと", pos="adverb")]
        report = migrate.migrate(
            {"adverbs": [["きっと", "きっと", "sort", 11], ["きっと", "きっと", "sort", 22]]}, arr
        )
        self.assertEqual(matched(arr), [[]])
        self.assertEqual({lo.reason for lo in report.leftovers}, {migrate.CONTESTED})

    def test_one_link_written_under_two_spellings_is_still_that_link(self):
        arr = [word("屹度", "きっと", pos="adverb")]
        report = migrate.migrate(
            {"adverbs": [["屹度", "きっと", "sort", 11], ["きっと", "きっと", "sort", 11]]}, arr
        )
        self.assertEqual((matched(arr), report.leftovers), ([[11]], []))

    def test_one_link_written_under_two_categories_is_not_read_twice(self):
        arr = [word("連れる", "つれる", pos="verb")]
        report = migrate.migrate(
            {
                "verbs": [["連れる", "つれる", "sort", 11]],
                "prefix_verbs": [["連れる", "つれる", "sort", 11]],
            },
            arr,
        )
        self.assertEqual((matched(arr), report.linked, report.leftovers), ([[11]], 1, []))

    def test_the_stronger_claim_on_a_word_wins_instead_of_both_losing(self):
        # だ is the copula as written; です only reaches it through the raw text
        arr = [word("だ", "だ", pos="copula", raw="です")]
        report = migrate.migrate(
            {"particles": [["です", "です", "sort", 11], ["だ", "だ", "sort", 22]]}, arr
        )
        self.assertEqual((matched(arr), report.by_step["form"]), ([[22]], 1))
        self.assertEqual([lo.entry.note_id for lo in report.leftovers], [11])

    def test_a_word_the_array_does_not_have_is_reported(self):
        arr = [word("口紅", "くちべに")]
        report = migrate.migrate({"particles": [["には", "には", "sort", 11]]}, arr)
        self.assertEqual((matched(arr), report.lost_note_ids), ([[]], [11]))
        self.assertEqual(report.leftovers[0].reason, migrate.NO_ELEMENT)

    def test_a_flag_is_not_overwritten_by_an_old_link(self):
        arr = [word("二十八日", "にじゅうはちにち", match_data=["dont_match"])]
        report = migrate.migrate({"nouns": [["二十八日", "にじゅうはちにち", "sort", 11]]}, arr)
        self.assertEqual((matched(arr), report.lost_note_ids), ([["dont_match"]], [11]))
        self.assertEqual(report.leftovers[0].reason, migrate.FLAGGED)

    def test_a_flagged_word_does_not_make_an_unambiguous_link_look_ambiguous(self):
        arr = [word("一", "いち", match_data=["dont_match"]), word("一", "いち")]
        report = migrate.migrate({"numbers": [["一", "いち", "sort", 11]]}, arr)
        self.assertEqual((matched(arr), report.leftovers), ([["dont_match"], [11]], []))

    def test_an_id_with_no_word_to_place_it_by_is_reported(self):
        arr = [word("口紅", "くちべに")]
        report = migrate.migrate({"nouns": [1378555076170]}, arr)
        self.assertEqual(report.leftovers[0].reason, migrate.NO_WORD)
        self.assertEqual((report.unreadable, report.lost_note_ids), (1, [1378555076170]))

    def test_an_entry_carrying_no_note_id_is_counted_and_not_reported(self):
        arr = [word("口紅", "くちべに")]
        report = migrate.migrate({"nouns": [["口紅", "くちべに"], ["笑顔", "えがお"]]}, arr)
        self.assertEqual((report.entries, report.without_note_id), (2, 2))
        self.assertEqual((report.leftovers, report.lost_note_ids, matched(arr)), ([], [], [[]]))

    def test_a_category_holding_something_other_than_a_list_is_skipped(self):
        report = migrate.migrate({"nouns": "口紅", "verbs": None}, [word()])
        self.assertEqual((report.entries, report.leftovers), (0, []))


if __name__ == "__main__":
    unittest.main()
