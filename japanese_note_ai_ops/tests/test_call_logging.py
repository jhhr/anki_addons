"""Which log file a record goes to, and whose job it is to decide.

The question this file exists to settle is the one that produced 1,453 log files for a single
36-minute run: a hook that fires per note has to be able to tell that a bulk operation has
already chosen the file, and leave it alone. Everything else here is the mechanics of putting
the run's own handler back afterwards, so the phases either side of a phase log stay in one
file and the phase table is still one grep.
"""

import logging
import unittest

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
import addon_modules  # noqa: F401
from anki_stubs import load_ops_module

cl = load_ops_module("call_logging", "")


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


if __name__ == "__main__":
    unittest.main()
