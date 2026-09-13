"""The word-list bookkeeping `match_single_word_to_notes_from_selected` does per selected note.

Two defects the review found in that function's `bulk_op`, both of them pre-existing:

* the duplicate word it dropped was dropped with `list.remove` while iterating the same list,
  so a third identical entry survived a run that believed it had deduplicated the list;
* `word_list_dict` was bound only inside `if word_list_field in cur_note:` and read
  unconditionally afterwards, so a note whose notetype lacks the configured word list field
  either crashed the whole bulk operation with a `NameError` or, if an earlier note in the
  same selection had bound it, silently re-read *that* note's word lists.

The deduplication is exercised through `drop_duplicate_word_tuples`, which is the loop itself;
the binding is exercised through `bulk_op`, because the unbound name is a property of that
function's body rather than of anything it calls.
"""

import unittest
from copy import deepcopy

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
from addon_modules import load_ops_module

mwtn = load_ops_module("match_words_to_notes")

NOTE_TYPE_NAME = "Japanese vocab"
WORD_LIST_FIELD = "WordList"
WORD_LIST_KEYS = ["nouns", "verbs"]

# get_field_config reads the per-notetype block out of the add-on config
CONFIG = {
    "word_lists_to_process": {key: True for key in WORD_LIST_KEYS},
    NOTE_TYPE_NAME: {
        "word_list_field": WORD_LIST_FIELD,
        "word_kanjified_field": "WordKanjified",
        "word_reading_field": "WordReading",
    },
}


class _AddonManager:
    """Enough of aqt's add-on manager for `mw.addonManager.getConfig(__name__)`."""

    def getConfig(self, module_name):
        return CONFIG


class FakeNote:
    """Field access and a notetype, which is all bulk_op asks of a note."""

    def __init__(self, note_id, fields, note_type_name=NOTE_TYPE_NAME):
        self.id = note_id
        self.fields_dict = dict(fields)
        self.note_type_name = note_type_name
        self.tags: list = []

    def note_type(self):
        return {"name": self.note_type_name}

    def __contains__(self, field):
        return field in self.fields_dict

    def __getitem__(self, field):
        return self.fields_dict[field]

    def add_tag(self, tag):
        self.tags.append(tag)


class DuplicateWordRemovalTests(unittest.TestCase):
    """Finding 6: three identical entries, of which the run used to drop only one."""

    def dedupe(self, word_list_dict, keys=None):
        mwtn.drop_duplicate_word_tuples(
            word_list_dict, keys if keys is not None else WORD_LIST_KEYS, "test--"
        )
        return word_list_dict

    def test_three_identical_entries_leave_one(self):
        """The review's scenario.

        Removing while iterating deletes the *first* equal element and shifts everything left,
        so the iterator steps straight past the third entry and the list comes back with two.
        """
        word_list_dict = {"nouns": [["あ", "ア"], ["あ", "ア"], ["あ", "ア"]]}
        self.assertEqual(self.dedupe(word_list_dict), {"nouns": [["あ", "ア"]]})

    def test_two_identical_entries_leave_one(self):
        word_list_dict = {"nouns": [["あ", "ア"], ["あ", "ア"]]}
        self.assertEqual(self.dedupe(word_list_dict), {"nouns": [["あ", "ア"]]})

    def test_duplicates_around_a_word_that_is_kept(self):
        """The entry after a removed one used to be skipped, whatever it was."""
        word_list_dict = {"nouns": [["あ", "ア"], ["あ", "ア"], ["い", "イ"], ["あ", "ア"]]}
        self.assertEqual(self.dedupe(word_list_dict), {"nouns": [["あ", "ア"], ["い", "イ"]]})

    def test_the_surviving_list_is_the_same_object(self):
        """The lists are the note's own, edited in place, as the caller's dict is not rebuilt."""
        word_tuples = [["あ", "ア"], ["あ", "ア"]]
        word_list_dict = {"nouns": word_tuples}
        self.dedupe(word_list_dict)
        self.assertIs(word_list_dict["nouns"], word_tuples)

    def test_a_multi_meaning_word_may_repeat(self):
        """Duplicates carrying a meaning index are intended and stay."""
        word_list_dict = {"nouns": [["掛かる", "かかる", 1], ["掛かる", "かかる", 2]]}
        self.assertEqual(
            self.dedupe(word_list_dict),
            {"nouns": [["掛かる", "かかる", 1], ["掛かる", "かかる", 2]]},
        )

    def test_a_word_seen_in_an_earlier_list_is_a_duplicate_in_a_later_one(self):
        """One `encountered_words` spans the note's lists, which is the existing behaviour."""
        word_list_dict = {"nouns": [["あ", "ア"]], "verbs": [["あ", "ア"], ["い", "イ"]]}
        self.assertEqual(
            self.dedupe(word_list_dict),
            {"nouns": [["あ", "ア"]], "verbs": [["い", "イ"]]},
        )

    def test_an_unreadable_entry_is_kept_and_never_counts_as_a_duplicate(self):
        """Nothing here understands a bare note id, so it is left where it was found."""
        word_list_dict = {"nouns": [1378555076170, ["あ", "ア"], 1378555076170, ["あ", "ア"]]}
        self.assertEqual(
            self.dedupe(word_list_dict),
            {"nouns": [1378555076170, ["あ", "ア"], 1378555076170]},
        )

    def test_a_bare_string_is_the_same_word_as_its_list_form(self):
        """Read positionally, "なんと" was the word な with the reading ん - a different key."""
        word_list_dict = {"nouns": ["なんと", ["なんと", "なんと"]]}
        self.assertEqual(self.dedupe(word_list_dict), {"nouns": ["なんと"]})

    def test_a_word_list_that_is_not_a_list_is_left_alone(self):
        word_list_dict = {"nouns": "not a list", "verbs": [["あ", "ア"], ["あ", "ア"]]}
        self.assertEqual(
            self.dedupe(word_list_dict),
            {"nouns": "not a list", "verbs": [["あ", "ア"]]},
        )

    def test_a_missing_word_list_key_is_not_invented(self):
        word_list_dict: dict = {}
        self.assertEqual(self.dedupe(word_list_dict), {})


class BulkOpTestCase(unittest.TestCase):
    """Runs the real `bulk_op`, with the collection and the follow-up bulk op stubbed out.

    `bulk_op` is a closure, so it is taken off the `selected_notes_op` call that
    `match_single_word_to_notes_from_selected` ends with rather than imported.
    """

    def setUp(self):
        self.saved = {
            name: getattr(mwtn, name)
            for name in (
                "selected_notes_op",
                "col_find_notes",
                "col_get_notes",
                "bulk_match_words_to_notes",
                "drop_duplicate_word_tuples",
            )
        }
        self.captured: list = []
        self.deduped: list = []
        self.processed_notes: list = []

        def fake_selected_notes_op(done_text, bulk_op, *args, **kwargs):
            self.captured.append(bulk_op)
            return None

        real_dedupe = self.saved["drop_duplicate_word_tuples"]

        def recording_dedupe(word_list_dict, word_list_keys, log_prefix=""):
            # A copy, because the real helper edits the lists in place and what these tests
            # are after is which dict each note's turn was handed
            self.deduped.append(deepcopy(word_list_dict))
            return real_dedupe(word_list_dict, word_list_keys, log_prefix)

        def fake_bulk_match(**kwargs):
            self.processed_notes.append(list(kwargs["notes"]))
            return None

        mwtn.selected_notes_op = fake_selected_notes_op
        mwtn.col_find_notes = lambda query: []
        mwtn.col_get_notes = lambda nids: []
        mwtn.bulk_match_words_to_notes = fake_bulk_match
        mwtn.drop_duplicate_word_tuples = recording_dedupe

        # The stub main window has no add-on manager of its own; the instance attribute
        # shadows the class's placeholder and is removed again in tearDown, because `mw` is
        # one object shared by every suite loaded into this process.
        mwtn.mw.addonManager = _AddonManager()

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(mwtn, name, value)
        del mwtn.mw.addonManager

    def run_bulk_op(self, notes):
        mwtn.match_single_word_to_notes_from_selected([n.id for n in notes], parent=None)
        self.assertEqual(len(self.captured), 1, "bulk_op was not handed to selected_notes_op")
        bulk_op = self.captured[0]
        return bulk_op(
            col=None,
            notes=notes,
            edited_nids=[],
            progress_updater=None,
            notes_to_add_dict={},
            notes_to_update_dict={},
        )


class MissingWordListFieldTests(BulkOpTestCase):
    """Finding 7: `word_list_dict` read whether or not the note had the field to bind it."""

    def note_with_list(self, note_id, word_list_json):
        return FakeNote(
            note_id,
            {
                "WordKanjified": "引く",
                "WordReading": "ひく",
                WORD_LIST_FIELD: word_list_json,
            },
        )

    def note_without_list(self, note_id):
        """A misconfigured `word_list_field`: the notetype simply has no such field."""
        return FakeNote(note_id, {"WordKanjified": "見る", "WordReading": "みる"})

    def test_a_note_without_the_field_does_not_crash_the_run(self):
        """This used to raise NameError: word_list_dict out of the whole bulk operation."""
        self.run_bulk_op([self.note_without_list(1)])
        self.assertEqual(self.deduped, [{}])

    def test_a_note_without_the_field_does_not_reread_the_previous_note(self):
        """The leak that did not crash: note 2's turn used note 1's still-bound dict."""
        notes = [
            self.note_with_list(1, '{"nouns": [["あ", "ア"], ["あ", "ア"]]}'),
            self.note_without_list(2),
        ]
        self.run_bulk_op(notes)
        self.assertEqual(self.deduped, [{"nouns": [["あ", "ア"], ["あ", "ア"]]}, {}])

    def test_every_selected_note_still_reaches_the_follow_up_op(self):
        notes = [
            self.note_without_list(1),
            self.note_with_list(2, '{"nouns": [["あ", "ア"]]}'),
        ]
        self.run_bulk_op(notes)
        self.assertEqual(len(self.deduped), 2)
        self.assertEqual(len(self.processed_notes), 1)

    def test_an_undecodable_word_list_field_dedupes_nothing(self):
        """decode_word_list_field returns None and tags the note; the run carries on."""
        note = self.note_with_list(1, "not json at all")
        self.run_bulk_op([note])
        self.assertEqual(self.deduped, [{}])


if __name__ == "__main__":
    unittest.main()
