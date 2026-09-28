"""Which log file a record goes to, and whose job it is to decide.

The question this file exists to settle is the one that produced 1,453 log files for a single
36-minute run: a hook that fires per note has to be able to tell that a bulk operation has
already chosen the file, and leave it alone. Everything else here is the mechanics of putting
the run's own handler back afterwards, so the phases either side of a phase log stay in one
file and the phase table is still one grep.
"""

import io
import logging
import os
import tempfile
import types
import unittest
from unittest import mock

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
from addon_modules import load_ops_module, mw

cl = load_ops_module("call_logging", "")
capture = load_ops_module("capture")


class FakeHandler(logging.Handler):
    """A handler that records rather than writes, flagged the way a real one is."""

    def __init__(self, name):
        super().__init__()
        self.name_ = name
        self.records: "list[str]" = []
        self.closed = False
        setattr(self, cl._ADDON_HANDLER_FLAG, True)

    def emit(self, record):
        self.records.append(record.getMessage())

    def close(self):
        self.closed = True
        super().close()


class LoggingTestCase(unittest.TestCase):
    """The real logger, with the handlers taken off it for the duration."""

    def setUp(self):
        self.logger = cl.addon_logger()
        self.saved = list(self.logger.handlers)
        self.saved_level = self.logger.level
        for handler in self.saved:
            self.logger.removeHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        # The name the last call log was opened under, which a phase's file takes on
        self.saved_log_name = cl._log_name
        cl._log_name = None

        self.created: "list[FakeHandler]" = []

        def create(function_name):
            handler = FakeHandler(function_name)
            self.created.append(handler)
            return handler

        self.real_create = cl.create_call_log_handler
        cl.create_call_log_handler = create
        # Nothing in the tests keeps a worker thread alive, so a detached handler closes
        # straight away rather than starting a closer thread
        self.addCleanup(self.restore)

    def restore(self):
        cl.create_call_log_handler = self.real_create
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
        for handler in self.saved:
            self.logger.addHandler(handler)
        self.logger.setLevel(self.saved_level)
        cl._bulk_state.depth = 0
        cl._log_name = self.saved_log_name


class InBulkOpTests(LoggingTestCase):
    """The guard a per-note hook asks before touching a handler."""

    def test_nothing_is_in_progress_by_default(self):
        self.assertFalse(cl.in_bulk_op())

    def test_a_bulk_op_is_visible_for_as_long_as_it_runs(self):
        with cl.bulk_op_logging():
            self.assertTrue(cl.in_bulk_op())
        self.assertFalse(cl.in_bulk_op())

    def test_it_survives_an_exception_out_of_the_run(self):
        with self.assertRaises(ValueError):
            with cl.bulk_op_logging():
                raise ValueError("cancelled")
        self.assertFalse(cl.in_bulk_op())

    def test_a_phase_inside_a_bulk_op_does_not_end_it(self):
        """The fall-through that must not happen.

        A phase installs a file of its own, and the hooks firing inside it still have to see a
        bulk op in progress - otherwise each one replaces the phase's handler with a file of
        its own, which is the behaviour being removed.
        """
        with cl.bulk_op_logging():
            with cl.phase_log("add_note_phase"):
                self.assertTrue(cl.in_bulk_op())
            self.assertTrue(cl.in_bulk_op())

    def test_it_nests(self):
        with cl.bulk_op_logging():
            with cl.bulk_op_logging():
                self.assertTrue(cl.in_bulk_op())
            self.assertTrue(cl.in_bulk_op())
        self.assertFalse(cl.in_bulk_op())


class PhaseLogTests(LoggingTestCase):
    def test_a_phase_gets_the_records_and_the_run_gets_the_rest(self):
        run_handler = FakeHandler("run")
        self.logger.addHandler(run_handler)

        self.logger.info("before")
        with cl.phase_log("add_note_phase"):
            self.logger.info("during")
        self.logger.info("after")

        self.assertEqual(run_handler.records, ["before", "after"])
        phase_handler = self.created[-1]
        self.assertEqual(phase_handler.records, ["during"])

    def test_the_run_handler_is_put_back_and_never_closed(self):
        run_handler = FakeHandler("run")
        self.logger.addHandler(run_handler)

        with cl.phase_log("add_note_phase"):
            self.assertNotIn(run_handler, self.logger.handlers)

        self.assertIn(run_handler, self.logger.handlers)
        self.assertFalse(run_handler.closed)
        self.assertTrue(self.created[-1].closed)

    def test_the_run_handler_comes_back_after_an_exception(self):
        run_handler = FakeHandler("run")
        self.logger.addHandler(run_handler)

        with self.assertRaises(ValueError):
            with cl.phase_log("add_note_phase"):
                raise ValueError("cancelled")

        self.assertIn(run_handler, self.logger.handlers)

    def test_a_phase_that_cannot_open_a_file_keeps_the_run_logging(self):
        """A log file is diagnostics. Failing to make one must not take the phase down."""
        run_handler = FakeHandler("run")
        self.logger.addHandler(run_handler)

        def refuse(function_name):
            raise OSError("read-only")

        cl.create_call_log_handler = refuse
        with cl.phase_log("add_note_phase"):
            self.logger.info("during")
        self.assertIn("during", run_handler.records)

    def test_handlers_that_are_not_this_addons_are_left_where_they_are(self):
        """Anki's own, or a developer's. Only the flagged ones are ours to move."""
        foreign = logging.Handler()
        self.logger.addHandler(foreign)
        try:
            with cl.phase_log("add_note_phase"):
                self.assertIn(foreign, self.logger.handlers)
        finally:
            self.logger.removeHandler(foreign)


class LogNameTests(LoggingTestCase):
    """Which name a file gets: the op that started it, so one op's files can be picked out of the
    log folder, where every file was once named after the context menu hook."""

    def names(self):
        return [handler.name_ for handler in self.created]

    def test_a_call_log_is_named_as_it_was_started(self):
        cl.start_call_log("match_words")
        self.assertEqual(self.names(), ["match_words"])
        self.assertEqual(self.logger.handlers, [self.created[0]])

    def test_a_phase_file_is_named_after_the_run_it_belongs_to(self):
        cl.start_call_log("match_words")
        with cl.phase_log("add_note_phase"):
            pass
        self.assertEqual(self.names(), ["match_words", "match_words_add_note_phase"])

    def test_a_phase_follows_the_latest_run_s_name(self):
        cl.start_call_log("match_words")
        cl.start_call_log("new_note_all_ops")
        with cl.phase_log("add_note_phase"):
            pass
        self.assertEqual(self.names()[-1], "new_note_all_ops_add_note_phase")

    def test_a_phase_with_no_run_log_keeps_its_own_name(self):
        with cl.phase_log("add_note_phase"):
            pass
        self.assertEqual(self.names(), ["add_note_phase"])

    def test_a_log_that_cannot_be_opened_leaves_the_previous_one_in_place(self):
        cl.start_call_log("match_words")

        def cannot(function_name):
            raise OSError("disk full")

        cl.create_call_log_handler = cannot
        # Right before an op starts: a missing log file must not keep it from running
        cl.start_call_log("translate_sentence")
        self.assertEqual(self.logger.handlers, [self.created[0]])
        self.assertFalse(self.created[0].closed)
        # Still the new op's name: its phase's file is its own, not named after the op before
        self.assertEqual(cl.phase_log_name("add_note_phase"), "translate_sentence_add_note_phase")


class CaptureIdsTests(LoggingTestCase):
    """The handlers call_logging makes, as they are: every line carries the capture ids, and
    the run's file is the path the capture store records."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = {"log_level": "DEBUG", "log_to_console": False}
        manager = types.SimpleNamespace(getConfig=lambda _name: self.config)
        # call_logging puts its files beside itself, under user_files/logs
        for target, name, value in (
            (mw, "addonManager", manager),
            (cl, "__file__", os.path.join(directory.name, "call_logging.py")),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def attach(self, log_to_console: bool) -> logging.Handler:
        self.config["log_to_console"] = log_to_console
        handler = self.real_create("call")
        self.addCleanup(handler.close)
        if log_to_console:
            self.stream = io.StringIO()
            handler.setStream(self.stream)
        self.logger.addHandler(handler)
        return handler

    def written(self, handler: logging.Handler) -> str:
        if isinstance(handler, logging.FileHandler):
            handler.flush()
            with open(handler.baseFilename, encoding="utf-8") as file:
                return file.read()
        return self.stream.getvalue()

    def test_a_record_with_no_ids_has_a_dash(self):
        for log_to_console in (True, False):
            with self.subTest(log_to_console=log_to_console):
                handler = self.attach(log_to_console)

                self.logger.warning("nothing current")

                self.assertRegex(
                    self.written(handler),
                    r" - WARNING - \[-\] nothing current\n$",
                )
                self.logger.removeHandler(handler)

    def test_a_child_logger_s_record_has_the_ids_where_it_was_logged(self):
        """The filter is on the handler: a logger's own filters never see its children's."""
        handler = self.attach(log_to_console=True)
        child = logging.getLogger(f"{cl.ADDON_MODULE}.async_api_ops.base_ops")

        with capture.run_scope(12), capture.note_scope(1712345678901):
            child.info("inside")

        self.assertIn(" - INFO - [r12 n1712345678901] inside\n", self.written(handler))

    def test_the_log_path_is_the_file_handler_s(self):
        self.assertIsNone(cl.current_log_path())
        # Not the addon's: a developer's or Anki's own file is not the run's log
        foreign_path = os.path.join(os.path.dirname(cl.__file__), "anki.log")
        foreign = logging.FileHandler(foreign_path, delay=True)
        self.logger.addHandler(foreign)
        self.addCleanup(foreign.close)
        self.assertIsNone(cl.current_log_path())

        handler = self.attach(log_to_console=False)

        self.assertEqual(cl.current_log_path(), handler.baseFilename)
        self.assertTrue(handler.baseFilename.endswith(".log"))

    def test_console_logging_has_no_log_path(self):
        self.attach(log_to_console=True)

        self.assertIsNone(cl.current_log_path())


if __name__ == "__main__":
    unittest.main()
