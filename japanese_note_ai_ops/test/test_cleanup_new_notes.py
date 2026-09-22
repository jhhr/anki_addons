"""The cleanup's note adding, which a cancelled run goes through as a finished one does.

A cancelled match run used to add none of the new notes it had prepared: the adding loop
stopped on the dialog's cancel flag, which stays set for the rest of a cancelled run. The
notes' meanings were paid for, and their placeholder ids were already in the saved word
arrays. These tests drive `add_new_notes` with the flag set, against a stand-in collection.
"""

import contextlib
import json
import unittest
from unittest import mock

from addon_modules import load_ops_module, mw

base_ops = load_ops_module("base_ops")
mwtn = load_ops_module("match_words_to_notes")

POS = 101


class FakeNote:
    def __init__(self, fields: dict, note_id: int = 0):
        self.id = note_id
        self.fields = fields

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

    def update_note_adding_progress(self, notes_added=0, total_notes=0, failed=0):
        self.adding.append((notes_added, total_notes, failed))

    def update_new_note_processing_progress(self, **_):
        pass


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
