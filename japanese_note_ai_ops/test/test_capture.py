"""capture: install and shutdown, the ContextVars, runs, calls and their outcomes, the log filter,
the config scrubbing.

A test that installs a store installs it in a temporary directory and shuts it down in a
cleanup that runs before the directory is removed: a store left installed would record the
next test's calls, which is why setUp checks that none is. Rows are read with the test's own
connection after the store's `flush()`, never by waiting on the batch timer.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import io
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from addon_modules import load_ops_module

capture = load_ops_module("capture")
capture_store = load_ops_module("capture_store")

# The package's logger: install's warnings come from capture, the store's from capture_store
PACKAGE_LOGGER = capture.__name__.rpartition(".")[0]

MODEL = "gemini-2.5-flash"
INSTRUCTIONS = "語の意味を一つ選び、JSONで答えてください。"
PROMPT = "Word: 食べる\nSentence: パンを食べる。"
SCHEMA = {"type": "object", "properties": {"meaning": {"type": "string"}}}
PARAMS = {"max_output_tokens": None, "temperature": 0.2, "effort": None}
KIND = "match.meanings"
INPUTS = {"word": "食べる", "reading": "たべる", "sentence": "パンを食べる。"}
VERSIONS = {"addon": "1.2.3", "anki": "25.02", "python": "3.9.18", "platform": "linux"}
LOG_PATH = "logs/match_words_20260927_120000.log"
# Anki's name for the first profile of a new install
PROFILE = "User 1"
NOTE_ID = 1712345678901
API_KEY = "sk-capture-test-0123456789abcdef"


def call_args(**overrides):
    args = dict(
        model=MODEL, prompt=PROMPT, instructions=INSTRUCTIONS, schema=SCHEMA, params=PARAMS
    )
    args.update(overrides)
    return args


def record_call(kind=KIND, inputs=INPUTS, result=None, cancelled=False, **overrides):
    """One call that finishes with `result`; returns its trace."""
    with capture.call(kind, inputs, **call_args(**overrides)) as trace:
        if trace is not None:
            trace.finish(result, cancelled)
    return trace


class CaptureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(capture.current_store(), "a store was left installed")
        # Capture's warnings are rate limited per process; a test expecting one must not find
        # it silenced by an earlier test
        capture._quiet_until.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(directory.name, "user_files", "capture.sqlite3")

    def install(self, path=None, **options):
        options.setdefault("versions", VERSIONS)
        options.setdefault("log_path", lambda: LOG_PATH)
        options.setdefault("profile", PROFILE)
        self.assertTrue(capture.install(path or self.path, **options))
        self.addCleanup(capture.shutdown)
        return capture.current_store()

    def rows(self, table, order=None):
        self.assertTrue(capture.current_store().flush())
        key = order or {"runs": "run_id", "calls": "call_id", "blobs": "hash"}[table]
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            query = f"SELECT * FROM {table} ORDER BY {key}"
            return [dict(row) for row in connection.execute(query)]


class NotInstalledTests(CaptureTestCase):
    def test_every_function_is_a_no_op_and_no_file_is_made(self):
        self.assertFalse(capture.installed())
        self.assertIsNone(capture.begin_run("run", note_count=1, config={"model": MODEL}))
        capture.end_run(None, "completed")
        capture.end_run(7, "completed")
        with capture.run_scope(None):
            self.assertIsNone(capture.current_run_id())
            with capture.call(KIND, INPUTS, **call_args()) as trace:
                self.assertIsNone(trace)
                self.assertIsNone(capture.current_call_id())
                self.assertIsNone(capture.current_run_id())
                capture.note_attempt()
                capture.note_response(raw="{}", usage={"tokens": 1}, corrected=True)
                capture.note_outcome("refused", "HTTP 400")

        self.assertEqual(os.listdir(self.directory), [])

    def test_an_exception_passes_through_unchanged(self):
        error = ValueError("provider broke")
        with self.assertRaises(ValueError) as caught:
            with capture.call(KIND, INPUTS, **call_args()):
                raise error
        self.assertIs(caught.exception, error)

    def test_a_store_that_stopped_recording_counts_as_none(self):
        store = self.install()
        store.close()

        self.assertFalse(capture.installed())
        self.assertIs(capture.current_store(), store)
        self.assertIsNone(capture.begin_run("run"))
        self.assertIsNone(record_call(result={"a": 1}))


class InstallTests(CaptureTestCase):
    def test_install_then_shutdown(self):
        store = self.install()
        self.assertTrue(capture.installed())
        self.assertEqual(store.path, self.path)
        self.assertTrue(os.path.exists(self.path))

        self.assertTrue(capture.shutdown())
        self.assertFalse(capture.installed())
        self.assertIsNone(capture.current_store())
        self.assertFalse(store.enabled)
        self.assertTrue(capture.shutdown())

    def test_a_store_that_cannot_open_is_not_installed(self):
        blocker = os.path.join(self.directory, "a_file")
        Path(blocker).write_text("not a directory")

        with self.assertLogs(PACKAGE_LOGGER, "WARNING"):
            self.assertFalse(capture.install(os.path.join(blocker, "capture.sqlite3")))

        self.assertFalse(capture.installed())
        self.assertIsNone(capture.current_store())

    def test_installing_again_closes_the_previous_store(self):
        first = self.install()
        second_path = os.path.join(self.directory, "other", "capture.sqlite3")

        second = self.install(second_path)

        self.assertFalse(first.enabled)
        self.assertIs(capture.current_store(), second)
        self.assertEqual(second.path, second_path)

    def check_a_store_opened_while_the_last_writer_is_busy(self, shut_down_first):
        """The next profile's store on the same file, while the last one's writer still holds
        rows the file does not: its ids must be past every id the last store handed out."""
        first = self.install()
        # Past its prune, so that it is the run's rows that meet the lock
        self.assertTrue(first.flush())
        blocker = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")

        with (
            mock.patch.object(capture, "REPLACE_TIMEOUT_SECONDS", 0.05),
            self.assertLogs(PACKAGE_LOGGER, "WARNING") as logs,
        ):
            first_run = capture.begin_run("first")
            with capture.run_scope(first_run):
                first_call = record_call(result="first").call_id
            if shut_down_first:
                self.assertFalse(capture.shutdown(timeout=0.05))
            second = self.install()
            self.assertIsNot(second, first)
            second_run = capture.begin_run("second")
            with capture.run_scope(second_run):
                second_call = record_call(result="second").call_id

            self.assertTrue(first._thread.is_alive(), "the first writer was to be busy")
            self.assertGreater(second_run, first_run)
            self.assertGreater(second_call, first_call)
            blocker.execute("COMMIT")
            first._thread.join(10.0)
            self.assertFalse(first._thread.is_alive())
            self.assertTrue(second.flush(10.0))

        self.assertEqual(
            [(run["run_id"], run["label"]) for run in self.rows("runs")],
            [(first_run, "first"), (second_run, "second")],
        )
        self.assertEqual(
            [(call["call_id"], call["response_json"]) for call in self.rows("calls")],
            [(first_call, '"first"'), (second_call, '"second"')],
        )
        self.assertEqual([line for line in logs.output if "refused" in line], [])

    def test_installing_again_while_the_writer_is_busy_opens_a_store_past_its_ids(self):
        self.check_a_store_opened_while_the_last_writer_is_busy(shut_down_first=False)

    def test_installing_after_a_shutdown_that_timed_out_opens_a_store_past_its_ids(self):
        self.check_a_store_opened_while_the_last_writer_is_busy(shut_down_first=True)


class RunTests(CaptureTestCase):
    def test_a_call_in_a_run_is_one_row_with_every_column(self):
        self.install()
        config = {"google_api_key": API_KEY, "gemini_model": MODEL, "nested": {"x": 1}}
        run_id = capture.begin_run(
            "Matched words",
            ops=["bulk_match_words_op"],
            chain_step="Step 1/2: Match words",
            note_count=2,
            config=config,
            extra={"source": "menu"},
        )
        self.assertIsNotNone(run_id)

        with capture.run_scope(run_id), capture.note_scope(NOTE_ID):
            with capture.task_scope("食べる|たべる"), capture.task_scope("m1"):
                with capture.call(KIND, INPUTS, **call_args()) as trace:
                    self.assertEqual(capture.current_call_id(), trace.call_id)
                    capture.note_attempt()
                    capture.note_attempt()
                    capture.note_response(raw='```json\n{"meaning": 1}\n```', usage={"n": 7})
                    trace.finish({"meaning": 1}, cancelled=False)
                self.assertIsNone(capture.current_call_id())
        capture.end_run(run_id, "completed")

        [call] = self.rows("calls")
        started, latency = call.pop("started"), call.pop("latency_ms")
        self.assertEqual(
            call,
            {
                "call_id": trace.call_id,
                "v": capture_store.SCHEMA_VERSION,
                "run_id": run_id,
                "note_id": NOTE_ID,
                "task_id": "m1",
                "parent_task_id": "食べる|たべる",
                "kind": KIND,
                "request_key": capture_store.request_key(KIND, INPUTS),
                "prompt_key": capture_store.prompt_key(
                    MODEL, INSTRUCTIONS, PROMPT, SCHEMA, PARAMS
                ),
                "model": MODEL,
                "params_json": capture_store.canonical_json(PARAMS),
                "inputs_json": capture_store.canonical_json(INPUTS),
                "instructions_hash": capture_store.text_hash(INSTRUCTIONS),
                "prompt": PROMPT,
                "schema_hash": capture_store.text_hash(capture_store.canonical_json(SCHEMA)),
                "response_raw": '```json\n{"meaning": 1}\n```',
                "response_json": '{"meaning":1}',
                "outcome": "ok",
                "error": None,
                "attempts": 2,
                "usage_json": '{"n":7}',
                "extra_json": None,
                "context_json": None,
            },
        )
        self.assertGreaterEqual(started, 0.0)
        self.assertLess(started, 5.0)
        self.assertGreaterEqual(latency, 0.0)

        blobs = {row["hash"]: row["text"] for row in self.rows("blobs")}
        schema_text = capture_store.canonical_json(SCHEMA)
        self.assertEqual(
            blobs,
            {
                capture_store.text_hash(INSTRUCTIONS): INSTRUCTIONS,
                capture_store.text_hash(schema_text): schema_text,
            },
        )

        [run] = self.rows("runs")
        self.assertLessEqual(run.pop("started"), run.pop("ended"))
        self.assertEqual(
            run,
            {
                "run_id": run_id,
                "v": capture_store.SCHEMA_VERSION,
                "label": "Matched words",
                "implicit": 0,
                "ops_json": '["bulk_match_words_op"]',
                "chain_step": "Step 1/2: Match words",
                "note_count": 2,
                "config_json": '{"gemini_model":"gemini-2.5-flash","nested":{"x":1}}',
                "versions_json": capture_store.canonical_json(VERSIONS),
                "log_path": LOG_PATH,
                "outcome": "completed",
                # end_run was given no extra, and does not wipe begin_run's
                "extra_json": '{"source":"menu"}',
                "profile": PROFILE,
            },
        )

    def test_the_calls_of_a_run_count_their_start_from_the_run(self):
        self.install()
        run_id = capture.begin_run("run")
        with capture.run_scope(run_id):
            first = record_call(result="a")
            second = record_call(result="b")

        calls = self.rows("calls")
        self.assertEqual([c["call_id"] for c in calls], [first.call_id, second.call_id])
        self.assertEqual({c["run_id"] for c in calls}, {run_id})
        self.assertLessEqual(0.0, calls[0]["started"])
        self.assertLessEqual(calls[0]["started"], calls[1]["started"])
        self.assertEqual(len(self.rows("runs")), 1)

    def test_each_run_records_the_profile_its_store_was_installed_for(self):
        self.install(profile="User 2")
        capture.begin_run("in User 2")
        self.assertTrue(capture.shutdown())
        self.install(profile=None)
        capture.begin_run("with no profile given")
        record_call(kind="translate.sentence", result="implicit")

        self.assertEqual(
            [(run["label"], run["profile"]) for run in self.rows("runs")],
            [("in User 2", "User 2"), ("with no profile given", None),
             ("translate.sentence", None)],
        )

    def test_end_run_extra_replaces_the_one_begin_run_had(self):
        self.install()
        run_id = capture.begin_run("run", extra={"source": "menu"})
        capture.end_run(run_id, "failed", extra={"error": "ValueError"})

        [run] = self.rows("runs")
        self.assertEqual((run["outcome"], run["extra_json"]), ("failed", '{"error":"ValueError"}'))

    def test_a_log_path_that_raises_leaves_the_run_without_one(self):
        def broken_log_path():
            raise OSError("no handler")

        self.install(log_path=broken_log_path)
        with self.assertLogs(capture.logger, "WARNING"):
            run_id = capture.begin_run("run")

        [run] = self.rows("runs")
        self.assertEqual((run["run_id"], run["log_path"]), (run_id, None))


class ImplicitRunTests(CaptureTestCase):
    def test_a_call_outside_a_run_gets_a_run_of_its_own_with_its_outcome(self):
        self.install()
        with capture.call("translate.sentence", {"sentence": "猫"}, **call_args()) as trace:
            run_id = capture.current_run_id()
            self.assertIsNotNone(run_id)
            self.assertEqual(trace.run_id, run_id)
            trace.finish(None, cancelled=True)
        self.assertIsNone(capture.current_run_id())

        [run] = self.rows("runs")
        [call] = self.rows("calls")
        self.assertEqual(
            (run["run_id"], run["implicit"], run["label"], run["outcome"]),
            (run_id, 1, "translate.sentence", "cancelled"),
        )
        self.assertIsNotNone(run["ended"])
        self.assertEqual(run["versions_json"], capture_store.canonical_json(VERSIONS))
        self.assertEqual(run["log_path"], LOG_PATH)
        self.assertEqual(run["profile"], PROFILE)
        self.assertEqual((call["run_id"], call["started"]), (run_id, 0.0))
        self.assertEqual(call["outcome"], "cancelled")

    def test_each_call_outside_a_run_gets_its_own_and_one_without_a_kind_is_a_call(self):
        self.install()
        record_call(kind="", result="a")
        record_call(kind="judge.words", result="b")

        runs = self.rows("runs")
        self.assertEqual([r["label"] for r in runs], ["call", "judge.words"])
        self.assertEqual([r["outcome"] for r in runs], ["ok", "ok"])
        self.assertEqual([c["run_id"] for c in self.rows("calls")], [r["run_id"] for r in runs])


class OutcomeTests(CaptureTestCase):
    def test_without_a_noted_outcome_the_result_and_the_cancel_decide(self):
        self.install()
        cases = [
            ({"a": [1, 2]}, False, "ok", '{"a":[1,2]}'),
            ("plain text", False, "ok", '"plain text"'),
            (3, True, "ok", "3"),
            (None, True, "cancelled", None),
            (None, False, "error", None),
        ]
        for result, cancelled, _, _ in cases:
            record_call(result=result, cancelled=cancelled)

        calls = self.rows("calls")
        self.assertEqual(
            [(c["outcome"], c["response_json"]) for c in calls],
            [(outcome, text) for _, _, outcome, text in cases],
        )

    def test_the_last_noted_outcome_wins_over_the_result(self):
        self.install()
        with capture.call(KIND, INPUTS, **call_args()) as trace:
            capture.note_outcome("unparseable", "not valid JSON")
            capture.note_outcome("refused", "HTTP 400: quota")
            trace.finish({"a": 1})
        self.assertEqual(trace.outcome, "refused")

        [call] = self.rows("calls")
        self.assertEqual((call["outcome"], call["error"]), ("refused", "HTTP 400: quota"))
        self.assertEqual(self.rows("runs")[0]["outcome"], "refused")

    def test_a_block_left_without_finish_records_no_outcome(self):
        self.install()
        with capture.call(KIND, INPUTS, **call_args()):
            pass

        [call] = self.rows("calls")
        self.assertIsNone(call["outcome"])

    def test_a_corrected_answer_is_marked_in_extra(self):
        self.install()
        with capture.call(KIND, INPUTS, **call_args()) as trace:
            capture.note_response(raw="{broken", corrected=True)
            # A later note without it leaves it set
            capture.note_response(usage={"n": 1})
            trace.finish({"a": 1})

        [call] = self.rows("calls")
        self.assertEqual(call["extra_json"], '{"corrected":true}')
        self.assertEqual((call["response_raw"], call["usage_json"]), ("{broken", '{"n":1}'))

    def test_an_exception_is_recorded_and_raised_unchanged(self):
        self.install()
        error = ValueError("provider broke")
        with self.assertRaises(ValueError) as caught:
            with capture.call(KIND, INPUTS, **call_args()) as trace:
                capture.note_outcome("refused", "HTTP 400")
                trace.finish({"a": 1})
                raise error
        self.assertIs(caught.exception, error)
        self.assertIsNone(capture.current_call_id())
        self.assertIsNone(capture.current_run_id())

        [call] = self.rows("calls")
        self.assertEqual((call["outcome"], call["error"]), ("error", repr(error)))
        self.assertEqual(self.rows("runs")[0]["outcome"], "error")

    def test_a_keyboard_interrupt_passes_through_unrecorded(self):
        self.install()
        with self.assertRaises(KeyboardInterrupt):
            with capture.call(KIND, INPUTS, **call_args()):
                raise KeyboardInterrupt
        self.assertIsNone(capture.current_call_id())
        self.assertIsNone(capture.current_run_id())
        self.assertEqual(self.rows("calls"), [])


class CaptureFailureTests(CaptureTestCase):
    def test_a_row_that_cannot_be_queued_changes_neither_result_nor_exception(self):
        store = self.install()
        error = ValueError("provider broke")

        def get_response(fail):
            with capture.call(KIND, INPUTS, **call_args()) as trace:
                if fail:
                    raise error
                trace.finish("answer")
                return "answer"

        with mock.patch.object(store, "insert_call", side_effect=RuntimeError("store broke")):
            with self.assertLogs(capture.logger, "WARNING") as logs:
                self.assertEqual(get_response(False), "answer")
                with self.assertRaises(ValueError) as caught:
                    get_response(True)

        self.assertIs(caught.exception, error)
        # Rate limited: one line for both
        self.assertEqual(len(logs.output), 1)
        self.assertIsNone(capture.current_call_id())
        self.assertIsNone(capture.current_run_id())

    def test_inputs_that_cannot_be_keyed_leave_the_call_unrecorded(self):
        self.install()
        # Keys of mixed types cannot be sorted into canonical JSON
        with self.assertLogs(capture.logger, "WARNING"):
            with capture.call(KIND, {1: "a", "b": 2}, **call_args()) as trace:
                self.assertIsNone(trace)
                self.assertIsNone(capture.current_run_id())

        self.assertEqual(self.rows("calls"), [])
        self.assertEqual(self.rows("runs"), [])

    def test_a_result_with_no_json_text_is_recorded_without_it(self):
        self.install()
        with self.assertLogs(capture.logger, "WARNING"):
            record_call(result={1: "a", "b": 2})

        [call] = self.rows("calls")
        self.assertEqual((call["outcome"], call["response_json"]), ("ok", None))

    def test_a_call_with_a_lone_surrogate_is_recorded_and_names_a_blob_that_is_there(self):
        self.install()
        # sqlite cannot bind either; json.loads makes such text of a JSON "\ud800"
        instructions = INSTRUCTIONS + "\ud800"
        with capture.call(KIND, INPUTS, **call_args(instructions=instructions)) as trace:
            capture.note_response(raw='{"meaning": "\ud800"}')
            trace.finish({"meaning": "\ud800"})

        [call] = self.rows("calls")
        self.assertEqual(call["response_raw"], '{"meaning": "\\ud800"}')
        self.assertEqual(call["outcome"], "ok")
        self.assertEqual(call["instructions_hash"], capture_store.text_hash(instructions))
        blobs = {row["hash"]: row["text"] for row in self.rows("blobs")}
        self.assertEqual(blobs[call["instructions_hash"]], INSTRUCTIONS + "\\ud800")

    def test_a_context_with_no_json_text_is_recorded_without_it(self):
        self.install()
        with self.assertLogs(capture.logger, "WARNING"):
            record_call(result={"a": 1}, context={1: "a", "b": 2})

        [call] = self.rows("calls")
        self.assertEqual((call["outcome"], call["context_json"]), ("ok", None))


class AnswerContextTests(CaptureTestCase):
    """`context`: what reading the answer needs that the prompt does not show."""

    CONTEXT = {"note_ids": [1712000000002, -4242424], "depth": 0}

    def test_the_context_is_recorded_as_it_was_at_entry_and_keys_nothing(self):
        self.install()
        context = copy.deepcopy(self.CONTEXT)
        with capture.call(KIND, INPUTS, context=context, **call_args()) as trace:
            # The op may change what it passed once the call has begun
            context["note_ids"].append(7)
            trace.finish({"a": 1})
        record_call(result={"a": 1})

        with_context, without = self.rows("calls")
        self.assertEqual(
            with_context["context_json"], capture_store.canonical_json(self.CONTEXT)
        )
        self.assertIsNone(without["context_json"])
        # The same request, whatever notes it was made for
        self.assertEqual(with_context["request_key"], without["request_key"])
        self.assertEqual(with_context["prompt_key"], without["prompt_key"])
        self.assertEqual(with_context["request_key"], capture_store.request_key(KIND, INPUTS))


class ContextTests(CaptureTestCase):
    def test_run_and_note_reach_the_calls_of_tasks_and_their_worker_threads(self):
        # How a bulk run works: the run is set on the op thread around the loop, each note
        # around the creation of its task, and get_response runs in asyncio.to_thread
        self.install()
        run_id = capture.begin_run("run")

        def get_response(label):
            record_call(inputs={"label": label}, result=label)
            return capture.current_note_id()

        async def main():
            tasks = []
            for note_id in (101, 102):
                with capture.note_scope(note_id):
                    tasks.append(asyncio.create_task(asyncio.to_thread(get_response, note_id)))
            with capture.note_scope(103):
                direct = await asyncio.to_thread(get_response, 103)
            return await asyncio.gather(*tasks) + [direct]

        with capture.run_scope(run_id):
            seen = asyncio.run(main())

        self.assertEqual(seen, [101, 102, 103])
        calls = self.rows("calls")
        self.assertEqual(
            sorted((c["run_id"], c["note_id"]) for c in calls),
            [(run_id, 101), (run_id, 102), (run_id, 103)],
        )
        self.assertEqual(len(self.rows("runs")), 1)

    def test_tasks_nest_and_a_none_label_sets_nothing(self):
        self.install()
        with capture.task_scope("word|reading"):
            record_call(result="outer")
            with capture.task_scope(None), capture.task_scope("m2"):
                record_call(result="inner")
        record_call(result="none")

        calls = self.rows("calls")
        self.assertEqual(
            [(c["task_id"], c["parent_task_id"]) for c in calls],
            [("word|reading", None), ("m2", "word|reading"), (None, None)],
        )


class FilterTests(CaptureTestCase):
    def ids(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "message", None, None)
        record.capture_ids = "stale"
        self.assertTrue(capture.CaptureContextFilter().filter(record))
        return record.capture_ids

    def test_the_ids_there_are_or_a_dash(self):
        self.assertEqual(self.ids(), "-")
        with capture.note_scope(NOTE_ID):
            # Capture off: a note scope still names its note
            self.assertEqual(self.ids(), f"n{NOTE_ID}")

        self.install()
        run_id = capture.begin_run("run")
        with capture.run_scope(run_id):
            self.assertEqual(self.ids(), f"r{run_id}")
            with capture.note_scope(NOTE_ID), capture.call(KIND, INPUTS, **call_args()) as trace:
                self.assertEqual(self.ids(), f"r{run_id} n{NOTE_ID} c{trace.call_id}")
        with capture.call(KIND, INPUTS, **call_args()) as trace:
            self.assertEqual(self.ids(), f"r{trace.run_id} c{trace.call_id}")
        self.assertEqual(self.ids(), "-")

    def test_the_filter_passes_the_record_even_when_it_fails(self):
        with mock.patch.object(capture, "_ids_text", side_effect=RuntimeError("broken")):
            self.assertEqual(self.ids(), "-")

    def test_a_handler_with_the_filter_formats_the_ids(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("[%(capture_ids)s] %(message)s"))
        handler.addFilter(capture.CaptureContextFilter())
        log = logging.getLogger("test_capture.formatted")
        log.propagate = False
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)

        log.warning("outside")
        with capture.note_scope(5):
            log.warning("inside")

        self.assertEqual(stream.getvalue(), "[-] outside\n[n5] inside\n")


class ScrubTests(CaptureTestCase):
    def test_secret_keys_go_at_any_depth_and_the_input_stays_as_it_was(self):
        config = {
            "google_api_key": "k1",
            "model": "gemini",
            "max_output_tokens": 100,
            "Nested": {
                "Access_Token": "t",
                "authToken": "t2",
                "apiKey": "k2",
                "clientSecret": "s",
                "keep": 1,
                "list": [{"password": "p", "x": 2}, "plain", ["a"]],
            },
        }
        before = copy.deepcopy(config)

        scrubbed = capture.scrub_config(config)

        self.assertEqual(
            scrubbed,
            {
                "model": "gemini",
                # A token count is a setting, not a secret
                "max_output_tokens": 100,
                "Nested": {"keep": 1, "list": [{"x": 2}, "plain", ["a"]]},
            },
        )
        self.assertEqual(config, before)
        scrubbed["Nested"]["list"][0]["y"] = 3
        scrubbed["Nested"]["list"][2].append("b")
        self.assertEqual(config, before)

    def test_an_api_key_in_the_config_is_nowhere_in_the_database_file(self):
        self.install()
        config = {"google_api_key": API_KEY, "model": MODEL, "ops": {"anthropic_api_key": API_KEY}}
        run_id = capture.begin_run("run", config=config)
        with capture.run_scope(run_id):
            record_call(result={"a": 1})
        capture.end_run(run_id, "completed")
        self.assertIn(MODEL, self.rows("runs")[0]["config_json"])
        self.assertTrue(capture.shutdown())

        files = [p for p in Path(self.directory).rglob("*") if p.is_file()]
        self.assertTrue(files)
        for path in files:
            self.assertNotIn(API_KEY.encode("utf-8"), path.read_bytes(), path.name)


class ImportTests(unittest.TestCase):
    def test_the_module_imports_the_standard_library_and_the_store_only(self):
        # An aqt import here takes the suite offline, and api_client will import this module
        tree = ast.parse(Path(capture.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    self.assertEqual((node.level, node.module), (1, "capture_store"))
                    continue
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                self.assertIn(name.split(".")[0], sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
