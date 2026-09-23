"""What the progress dialog shows about a paused run, and the buttons' logic.

The dialog runs on the main thread, which belongs to no run, while the updater's ticker runs
on the op thread's event loop, which does. These tests enrol the test thread in a run, as the
op thread is, and record the labels the updater draws by standing in for
`mw.progress.update`; the stubs' `run_on_main` runs every redraw at once.

The Qt half of progress_controls - building the buttons into Anki's dialog - needs a real
Anki and is not tested here. What is: that it keeps quiet without that dialog, and the
buttons' behaviour, which is plain Python over the run's pause.
"""

import threading
import time
import types
import unittest
from unittest import mock

from addon_modules import FakeClock, load_ops_module, mw

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

    def test_cleanup_greys_the_buttons_and_the_adding_re_arms_cancel_until_it_ends(self):
        """Cancelled or not: a finished run's adding can be stopped too, and only the adding."""
        disabled: list = []
        rearmed: list = []
        self.replace("disable_run_controls", lambda: disabled.append(True))
        self.replace("rearm_cleanup_cancel", lambda: rearmed.append(True) or True)
        updater = self.make_updater()

        updater.begin_cleanup()
        self.assertEqual((rearmed, disabled), ([], [True]))

        updater.arm_cleanup_cancel(total_notes=3)
        self.assertEqual((rearmed, disabled), ([True], [True]))

        updater.end_cleanup_cancel()
        self.assertEqual((rearmed, disabled), ([True], [True, True]))


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


class CleanupStageClockTests(DialogTestCase):
    """A cleanup stage's time, average and ETA are its own, not the API phase's before it."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.replace("time", self.clock)

    def after_an_hour_of_api_work(self):
        updater = self.make_updater(total_tasks=200)
        updater.set_total_notes(40)
        updater.increment_counts(tasks_done=200, notes_done=40, cumulative_task_time=3000.0)
        updater.paused_seconds = 600.0
        self.clock.advance(3600)
        updater.begin_cleanup()
        return updater

    def draw_adding(self, updater, notes_added: int, failed: int = 0) -> str:
        updater._last_update_at = 0.0
        updater.update_note_adding_progress(
            notes_added=notes_added, total_notes=10, failed=failed
        )
        return self.labels[-1]

    def test_the_adding_is_timed_from_the_start_of_its_stage(self):
        updater = self.after_an_hour_of_api_work()

        updater.begin_cleanup_stage()
        self.clock.advance(4)
        label = self.draw_adding(updater, 2)

        self.assertIn("Time: 00:00:04", label)
        self.assertIn("Avg time per note: 2.00s", label)
        # Eight notes left at two seconds each
        self.assertIn("ETA: 00:00:16", label)

    def test_a_failed_add_is_a_note_tried(self):
        updater = self.after_an_hour_of_api_work()

        updater.begin_cleanup_stage()
        self.clock.advance(6)
        label = self.draw_adding(updater, 2, failed=1)

        self.assertIn("Avg time per note: 2.00s", label)
        self.assertIn("ETA: 00:00:14", label)

    def test_the_new_note_processing_is_timed_from_the_start_of_its_stage(self):
        updater = self.after_an_hour_of_api_work()

        updater.begin_cleanup_stage()
        self.clock.advance(3)
        updater._last_update_at = 0.0
        updater.update_new_note_processing_progress(new_notes_processed=1, total_notes=4)

        self.assertIn("Time: 00:00:03", self.labels[-1])
        self.assertIn("ETA: 00:00:09", self.labels[-1])

    def test_the_unlinking_of_notes_not_added_has_its_own_label_and_clock(self):
        updater = self.after_an_hour_of_api_work()

        updater.begin_cleanup_stage()
        self.clock.advance(2)
        updater._last_update_at = 0.0
        updater.update_unadded_note_clearing_progress(notes_cleared=1, total_notes=5)

        self.assertIn("Unlinking notes not added", self.labels[-1])
        self.assertIn("1/5", self.labels[-1])
        self.assertIn("Time: 00:00:02", self.labels[-1])
        self.assertIn("ETA: 00:00:08", self.labels[-1])

    def test_a_stage_leaves_the_api_phase_counters_alone(self):
        """No cleanup label reads them; the paused time goes, as the stage cannot pause."""
        updater = self.after_an_hour_of_api_work()

        updater.begin_cleanup_stage()

        self.assertEqual(
            (updater.total_tasks, updater.tasks_done, updater.notes_done),
            (200, 200, 40),
        )
        self.assertEqual(updater.cumulative_task_time, 3000.0)
        self.assertEqual(updater.paused_seconds, 0.0)
        self.assertEqual(updater.start_time, self.clock.now)


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


class CleanupCancelTests(DialogTestCase):
    """The cleanup's own cancel: re-armed on the main thread, which the op thread waits for."""

    def reset_flag(self) -> bool:
        """rearm_cleanup_cancel over the stub's flag, which it resets"""
        mw.progress.cancel = False
        return True

    def test_the_op_thread_waits_for_the_main_thread_to_re_arm(self):
        """Reading the flag before the reset lands would take the first cancel for a second."""
        updater = self.make_updater()
        mw.progress.cancel = True
        landed: list = []

        def rearm():
            landed.append(time.monotonic())
            return self.reset_flag()

        self.replace("rearm_cleanup_cancel", rearm)

        def run_on_main_later(callback):
            threading.Timer(0.2, callback).start()

        with mock.patch.object(mw.taskman, "run_on_main", run_on_main_later):
            updater.arm_cleanup_cancel(total_notes=3)
            returned = time.monotonic()

        self.assertEqual(len(landed), 1)
        self.assertLessEqual(landed[0], returned)
        self.assertFalse(updater.cleanup_cancel_requested())
        mw.progress.cancel = True
        self.assertTrue(updater.cleanup_cancel_requested())

    def test_a_re_arm_that_never_lands_leaves_the_stale_flag_unheard(self):
        updater = self.make_updater()
        self.replace("CLEANUP_REARM_TIMEOUT", 0.05)
        # The cancel of the API work, which the main thread never gets to reset
        mw.progress.cancel = True

        with (
            mock.patch.object(mw.taskman, "run_on_main", lambda _callback: None),
            self.assertLogs(base_ops.logger, "WARNING") as logs,
        ):
            updater.arm_cleanup_cancel(total_notes=3)

        self.assertIn("did not re-arm the cancel", "\n".join(logs.output))
        self.assertFalse(updater.cleanup_cancel_requested())

    def test_the_start_of_the_cleanup_neither_re_arms_nor_waits(self):
        """Its first writes can be long, and a Cancel live through them would stop nothing;
        a run with no notes to add never gets further than this."""
        updater = self.make_updater()
        self.replace("CLEANUP_REARM_TIMEOUT", 0.5)
        rearmed: list = []
        self.replace("rearm_cleanup_cancel", lambda: rearmed.append(True) or True)
        queued: list = []

        with mock.patch.object(mw.taskman, "run_on_main", queued.append):
            began = time.monotonic()
            updater.begin_cleanup()
            waited = time.monotonic() - began
        for callback in queued:
            callback()

        self.assertEqual(rearmed, [])
        self.assertLess(waited, 0.25)

    def test_without_a_dialog_the_stale_flag_is_never_heard(self):
        """Nothing reset it: it may still hold the cancel of the API work, which would stop
        the adding before it began and unlink every note."""
        updater = self.make_updater()
        mw.progress.cancel = True

        with self.assertNoLogs(base_ops.logger, "WARNING"):
            updater.arm_cleanup_cancel(total_notes=3)

        self.assertFalse(updater.cleanup_cancel_requested())

    def test_the_cancel_is_heard_only_until_nothing_is_left_to_stop(self):
        updater = self.make_updater()
        self.replace("disable_run_controls", lambda: None)
        self.replace("rearm_cleanup_cancel", self.reset_flag)
        updater.arm_cleanup_cancel(total_notes=3)
        mw.progress.cancel = True
        self.assertTrue(updater.cleanup_cancel_requested())

        updater.end_cleanup_cancel()

        self.assertFalse(updater.cleanup_cancel_requested())

    def test_the_adding_says_cancel_stops_it_only_while_it_does(self):
        updater = self.make_updater()
        self.replace("disable_run_controls", lambda: None)
        self.replace("rearm_cleanup_cancel", self.reset_flag)
        updater.arm_cleanup_cancel(total_notes=3)

        updater._last_update_at = 0.0
        updater.update_note_adding_progress(notes_added=1, total_notes=3)
        self.assertIn("Cancel stops the adding", self.labels[-1])

        updater.end_cleanup_cancel()
        updater._last_update_at = 0.0
        updater.update_note_adding_progress(notes_added=2, total_notes=3)
        self.assertNotIn("Cancel stops", self.labels[-1])


class ReArmedControlsTests(DialogTestCase):
    """The buttons once the cleanup has re-armed the cancel: Cancel live, Pause grey."""

    def setUp(self):
        super().setUp()
        self.win = FakeWindow()
        # The designer form's layout; the buttons are already in it
        self.win.form = types.SimpleNamespace(verticalLayout=object())  # type: ignore[attr-defined]
        self.controls = controls_module._RunControls(FakeButton("Pause"), FakeButton("Cancel"))
        setattr(self.win, controls_module._CONTROLS_ATTR, self.controls)
        patches = (
            mock.patch.object(mw.progress, "_win", self.win, create=True),
            mock.patch.object(
                controls_module, "sip", types.SimpleNamespace(isdeleted=lambda _win: False)
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def assert_enabled(self, toggle: bool, cancel: bool) -> None:
        self.assertEqual(
            (self.controls.toggle.enabled, self.controls.cancel.enabled), (toggle, cancel)
        )

    def test_after_a_cancel_of_the_api_work_only_cancel_comes_back(self):
        controls_module._on_cancel(self.win)
        self.assert_enabled(False, False)

        controls_module.rearm_cleanup_cancel()

        self.assertFalse(self.win.wantCancel)
        self.assert_enabled(False, True)

    def test_a_redraw_keeps_cancel_live_and_pause_grey_even_while_paused(self):
        controls_module.rearm_cleanup_cancel()
        api.pause_run("paused by user")

        # What runs after every redraw
        controls_module.refresh_run_controls()

        self.assert_enabled(False, True)
        self.assertEqual(self.controls.toggle.text(), "Pause")

    def test_a_cancel_of_the_adding_greys_it_again_for_good(self):
        controls_module._on_cancel(self.win)
        controls_module.rearm_cleanup_cancel()

        controls_module._on_cancel(self.win)
        self.assertTrue(self.win.wantCancel)
        self.assert_enabled(False, False)

        controls_module.refresh_run_controls()
        self.assert_enabled(False, False)

    def test_escape_in_the_cleanup_greys_it_at_the_next_redraw(self):
        controls_module.rearm_cleanup_cancel()

        self.win.wantCancel = True
        controls_module.refresh_run_controls()

        self.assert_enabled(False, False)

    def test_the_end_of_what_a_cancel_stops_greys_it(self):
        controls_module.rearm_cleanup_cancel()

        controls_module.disable_run_controls()
        controls_module.refresh_run_controls()

        self.assert_enabled(False, False)
        self.assertFalse(self.win.wantCancel)

    def test_the_flag_is_reset_even_in_a_dialog_without_the_buttons(self):
        """Escape still cancels there, and the first cancel must not stop the adding."""
        del self.win.form  # type: ignore[attr-defined]
        delattr(self.win, controls_module._CONTROLS_ATTR)
        self.win.wantCancel = True

        self.assertTrue(controls_module.rearm_cleanup_cancel())

        self.assertFalse(self.win.wantCancel)

    def test_it_says_it_reset_the_flag_only_when_it_did(self):
        self.win.wantCancel = True
        self.assertTrue(controls_module.rearm_cleanup_cancel())

        with mock.patch.object(mw.progress, "_win", None):
            self.assertFalse(controls_module.rearm_cleanup_cancel())

        class DeletedWindow:
            """What a dialog Qt has deleted under us looks like to the flag's setter."""

            wantCancel = property(lambda _self: True)

            @wantCancel.setter
            def wantCancel(self, value):
                raise RuntimeError("wrapped C/C++ object has been deleted")

        with (
            mock.patch.object(mw.progress, "_win", DeletedWindow()),
            self.assertLogs(controls_module.logger, "ERROR"),
        ):
            self.assertFalse(controls_module.rearm_cleanup_cancel())

    def test_the_flag_reset_counts_even_if_the_buttons_then_fail(self):
        """Escape and the close box still set the flag, so the adding can still be stopped."""
        self.controls.cancel = None  # type: ignore[assignment]

        with self.assertLogs(controls_module.logger, "ERROR"):
            self.assertTrue(controls_module.rearm_cleanup_cancel())


class LateReArmTests(DialogTestCase):
    """A re-arm the main thread gets to only after the op thread stopped waiting for it, or
    after the adding it was for is over, does nothing (the real re-arm, over a fake dialog)."""

    def setUp(self):
        super().setUp()
        self.win = FakeWindow()
        self.win.form = types.SimpleNamespace(verticalLayout=object())  # type: ignore[attr-defined]
        self.controls = controls_module._RunControls(FakeButton("Pause"), FakeButton("Cancel"))
        setattr(self.win, controls_module._CONTROLS_ATTR, self.controls)
        patches = (
            mock.patch.object(mw.progress, "_win", self.win, create=True),
            mock.patch.object(mw.progress, "want_cancel", lambda: self.win.wantCancel),
            mock.patch.object(
                controls_module, "sip", types.SimpleNamespace(isdeleted=lambda _win: False)
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.replace("CLEANUP_REARM_TIMEOUT", 0.05)
        self.updater = self.make_updater()
        # What the main thread is to run, held back until the test runs it
        self.queued: list = []

    def arm_without_the_main_thread(self) -> None:
        with (
            mock.patch.object(mw.taskman, "run_on_main", self.queued.append),
            self.assertLogs(base_ops.logger, "WARNING"),
        ):
            self.updater.arm_cleanup_cancel(total_notes=3)

    def run_main_thread(self) -> None:
        queued, self.queued[:] = list(self.queued), []
        for callback in queued:
            callback()

    def test_after_the_wait_gave_up_it_keeps_a_cancel_pressed_meanwhile(self):
        self.arm_without_the_main_thread()
        self.win.wantCancel = True

        self.run_main_thread()

        self.assertTrue(self.win.wantCancel)
        self.assertFalse(self.updater._cleanup_cancel_armed.is_set())
        self.assertFalse(self.controls.cleanup)
        # A late label would draw over the adding's own
        self.assertFalse([label for label in self.labels if "Adding notes" in label])

    def test_after_the_adding_ended_it_neither_arms_nor_brings_cancel_back(self):
        self.arm_without_the_main_thread()
        self.updater.end_cleanup_cancel()

        self.run_main_thread()

        self.assertFalse(self.updater._cleanup_cancel_armed.is_set())
        self.assertFalse(self.updater.cleanup_cancel_requested())
        self.assertEqual((self.controls.cleanup, self.controls.cancel.enabled), (False, False))

    def test_one_landing_as_the_wait_gives_up_counts(self):
        """The give-up and the main thread's check-and-reset are one or the other, never half
        of each: here the re-arm lands between the wait's timeout and the give-up."""
        run_on_main = self.queued.append
        real_event = threading.Event

        class LandsAsTheWaitEnds(real_event):
            def wait(event_self, timeout=None):
                for callback in self.queued:
                    callback()
                return False

        self.win.wantCancel = True
        with (
            mock.patch.object(mw.taskman, "run_on_main", run_on_main),
            mock.patch.object(base_ops.threading, "Event", LandsAsTheWaitEnds),
            self.assertNoLogs(base_ops.logger, "WARNING"),
        ):
            self.updater.arm_cleanup_cancel(total_notes=3)

        self.assertFalse(self.win.wantCancel)
        self.assertFalse(self.updater.cleanup_cancel_requested())
        self.win.wantCancel = True
        self.assertTrue(self.updater.cleanup_cancel_requested())

    def test_the_label_is_the_addings_as_cancel_comes_back(self):
        """Not "Cancelling operations... Finishing up" with a Cancel that stops the adding."""
        self.updater.show_cancelling()
        self.win.wantCancel = True
        self.updater.begin_cleanup()

        self.updater.arm_cleanup_cancel(total_notes=3)

        self.assertFalse(self.win.wantCancel)
        self.assertEqual((self.controls.cleanup, self.controls.cancel.enabled), (True, True))
        self.assertIn("Adding notes", self.labels[-1])
        self.assertIn("0/3", self.labels[-1])
        self.assertIn("Cancel stops the adding", self.labels[-1])

    def test_without_the_flag_reset_the_label_does_not_promise_a_cancel(self):
        with mock.patch.object(mw.progress, "_win", None):
            self.updater.arm_cleanup_cancel(total_notes=3)

        self.assertIn("Adding notes", self.labels[-1])
        self.assertNotIn("Cancel stops", self.labels[-1])


if __name__ == "__main__":
    unittest.main()
