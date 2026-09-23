"""One op run as a step of a chain: `selected_notes_op(..., chain=...)`.

A chain starts its next step from the step before's `on_done`, so a path that never calls it
hangs the chain and one that calls it twice runs a step twice. These drive the real
`selected_notes_op` through a stand-in `CollectionOp` that runs the op on the test's thread
and then calls the success or failure handler the way aqt does, and check that `on_done` is
called once, after the progress is finished, with the status the run earned - and that a run
from the menu, with no chain, still ends in its tooltip.
"""

import types
import unittest
from unittest import mock

from addon_modules import load_ops_module, mw

base_ops = load_ops_module("base_ops")
# The same module objects base_ops imported, so cancelling here cancels base_ops' run
api = load_ops_module("api_client")
collection_access = load_ops_module("collection_access")

TITLE = "Async AI op: Doing the thing"


class FakeNote:
    def __init__(self, note_id: int):
        self.id = note_id
        self.fields: list = []


class FakeCollection:
    def get_note(self, nid):
        return FakeNote(nid)

    def update_notes(self, notes):
        pass

    def remove_notes(self, nids):
        pass

    def merge_undo_entries(self, pos):
        return "changes"

    def add_custom_undo_entry(self, message):
        return 1


class FakeCollectionOp:
    """aqt's CollectionOp, run on demand: `finish()` is the op thread's run plus aqt's end.

    Like aqt's `with_progress`, the progress is finished before either handler, and an
    exception goes to the failure handler only if one was given; without one aqt shows it,
    which is recorded here as `shown_by_aqt`.
    """

    def __init__(self, parent, op, open_dialog):
        self.op = op
        self.open_dialog = open_dialog
        self.on_success = None
        self.on_failure = None
        self.started = False
        self.shown_by_aqt = None

    def success(self, callback):
        self.on_success = callback
        return self

    def failure(self, callback):
        self.on_failure = callback
        return self

    def run_in_background(self):
        self.started = True
        # aqt's progress.start, before run_in_background returns
        self.open_dialog()

    def finish(self):
        try:
            result = self.op(mw.col)
        except Exception as e:
            mw.progress.finish()
            if self.on_failure is not None:
                self.on_failure(e)
            else:
                self.shown_by_aqt = e
            return
        mw.progress.finish()
        self.on_success(result)


class ChainStepTest(unittest.TestCase):
    def setUp(self):
        # What the main thread sees, in order: finishes, titles, messages and outcomes
        self.events: list = []
        self.outcomes: list = []
        self.ops: list = []
        # aqt's set_title does nothing while there is no progress dialog
        self.dialog_open = False
        mw.progress.cancel = False
        self.addCleanup(setattr, mw.progress, "cancel", False)
        patches = [
            mock.patch.object(mw, "col", FakeCollection(), create=True),
            mock.patch.object(
                mw, "addonManager", types.SimpleNamespace(getConfig=lambda _: {})
            ),
            mock.patch.object(mw.progress, "finish", self.finish_progress),
            mock.patch.object(mw.progress, "set_title", self.set_title),
            mock.patch.object(base_ops, "CollectionOp", self.make_op),
            mock.patch.object(base_ops, "start_run_controls", lambda: None),
            mock.patch.object(
                base_ops,
                "tooltip",
                lambda message, **_: self.events.append(("tooltip", message)),
            ),
            mock.patch.object(
                base_ops,
                "showWarning",
                lambda message, **_: self.events.append(("warning", message)),
            ),
            mock.patch.object(
                base_ops,
                "show_exception",
                lambda parent, exception: self.events.append(("error", exception)),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # A run ended by a test must not leave a stop reason for the next one
        self.addCleanup(api.take_stop_reason)

    def finish_progress(self):
        self.dialog_open = False
        self.events.append("finish")

    def set_title(self, title):
        if self.dialog_open:
            self.events.append(("title", title))

    def make_op(self, parent, op):
        fake = FakeCollectionOp(parent, op, lambda: setattr(self, "dialog_open", True))
        self.ops.append(fake)
        return fake

    def on_done(self, outcome):
        self.events.append("done")
        self.outcomes.append(outcome)

    def start(self, bulk_op, chain=True, on_success=None):
        """Start the op as `selected_notes_op` would from a menu or a chain, not yet run."""
        step = base_ops.ChainStep("Step 2/3", self.on_done) if chain else None
        updater = base_ops.AsyncTaskProgressUpdater(title=TITLE)
        base_ops.selected_notes_op(
            "Did the thing",
            bulk_op,
            [1, 2],
            None,
            updater,
            on_success=on_success,
            chain=step,
        )
        self.assertEqual(len(self.ops), 1)
        self.assertTrue(self.ops[0].started)
        return self.ops[0]

    def run_step(self, bulk_op, **kwargs):
        op = self.start(bulk_op, **kwargs)
        op.finish()
        return op

    def assert_one_outcome(self, status):
        self.assertEqual(len(self.outcomes), 1, self.events)
        outcome = self.outcomes[0]
        self.assertEqual(outcome.status, status)
        # After the progress is finished, and nothing shown by the step itself
        self.assertIn("finish", self.events[: self.events.index("done")])
        self.assertEqual(
            [e for e in self.events if isinstance(e, tuple) and e[0] in ("tooltip", "warning")],
            [],
        )
        return outcome

    # --- The bulk ops ------------------------------------------------------------------

    @staticmethod
    async def edits_one(col, notes, edited_nids, progress_updater, **dicts):
        dicts["notes_to_update_dict"][notes[0].id] = notes[0]
        return 1, dicts["notes_to_add_dict"], dicts["notes_to_update_dict"], []

    @staticmethod
    async def is_cancelled(col, notes, edited_nids, progress_updater, **dicts):
        # As the cancel monitor does on Cancel in the dialog
        api.cancel_run()
        return 1, {}, {}, []

    @staticmethod
    async def stops_itself(col, notes, edited_nids, progress_updater, **dicts):
        # As the claude CLI does on an expired login
        api.cancel_run(reason="the login expired")
        return 1, {}, {}, []

    @staticmethod
    async def escaped(col, notes, edited_nids, progress_updater, **dicts):
        # A sync op stops on the dialog's flag alone; the run itself is never cancelled
        mw.progress.cancel = True
        return 1, {}, {}, []

    @staticmethod
    async def ends_paused(col, notes, edited_nids, progress_updater, **dicts):
        api.pause_run("paused by user")
        return 1, {}, {}, []

    @staticmethod
    async def raises(col, notes, edited_nids, progress_updater, **dicts):
        raise ValueError("boom")

    @staticmethod
    async def read_after_cancel(col, notes, edited_nids, progress_updater, **dicts):
        api.cancel_run()
        # What collection_access does to a read on a cancelled run
        raise collection_access.RunCancelled("read refused")

    # --- Outcomes ----------------------------------------------------------------------

    def test_a_finished_run_is_completed_with_the_menu_s_message(self):
        self.run_step(self.edits_one)

        outcome = self.assert_one_outcome("completed")
        self.assertEqual(outcome.message, "Did the thing in 1/2 selected notes.")
        self.assertIsNone(outcome.stop_reason)
        self.assertFalse(outcome.stops_chain)

    def test_a_cancelled_run_is_cancelled(self):
        self.run_step(self.is_cancelled)

        outcome = self.assert_one_outcome("cancelled")
        self.assertTrue(outcome.stops_chain)

    def test_escape_in_a_sync_op_is_a_cancel_though_the_run_never_was(self):
        self.run_step(self.escaped)

        self.assert_one_outcome("cancelled")

    def test_escape_counts_though_the_cleanup_resets_the_flag(self):
        """The cleanup's note adding re-arms the dialog's cancel for itself, clearing it."""

        def rearm(pos):
            mw.progress.cancel = False
            return "changes"

        with mock.patch.object(mw.col, "merge_undo_entries", rearm):
            self.run_step(self.escaped)

        self.assert_one_outcome("cancelled")

    def test_a_run_that_ends_paused_is_cancelled(self):
        """Teardown cancels a run left paused; its notes were not all done."""
        self.run_step(self.ends_paused)

        self.assert_one_outcome("cancelled")

    def test_a_stop_reason_is_a_stop_and_is_taken(self):
        self.run_step(self.stops_itself)

        outcome = self.assert_one_outcome("stopped")
        self.assertEqual(outcome.stop_reason, "the login expired")
        # Not repeated in the message; the chain words it
        self.assertNotIn("Stopped early", outcome.message)
        self.assertIsNone(api.take_stop_reason())

    def test_run_cancelled_out_of_the_op_is_a_cancel(self):
        self.run_step(self.read_after_cancel)

        outcome = self.assert_one_outcome("cancelled")
        self.assertEqual(outcome.message, "Did the thing in 0/2 selected notes.")

    def test_an_exception_is_shown_as_aqt_would_and_fails_the_step(self):
        self.run_step(self.raises)

        outcome = self.assert_one_outcome("failed")
        self.assertEqual(outcome.error, "ValueError: boom")
        errors = [e[1] for e in self.events if isinstance(e, tuple) and e[0] == "error"]
        self.assertEqual([str(e) for e in errors], ["boom"])
        self.assertIsNone(self.ops[0].shown_by_aqt)

    def test_an_error_in_the_success_handler_still_ends_the_step(self):
        def on_success():
            raise RuntimeError("callback broke")

        self.run_step(self.edits_one, on_success=on_success)

        outcome = self.assert_one_outcome("failed")
        self.assertEqual(outcome.error, "RuntimeError: callback broke")

    # --- Title -------------------------------------------------------------------------

    def titles(self):
        return [e[1] for e in self.events if isinstance(e, tuple) and e[0] == "title"]

    def test_the_dialog_title_starts_with_the_step(self):
        op = self.start(self.edits_one)

        # Drawn again once the dialog exists, which it does only after the start
        self.assertEqual(self.titles()[-1], f"Step 2/3: {TITLE}")
        op.finish()

    def test_the_step_survives_a_phase_title(self):
        phases = [base_ops.OpPhase("One", self.edits_one), base_ops.OpPhase("Two", self.edits_one)]

        self.run_step(phases)

        self.assertIn(f"Step 2/3: {TITLE} (Phase 2/2: Two)", self.titles())
        self.assert_one_outcome("completed")

    # --- No chain ----------------------------------------------------------------------

    def test_without_a_chain_the_run_ends_in_its_tooltip(self):
        op = self.run_step(self.edits_one, chain=False)

        self.assertIsNone(op.on_failure)
        self.assertEqual(self.outcomes, [])
        self.assertIn(("tooltip", "Did the thing in 1/2 selected notes."), self.events)
        self.assertTrue(all(not title.startswith("Step") for title in self.titles()))

    def test_without_a_chain_a_stop_is_a_warning(self):
        self.run_step(self.stops_itself, chain=False)

        warnings = [e[1] for e in self.events if isinstance(e, tuple) and e[0] == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("<b>Stopped early.</b> the login expired", warnings[0])

    def test_without_a_chain_aqt_shows_the_exception(self):
        op = self.run_step(self.raises, chain=False)

        self.assertIsInstance(op.shown_by_aqt, ValueError)
        self.assertEqual(self.outcomes, [])


if __name__ == "__main__":
    unittest.main()
