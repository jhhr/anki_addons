"""get_matching_notes_for_word_and_reading, now that it reads the index instead of searching.

The index's own behaviour is covered in test_word_index. What is left here is the translation
that used to be a query string: which spellings of the word get looked up, that the reading
filter still drops what it dropped, and that the fetch afterwards asks for only what survived
both - which is the point of moving the reading filter ahead of it.

Notes are faked down to an id, because nothing in this function reads a field off one any
more: the word, the reading and the sort marker all come out of the index.

WordArraySearchTests covers the searches the other way, from a vocab note to the arrays
holding its word.
"""

import asyncio
import types
import unittest
from unittest import mock

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
from addon_modules import load_ops_module, mw
from test_word_index import FIELDS, VOCAB_MID, VOCAB_ORDS, vocab_row

wi = load_ops_module("word_index")
nc = load_ops_module("note_cache")
mwtn = load_ops_module("match_words_to_notes")
role_gate = load_ops_module("role_gate")
tsm = load_ops_module("tag_notes_matched_status", "sync_local_ops")


class FakeNote:
    """All the function does with a fetched note is put it in the list it returns."""

    def __init__(self, note_id):
        self.id = note_id


class LookupTestCase(unittest.TestCase):
    """The collection is the one thing under this function that is not the index.

    Faked at note_cache's own seam rather than this function's, so what the tests see in
    self.fetched is what actually reached the collection - the run's note cache included.
    """

    def setUp(self):
        self.fetched: "list[list[int]]" = []
        self.real_get_notes = nc.get_notes_async

        async def fake_get_notes_async(note_ids):
            ids = list(note_ids)
            self.fetched.append(ids)
            return [FakeNote(note_id) for note_id in ids]

        nc.get_notes_async = fake_get_notes_async
        self.note_cache = nc.NoteCache()

    def tearDown(self):
        nc.get_notes_async = self.real_get_notes

    def lookup(self, word, reading, *rows, notes_to_update_dict=None):
        index = wi.WordIndex.from_rows(FIELDS, {VOCAB_MID: VOCAB_ORDS}, list(rows))
        notes = asyncio.run(
            mwtn.get_matching_notes_for_word_and_reading(
                word=word,
                reading=reading,
                notes_to_update_dict=notes_to_update_dict or {},
                log_prefix="test--",
                word_note_index=index,
                note_cache=self.note_cache,
            )
        )
        return [note.id for note in notes]


class WordSpellingTests(LookupTestCase):
    def test_a_word_with_a_matching_reading_is_found(self):
        self.assertEqual(self.lookup("私", "わたし", vocab_row(1, "私", "私", "わたし", "私")), [1])

    def test_a_suru_verb_is_found_by_its_own_spelling(self):
        # The element carries する, so it matches exactly; nothing adds it or takes it off.
        self.assertEqual(
            self.lookup(
                "勉強する",
                "べんきょうする",
                vocab_row(1, "勉強する", "勉強する", "べんきょうする", "勉強する"),
            ),
            [1],
        )

    def test_a_bare_noun_does_not_reach_the_suru_verb(self):
        # 期[き] used to land on 期する this way. Whether Xする is a word of its own is
        # recorded by there being a note for it, so the bare noun stays unmatched until
        # one exists and the judge decides whether it deserves one.
        self.assertEqual(
            self.lookup(
                "勉強",
                "べんきょう",
                vocab_row(1, "勉強する", "勉強する", "べんきょうする", "勉強する"),
            ),
            [],
        )

    def test_a_zuru_verb_finds_its_jiru_note(self):
        # 奉ずる and 奉じる are one verb and the collection spells it じる, so the ずる element
        # asks for the じる note. The reading differs too, which the reading filter allows.
        self.assertEqual(
            self.lookup(
                "奉ずる", "ほうずる", vocab_row(1, "奉じる", "奉じる", "ほうじる", "奉じる")
            ),
            [1],
        )

    def test_a_zuru_verb_still_finds_a_zuru_note(self):
        # Where no じる note exists the ずる one still answers: nothing is taken away.
        self.assertEqual(
            self.lookup(
                "奉ずる", "ほうずる", vocab_row(1, "奉ずる", "奉ずる", "ほうずる", "奉ずる")
            ),
            [1],
        )

    def test_a_jiru_verb_does_not_reach_a_zuru_note(self):
        # One direction only. Everything points at じる, and a ずる note left over is a
        # duplicate for the dedup op rather than one to keep feeding.
        self.assertEqual(
            self.lookup(
                "奉じる", "ほうじる", vocab_row(1, "奉ずる", "奉ずる", "ほうずる", "奉ずる")
            ),
            [],
        )

    def test_both_notes_come_back_when_both_exist(self):
        # Choosing between a word's two notes is not the matcher's call to make.
        self.assertEqual(
            sorted(
                self.lookup(
                    "奉ずる",
                    "ほうずる",
                    vocab_row(1, "奉ずる", "奉ずる", "ほうずる", "奉ずる"),
                    vocab_row(2, "奉じる", "奉じる", "ほうじる", "奉じる"),
                )
            ),
            [1, 2],
        )

    def test_a_reading_that_does_not_end_in_zuru_is_left_alone(self):
        # 論ずる spelled so but read another way is not silently turned into a じる verb.
        self.assertEqual(
            self.lookup(
                "捻ずる", "ねじる", vocab_row(1, "捻じる", "捻じる", "ねじじる", "捻じる")
            ),
            [],
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
            self.lookup(
                "ください", "ください", vocab_row(1, "", "ください", "ください", "ください")
            ),
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

    def test_a_suru_reading_is_not_matched_by_the_plain_one(self):
        # The spelling is found, so this is the reading filter's call alone: べんきょう is
        # not べんきょうする, and する is no longer bridged on either side of the comparison.
        self.assertEqual(
            self.lookup(
                "勉強する",
                "べんきょう",
                vocab_row(1, "勉強する", "勉強する", "べんきょうする", "勉強する"),
            ),
            [],
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
        self.assertEqual(
            self.lookup("彼女", "かのじょ", vocab_row(1, "私", "私", "わたし", "私")), []
        )
        # The collection is not asked at all, not even for an empty list of ids
        self.assertEqual(self.fetched, [])

    def test_a_note_the_run_already_fetched_is_not_fetched_again(self):
        # Why the cache exists: a hot word is looked up once per sentence that mentions it,
        # and every one of those lookups used to re-fetch the same notes
        rows = [vocab_row(1, "私", "私", "わたし", "私")]
        self.assertEqual(self.lookup("私", "わたし", *rows), [1])
        self.assertEqual(self.lookup("私", "わたし", *rows), [1])
        self.assertEqual(self.fetched, [[1]])

    def test_the_same_note_object_comes_back_each_time(self):
        # Every reader prefers notes_to_update_dict for a note that has been edited, so a
        # cached object is only handed out for notes nobody has touched - but it has to be the
        # same object, or an edit made through one copy would be invisible through the other
        rows = [vocab_row(1, "私", "私", "わたし", "私")]
        index = wi.WordIndex.from_rows(FIELDS, {VOCAB_MID: VOCAB_ORDS}, rows)

        def fetch():
            return asyncio.run(
                mwtn.get_matching_notes_for_word_and_reading(
                    word="私",
                    reading="わたし",
                    notes_to_update_dict={},
                    log_prefix="test--",
                    word_note_index=index,
                    note_cache=self.note_cache,
                )
            )

        self.assertIs(fetch()[0], fetch()[0])

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
                word_note_index=wi.WordIndex.from_rows(FIELDS, {VOCAB_MID: VOCAB_ORDS}, rows),
                note_cache=self.note_cache,
            )
        )
        self.assertEqual(self.fetched, [[2]])
        self.assertIs(notes[0], edited)
        self.assertEqual([note.id for note in notes], [1, 2])


class VocabNote:
    """A selected vocab note, its word and reading under its type's field names."""

    def __init__(self, type_name, fields):
        self.id = 5
        self.type_name = type_name
        self.fields = fields
        self.tags: "list[str]" = []

    def note_type(self):
        return {"name": self.type_name}

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def add_tag(self, tag):
        self.tags.append(tag)

    def remove_tag(self, tag):
        if tag in self.tags:
            self.tags.remove(tag)


ONE_TYPE = {
    "Word": {
        "word_list_field": "word_list_field",
        "word_kanjified_field": "word_kanjified_field",
        "word_reading_field": "word_reading_field",
        "word_sort_field": "word_sort_field",
    }
}
# Each block's field names its own, so that one read from the wrong block is not on the note
TWO_TYPE = {
    "Sentence": {"vocab_note_type": "Word", "word_list_field": "s_array"},
    "Word": {
        "sentence_note_type": "Sentence",
        "word_kanjified_field": "v_kanjified",
        "word_reading_field": "v_reading",
        "word_sort_field": "v_sort",
    },
}


class WordArraySearchTests(unittest.TestCase):
    """The other way round: from a selected vocab note to the arrays holding its word, which
    the single-word rematch and the matched-status tags search. They are its sentence type's
    notes, searched by that type's array field (SPEC 6 P4); a vocab note's old array field of
    the same name is not one of them."""

    def setUp(self):
        self.queries: "list[str]" = []

    def find_notes(self, query):
        self.queries.append(query)
        return [11]

    def rematch(self, config, note) -> dict:
        """Runs the single-word rematch's selection: what it hands the match run."""
        started: list = []
        handed: list = []
        manager = types.SimpleNamespace(getConfig=lambda _name: config)
        with (
            mock.patch.object(mw, "addonManager", manager),
            mock.patch.object(mwtn, "selected_notes_op", lambda *args, **_: started.append(args)),
            mock.patch.object(mwtn, "col_find_notes", self.find_notes),
            mock.patch.object(mwtn, "col_get_notes", lambda nids: [FakeNote(n) for n in nids]),
            mock.patch.object(mwtn, "bulk_match_words_to_notes", lambda **op: handed.append(op)),
        ):
            mwtn.match_single_word_to_notes_from_selected([note.id], None)
            bulk_op = started[0][1]
            bulk_op(None, [note], [], None, {}, {})
        return handed[0]

    def rematch_regex(self) -> str:
        states = mwtn.match_targets.states_to_match(reprocess=None)
        return mwtn.word_array_query_regex("本", "ほん", states)

    def test_the_rematch_of_a_one_type_note_searches_its_own_type_as_before(self):
        note = VocabNote("Word", {"word_kanjified_field": "本", "word_reading_field": "ほん"})

        handed = self.rematch(ONE_TYPE, note)

        self.assertEqual(
            self.queries, [f'"note:Word" "word_list_field:re:{self.rematch_regex()}"']
        )
        self.assertEqual(handed["limit_word_and_reading_dict"], {11: [("本", "ほん")]})

    def test_the_rematch_of_a_vocab_note_searches_the_sentence_notes(self):
        note = VocabNote("Word", {"v_kanjified": "本", "v_reading": "ほん", "s_array": ""})

        handed = self.rematch(TWO_TYPE, note)

        self.assertEqual(self.queries, [f'"note:Sentence" "s_array:re:{self.rematch_regex()}"'])
        self.assertEqual([found.id for found in handed["notes"]], [11])
        self.assertEqual(handed["limit_word_and_reading_dict"], {11: [("本", "ほん")]})

    def test_a_selected_sentence_note_is_left_out_of_the_rematch_with_one_report(self):
        # Its type has no word fields: the rematch used to fail the whole run on it
        note = VocabNote("Sentence", {"s_array": ""})

        with mock.patch.object(role_gate, "report_error") as report:
            handed = self.rematch(TWO_TYPE, note)

        self.assertEqual(self.queries, [])
        self.assertEqual(handed["notes"], [])
        report.assert_called_once()
        self.assertIn("runs on vocab notes", report.call_args.args[0])

    def tag(self, config, note) -> "list[str]":
        col = types.SimpleNamespace(find_notes=self.find_notes)
        with mock.patch.object(mw, "col", col, create=True):
            self.assertTrue(tsm.tag_notes_matched_status_for_note(config, note, {}, {}))
        return note.tags

    def status_regexes(self) -> "list[str]":
        linked = [mwtn.MatchState.LINKED, mwtn.MatchState.RATED]
        return [
            mwtn.word_array_query_regex("本", "ほん", states)
            for states in (linked, [mwtn.MatchState.MATCH])
        ]

    def test_the_matched_status_of_a_one_type_note_counts_its_own_type_as_before(self):
        note = VocabNote("Word", {"word_kanjified_field": "本", "word_reading_field": "ほん"})

        tags = self.tag(ONE_TYPE, note)

        self.assertEqual(
            self.queries,
            [f'"note:Word" "word_list_field:re:{regex}"' for regex in self.status_regexes()],
        )
        self.assertEqual(tags, [tsm.SOME_NOTES_UNMATCHED_TAG])

    def test_the_matched_status_of_a_vocab_note_counts_the_sentence_notes(self):
        note = VocabNote("Word", {"v_kanjified": "本", "v_reading": "ほん", "s_array": ""})

        tags = self.tag(TWO_TYPE, note)

        self.assertEqual(
            self.queries,
            [f'"note:Sentence" "s_array:re:{regex}"' for regex in self.status_regexes()],
        )
        self.assertEqual(tags, [tsm.SOME_NOTES_UNMATCHED_TAG])


if __name__ == "__main__":
    unittest.main()
