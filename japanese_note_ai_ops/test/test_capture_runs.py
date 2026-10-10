"""A bulk run in the capture store: `selected_notes_op` leaves one `runs` row with what it ran
and how it ended, and each AI call it made is a `calls` row joined to that run and to its note,
whose log lines carry the same ids.

The real run path runs: `selected_notes_op` through the stand-in `CollectionOp` of
test_op_chain_step (the op thread's work on the test's thread), the real `bulk_notes_op`, which
creates a task per note and runs the op in a `to_thread` worker, and the real `get_response`,
answered by a fake Gemini session. A store is installed in a temporary directory and shut down
in a cleanup (test_capture_calls); rows are read after `flush()`.
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from functools import partial
from typing import Any, Optional
from unittest import mock

from addon_modules import RunGate, load_ops_module, mw
from test_capture_calls import PROVIDERS, FakeSession, answer
from test_op_chain_step import FakeCollection, FakeCollectionOp, FakeNote

capture = load_ops_module("capture")
api = load_ops_module("api_client")
run_errors = load_ops_module("run_errors")
collection_access = load_ops_module("collection_access")
base_ops = load_ops_module("base_ops")
cl = load_ops_module("call_logging", "")
configuration = load_ops_module("configuration", "")

GEMINI = next(p for p in PROVIDERS if p.name == api.GEMINI)
API_KEY = "sk-capture-runs-test-0123456789abcdef"
CONFIG = {
    "google_api_key": API_KEY,
    "max_request_retries": 0,
    "request_timeout": 5,
    "log_level": "INFO",
    "log_to_console": False,
    "Japanese vocab note": {"meaning_field": "Meaning"},
}
DONE_TEXT = "Asked twice"
KIND = "test.ask"
VERSIONS = {"addon": "1.2.3", "anki": "25.02.7", "python": "3.9.18", "platform": "win32"}


class GateWithStatus(RunGate):
    """RunGate, and the status line the real progress updater draws from the gate."""

    def status_text(self) -> str:
        return ""


def ask(config, note, notes_to_add_dict, notes_to_update_dict) -> bool:
    """The per-note op: one AI call, run by bulk_notes_op in a to_thread worker."""
    result = base_ops.get_response(GEMINI.model, f"note {note.id}", kind=KIND, inputs={})
    if not result:
        return False
    notes_to_update_dict[note.id] = note
    return True


async def bulk_asks(col, notes, edited_nids, progress_updater, **dicts):
    return await base_ops.bulk_notes_op(
        "Asking", CONFIG, ask, col, notes, edited_nids, progress_updater, **dicts
    )


async def bulk_returns(col, notes, edited_nids, progress_updater, **dicts):
    return 1, {}, {}, []


async def bulk_asks_then_cancels(col, notes, edited_nids, progress_updater, **dicts):
    result = await bulk_asks(col, notes, edited_nids, progress_updater, **dicts)
    # As the cancel monitor does on Cancel in the dialog
    api.cancel_run()
    return result


async def bulk_ends_paused(col, notes, edited_nids, progress_updater, **dicts):
    api.pause_run("paused by user")
    return 1, {}, {}, []


async def bulk_raises(col, notes, edited_nids, progress_updater, **dicts):
    raise ValueError("boom")


async def bulk_read_after_cancel(col, notes, edited_nids, progress_updater, **dicts):
    api.cancel_run()
    # What collection_access does to a read on a cancelled run
    raise collection_access.RunCancelled("read refused")


class CaptureRunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(capture.current_store(), "a store was left installed")
        # Capture's warnings are rate limited per process (test_capture)
        capture._quiet_until.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(directory.name, "capture.sqlite3")

        self.ops: list[FakeCollectionOp] = []
        self.done: list = []
        self.session = FakeSession(answer(GEMINI))
        mw.progress.cancel = False
        self.addCleanup(setattr, mw.progress, "cancel", False)
        patches: list[Any] = [
            mock.patch.object(mw, "col", FakeCollection(), create=True),
            mock.patch.object(
                mw, "addonManager", types.SimpleNamespace(getConfig=lambda _: dict(CONFIG))
            ),
            mock.patch.object(base_ops, "CollectionOp", self.make_op),
            mock.patch.object(base_ops, "start_run_controls", lambda: None),
            mock.patch.object(base_ops, "tooltip", lambda *_, **__: None),
            mock.patch.object(base_ops, "showWarning", lambda *_, **__: None),
            mock.patch.object(base_ops, "ConcurrencyGate", lambda *_, **__: GateWithStatus()),
            mock.patch.object(base_ops, "size_pools_to_ceiling", lambda _: None),
            mock.patch.object(api, "get_session", lambda _provider: self.session),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # A run ended by a test must not leave a stop reason or its errors for the next one
        self.addCleanup(api.take_stop_reason)
        deliver = run_errors._deliver
        run_errors.deliver_with(lambda _title, _text: None)
        self.addCleanup(run_errors.deliver_with, deliver)
        self.addCleanup(run_errors.take_run)

    def make_op(self, parent, op):
        fake = FakeCollectionOp(parent, op, lambda: None)
        self.ops.append(fake)
        return fake

    def install(self, **kwargs: Any) -> None:
        self.assertTrue(capture.install(self.path, **kwargs))
        self.addCleanup(capture.shutdown)

    def run_op(self, bulk_op, chain: bool = False) -> FakeCollectionOp:
        step = base_ops.ChainStep("Step 2/3", self.done.append, "Asked") if chain else None
        updater = base_ops.AsyncTaskProgressUpdater(title="Async AI op: Asking")
        base_ops.selected_notes_op(DONE_TEXT, bulk_op, [1, 2], None, updater, chain=step)
        [op] = self.ops
        op.finish()
        return op

    def rows(self, table: str) -> list[dict]:
        self.assertTrue(capture.current_store().flush())
        key = {"runs": "run_id", "calls": "call_id"}[table]
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY {key}")]

    def the_run(self) -> dict:
        [run] = self.rows("runs")
        return run


class RunRowTests(CaptureRunTestCase):
    def test_a_run_over_two_notes_is_one_run_and_one_call_per_note(self):
        self.install(versions=VERSIONS)

        op = self.run_op(bulk_asks)

        self.assertIsNone(op.shown_by_aqt)
        run = self.the_run()
        self.assertEqual(run["label"], DONE_TEXT)
        self.assertEqual(run["implicit"], 0)
        self.assertEqual(json.loads(run["ops_json"]), ["bulk_asks"])
        self.assertIsNone(run["chain_step"])
        self.assertEqual(run["note_count"], 2)
        self.assertEqual(json.loads(run["versions_json"]), VERSIONS)
        self.assertEqual(run["outcome"], "completed")
        self.assertIsNotNone(run["ended"])
        self.assertGreaterEqual(run["ended"], run["started"])

        calls = self.rows("calls")
        self.assertEqual(sorted(c["note_id"] for c in calls), [1, 2])
        for call in calls:
            self.assertEqual(call["run_id"], run["run_id"])
            self.assertEqual(call["kind"], KIND)
            self.assertEqual(call["outcome"], "ok")
            self.assertGreaterEqual(call["started"], 0)
            self.assertLess(call["started"], 60)

    def test_the_config_is_recorded_without_its_keys(self):
        self.install()

        self.run_op(bulk_asks)

        config = json.loads(self.the_run()["config_json"])
        self.assertNotIn("google_api_key", config)
        self.assertEqual(config["max_request_retries"], 0)
        self.assertEqual(config["Japanese vocab note"], {"meaning_field": "Meaning"})
        with open(self.path, "rb") as file:
            self.assertNotIn(API_KEY.encode(), file.read())

    def test_a_chain_step_is_recorded_by_its_title(self):
        self.install()

        self.run_op(bulk_asks, chain=True)

        self.assertEqual(len(self.done), 1)
        self.assertEqual(self.the_run()["chain_step"], "Step 2/3: Asked")

    def test_every_phase_is_named(self):
        self.install()
        phases = [base_ops.OpPhase("One", bulk_asks), base_ops.OpPhase("Two", bulk_returns)]

        self.run_op(phases)

        self.assertEqual(json.loads(self.the_run()["ops_json"]), ["bulk_asks", "bulk_returns"])

    def test_with_capture_off_the_run_is_as_before(self):
        op = self.run_op(bulk_asks)

        self.assertIsNone(op.shown_by_aqt)
        self.assertEqual(len(self.session.calls), 2)
        self.assertFalse(os.path.exists(self.path))


class OutcomeTests(CaptureRunTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.install()

    def assert_outcome(self, outcome: str) -> dict:
        run = self.the_run()
        self.assertEqual(run["outcome"], outcome)
        self.assertIsNotNone(run["ended"])
        return run

    def test_an_exception_out_of_the_op_is_failed_and_still_reaches_aqt(self):
        op = self.run_op(bulk_raises)

        self.assert_outcome("failed")
        self.assertIsInstance(op.shown_by_aqt, ValueError)

    def test_a_run_cancelled_out_of_the_op_is_abandoned(self):
        op = self.run_op(bulk_read_after_cancel)

        self.assert_outcome("abandoned")
        self.assertIsNone(op.shown_by_aqt)

    def test_a_cancelled_run_is_cancelled_and_keeps_its_calls(self):
        self.run_op(bulk_asks_then_cancels)

        run = self.assert_outcome("cancelled")
        self.assertEqual([c["run_id"] for c in self.rows("calls")], [run["run_id"]] * 2)

    def test_a_run_that_ends_paused_is_cancelled(self):
        """Teardown cancels it, after the op returned: the outcome is read after that."""
        self.run_op(bulk_ends_paused)

        self.assert_outcome("cancelled")

    def test_a_press_of_cancel_before_the_cleanup_is_cancelled(self):
        async def escaped(col, notes, edited_nids, progress_updater, **dicts):
            mw.progress.cancel = True
            return 1, {}, {}, []

        self.run_op(escaped)

        self.assert_outcome("cancelled")


class RunLogTests(CaptureRunTestCase):
    """The run's log file, a handler of call_logging's own writing it, as in Anki."""

    def setUp(self) -> None:
        super().setUp()
        # call_logging puts its files beside itself, under user_files/logs
        patcher = mock.patch.object(cl, "__file__", os.path.join(self.directory, "cl.py"))
        patcher.start()
        self.addCleanup(patcher.stop)
        logger = cl.addon_logger()
        self.addCleanup(logger.setLevel, logger.level)
        self.handler = cl.create_call_log_handler("run")
        logger.addHandler(self.handler)
        self.addCleanup(self.handler.close)
        self.addCleanup(logger.removeHandler, self.handler)

    def log_lines(self) -> list[str]:
        self.handler.flush()
        with open(self.handler.baseFilename, encoding="utf-8") as file:
            return file.read().splitlines()

    def test_a_call_s_lines_carry_its_run_note_and_call(self):
        self.install(log_path=cl.current_log_path)

        self.run_op(bulk_asks)

        run = self.the_run()
        self.assertEqual(run["log_path"], self.handler.baseFilename)
        lines = self.log_lines()
        for call in self.rows("calls"):
            ids = f"[r{run['run_id']} n{call['note_id']} c{call['call_id']}]"
            reference = f"{ids} call {call['call_id']} {KIND} ok"
            self.assertTrue(any(reference in line for line in lines), (reference, lines))


class NotesRunTests(CaptureRunTestCase):
    """`notes_run` called on this thread with a collection, as a script calls it with no
    CollectionOp and no dialog, and a config that records the run's notes."""

    def test_a_run_records_its_notes_from_selected_to_final_and_what_it_leaves(self):
        from test_capture_notes import Collection, Note

        self.install()
        col = Collection(Note(1, {"Word": "本"}), Note(2, {"Word": "箱"}))
        config = dict(CONFIG, capture_notes=True)

        async def edits_one(col, notes, edited_nids, progress_updater, **dicts):
            note = notes[0]
            note._fields["Word"] = "本棚"
            dicts["notes_to_update_dict"][note.id] = note
            return 1, {}, dicts["notes_to_update_dict"], []

        updater = base_ops.AsyncTaskProgressUpdater(title="Async AI op: Editing")
        with (
            mock.patch.object(mw, "col", col, create=True),
            mock.patch.object(
                mw, "addonManager", types.SimpleNamespace(getConfig=lambda _: config)
            ),
        ):
            run, result = base_ops.notes_run(DONE_TEXT, edits_one, [1, 2], updater)
            run(col)

        self.assertEqual((result.edited_nids, result.cancelled), ([1], False))
        self.assertEqual(col.updated, [1])
        store = capture.current_store()
        self.assertTrue(store.flush())
        with closing(sqlite3.connect(self.path)) as connection:
            snapshots = connection.execute(
                "SELECT note_id, stage, text FROM note_snapshots"
                " JOIN blobs ON blobs.hash = note_hash ORDER BY snapshot_id"
            ).fetchall()
            kinds = [row[0] for row in connection.execute("SELECT kind FROM events")]
        words = [(nid, stage, json.loads(text)["fields"]["Word"]) for nid, stage, text in snapshots]
        self.assertEqual(
            words,
            [(1, "selected", "本"), (2, "selected", "箱"), (1, "proposed", "本棚"),
             (1, "final", "本棚")],
        )
        self.assertIn("environment", kinds)
        self.assertEqual([kind for kind in kinds if kind == "undo"], ["undo", "undo"])
        run_row = self.the_run()
        self.assertEqual(
            (run_row["notes"], run_row["dropped"], run_row["outcome"]), (1, 0, "completed")
        )

    def test_a_run_without_capture_notes_records_its_calls_only(self):
        from test_capture_notes import Collection, Note

        self.install()
        col = Collection(Note(1, {"Word": "本"}))
        updater = base_ops.AsyncTaskProgressUpdater(title="Async AI op: Asking")
        with mock.patch.object(mw, "col", col, create=True):
            run, _ = base_ops.notes_run(DONE_TEXT, bulk_returns, [1], updater)
            run(col)

        self.assertEqual(self.rows("runs")[0]["notes"], 0)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM note_snapshots").fetchone(), (0,)
            )


class NoteContextTests(unittest.TestCase):
    def setUp(self) -> None:
        # A chain step's title, left by another test, would lead the error title
        run_errors.take_run()
        self.addCleanup(run_errors.take_run)

    def test_the_note_names_both_the_errors_and_the_calls(self):
        with base_ops.note_context(FakeNote(7)):
            self.assertEqual(capture.current_note_id(), 7)
            self.assertEqual(run_errors.error_title(), "Note 7")
        self.assertIsNone(capture.current_note_id())

    def test_a_note_not_added_yet_is_no_note(self):
        with capture.note_scope(3), base_ops.note_context(FakeNote(0)):
            self.assertIsNone(capture.current_note_id())
            self.assertEqual(run_errors.error_title(), "New note")
        self.assertIsNone(capture.current_note_id())


class OpNameTests(unittest.TestCase):
    def names(self, *ops) -> Optional[list[str]]:
        return base_ops._op_names([base_ops.OpPhase("", op) for op in ops])

    def test_the_shapes_a_bulk_op_comes_in(self):
        class Callable:
            async def __call__(self, *args, **kwargs):
                pass

        def make_bulk_op():
            async def bulk_op(*args, **kwargs):
                pass

            return bulk_op

        self.assertEqual(
            self.names(bulk_asks, make_bulk_op(), partial(partial(bulk_asks)), Callable()),
            ["bulk_asks", "bulk_op", "bulk_asks", "Callable"],
        )

    def test_it_never_raises(self):
        class Broken:
            @property
            def bulk_op(self):
                raise RuntimeError("no op")

        self.assertIsNone(base_ops._op_names([Broken()]))


class VersionsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def write(self, name: str, text: str) -> None:
        with open(os.path.join(self.directory, name), "w", encoding="utf-8") as file:
            file.write(text)

    def addon_version(self) -> Optional[str]:
        return configuration.capture_versions(self.directory)["addon"]

    def test_the_manifest_of_a_released_package_first(self):
        self.write("manifest.json", json.dumps({"human_version": "2.0.0"}))
        self.write("build.json", json.dumps({"human_version": "1.0.0"}))

        self.assertEqual(self.addon_version(), "2.0.0")

    def test_build_json_in_a_working_tree(self):
        self.write("build.json", json.dumps({"human_version": "1.0.0"}))

        self.assertEqual(self.addon_version(), "1.0.0")

    def test_a_garbled_manifest_falls_back(self):
        self.write("manifest.json", "{not json")
        self.write("build.json", json.dumps({"human_version": "1.0.0"}))

        self.assertEqual(self.addon_version(), "1.0.0")

    def test_no_file_is_none(self):
        self.write("build.json", json.dumps(["not", "a", "dict"]))

        self.assertIsNone(self.addon_version())

    def test_the_rest(self):
        buildinfo = types.SimpleNamespace(version="25.02.7")
        with mock.patch.dict(sys.modules, {"anki.buildinfo": buildinfo}):
            versions = configuration.capture_versions(self.directory)

        self.assertEqual(
            versions,
            {
                "addon": None,
                "anki": "25.02.7",
                "python": platform.python_version(),
                "platform": sys.platform,
            },
        )

    def test_an_anki_without_its_version_is_none(self):
        with mock.patch.dict(sys.modules, {"anki.buildinfo": None}):
            self.assertIsNone(configuration.capture_versions(self.directory)["anki"])


if __name__ == "__main__":
    unittest.main()
