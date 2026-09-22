"""What the progress dialog shows about a paused run, and the buttons' logic.

The dialog runs on the main thread, which belongs to no run, while the updater's ticker runs
on the op thread's event loop, which does. These tests enrol the test thread in a run, as the
op thread is, and record the labels the updater draws by standing in for
`mw.progress.update`; the stubs' `run_on_main` runs every redraw at once.

The Qt half of progress_controls - building the buttons into Anki's dialog - needs a real
Anki and is not tested here. What is: that it keeps quiet without that dialog, and the
buttons' behaviour, which is plain Python over the run's pause.
"""

import time
import unittest

from addon_modules import load_ops_module, mw

base_ops = load_ops_module("base_ops")
# The same module objects base_ops imported, so pausing here pauses base_ops' run
api = load_ops_module("api_client")
controls_module = load_ops_module("progress_controls")


class DialogTestCase(unittest.TestCase):
    def setUp(self):
        mw.progress.cancel = False
        self.addCleanup(setattr, mw.progress, "cancel", False)
        self.drawn: list = []
        saved_update = mw.progress.update
        mw.progress.update = lambda **kwargs: self.drawn.append(kwargs)
        self.addCleanup(setattr, mw.progress, "update", saved_update)
        api.begin_run()
        self.addCleanup(api.end_run)

    @property
    def labels(self) -> list:
        return [drawn["label"] for drawn in self.drawn]

    def make_updater(self, total_tasks: int = 10):
        return base_ops.AsyncTaskProgressUpdater(total_tasks=total_tasks, title="Doing it")

    def draw(self, updater) -> str:
        """One update_progress, past the redraw throttle, and the label it drew."""
        updater._last_update_at = 0.0
        before = len(self.drawn)
        updater.update_progress()
        self.assertEqual(len(self.drawn), before + 1, "update_progress drew nothing")
        return self.labels[-1]

    def replace(self, name: str, replacement) -> None:
        saved = getattr(base_ops, name)
        setattr(base_ops, name, replacement)
        self.addCleanup(setattr, base_ops, name, saved)


class PausedTimeTests(DialogTestCase):
    def test_the_eta_leaves_paused_time_out(self):
        """Five tasks in 100 s of which 50 paused: 10 s a task, 50 s for the five left."""
        updater = self.make_updater(total_tasks=10)
        updater.increment_counts(tasks_done=5, cumulative_task_time=5.0)
        updater.start_time -= 100
        updater.paused_seconds = 50.0

        label = self.draw(updater)

        self.assertIn("ETA: 00:00:50", label)
        # The wall clock stays the wall clock, and says how much of it was paused
        self.assertIn("Time: 00:01:40", label)
        self.assertIn("(paused 00:00:50)", label)

    def test_without_a_pause_the_eta_is_unchanged(self):
        updater = self.make_updater(total_tasks=10)
        updater.increment_counts(tasks_done=5, cumulative_task_time=5.0)
        updater.start_time -= 100

        label = self.draw(updater)

        self.assertIn("ETA: 00:01:40", label)
        self.assertNotIn("paused", label)

    def test_the_ticker_counts_only_the_time_the_run_was_paused(self):
        updater = self.make_updater()
        # The first beat has nothing before it to count, paused or not
        api.pause_run("paused by user")
        updater._tick(1000.0)
        self.assertEqual(updater.paused_seconds, 0.0)

        updater._tick(1002.5)
        updater._tick(1003.0)
        self.assertEqual(updater.paused_seconds, 3.0)

        api.resume_run()
        updater._tick(1010.0)
        self.assertEqual(updater.paused_seconds, 3.0)

    def test_begin_phase_starts_the_paused_time_over(self):
        """It is time since start_time, which the phase resets."""
        updater = self.make_updater()
        updater.paused_seconds = 30.0
        updater._last_tick = 5.0

        updater.begin_phase(2, 2, "Judge")

        self.assertEqual(updater.paused_seconds, 0.0)
        self.assertIsNone(updater._last_tick)


class PauseLineTests(DialogTestCase):
    def test_no_pause_line_while_running(self):
        label = self.draw(self.make_updater())

        self.assertNotIn("Paused", label)

    def test_a_manual_pause_says_why_and_what_is_still_running(self):
        updater = self.make_updater()
        updater.increment_counts(tasks_in_progress=3)
        api.pause_run("paused by user")

        label = self.draw(updater)

        self.assertTrue(
            label.startswith(
                "<b>Paused</b>: paused by user, 3 tasks finishing or waiting to retry<br>"
            ),
            label,
        )
        self.assertNotIn("resuming at", label)

    def test_one_task_is_one_task(self):
        updater = self.make_updater()
        updater.increment_counts(tasks_in_progress=1)
        api.pause_run("paused by user")

        self.assertIn(", 1 task finishing or waiting to retry", self.draw(updater))

    def test_nothing_running_is_not_mentioned(self):
        updater = self.make_updater()
        api.pause_run("paused by user")

        self.assertIn("<b>Paused</b>: paused by user<br>", self.draw(updater))

    def test_an_automatic_pause_says_when_it_ends(self):
        updater = self.make_updater()
        resume_at = time.time() + 30 * 60 - 5
        api.pause_run("usage limit was reached: resets <5pm>", resume_at)

        label = self.draw(updater)

        clock = time.strftime("%H:%M", time.localtime(resume_at))
        self.assertIn(
            "<b>Paused</b>: usage limit was reached: resets &lt;5pm&gt;,"
            f" resuming at {clock} (in 30 min)",
            label,
        )

    def test_an_automatic_pause_that_is_over_is_ended_not_shown(self):
        updater = self.make_updater()
        api.pause_run("usage limit was reached: resets 5pm", time.time() - 1)

        label = self.draw(updater)

        self.assertNotIn("Paused", label)
        self.assertIsNone(api.pause_state())


class ShowPausedTests(DialogTestCase):
    """The pause between phases, where no ticker runs to draw it."""

    def test_draws_the_pause_even_over_a_queued_redraw(self):
        updater = self.make_updater()
        api.pause_run("paused by user")
        # The phase before's last redraw, not yet run on the main thread
        updater._update_pending = True

        updater.show_paused("Phase 2/2 (Judge) starts when the run resumes")

        self.assertEqual(
            self.drawn[-1],
            {
                "label": "<b>Paused</b>: paused by user<br>"
                "Phase 2/2 (Judge) starts when the run resumes",
                "value": 1,
                "max": 1,
            },
        )

    def test_draws_nothing_when_not_paused(self):
        updater = self.make_updater()

        updater.show_paused("Phase 2/2 starts when the run resumes")

        self.assertEqual(self.drawn, [])

    def test_draws_nothing_over_the_cancelling_message(self):
        updater = self.make_updater()
        updater.show_cancelling()
        api.pause_run("paused by user")

        updater.show_paused("Phase 2/2 starts when the run resumes")

        self.assertEqual(len(self.drawn), 1)
        self.assertIn("Cancelling", self.labels[0])


class ControlsRefreshTests(DialogTestCase):
    """Every redraw brings the buttons along; a cancel greys them out."""

    def test_every_redraw_refreshes_the_buttons(self):
        refreshed: list = []
        self.replace("refresh_run_controls", lambda: refreshed.append(len(self.drawn)))
        updater = self.make_updater()

        self.draw(updater)
        self.draw(updater)

        # After each label, so the buttons match what the label says
        self.assertEqual(refreshed, [1, 2])

    def test_cancelling_disables_the_buttons(self):
        disabled: list = []
        self.replace("disable_run_controls", lambda: disabled.append(True))
        updater = self.make_updater()

        updater.show_cancelling()

        self.assertEqual(disabled, [True])

    def test_cleanup_disables_the_buttons_of_a_run_that_was_not_cancelled(self):
        disabled: list = []
        self.replace("disable_run_controls", lambda: disabled.append(True))
        updater = self.make_updater()

        updater.begin_cleanup()

        self.assertEqual(disabled, [True])


class CleanupDrawTests(DialogTestCase):
    """A cancelled run's cleanup adds its new notes, and shows it doing so."""

    def draw_adding(self, updater, notes_added: int) -> None:
        updater._last_update_at = 0.0
        updater.update_note_adding_progress(notes_added=notes_added, total_notes=3)

    def test_adding_progress_is_drawn_over_the_cancelling_message_once_cleanup_begins(self):
        updater = self.make_updater()
        updater.show_cancelling()
        self.draw_adding(updater, 1)
        self.assertEqual(len(self.drawn), 1, "the unwinding burst is still held back")

        updater.begin_cleanup()
        self.draw_adding(updater, 2)

        self.assertEqual(len(self.drawn), 2)
        self.assertIn("Adding notes", self.labels[-1])
        self.assertIn("2/3", self.labels[-1])

    def test_a_run_that_was_not_cancelled_draws_as_before(self):
        updater = self.make_updater()

        updater.begin_cleanup()
        self.draw_adding(updater, 1)

        self.assertIn("Adding notes", self.labels[-1])


class FakeButton:
    def __init__(self, text: str):
        self._text = text
        self.enabled = True

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


class FakeWindow:
    """Anki's progress dialog without its Qt: only the cancel flag the buttons use."""

    def __init__(self):
        self.wantCancel = False


class RunControlsTests(DialogTestCase):
    def make_controls(self):
        return controls_module._RunControls(FakeButton("Pause"), FakeButton("Cancel"))

    def test_no_dialog_no_buttons(self):
        """The stubs' progress manager has no `_win`, like an Anki that renamed it."""
        self.assertFalse(hasattr(mw.progress, "_win"))

        controls_module.install_run_controls()
        controls_module.refresh_run_controls()
        controls_module.disable_run_controls()

    def test_a_dialog_without_the_expected_form_gets_no_buttons(self):
        for form in (None, object()):
            with self.subTest(form=form):
                win = FakeWindow()
                win.form = form  # type: ignore[attr-defined]
                mw.progress._win = win  # type: ignore[attr-defined]
                try:
                    controls_module.install_run_controls()
                    controls_module.refresh_run_controls()
                finally:
                    del mw.progress._win  # type: ignore[attr-defined]

                self.assertFalse(hasattr(win, controls_module._CONTROLS_ATTR))

    def test_the_toggle_pauses_and_resumes_the_run(self):
        win = FakeWindow()

        controls_module._on_toggle(win)
        self.assertEqual(api.pause_state(), api.PauseState("paused by user", None, False))

        controls_module._on_toggle(win)
        self.assertIsNone(api.pause_state())

    def test_the_toggle_says_what_it_will_do(self):
        win = FakeWindow()
        controls = self.make_controls()

        controls_module._refresh(win, controls)
        self.assertEqual(controls.toggle.text(), "Pause")

        api.pause_run("paused by user")
        controls_module._refresh(win, controls)
        self.assertEqual(controls.toggle.text(), "Resume")

        api.resume_run()
        api.pause_run("usage limit was reached: resets 5pm", time.time() + 600)
        controls_module._refresh(win, controls)
        self.assertEqual(controls.toggle.text(), "Resume now")

    def test_cancel_is_what_escape_does_and_greys_the_buttons(self):
        win = FakeWindow()
        controls = self.make_controls()
        setattr(win, controls_module._CONTROLS_ATTR, controls)

        controls_module._on_cancel(win)

        self.assertTrue(win.wantCancel)
        self.assertFalse(controls.toggle.enabled)
        self.assertFalse(controls.cancel.enabled)

    def test_escape_greys_the_buttons_at_the_next_refresh_and_they_stay_grey(self):
        win = FakeWindow()
        controls = self.make_controls()
        win.wantCancel = True

        controls_module._refresh(win, controls)
        win.wantCancel = False
        controls_module._refresh(win, controls)

        self.assertFalse(controls.toggle.enabled)
        self.assertFalse(controls.cancel.enabled)


if __name__ == "__main__":
    unittest.main()
