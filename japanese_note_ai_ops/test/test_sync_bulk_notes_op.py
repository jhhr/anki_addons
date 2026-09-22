"""The sequential path local ops take: one note after another on the op thread.

Nothing here goes through the gate, so a pause has to be honoured by the loop itself: a paused
run processes no further note until it resumes, and Escape in the dialog still ends it while it
waits, although that cancel reaches only the dialog and not the run.
"""

import unittest

from addon_modules import PausingClock, load_ops_module, mw

base_ops = load_ops_module("base_ops")
# The same module object base_ops imported, so pausing it pauses base_ops' run
api = load_ops_module("api_client")


class FakeNote:
    def __init__(self, note_id: int):
        self.id = note_id


class SyncBulkNotesOpPauseTest(unittest.TestCase):
    def setUp(self):
        mw.progress.cancel = False
        self.addCleanup(setattr, mw.progress, "cancel", False)
        self.labels: list = []
        saved_update = mw.progress.update
        mw.progress.update = lambda **kwargs: self.labels.append(kwargs.get("label"))
        self.addCleanup(setattr, mw.progress, "update", saved_update)
        api.begin_run()
        self.addCleanup(api.end_run)
        self.processed: list = []

    def install_clock(self, on_sleep) -> PausingClock:
        # Only api_client's clock: base_ops formats its labels with the real time module
        clock = PausingClock(on_sleep)
        saved = api.time
        setattr(api, "time", clock)
        self.addCleanup(setattr, api, "time", saved)
        return clock

    def op(self, config, note, notes_to_add_dict, notes_to_update_dict):
        """Processes a note; the first one pauses the run, as a click on Pause would."""
        self.processed.append(note.id)
        if note.id == 1:
            api.pause_run("paused by user")
        return True

    def run_op(self):
        return base_ops.sync_bulk_notes_op(
            0,
            None,
            {},
            self.op,
            [FakeNote(1), FakeNote(2), FakeNote(3)],
            [],
            "Doing the thing",
            notes_to_add_dict={},
            notes_to_update_dict={},
        )

    def test_no_further_note_is_processed_while_paused(self):
        seen_while_paused: list = []

        def on_sleep(sleeps):
            seen_while_paused.append(list(self.processed))
            if sleeps == 3:
                api.resume_run()

        self.install_clock(on_sleep)
        self.run_op()

        self.assertEqual(seen_while_paused, [[1]] * 3)
        self.assertEqual(self.processed, [1, 2, 3])
        paused_labels = [label for label in self.labels if "Paused" in label]
        self.assertEqual(paused_labels, ["<b>Doing the thing</b><br><b>Paused</b>: paused by user"])

    def test_escape_while_paused_ends_the_op(self):
        """The dialog's cancel does not cancel the run, so the wait has to watch it too."""

        def on_sleep(sleeps):
            if sleeps == 2:
                mw.progress.cancel = True

        self.install_clock(on_sleep)
        self.run_op()

        self.assertEqual(self.processed, [1])
        # Still paused: selected_notes_op's teardown is what cancels a run left like this
        self.assertIsNotNone(api.pause_state())

    def test_a_run_cancelled_while_paused_ends_the_op(self):
        def on_sleep(sleeps):
            if sleeps == 2:
                api.cancel_run()

        self.install_clock(on_sleep)
        self.run_op()

        self.assertEqual(self.processed, [1])


if __name__ == "__main__":
    unittest.main()
