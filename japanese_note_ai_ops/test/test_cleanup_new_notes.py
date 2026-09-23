"""The cleanup's note adding, which a cancelled run goes through as a finished one does.

A cancelled match run used to add none of the new notes it had prepared: the adding loop
stopped on the dialog's cancel flag, which stays set for the rest of a cancelled run. The
notes' meanings were paid for, and their placeholder ids were already in the saved word
arrays. These tests drive `add_new_notes` with the flag set, against a stand-in collection.
"""

import contextlib
import json
import re
import unittest
from unittest import mock

from addon_modules import FakeClock, load_ops_module, mw

base_ops = load_ops_module("base_ops")
mwtn = load_ops_module("match_words_to_notes")

POS = 101


class FakeNote:
    def __init__(self, fields: dict, note_id: int = 0):
        self.id = note_id
        self.fields = fields
        self.tags: list = []

    def add_tag(self, tag):
        self.tags.append(tag)

    def note_type(self):
        return {"name": "Word"}

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def __setitem__(self, field, value):
        self.fields[field] = value


class FakeDecks:
    def id_for_name(self, name: str):
        return 7 if name == "Vocab" else None


class FakeCollection:
    def __init__(self, failing: tuple = ()):
        self.decks = FakeDecks()
        self.failing = failing
        self.added: list = []
        self.merged: list = []
        self.updated: list = []
        self.next_id = 500

    def add_note(self, note, deck_id):
        if note in self.failing:
            raise RuntimeError("the backend refused the note")
        self.next_id += 1
        note.id = self.next_id
        self.added.append((note, deck_id))

    def merge_undo_entries(self, pos):
        self.merged.append(pos)
        return f"changes after {len(self.merged)} merges"

    def update_notes(self, notes):
        self.updated.extend(notes)


class FakeUpdater:
    def __init__(self):
        self.adding: list = []
        self.clearing: list = []

    def begin_cleanup_stage(self):
        pass

    def update_note_adding_progress(self, notes_added=0, total_notes=0, failed=0):
        self.adding.append((notes_added, total_notes, failed))

    def update_new_note_processing_progress(self, **_):
        pass

    def update_unadded_note_clearing_progress(self, notes_cleared=0, total_notes=0):
        self.clearing.append((notes_cleared, total_notes))


CONFIG = {"Word": {"insert_deck": "Vocab", **{key: key for key in mwtn.MATCH_FIELD_KEYS}}}


def new_note(placeholder: str) -> FakeNote:
    return FakeNote({"word_list_field": "", "new_note_id_field": placeholder})


class AddNewNotesAfterCancelTests(unittest.TestCase):
    def setUp(self):
        # The flag a cancelled run leaves set for the rest of it, Escape during adding included
        mw.progress.cancel = True
        self.addCleanup(setattr, mw.progress, "cancel", False)
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def test_every_prepared_note_is_added_and_handed_on_with_its_id(self):
        col = FakeCollection()
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        handed: list = []

        def new_notes_op(new_notes, config, progress_updater):
            handed.extend((note, note.id) for note in new_notes)
            return {}

        result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, new_notes_op)

        self.assertEqual([note for note, _ in col.added], notes)
        self.assertEqual({deck for _, deck in col.added}, {7})
        self.assertEqual(handed, [(notes[0], 501), (notes[1], 502), (notes[2], 503)])
        # every add merged into the run's one undo entry
        self.assertEqual(col.merged, [POS, POS, POS])
        self.assertEqual(result.added, 3)
        self.assertEqual(result.op_changes, "changes after 3 merges")
        self.assertEqual(self.updater.adding[-1], (3, 3, 0))

    def test_what_new_notes_op_rewrote_is_saved_into_the_same_undo_entry(self):
        col = FakeCollection()
        notes = [new_note("-1111111")]
        referencing = FakeNote({"word_list_field": "[]"}, note_id=2)

        def new_notes_op(new_notes, config, progress_updater):
            return {2: referencing, new_notes[0].id: new_notes[0]}

        result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, new_notes_op)

        self.assertEqual(col.updated, [referencing, notes[0]])
        self.assertEqual(col.merged, [POS, POS])
        self.assertEqual(result.updated_nids, [2, 501])

    def test_a_note_that_fails_to_add_keeps_its_placeholder_where_it_is_referenced(self):
        failing = new_note("-1111111")
        added = new_note("-2222222")
        arr = [
            ["様", "noun", "様", "さま", [-1111111, 3], []],
            ["本", "noun", "本", "ほん", [-2222222], []],
        ]
        referencing = FakeNote({"word_list_field": json.dumps(arr)}, note_id=2)
        referencing["new_note_id_field"] = ""
        col = FakeCollection(failing=(failing,))

        with (
            mock.patch.object(mwtn, "col_find_notes", lambda _: [2]),
            mock.patch.object(mwtn, "col_get_notes", lambda _: [referencing]),
        ):
            result = base_ops.add_new_notes(
                col, [failing, added], CONFIG, POS, self.updater, mwtn.update_fake_note_ids
            )

        self.assertEqual((failing.id, added.id), (0, 501))
        saved = json.loads(referencing["word_list_field"])
        self.assertEqual([saved[0][4], saved[1][4]], [[-1111111, 3], [501]])
        self.assertEqual(failing["new_note_id_field"], "-1111111")
        self.assertEqual(added["new_note_id_field"], "501")
        self.assertEqual(result.added, 1)
        self.assertEqual(self.updater.adding[-1], (1, 2, 1))
        self.assertEqual(col.updated, [referencing, added])

    def test_without_new_notes_op_the_notes_are_still_added(self):
        col = FakeCollection()

        result = base_ops.add_new_notes(col, [new_note("-1111111")], CONFIG, POS, self.updater)

        self.assertEqual((result.added, result.updated_nids), (1, []))
        self.assertEqual(col.updated, [])


class SlowCollection(FakeCollection):
    def __init__(self, clock: FakeClock):
        super().__init__()
        self.clock = clock

    def add_note(self, note, deck_id):
        # copy_anywhere's on-add definitions run in here, which is what makes adding slow
        self.clock.advance(2)
        super().add_note(note, deck_id)


class CleanupStageClockTests(unittest.TestCase):
    """The adding and the new-note processing each time themselves, not the run before them.

    With the real progress updater under a fake clock, and an hour of API work behind it.
    """

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.drawn: list = []
        saved_update = mw.progress.update
        mw.progress.update = lambda **kwargs: self.drawn.append(kwargs["label"])
        self.addCleanup(setattr, mw.progress, "update", saved_update)
        self.clock = FakeClock()
        saved_time = base_ops.time
        base_ops.time = self.clock
        self.addCleanup(setattr, base_ops, "time", saved_time)
        self.updater = base_ops.AsyncTaskProgressUpdater(total_tasks=200, title="Matching")
        self.updater.increment_counts(tasks_done=200, notes_done=40)
        self.clock.advance(3600)
        self.updater.begin_cleanup()

    def test_each_stage_starts_its_own_clock_and_its_progress_calls_do_not_restart_it(self):
        col = SlowCollection(self.clock)
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]

        def new_notes_op(new_notes, config, progress_updater):
            for processed in (1, 2):
                self.clock.advance(5)
                progress_updater.update_new_note_processing_progress(
                    new_notes_processed=processed, total_notes=3
                )
            return {}

        base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, new_notes_op)

        adding = [label for label in self.drawn if "Adding notes" in label]
        self.assertIn("3/3", adding[-1])
        self.assertIn("Time: 00:00:06", adding[-1])
        self.assertIn("Avg time per note: 2.00s", adding[-1])
        processing = [label for label in self.drawn if "Processing new notes" in label]
        self.assertIn("2/3", processing[-1])
        self.assertIn("Time: 00:00:10", processing[-1])
        self.assertIn("ETA: 00:00:05", processing[-1])


def w(form: str, match_data: list, subs: tuple = ()) -> list:
    return [form, "noun", form, "よみ", match_data, list(subs)]


def sentence(note_id: int, *words: list) -> FakeNote:
    fields = {"word_list_field": mwtn.format_word_array(list(words)), "new_note_id_field": ""}
    return FakeNote(fields, note_id)


def match_data(note: FakeNote) -> list:
    return [elem[4] for _, elem in mwtn.match_flags.iter_words(words_of(note))]


def words_of(note: FakeNote) -> list:
    arr = mwtn.match_flags.decode_word_array(note["word_list_field"])
    assert arr is not None
    return arr


class FakeSearch:
    """The collection behind `col_find_notes` / `col_get_notes`, holding the notes as saved:
    nothing the op changes reaches it, as nothing is saved until cleanup gets the notes back,
    and each read is a fresh note. A `"field:*text*"` term is a substring search, as in Anki,
    so a longer placeholder holding a shorter one's digits is found too."""

    def __init__(self, *notes: FakeNote):
        self.saved = {note.id: dict(note.fields) for note in notes}
        self.queries: list = []
        self.read: list = []

    def find_notes(self, query: str) -> list:
        self.queries.append(query)
        terms = re.findall(r'"(\w+):\*(-\d+)\*"', query)
        return [
            nid
            for nid, fields in self.saved.items()
            if any(field in fields and text in fields[field] for field, text in terms)
        ]

    def get_notes(self, nids) -> list:
        self.read.extend(nids)
        return [FakeNote(dict(self.saved[nid]), nid) for nid in nids]

    def patched(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(mwtn, "col_find_notes", self.find_notes))
        stack.enter_context(mock.patch.object(mwtn, "col_get_notes", self.get_notes))
        return stack


class ClearUnaddedNoteIdsTests(unittest.TestCase):
    """The new notes a cancel of the adding left out: the words linked to them are matched
    again, since no note will ever hold their placeholders. A failed add keeps its own."""

    def setUp(self):
        self.updater = FakeUpdater()

    def test_the_words_of_a_note_not_added_are_cleared_where_they_are_saved(self):
        not_added = new_note("-1111111")
        # its add was attempted and raised; not among the notes handed over
        failed = new_note("-2222222")
        both = sentence(2, w("様", [-1111111, 3]), w("本", [-2222222]))
        nested = sentence(3, w("本棚", ["dontmatch"], (w("本", [-1111111]), w("棚", [444]))))
        longer = sentence(4, w("机", [-11111112]))
        search = FakeSearch(both, nested, longer)

        with search.patched():
            updated = mwtn.clear_unadded_note_ids([not_added], CONFIG, self.updater)

        # found by the substring search, but its placeholder is another note's: not handed
        # back to be saved
        self.assertEqual(sorted(updated), [2, 3])
        self.assertEqual(match_data(updated[2]), [["match"], [-2222222]])
        self.assertEqual(match_data(updated[3]), [["dontmatch"], ["match"], [444]])
        self.assertEqual(search.read, [2, 3, 4])
        self.assertEqual(search.queries, ['("word_list_field:*-1111111*")'])
        self.assertEqual((not_added.id, not_added["new_note_id_field"]), (0, "-1111111"))
        self.assertEqual(failed["new_note_id_field"], "-2222222")
        self.assertEqual(self.updater.clearing, [(0, 1), (1, 1)])

    def test_many_placeholders_are_searched_for_together_and_a_note_is_read_once(self):
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        first_and_last = sentence(2, w("様", [-1111111]), w("本", [-3333333, 2]))
        second = sentence(3, w("棚", [-2222222]))
        search = FakeSearch(first_and_last, second)

        with search.patched(), mock.patch.object(mwtn, "PLACEHOLDERS_PER_SEARCH", 2):
            updated = mwtn.clear_unadded_note_ids(notes, CONFIG, self.updater)

        self.assertEqual(
            search.queries,
            [
                '("word_list_field:*-1111111*" OR "word_list_field:*-2222222*")',
                '("word_list_field:*-3333333*")',
            ],
        )
        # the second search finds the first note again, in the collection as it was saved, and
        # takes it as the first search left it rather than reading it again
        self.assertEqual(search.read, [2, 3])
        self.assertEqual(sorted(updated), [2, 3])
        self.assertEqual(match_data(updated[2]), [["match"], ["match"]])
        self.assertEqual(match_data(updated[3]), [["match"]])
        self.assertEqual(self.updater.clearing, [(0, 3), (2, 3), (3, 3)])

    def test_a_field_that_is_no_array_is_left_as_it_is_and_tagged(self):
        broken_text = '[["様", "noun", "様", "さま", [-1111111], ['
        broken = FakeNote({"word_list_field": broken_text, "new_note_id_field": ""}, 5)
        # a note of a kind the match op writes no arrays into, whatever its field holds
        other_kind = FakeNote({"word_list_field": "-1111111"}, 6)
        search = FakeSearch(broken, other_kind)

        with search.patched():
            updated = mwtn.clear_unadded_note_ids([new_note("-1111111")], CONFIG, self.updater)

        self.assertEqual(search.read, [5, 6])
        self.assertEqual(list(updated), [5])
        self.assertEqual(updated[5]["word_list_field"], broken_text)
        self.assertEqual(updated[5].tags, [mwtn.INVALID_WORD_ARRAY_TAG])

    def test_a_note_holding_no_placeholder_is_not_searched_for(self):
        search = FakeSearch(sentence(2, w("様", [42])))

        with search.patched():
            updated = mwtn.clear_unadded_note_ids(
                [new_note(""), new_note("42")], CONFIG, self.updater
            )

        self.assertEqual((search.queries, updated), ([], {}))


class FinalMessageTests(unittest.TestCase):
    def setUp(self):
        self.shown: list = []
        saved = base_ops.tooltip
        base_ops.tooltip = lambda message, **_: self.shown.append(message)
        self.addCleanup(setattr, base_ops, "tooltip", saved)

    def succeed(self, new_notes_added: int) -> str:
        base_ops.on_bulk_success(
            None, "Matched words", [1], [], [1, 2], None, new_notes_added=new_notes_added
        )
        return self.shown[-1]

    def test_the_new_notes_added_are_counted(self):
        self.assertIn("Added 3 new notes.", self.succeed(3))
        self.assertIn("Added 1 new note.", self.succeed(1))

    def test_no_line_when_none_were_added(self):
        self.assertNotIn("Added", self.succeed(0))


if __name__ == "__main__":
    unittest.main()
