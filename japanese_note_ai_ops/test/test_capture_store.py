"""The capture store on its own: schema, ids, the writer thread, blobs, pruning, failures, keys.

Every store here writes to a temporary directory and is closed in a cleanup, so no writer
thread outlives its test. The tests wait on `flush()`, which answers once the writer has
committed, never on the batch timer; the two batching tests poll the file for a moment.
"""

from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from addon_modules import load_ops_module

capture_store = load_ops_module("capture_store")
diagnostics = load_ops_module("diagnostics")

# Spec section 4, in order
RUN_COLUMNS = [
    "run_id", "v", "started", "ended", "label", "implicit", "ops_json", "chain_step",
    "note_count", "config_json", "versions_json", "log_path", "outcome", "extra_json",
    # Schema version 3, last in a new file as in a migrated one
    "profile",
    # Schema version 4
    "notes", "dropped",
]
CALL_COLUMNS = [
    "call_id", "v", "run_id", "note_id", "task_id", "parent_task_id", "kind", "request_key",
    "prompt_key", "model", "params_json", "inputs_json", "instructions_hash", "prompt",
    "schema_hash", "response_raw", "response_json", "outcome", "error", "started",
    "latency_ms", "attempts", "usage_json", "extra_json",
    # Schema version 2, last in a new file as in a migrated one
    "context_json",
]
BLOB_COLUMNS = ["hash", "text"]
# Schema version 4's tables
SNAPSHOT_COLUMNS = ["snapshot_id", "v", "run_id", "note_id", "stage", "t", "mid", "note_hash"]
EVENT_COLUMNS = ["event_id", "v", "run_id", "note_id", "task_id", "kind", "t", "payload_json"]

# The schema version 1 files were made with, as that code created it: what a v1 file holds
V1_SCHEMA = [
    "CREATE TABLE runs (run_id INTEGER PRIMARY KEY, v INTEGER NOT NULL, started REAL,"
    " ended REAL, label TEXT, implicit INTEGER DEFAULT 0, ops_json TEXT, chain_step TEXT,"
    " note_count INTEGER, config_json TEXT, versions_json TEXT, log_path TEXT, outcome TEXT,"
    " extra_json TEXT)",
    "CREATE TABLE calls (call_id INTEGER PRIMARY KEY, v INTEGER NOT NULL, run_id INTEGER,"
    " note_id INTEGER, task_id TEXT, parent_task_id TEXT, kind TEXT, request_key TEXT,"
    " prompt_key TEXT, model TEXT, params_json TEXT, inputs_json TEXT, instructions_hash TEXT,"
    " prompt TEXT, schema_hash TEXT, response_raw TEXT, response_json TEXT, outcome TEXT,"
    " error TEXT, started REAL, latency_ms REAL, attempts INTEGER, usage_json TEXT,"
    " extra_json TEXT)",
    "CREATE TABLE blobs (hash TEXT PRIMARY KEY, text TEXT)",
    "CREATE INDEX runs_started ON runs(started)",
    "CREATE INDEX calls_run_id ON calls(run_id)",
    "CREATE INDEX calls_note_id ON calls(note_id)",
    "CREATE INDEX calls_kind ON calls(kind)",
    "CREATE INDEX calls_request_key ON calls(request_key)",
    "CREATE INDEX calls_prompt_key ON calls(prompt_key)",
    "CREATE INDEX calls_instructions_hash ON calls(instructions_hash)",
    "CREATE INDEX calls_schema_hash ON calls(schema_hash)",
    "PRAGMA user_version = 1",
]

# A new file of schema version 2 as that code created it (a v1 file it migrated differs only in
# the text sqlite keeps for the ALTERed table)
V2_SCHEMA = [
    "CREATE TABLE runs (run_id INTEGER PRIMARY KEY, v INTEGER NOT NULL, started REAL,"
    " ended REAL, label TEXT, implicit INTEGER DEFAULT 0, ops_json TEXT, chain_step TEXT,"
    " note_count INTEGER, config_json TEXT, versions_json TEXT, log_path TEXT, outcome TEXT,"
    " extra_json TEXT)",
    "CREATE TABLE calls (call_id INTEGER PRIMARY KEY, v INTEGER NOT NULL, run_id INTEGER,"
    " note_id INTEGER, task_id TEXT, parent_task_id TEXT, kind TEXT, request_key TEXT,"
    " prompt_key TEXT, model TEXT, params_json TEXT, inputs_json TEXT, instructions_hash TEXT,"
    " prompt TEXT, schema_hash TEXT, response_raw TEXT, response_json TEXT, outcome TEXT,"
    " error TEXT, started REAL, latency_ms REAL, attempts INTEGER, usage_json TEXT,"
    " extra_json TEXT, context_json TEXT)",
    "CREATE TABLE blobs (hash TEXT PRIMARY KEY, text TEXT)",
    "CREATE INDEX runs_started ON runs(started)",
    "CREATE INDEX calls_run_id ON calls(run_id)",
    "CREATE INDEX calls_note_id ON calls(note_id)",
    "CREATE INDEX calls_kind ON calls(kind)",
    "CREATE INDEX calls_request_key ON calls(request_key)",
    "CREATE INDEX calls_prompt_key ON calls(prompt_key)",
    "CREATE INDEX calls_instructions_hash ON calls(instructions_hash)",
    "CREATE INDEX calls_schema_hash ON calls(schema_hash)",
    "PRAGMA user_version = 2",
]

# A new file of schema version 3 as that code created it
V3_SCHEMA = [
    "CREATE TABLE runs (run_id INTEGER PRIMARY KEY, v INTEGER NOT NULL, started REAL,"
    " ended REAL, label TEXT, implicit INTEGER DEFAULT 0, ops_json TEXT, chain_step TEXT,"
    " note_count INTEGER, config_json TEXT, versions_json TEXT, log_path TEXT, outcome TEXT,"
    " extra_json TEXT, profile TEXT)",
    *V2_SCHEMA[1:-1],
    "PRAGMA user_version = 3",
]

DAY = 86400.0
# A fixed "now" for the prune function, so its cutoff is exact
NOW = 1_800_000_000.0


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        # Not created yet: opening the store creates it
        self.path = os.path.join(directory.name, "user_files", "capture.sqlite3")

    def open_store(self, path=None, **options):
        store = capture_store.CaptureStore(path or self.path, **options)
        self.addCleanup(store.close)
        return store

    def open_held_store(self, **options):
        """A store whose writer has not started, so what is queued stays queued."""
        with mock.patch.object(capture_store.CaptureStore, "_start_writer"):
            store = self.open_store(**options)
        self.assertIsNone(store._thread)
        return store

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(sql, params).fetchall()

    def ids(self, table):
        key = {"runs": "run_id", "calls": "call_id", "blobs": "hash"}[table]
        return [row[0] for row in self.rows(f"SELECT {key} FROM {table} ORDER BY {key}")]

    def wait_for_rows(self, sql, expected, seconds=2.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.rows(sql) == expected:
                return True
            time.sleep(0.01)
        return False


class SchemaTests(StoreTestCase):
    def test_a_fresh_file_gets_the_schema_its_version_and_wal(self):
        store = self.open_store()
        self.assertTrue(store.enabled)
        self.assertTrue(store.flush())

        tables = ("runs", "calls", "blobs", "note_snapshots", "events")
        with closing(sqlite3.connect(self.path)) as connection:
            columns = {
                table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                for table in tables
            }
            indexed = set()
            for table in tables:
                for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
                    for info in connection.execute(f"PRAGMA index_info({index[1]})"):
                        indexed.add((table, info[2]))
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(
            columns,
            {
                "runs": RUN_COLUMNS,
                "calls": CALL_COLUMNS,
                "blobs": BLOB_COLUMNS,
                "note_snapshots": SNAPSHOT_COLUMNS,
                "events": EVENT_COLUMNS,
            },
        )
        self.assertLessEqual(
            {
                ("runs", "started"),
                ("calls", "run_id"),
                ("calls", "note_id"),
                ("calls", "kind"),
                ("calls", "request_key"),
                ("calls", "prompt_key"),
                # The prune's, beyond the spec's list
                ("calls", "instructions_hash"),
                ("calls", "schema_hash"),
                ("note_snapshots", "run_id"),
                ("note_snapshots", "note_id"),
                ("note_snapshots", "note_hash"),
                ("events", "run_id"),
                ("events", "kind"),
            },
            indexed,
        )
        self.assertEqual((version, mode), (capture_store.SCHEMA_VERSION, "wal"))

    def test_every_row_carries_the_schema_version_the_store_fills_in(self):
        store = self.open_store()
        store.insert_run({"run_id": 1, "v": 99})
        store.insert_call({"call_id": 1, "run_id": 1})
        self.assertTrue(store.flush())

        version = capture_store.SCHEMA_VERSION
        self.assertEqual(
            self.rows("SELECT v FROM runs UNION ALL SELECT v FROM calls"), [(version,)] * 2
        )


class MigrationTests(StoreTestCase):
    def make_old_file(self, schema, version):
        """A file as an older store left it: its schema, a run and a call."""
        os.makedirs(os.path.dirname(self.path))
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in schema:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO runs (run_id, v, label) VALUES (4, ?, 'old run')", (version,)
            )
            connection.execute(
                "INSERT INTO calls (call_id, v, run_id, kind) VALUES (8, ?, 4, 'match.meanings')",
                (version,),
            )
            connection.commit()

    def assert_brought_up_to_date(self, old_version):
        store = self.open_store()
        self.assertTrue(store.enabled)
        run_id = store.insert_run({"label": "new run", "profile": "User 1"})
        call_id = store.insert_call(
            {"run_id": run_id, "kind": "match.rating", "context_json": {"a": 1}}
        )
        self.assertTrue(store.flush())

        store.insert_snapshot({"run_id": run_id, "note_id": 7, "stage": "read"})
        store.insert_event({"run_id": run_id, "kind": "phase"})
        self.assertTrue(store.flush())

        # Their columns in the order a new file has them, so the two read alike
        self.assertEqual([row[1] for row in self.rows("PRAGMA table_info(runs)")], RUN_COLUMNS)
        self.assertEqual([row[1] for row in self.rows("PRAGMA table_info(calls)")], CALL_COLUMNS)
        self.assertEqual(self.rows("PRAGMA user_version"), [(capture_store.SCHEMA_VERSION,)])
        self.assertEqual((run_id, call_id), (5, 9), "ids carry on after the old rows")
        version = capture_store.SCHEMA_VERSION
        self.assertEqual(
            self.rows("SELECT call_id, v, kind, context_json FROM calls ORDER BY call_id"),
            [(8, old_version, "match.meanings", None), (9, version, "match.rating", '{"a":1}')],
        )
        # An old run recorded no notes
        self.assertEqual(
            self.rows("SELECT run_id, v, label, profile, notes FROM runs ORDER BY run_id"),
            [(4, old_version, "old run", None, 0), (5, version, "new run", "User 1", 0)],
        )
        self.assertEqual(self.rows("SELECT note_id, stage FROM note_snapshots"), [(7, "read")])
        self.assertEqual(self.rows("SELECT kind FROM events"), [("phase",)])

    def test_a_version_1_file_is_brought_up_to_date_and_keeps_its_rows(self):
        self.make_old_file(V1_SCHEMA, 1)
        self.assert_brought_up_to_date(1)

    def test_a_version_2_file_is_brought_up_to_date_and_keeps_its_rows(self):
        self.make_old_file(V2_SCHEMA, 2)
        self.assert_brought_up_to_date(2)

    def test_a_version_3_file_gains_the_note_tables_and_keeps_its_rows(self):
        self.make_old_file(V3_SCHEMA, 3)
        self.assert_brought_up_to_date(3)

    def test_a_migration_that_fails_leaves_the_file_at_its_version(self):
        self.make_old_file(V1_SCHEMA, 1)
        v1_columns = [self.rows(f"PRAGMA table_info({table})") for table in ("runs", "calls")]
        # A statement that fails after both ALTER TABLEs have run
        statements = capture_store._schema_statements() + ["NOT A STATEMENT"]

        with (
            mock.patch.object(capture_store, "_schema_statements", lambda: statements),
            self.assertLogs(capture_store.logger, "WARNING") as logs,
        ):
            store = self.open_store()

        self.assertIn(self.path, logs.output[0])
        self.assertFalse(store.enabled)
        self.assertEqual(self.rows("PRAGMA user_version"), [(1,)])
        self.assertEqual(
            [self.rows(f"PRAGMA table_info({table})") for table in ("runs", "calls")], v1_columns
        )
        self.assertEqual(self.rows("SELECT call_id, v FROM calls"), [(8, 1)])


class IdTests(StoreTestCase):
    def test_ids_start_at_one_and_carry_on_after_the_file_is_reopened(self):
        store = self.open_store()
        self.assertEqual([store.new_run_id(), store.new_run_id()], [1, 2])
        # A row without an id gets the counter's next, one with an id keeps it
        run_id = store.insert_run({"label": "first"})
        self.assertEqual(run_id, 3)
        self.assertEqual(store.insert_call({"run_id": run_id}), 1)
        self.assertEqual(store.insert_call({"call_id": store.new_call_id(), "run_id": 3}), 2)
        self.assertEqual(store.insert_call({"call_id": 7, "run_id": 3}), 7)
        self.assertTrue(store.close())

        reopened = self.open_store()
        # Ids handed out but never written (the run ids 1 and 2) are not remembered
        self.assertEqual((reopened.new_run_id(), reopened.new_call_id()), (4, 8))

    def test_a_new_run_never_takes_the_id_of_a_missing_run_its_calls_still_name(self):
        store = self.open_store()
        store.insert_run({"run_id": 1})
        # Run 2's row was dropped at a full queue; its call was written
        store.insert_call({"call_id": 1, "run_id": 2})
        self.assertTrue(store.close())

        self.assertEqual(self.open_store().new_run_id(), 3)

    def test_first_ids_are_a_floor_under_what_the_file_gives(self):
        store = self.open_store()
        store.insert_run({"run_id": 5})
        store.insert_call({"call_id": 5})
        self.assertTrue(store.close())

        above = self.open_store(first_ids=(10, 20))
        self.assertEqual((above.new_run_id(), above.new_call_id()), (10, 20))
        self.assertTrue(above.close())
        below = self.open_store(first_ids=(2, 2))
        self.assertEqual((below.new_run_id(), below.new_call_id()), (6, 6))
        self.assertTrue(below.close())
        # A store that could not open has nothing of the file's, and still keeps above the floor
        with mock.patch.object(capture_store.CaptureStore, "_open", return_value=None):
            unopened = self.open_store(first_ids=(10, 20))
        self.assertFalse(unopened.enabled)
        self.assertEqual((unopened.new_run_id(), unopened.new_call_id()), (10, 20))

    def test_next_ids_takes_the_next_of_each_so_neither_is_handed_out(self):
        store = self.open_store()
        handed_out = [store.new_run_id(), store.new_call_id()]
        self.assertTrue(store.close())

        run_id, call_id = store.next_ids()
        self.assertEqual((run_id, call_id), (handed_out[0] + 1, handed_out[1] + 1))
        # Still handed out once closed, as a store that is off does, but past them
        self.assertEqual((store.new_run_id(), store.new_call_id()), (run_id + 1, call_id + 1))


class WriteTests(StoreTestCase):
    def test_records_from_many_threads_are_all_written_by_the_time_flush_answers(self):
        store = self.open_store()
        start = threading.Barrier(8)

        def record(n):
            start.wait()
            run_id = store.insert_run({"label": f"thread {n}", "started": time.time()})
            for i in range(50):
                store.insert_call({"call_id": store.new_call_id(), "run_id": run_id,
                                   "kind": "test.threads", "prompt": f"{n}/{i}"})

        threads = [threading.Thread(target=record, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(store.flush())

        self.assertEqual(self.ids("calls"), list(range(1, 401)))
        self.assertEqual(self.ids("runs"), list(range(1, 9)))
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM calls GROUP BY run_id"), [(50,)] * 8
        )

    def test_an_update_after_the_insert_sets_only_its_columns(self):
        store = self.open_store()
        run_id = store.insert_run({"label": "bulk", "started": 100.0, "note_count": 2})
        store.update_run(run_id, ended=160.0, outcome="completed",
                         extra_json={"b": 1, "a": "日本"})
        self.assertTrue(store.flush())

        self.assertEqual(
            self.rows("SELECT label, started, ended, note_count, outcome, extra_json FROM runs"),
            [("bulk", 100.0, 160.0, 2, "completed", '{"a":"日本","b":1}')],
        )

    def test_an_unknown_column_is_left_out_with_one_warning(self):
        store = self.open_store()
        with self.assertLogs(capture_store.logger, "WARNING") as logs:
            store.insert_call({"call_id": 1, "kind": "a", "nope": 1})
            store.insert_call({"call_id": 2, "kind": "b", "nope": 2})
            self.assertTrue(store.flush())

        self.assertEqual(
            self.rows("SELECT call_id, kind FROM calls ORDER BY call_id"), [(1, "a"), (2, "b")]
        )
        self.assertEqual(len([line for line in logs.output if "'nope'" in line]), 1)

    def test_a_row_the_database_refuses_is_skipped_and_the_rest_written(self):
        store = self.open_store()
        with self.assertLogs(capture_store.logger, "WARNING") as logs:
            store.insert_call({"call_id": 1, "kind": "first"})
            store.insert_call({"call_id": 1, "kind": "the same id again"})
            store.insert_call({"call_id": 2, "kind": "after it"})
            self.assertTrue(store.flush())

        self.assertTrue(store.enabled)
        self.assertEqual(
            self.rows("SELECT call_id, kind FROM calls ORDER BY call_id"),
            [(1, "first"), (2, "after it")],
        )
        self.assertIn("refused", logs.output[0])

    def test_text_with_a_lone_surrogate_is_written_with_it_escaped(self):
        # What json.loads makes of a JSON "\ud800" without its pair; sqlite cannot bind it
        answer = '{"meaning": "x\ud800y"}'
        result = {"meaning": "x\ud800y"}
        store = self.open_store()
        store.insert_call(
            {"call_id": 1, "response_raw": answer, "response_json": result, "prompt": "食べる"}
        )
        store.insert_run({"run_id": 1, "label": "\udc80"})
        self.assertTrue(store.flush())

        [(raw, response_json, prompt)] = self.rows(
            "SELECT response_raw, response_json, prompt FROM calls"
        )
        self.assertEqual(raw, '{"meaning": "x\\ud800y"}')
        self.assertEqual(response_json, '{"meaning":"x\\ud800y"}')
        # The escape is JSON's own, so the text parses back to the value that was recorded
        self.assertEqual(json.loads(raw), result)
        self.assertEqual(json.loads(response_json), result)
        self.assertEqual(prompt, "食べる")
        self.assertEqual(self.rows("SELECT label FROM runs"), [("\\udc80",)])

    def test_a_batch_that_cannot_be_written_turns_the_store_off(self):
        store = self.open_store()
        # Past its prune, so that it is the batch that meets the missing table
        self.assertTrue(store.flush())
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE calls")
            connection.commit()

        with self.assertLogs(capture_store.logger, "WARNING") as logs:
            store.insert_run({"run_id": 1})
            store.insert_call({"call_id": store.new_call_id()})
            started = time.monotonic()
            self.assertFalse(store.flush())
        self.assertLess(time.monotonic() - started, 2.0)

        self.assertFalse(store.enabled)
        self.assertIn(self.path, logs.output[0])
        # The whole batch went, the run with it
        self.assertEqual(self.ids("runs"), [])
        # Still no raising, and ids still come
        self.assertEqual(store.insert_call({"kind": "later"}), 2)
        store.update_run(1, outcome="failed")
        self.assertIsInstance(store.put_blob("later"), str)
        self.assertFalse(store.flush())
        self.assertTrue(store.close())
        self.assertTrue(store.close())


class BatchingTests(StoreTestCase):
    def test_a_batch_is_committed_when_its_time_is_up(self):
        store = self.open_store(batch_seconds=0.05, batch_size=1000)
        store.insert_run({"run_id": 1})
        self.assertTrue(self.wait_for_rows("SELECT run_id FROM runs", [(1,)]))

    def test_a_full_batch_is_committed_without_waiting_for_its_time(self):
        store = self.open_store(batch_seconds=60.0, batch_size=5)
        for run_id in range(1, 6):
            store.insert_run({"run_id": run_id})
        self.assertTrue(self.wait_for_rows("SELECT COUNT(*) FROM runs", [(5,)]))


class BlobTests(StoreTestCase):
    def test_a_blob_is_queued_and_written_once_however_often_it_is_put(self):
        store = self.open_held_store()
        text = "Answer in JSON."
        hashes = []

        def put():
            for _ in range(25):
                hashes.append(store.put_blob(text))

        threads = [threading.Thread(target=put) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(set(hashes), {capture_store.text_hash(text)})
        self.assertEqual(store._queue.qsize(), 1)
        capture_store.CaptureStore._start_writer(store)
        self.assertTrue(store.flush())
        self.assertEqual(self.rows("SELECT hash, text FROM blobs"),
                         [(capture_store.text_hash(text), text)])

    def test_a_schema_is_stored_as_its_canonical_json(self):
        store = self.open_store()
        schema = {"type": "object", "properties": {"b": {}, "a": {}}}
        text = '{"properties":{"a":{},"b":{}},"type":"object"}'

        self.assertEqual(store.put_blob(schema), capture_store.text_hash(text))
        self.assertEqual(store.put_blob(text), capture_store.text_hash(text))
        self.assertTrue(store.flush())
        self.assertEqual(self.rows("SELECT text FROM blobs"), [(text,)])

    def test_a_blob_with_a_lone_surrogate_is_stored_under_the_hash_put_blob_returned(self):
        store = self.open_store()
        text = "Answer in JSON.\ud800"

        digest = store.put_blob(text)
        # Named by it, as a call's instructions_hash would
        store.insert_call({"call_id": 1, "instructions_hash": digest})
        self.assertTrue(store.flush())

        self.assertEqual(digest, capture_store.text_hash(text))
        self.assertEqual(self.rows("SELECT hash, text FROM blobs"),
                         [(digest, "Answer in JSON.\\ud800")])
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM calls JOIN blobs ON instructions_hash = hash"), [(1,)]
        )

    def test_a_value_with_no_json_text_gives_no_hash_and_no_raise(self):
        store = self.open_store()
        with self.assertLogs(capture_store.logger, "WARNING"):
            self.assertIsNone(store.put_blob({1: "keys of two types", "b": 2}))


class PruneTests(StoreTestCase):
    def fill(self, store, now):
        blob = {
            name: store.put_blob(f"blob {name}") for name in ("old", "shared", "schema", "unused")
        }
        store.insert_run({"run_id": 1, "started": now - 100 * DAY})
        store.insert_run({"run_id": 2, "started": now - 10 * DAY})
        store.insert_call({"call_id": 1, "run_id": 1, "instructions_hash": blob["old"],
                           "schema_hash": blob["schema"]})
        store.insert_call({"call_id": 2, "run_id": 1, "instructions_hash": blob["shared"]})
        # Its schema_hash is NULL, which in a NOT IN list would have kept every blob
        store.insert_call({"call_id": 3, "run_id": 2, "instructions_hash": blob["shared"]})
        # Its run row was never written (dropped at a full queue, say): no age to go by
        store.insert_call({"call_id": 4, "run_id": 99})
        # No run at all, so no age either
        store.insert_call({"call_id": 5})
        self.assertTrue(store.close())
        return blob

    def test_prune_removes_old_runs_their_calls_and_the_blobs_nothing_references(self):
        blob = self.fill(self.open_store(keep_days=None), NOW)

        with closing(sqlite3.connect(self.path)) as connection, connection:
            counts = capture_store.prune(connection, keep_days=90, now=NOW)

        self.assertEqual(counts, (1, 2, 3))
        self.assertEqual(self.ids("runs"), [2])
        self.assertEqual(self.ids("calls"), [3, 4, 5])
        self.assertEqual(self.ids("blobs"), [blob["shared"]])

    def test_a_run_s_snapshots_and_events_go_with_it_and_a_note_stays_while_named(self):
        store = self.open_store(keep_days=None)
        note = {name: store.put_blob({"fields": {"Word": name}}) for name in ("old", "both")}
        store.insert_run({"run_id": 1, "started": NOW - 100 * DAY, "notes": 1})
        store.insert_run({"run_id": 2, "started": NOW - 10 * DAY, "notes": 1})
        store.insert_snapshot({"run_id": 1, "note_id": 1, "stage": "read", "note_hash": note["old"]})
        store.insert_snapshot({"run_id": 1, "note_id": 2, "stage": "read", "note_hash": note["both"]})
        # The same note unchanged in a later run: one blob for both
        store.insert_snapshot({"run_id": 2, "note_id": 2, "stage": "read", "note_hash": note["both"]})
        store.insert_event({"run_id": 1, "kind": "phase"})
        store.insert_event({"run_id": 2, "kind": "phase"})
        self.assertTrue(store.close())

        with closing(sqlite3.connect(self.path)) as connection, connection:
            capture_store.prune(connection, keep_days=90, now=NOW)

        self.assertEqual(self.rows("SELECT run_id, note_id FROM note_snapshots"), [(2, 2)])
        self.assertEqual(self.rows("SELECT run_id FROM events"), [(2,)])
        self.assertEqual(self.ids("blobs"), [note["both"]])

    def test_the_calls_delete_looks_runs_up_in_the_run_id_index(self):
        # Not a scan of the calls table, whose rows hold whole prompts and answers
        with closing(sqlite3.connect(":memory:")) as connection:
            for statement in capture_store._schema_statements():
                connection.execute(statement)
            plan = [
                row[3]
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN " + capture_store._PRUNE_CALLS, (NOW,)
                )
            ]

        self.assertTrue(any("INDEX calls_run_id" in step for step in plan), plan)
        self.assertFalse(
            any(step.startswith(("SCAN calls", "SCAN TABLE calls")) for step in plan), plan
        )

    def test_a_recent_call_whose_run_row_is_missing_outlives_the_next_open(self):
        now = time.time()
        first = self.open_store(keep_days=None)
        first.insert_run({"run_id": 1, "started": now - 100 * DAY})
        first.insert_call({"call_id": 1, "run_id": 1})
        # Run 2's row was dropped at a full queue a minute ago
        first.insert_call({"call_id": 2, "run_id": 2})
        self.assertTrue(first.close())

        second = self.open_store(keep_days=90)
        self.assertTrue(second.flush())

        self.assertEqual(self.ids("runs"), [])
        self.assertEqual(self.ids("calls"), [2])

    def test_keep_days_of_zero_or_none_keeps_everything(self):
        self.fill(self.open_store(keep_days=None), NOW)

        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(capture_store.prune(connection, keep_days=0, now=NOW), (0, 0, 0))
            self.assertEqual(capture_store.prune(connection, keep_days=None, now=NOW), (0, 0, 0))

        self.assertEqual(self.ids("runs"), [1, 2])
        self.assertEqual(self.ids("calls"), [1, 2, 3, 4, 5])
        self.assertEqual(len(self.ids("blobs")), 4)

    def test_opening_prunes_and_keeps_what_this_session_records(self):
        now = time.time()
        first = self.open_store(keep_days=None)
        first.insert_run({"run_id": 1, "started": now - DAY})
        first.insert_run({"run_id": 2, "started": now - 60})
        first.insert_call({"call_id": 1, "run_id": 1})
        first.insert_call({"call_id": 2, "run_id": 2})
        self.assertTrue(first.close())

        # Half a day: run 1 is past it; run 2 and the run this session starts are not
        second = self.open_store(keep_days=0.5)
        run_id = second.insert_run({"started": time.time()})
        second.insert_call({"call_id": second.new_call_id(), "run_id": run_id})
        self.assertTrue(second.flush())

        self.assertEqual(self.ids("runs"), [2, 3])
        self.assertEqual(self.ids("calls"), [2, 3])


class KeepDaysTests(StoreTestCase):
    """keep_days comes from the user's config as typed there."""

    def test_a_number_or_a_numeric_string_is_its_float_and_none_keeps_all(self):
        for given, taken in [(None, None), (30, 30.0), (7.5, 7.5), ("30", 30.0), (" 2.5 ", 2.5)]:
            with self.subTest(given=given):
                store = self.open_held_store(keep_days=given)
                self.assertEqual(store._keep_days, taken)
                self.assertIs(type(store._keep_days), type(taken))

    def test_anything_else_is_the_default_with_a_warning(self):
        for given in [True, False, "thirty", "", [30], {"days": 30}, 10**400]:
            with self.subTest(given=given):
                with self.assertLogs(capture_store.logger, "WARNING") as logs:
                    store = self.open_held_store(keep_days=given)
                self.assertEqual(store._keep_days, capture_store.DEFAULT_KEEP_DAYS)
                self.assertEqual(len(logs.output), 1)
                self.assertIn(self.path, logs.output[0])

    def fill_and_reopen(self, keep_days):
        now = time.time()
        first = self.open_store(keep_days=None)
        first.insert_run({"run_id": 1, "started": now - 100 * DAY})
        first.insert_run({"run_id": 2, "started": now - 40 * DAY})
        first.insert_run({"run_id": 3, "started": now - 10 * DAY})
        for run_id in (1, 2, 3):
            first.insert_call({"call_id": run_id, "run_id": run_id})
        self.assertTrue(first.close())
        self.assertTrue(self.open_store(keep_days=keep_days).flush())
        return self.ids("runs"), self.ids("calls")

    def test_a_store_given_a_numeric_string_prunes(self):
        self.assertEqual(self.fill_and_reopen("30"), ([3], [3]))

    def test_a_store_given_true_prunes_by_the_default_not_by_one_day(self):
        with self.assertLogs(capture_store.logger, "WARNING"):
            self.assertEqual(self.fill_and_reopen(True), ([2, 3], [2, 3]))


class FailureTests(StoreTestCase):
    def assert_off_and_harmless(self, store):
        self.assertFalse(store.enabled)
        self.assertIsNone(store._thread)
        self.assertIsInstance(store.new_run_id(), int)
        self.assertIsInstance(store.insert_run({"label": "x"}), int)
        store.update_run(1, outcome="completed")
        self.assertIsInstance(store.insert_call({"kind": "x"}), int)
        self.assertEqual(store.put_blob("x"), capture_store.text_hash("x"))
        self.assertFalse(store.flush())
        self.assertTrue(store.close())
        self.assertTrue(store.close())

    def test_a_file_from_a_newer_version_is_left_untouched(self):
        os.makedirs(os.path.dirname(self.path))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE runs (run_id INTEGER PRIMARY KEY, future TEXT)")
            connection.execute(f"PRAGMA user_version = {capture_store.SCHEMA_VERSION + 1}")
            connection.commit()
        before = Path(self.path).read_bytes()

        with self.assertLogs(capture_store.logger, "WARNING") as logs:
            store = self.open_store()

        self.assertIn(self.path, logs.output[0])
        self.assert_off_and_harmless(store)
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["capture.sqlite3"])

    def test_a_path_that_cannot_be_opened_turns_the_store_off_without_raising(self):
        blocker = os.path.join(self.directory, "a_file")
        Path(blocker).write_text("not a directory")
        path = os.path.join(blocker, "capture.sqlite3")

        with self.assertLogs(capture_store.logger, "WARNING") as logs:
            store = self.open_store(path)

        self.assertIn(path, logs.output[0])
        self.assert_off_and_harmless(store)

    def test_a_full_queue_drops_records_without_blocking_and_warns_once(self):
        store = self.open_held_store(max_queue=3)
        finished = threading.Event()

        def record():
            for call_id in range(1, 11):
                store.insert_call({"call_id": call_id})
            store.put_blob("dropped")
            finished.set()

        with self.assertLogs(capture_store.logger, "WARNING") as logs:
            threading.Thread(target=record, daemon=True).start()
            # Nothing drains the queue yet, so a put that blocked would still be waiting
            self.assertTrue(finished.wait(2.0))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("full", logs.output[0])

        capture_store.CaptureStore._start_writer(store)
        self.assertTrue(store.flush())
        self.assertEqual(self.ids("calls"), [1, 2, 3])
        # A dropped blob is not taken as stored: put again, it is written
        self.assertEqual(self.ids("blobs"), [])
        store.put_blob("dropped")
        self.assertTrue(store.flush())
        self.assertEqual(self.ids("blobs"), [capture_store.text_hash("dropped")])


class LifecycleTests(StoreTestCase):
    def test_close_writes_what_is_queued_and_can_be_called_again(self):
        # A batch time no test would wait for: the stop has to end the batch
        store = self.open_store(batch_seconds=60.0)
        store.insert_run({"run_id": 1, "label": "never flushed"})

        self.assertTrue(store.close())
        self.assertFalse(store.enabled)
        self.assertFalse(store._thread.is_alive())
        self.assertTrue(store.close())
        store.insert_run({"run_id": 2})
        self.assertFalse(store.flush())
        self.assertEqual(self.ids("runs"), [1])

    def test_a_writer_close_could_not_queue_its_stop_for_ends_once_it_has_drained(self):
        store = self.open_store(max_queue=2, batch_size=1)
        # Past its prune, so that it is the first record that meets the lock
        self.assertTrue(store.flush())
        blocker = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")

        with (
            mock.patch.object(capture_store, "_IDLE_POLL_SECONDS", 0.02),
            self.assertLogs(capture_store.logger, "WARNING") as logs,
        ):
            store.insert_run({"run_id": 1})
            # The writer took it and waits on the lock, so the next two fill the queue
            deadline = time.monotonic() + 2.0
            while store._queue.qsize() and time.monotonic() < deadline:
                time.sleep(0.005)
            store.insert_run({"run_id": 2})
            store.insert_run({"run_id": 3})
            self.assertTrue(store._queue.full())
            self.assertFalse(store.close(timeout=0.05))
            blocker.execute("COMMIT")
            store._thread.join(5.0)

        self.assertFalse(store._thread.is_alive())
        self.assertIn("still busy", logs.output[0])
        self.assertEqual(self.ids("runs"), [1, 2, 3])

    def test_the_writer_is_a_daemon_that_closing_a_log_does_not_wait_for(self):
        store = self.open_store()
        writer = store._thread
        self.assertTrue(writer.daemon)
        self.assertEqual(writer.name, capture_store.WRITER_THREAD_NAME)
        self.assertFalse(writer.name.startswith(diagnostics.WORKER_THREAD_PREFIX))

    def test_after_opening_only_the_writer_thread_touches_sqlite(self):
        opened_on = []
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            opened_on.append(threading.current_thread().name)
            return real_connect(*args, **kwargs)

        with mock.patch.object(capture_store.sqlite3, "connect", connect):
            store = self.open_store()
            run_id = store.insert_run({"label": "x"})
            store.update_run(run_id, outcome="completed")
            store.insert_call({"run_id": run_id})
            store.put_blob("x")
            self.assertTrue(store.flush())
            self.assertTrue(store.close())

        self.assertEqual(
            opened_on, [threading.current_thread().name, capture_store.WRITER_THREAD_NAME]
        )


class KeyTests(unittest.TestCase):
    def test_canonical_json_is_one_text_whatever_the_order(self):
        one = {"b": [1, {"y": 2, "x": 1}], "a": "日本語"}
        other = {"a": "日本語", "b": [1, {"x": 1, "y": 2}]}

        self.assertEqual(capture_store.canonical_json(one), capture_store.canonical_json(other))
        self.assertEqual(
            capture_store.canonical_json(one), '{"a":"日本語","b":[1,{"x":1,"y":2}]}'
        )
        # What JSON has no type for is written as its str(), not refused
        self.assertEqual(
            capture_store.canonical_json({"when": datetime.date(2026, 9, 27)}),
            '{"when":"2026-09-27"}',
        )

    def test_request_key_follows_the_inputs_and_not_the_prompt(self):
        inputs = {"word": "食べる", "sentence": "ご飯を食べる。"}
        reordered = {"sentence": "ご飯を食べる。", "word": "食べる"}
        key = capture_store.request_key("match.meanings", inputs)

        # The same request, prompted two ways
        self.assertEqual(capture_store.request_key("match.meanings", reordered), key)
        self.assertNotEqual(
            capture_store.prompt_key("m", None, "Prompt one", None, {}),
            capture_store.prompt_key("m", None, "Prompt two", None, {}),
        )
        self.assertNotEqual(
            capture_store.request_key("match.meanings", {**inputs, "word": "食う"}), key
        )
        self.assertNotEqual(capture_store.request_key("match.rating", inputs), key)
        self.assertEqual(
            key,
            hashlib.sha1(
                ("match.meanings\n" + capture_store.canonical_json(inputs)).encode("utf-8")
            ).hexdigest(),
        )

    def test_prompt_key_changes_with_the_instructions(self):
        params = {"max_output_tokens": None, "temperature": 0.2, "effort": None}
        schema = {"type": "object"}
        key = capture_store.prompt_key("gemini-x", "Be brief.", "P", schema, params)

        self.assertNotEqual(
            capture_store.prompt_key("gemini-x", "Be thorough.", "P", schema, params), key
        )
        self.assertEqual(
            key,
            hashlib.sha1(
                capture_store.canonical_json(["gemini-x", "Be brief.", "P", schema, params])
                .encode("utf-8")
            ).hexdigest(),
        )

    def test_research_cache_key_is_the_eval_scripts_key(self):
        model, prompt = "gemini-2.5-flash", "この文を訳して"
        self.assertEqual(
            capture_store.research_cache_key(model, prompt),
            hashlib.sha1(f"{model}\n{prompt}".encode("utf-8")).hexdigest(),
        )


class ImportTests(unittest.TestCase):
    def test_the_module_imports_the_standard_library_only(self):
        # A relative import could bring aqt in through a sibling module, and an aqt import
        # takes the suite offline
        tree = ast.parse(Path(capture_store.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                self.assertIn(name.split(".")[0], sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
