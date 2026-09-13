"""A dictionary that could not answer must not be written down as a dictionary that had nothing.

`MDXLookupError` exists because a transient failure used to be indistinguishable from a genuine
miss and so got cached as one. Keeping it out of the memo was only half the problem: the other
half is the collection. Both tagging paths write `NO_DICTIONARY_ENTRY_TAG` when a lookup comes
back empty, and that tag is terminal - `needs_meaning_mapping` skips a note carrying it - so a
600ms outage used to cost those notes their meanings for good, on every run after.

So the tests here are all the same shape: run the path twice, once with the dictionaries raising
and once with them answering "nothing", and assert the two now differ in exactly one way, the
tag. The word itself still gets no definition either way, which is the behaviour that was always
there and is not what the fix changes.
"""

import unittest

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
from addon_modules import load_ops_module

mdx = load_ops_module("mdx_dictionary", "sync_local_ops")
mam = load_ops_module("make_all_meanings")
cm = load_ops_module("clean_meaning")

NO_DICTIONARY_ENTRY_TAG = mam.NO_DICTIONARY_ENTRY_TAG
MakeMeaningsResult = mam.MakeMeaningsResult

NOTETYPE = "Japanese vocab"

# Every field name either function asks for, under the one notetype the notes below claim
CONFIG = {
    NOTETYPE: {
        "word_field": "vocab-kanjified",
        "word_normal_field": "vocab",
        "word_reading_field": "vocab-kana",
        "word_sort_field": "vocab-key",
        "meaning_field": "jp-meaning",
        "english_meaning_field": "en-meaning",
        "sentence_field": "sentence",
        "new_note_id_field": "new-note-id",
    },
    "make_meanings_model": "a-model",
}


class FakeNote:
    """A note with real field storage and real tags, which the aqt stub's Note has neither of."""

    def __init__(self, note_id: int, **fields: str):
        self.id = note_id
        self._fields = {
            "vocab-kanjified": "見る",
            "vocab": "見る",
            "vocab-kana": "みる",
            "vocab-key": "見る",
            "jp-meaning": "",
            "en-meaning": "",
            "sentence": "",
            "new-note-id": "0",
        }
        self._fields.update(fields)
        self.tags: list[str] = []

    def note_type(self):
        return {"name": NOTETYPE}

    def __contains__(self, key):
        return key in self._fields

    def __getitem__(self, key):
        return self._fields[key]

    def __setitem__(self, key, value):
        self._fields[key] = value

    def add_tag(self, tag: str) -> None:
        if tag not in self.tags:
            self.tags.append(tag)

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags


class StubHelper:
    """`mdx_helper` reduced to the one call that matters, in its two indistinguishable moods."""

    def __init__(self, outage: bool):
        self.outage = outage
        self.calls = 0

    def load_mdx_dictionaries_if_needed(self, *args, **kwargs):
        return self

    def get_definition_text(self, **kwargs):
        self.calls += 1
        if self.outage:
            raise mdx.MDXLookupError("unable to open database file")
        # What a word that is in none of the dictionaries returns
        return None


class RaisingDictionaries:
    """A `MultiDictionaryQuery` whose queries fail, the way the measured outage made them."""

    def __init__(self):
        self.queries = 0

    def query_all_japanese(self, *args, **kwargs):
        self.queries += 1
        raise mdx.MDXLookupError("unable to open database file")

    def query(self, *args, **kwargs):
        return self.query_all_japanese()


class GetDefinitionTextTests(unittest.TestCase):
    """The seam itself: the failure leaves `get_definition_text` as a failure, not as None."""

    def helper(self) -> "mdx.AnkiMDXHelper":
        helper = mdx.AnkiMDXHelper()
        helper.multi_dict = RaisingDictionaries()
        return helper

    def test_a_failed_lookup_raises_rather_than_returning_none(self):
        helper = self.helper()
        with self.assertRaises(mdx.MDXLookupError):
            helper.get_definition_text(word="見る", reading="みる")

    def test_the_failure_is_still_not_remembered(self):
        # The memo behaviour MDXLookupError was introduced for, unchanged by the re-raise: the
        # second ask scans again instead of being served the first ask's failure as a fact.
        helper = self.helper()
        for _ in range(2):
            with self.assertRaises(mdx.MDXLookupError):
                helper.get_definition_text(word="見る", reading="みる")
        self.assertEqual(helper.multi_dict.queries, 2)

    def test_a_word_in_no_dictionary_still_returns_none(self):
        helper = mdx.AnkiMDXHelper()
        helper.multi_dict = object()
        helper._scan = lambda word, reading, pick: []
        self.assertIsNone(helper.get_definition_text(word="見る", reading="みる"))


class MakeAllMeaningsForWordTests(unittest.TestCase):
    """The result the tagging decision reads has to carry the distinction."""

    def setUp(self):
        self.real_helper = mam.mdx_helper
        self.addCleanup(setattr, mam, "mdx_helper", self.real_helper)

    def result_for(self, outage: bool) -> "MakeMeaningsResult":
        mam.mdx_helper = StubHelper(outage=outage)
        return mam.make_all_meanings_for_word(CONFIG, "見る", "みる", {})

    def test_an_outage_is_not_no_dictionary_entry(self):
        self.assertEqual(self.result_for(outage=True), MakeMeaningsResult.DICTIONARY_LOOKUP_FAILED)

    def test_a_genuine_miss_is_still_no_dictionary_entry(self):
        self.assertEqual(self.result_for(outage=False), MakeMeaningsResult.NO_DICTIONARY_ENTRY)


class MakeMeaningsInNoteTests(unittest.TestCase):
    """`make_meanings_in_note` tags the note and every sibling meaning note - or must not."""

    def setUp(self):
        self.addCleanup(setattr, mam, "mdx_helper", mam.mdx_helper)
        self.addCleanup(setattr, mam, "col_find_notes", mam.col_find_notes)
        self.addCleanup(setattr, mam, "col_get_notes", mam.col_get_notes)
        self.sibling = FakeNote(101)
        mam.col_find_notes = lambda query: [101]
        mam.col_get_notes = lambda ids: [self.sibling]

    def run_for(self, outage: bool):
        mam.mdx_helper = StubHelper(outage=outage)
        note = FakeNote(100)
        processed: set = set()
        notes_to_update: dict = {}
        mam.make_meanings_in_note(CONFIG, note, processed, {}, {}, notes_to_update)
        return note, processed, notes_to_update

    def test_an_outage_leaves_the_note_and_its_siblings_untagged(self):
        note, processed, notes_to_update = self.run_for(outage=True)
        self.assertEqual(note.tags, [])
        self.assertEqual(self.sibling.tags, [])
        self.assertEqual(notes_to_update, {})
        # Nothing is known about the word yet, so a later note of the same word still tries
        self.assertEqual(processed, set())

    def test_a_genuine_miss_still_tags_the_note_and_its_siblings(self):
        note, processed, notes_to_update = self.run_for(outage=False)
        self.assertEqual(note.tags, [NO_DICTIONARY_ENTRY_TAG])
        self.assertEqual(self.sibling.tags, [NO_DICTIONARY_ENTRY_TAG])
        self.assertEqual(sorted(notes_to_update), [100, 101])
        self.assertEqual(processed, {mam.make_meaning_dict_key("見る", "みる")})

    def test_a_note_left_untagged_by_an_outage_is_retried_on_the_next_run(self):
        # The whole point of not tagging: NO_DICTIONARY_ENTRY_TAG is one of the two tags this
        # function skips a note for, so tagging on an outage is what made the loss permanent.
        note, _, _ = self.run_for(outage=True)
        mam.mdx_helper = StubHelper(outage=False)
        mam.make_meanings_in_note(CONFIG, note, set(), {}, {}, {})
        self.assertEqual(note.tags, [NO_DICTIONARY_ENTRY_TAG])


class CleanMeaningInNoteTests(unittest.TestCase):
    """The same decision, made directly rather than through a result, in the single-note path."""

    def setUp(self):
        self.addCleanup(setattr, cm, "mdx_helper", cm.mdx_helper)
        self.addCleanup(setattr, cm, "get_sentences_for_note", cm.get_sentences_for_note)
        self.addCleanup(setattr, cm, "get_new_meaning_from_model", cm.get_new_meaning_from_model)
        cm.get_sentences_for_note = lambda *args, **kwargs: []
        # No dictionary entry either way, so both runs reach the write-one-from-scratch branch
        cm.get_new_meaning_from_model = lambda *args, **kwargs: ("新しい", "new")

    def run_for(self, outage: bool):
        cm.mdx_helper = StubHelper(outage=outage)
        note = FakeNote(100)
        notes_to_update: dict = {}
        cm.clean_meaning_in_note(
            CONFIG, note, {}, notes_to_update, {}, allow_update_all_meanings=False
        )
        return note, notes_to_update

    def test_an_outage_does_not_tag_the_note(self):
        note, notes_to_update = self.run_for(outage=True)
        self.assertNotIn(NO_DICTIONARY_ENTRY_TAG, note.tags)
        # The meaning was still written, which is what the run did before and still does
        self.assertEqual(note["jp-meaning"], "新しい")
        self.assertIn(100, notes_to_update)

    def test_a_genuine_miss_still_tags_the_note(self):
        note, notes_to_update = self.run_for(outage=False)
        self.assertIn(NO_DICTIONARY_ENTRY_TAG, note.tags)
        self.assertEqual(note["jp-meaning"], "新しい")
        self.assertIn(100, notes_to_update)


if __name__ == "__main__":
    unittest.main()
