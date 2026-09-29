"""capture_notes, and the read points that feed it: what a run that records its notes stores of
the notes it read and wrote, of the meanings file, and of the notes it only learned the ids of.

Each test installs a store in a temporary directory, begins a run with `notes=True` and works in
its scope, as `notes_run` does; rows are read with the test's own connection after `flush()`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import types
import unittest
from contextlib import closing
from unittest import mock

from addon_modules import load_ops_module, mw

capture = load_ops_module("capture")
capture_notes = load_ops_module("capture_notes")
ca = load_ops_module("collection_access")
api = load_ops_module("api_client")

CONFIG = {"Vocab": {"new_note_id_field": "new-id"}}


class Note:
    """What capture_notes reads of an Anki note."""

    def __init__(self, nid, fields, mid=1500, tags=(), type_name="Vocab"):
        self.id = nid
        self.mid = mid
        self.guid = f"g{nid}"
        self.tags = list(tags)
        self._fields = dict(fields)
        self._type = {"name": type_name}

    def items(self):
        return list(self._fields.items())

    def keys(self):
        return list(self._fields)

    @property
    def fields(self):
        return list(self._fields.values())

    def note_type(self):
        return self._type

    def __contains__(self, key):
        return key in self._fields

    def __getitem__(self, key):
        return self._fields[key]


class Collection:
    """Notes by id, and the writes a run's cleanup makes (test_capture_runs runs one)."""

    def __init__(self, *notes):
        self.notes = {note.id: note for note in notes}
        self.fetched: list[int] = []
        self.updated: list[int] = []
        self.db = None

    def get_note(self, nid):
        self.fetched.append(int(nid))
        if nid not in self.notes:
            raise KeyError(nid)
        return self.notes[nid]

    def find_notes(self, query):
        return sorted(self.notes)

    def update_notes(self, notes):
        self.updated.extend(note.id for note in notes)

    def remove_notes(self, nids):
        for nid in nids:
            self.notes.pop(nid, None)

    def merge_undo_entries(self, pos):
        return "changes"

    def undo_status(self):
        return types.SimpleNamespace(undo="Edited", redo="", last_step=pos_of_run)


# The undo entry every run here merges into
pos_of_run = 3


class CaptureNotesTestCase(unittest.TestCase):
    notes_on = True

    def setUp(self) -> None:
        self.assertIsNone(capture.current_store(), "a store was left installed")
        capture._quiet_until.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "capture.sqlite3")
        self.assertTrue(capture.install(self.path))
        self.addCleanup(capture.shutdown)
        self.run_id = capture.begin_run("run", notes=self.notes_on)
        scope = capture.run_scope(self.run_id)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def rows(self, sql):
        self.assertTrue(capture.current_store().flush())
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(sql).fetchall()

    def snapshots(self):
        return [
            (note_id, stage, json.loads(text))
            for note_id, stage, text in self.rows(
                "SELECT note_id, stage, text FROM note_snapshots"
                " JOIN blobs ON blobs.hash = note_hash ORDER BY snapshot_id"
            )
        ]

    def events(self, kind=None):
        rows = self.rows("SELECT kind, note_id, payload_json FROM events ORDER BY event_id")
        return [
            (row_kind, note_id, json.loads(payload))
            for row_kind, note_id, payload in rows
            if kind is None or row_kind == kind
        ]


class NoteRecordTests(CaptureNotesTestCase):
    def test_a_note_is_its_fields_tags_type_and_guid_without_its_bookkeeping(self):
        note = Note(7, {"Word": "食べる", "new-id": ""}, tags=["jp"])
        capture_notes.snapshot_notes("read", [note])

        self.assertEqual(
            self.snapshots(),
            [
                (7, "read", {"id": 7, "guid": "g7", "mid": 1500, "tags": ["jp"],
                             "fields": {"Word": "食べる", "new-id": ""}}),
            ],
        )

    def test_a_new_note_goes_by_its_placeholder_or_a_made_up_id(self):
        with_placeholder = Note(0, {"Word": "飲む", "new-id": "-4242"})
        without = Note(0, {"Word": "見る"}, type_name="Other")

        capture_notes.snapshot_new_notes("proposed", [with_placeholder, without], CONFIG)

        [(first, _, _), (second, _, _)] = self.snapshots()
        self.assertEqual(first, -4242)
        self.assertLess(second, -(10**14))


class LostRecordTests(CaptureNotesTestCase):
    """A record the run could not make is counted with those the store dropped: seen before it
    was built, the note is never recorded again, and a run that said it lost nothing was
    exported without it."""

    def dropped(self):
        capture.end_run(self.run_id, "completed")
        return self.rows(f"SELECT dropped FROM runs WHERE run_id = {self.run_id}")[0][0]

    def test_a_note_whose_record_raises_is_lost_and_not_retried(self):
        def broken():
            raise IndexError("a note stored short of its fields")

        with self.assertLogs(capture.logger, "WARNING"):
            capture.snapshot_note("read", 7, broken)
        capture.snapshot_note("read", 7, lambda: {"id": 7})

        self.assertEqual(self.snapshots(), [])
        self.assertEqual(capture.unread_references(), [])
        self.assertEqual(self.dropped(), 1)

    def test_an_event_whose_payload_has_no_json_is_lost(self):
        with self.assertLogs(capture.logger, "WARNING"):
            # Keys of two types cannot be sorted into the one JSON text
            capture.event("phase", {1: "one", "two": 2})

        self.assertEqual(self.rows("SELECT kind, payload_json FROM events"), [("phase", None)])
        self.assertEqual(self.dropped(), 1)

    def test_a_run_that_lost_nothing_says_so(self):
        capture_notes.snapshot_notes("read", [Note(2, {"Word": "本"})])

        self.assertEqual(self.dropped(), 0)


class ReferenceTests(CaptureNotesTestCase):
    def test_a_note_only_referenced_is_fetched_once_as_read_and_a_missing_one_noted(self):
        known = Note(3, {"Word": "箱"})
        col = Collection(known)
        capture_notes.snapshot_notes("read", [Note(2, {"Word": "本"})])
        capture_notes.found([2, 3, 9])

        capture_notes.fetch_unread(col)
        capture_notes.fetch_unread(col)

        self.assertEqual(col.fetched, [3, 9], "a note is fetched once, a read one never")
        self.assertEqual(
            [(nid, stage) for nid, stage, _ in self.snapshots()],
            [(2, "read"), (3, "read"), (9, "read")],
        )
        self.assertEqual(self.snapshots()[2][2], {"id": 9, "missing": True})
        self.assertEqual(self.events("note.missing"), [("note.missing", 9, {"note_id": 9})])

    def test_an_added_note_is_never_fetched_as_read(self):
        added = Note(11, {"Word": "飲む", "new-id": "-4242"})
        col = Collection(added)
        capture_notes.record_added([added], CONFIG)
        capture_notes.found([11])

        capture_notes.fetch_unread(col)

        self.assertEqual(col.fetched, [])
        self.assertEqual(
            self.events("note.added"),
            [("note.added", 11, {"note_id": 11, "placeholder": -4242})],
        )

    def test_the_final_state_is_read_from_the_collection_and_the_removed_noted(self):
        col = Collection(Note(4, {"Word": "箱"}), Note(5, {"Word": "本"}))

        capture_notes.record_final(col, [5, 4, 5, 0], removed=[5])

        self.assertEqual(
            [(nid, stage) for nid, stage, _ in self.snapshots()], [(4, "final")]
        )
        self.assertEqual(self.events("note.removed"), [("note.removed", None, {"note_ids": [5]})])


class MeaningsTests(CaptureNotesTestCase):
    def test_the_first_read_of_each_key_and_the_last_write_are_recorded(self):
        meanings = capture_notes.record_meanings({"本_ほん": [{"jp_meaning": "書物"}]})

        self.assertIn("本_ほん", meanings)
        meanings.get("本_ほん")
        self.assertIsNone(meanings.get("箱_はこ"))
        meanings["箱_はこ"] = [{"jp_meaning": "入れ物"}]
        meanings["飲む_のむ"] = []
        meanings["飲む_のむ"] = [{"jp_meaning": "液体を取る"}]
        # Read after its write: what it holds is the run's own, not the file's
        self.assertIn("飲む_のむ", meanings)
        capture_notes.record_final_meanings(meanings)

        self.assertEqual(
            [(kind, payload) for kind, _, payload in self.events()],
            [
                ("meanings.read", {"key": "本_ほん", "value": [{"jp_meaning": "書物"}]}),
                ("meanings.read", {"key": "箱_はこ", "value": None}),
                ("meanings.read", {"key": "飲む_のむ", "value": None}),
                ("meanings.final", {"key": "箱_はこ", "value": [{"jp_meaning": "入れ物"}]}),
                ("meanings.final", {"key": "飲む_のむ", "value": [{"jp_meaning": "液体を取る"}]}),
            ],
        )


class NotesOffTests(CaptureNotesTestCase):
    notes_on = False

    def test_a_run_that_does_not_record_notes_keeps_a_plain_dict_and_records_nothing(self):
        data = {"本_ほん": []}
        col = Collection(Note(3, {"Word": "箱"}))

        self.assertIs(capture_notes.record_meanings(data), data)
        capture_notes.snapshot_notes("read", [Note(2, {"Word": "本"})])
        capture_notes.found([3])
        capture_notes.fetch_unread(col)
        capture_notes.record_final(col, [3])

        self.assertEqual(col.fetched, [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM note_snapshots"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM events"), [(0,)])


class ReadPointTests(CaptureNotesTestCase):
    """collection_access records what a run reads, on the calling thread, but not the cleanup's
    reads, which come after its writes."""

    def setUp(self) -> None:
        super().setUp()
        col = Collection(Note(1, {"Word": "本"}), Note(2, {"Word": "箱"}))
        patcher = mock.patch.object(mw, "col", col, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # This thread is the op's, enrolled in the run whose reads collection_access serves
        api.begin_run()
        self.addCleanup(api.end_run)
        ca.end_cleanup_phase()
        self.addCleanup(ca.end_cleanup_phase)

    def test_a_fetch_is_read_and_a_search_is_referenced(self):
        ca.get_notes([1])
        found = ca.find_notes('"Word:箱"')

        self.assertEqual(list(found), [1, 2])
        self.assertEqual([(nid, stage) for nid, stage, _ in self.snapshots()], [(1, "read")])
        self.assertEqual(
            self.events("search"), [("search", None, {"query": '"Word:箱"', "count": 2})]
        )
        self.assertEqual(capture.unread_references(), [2])

    def test_the_cleanup_s_reads_are_not_the_run_s_state_before_it(self):
        ca.begin_cleanup_phase()
        ca.get_note(1)
        ca.find_notes("anything")

        self.assertEqual(self.snapshots(), [])
        self.assertEqual(capture.unread_references(), [])


if __name__ == "__main__":
    unittest.main()
