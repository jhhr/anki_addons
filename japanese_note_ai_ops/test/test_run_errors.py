"""The errors a run meets without failing: reported from the thread that met them, named after
the note and the chain step, grouped, and kept for the run's end message.

The pane that shows them is tested on real Qt in test_progress_errors; here `run_errors` runs on
its own and the reports are caught where `progress_errors` would take them (`deliver_with`).
"""

from __future__ import annotations

import asyncio
import threading
import types
import unittest
from unittest import mock

from addon_modules import load_ops_module, mw

run_errors = load_ops_module("run_errors")
base_ops = load_ops_module("base_ops")
api = load_ops_module("api_client")
progress_errors = load_ops_module("progress_errors")


class CatchReports(unittest.TestCase):
    """Takes every report as `progress_errors` would, keeping it as the pane does."""

    def setUp(self):
        self.reports: list = []
        saved = run_errors._deliver
        run_errors.deliver_with(self.deliver)
        self.addCleanup(run_errors.deliver_with, saved)
        self.addCleanup(run_errors.take_run)

    def deliver(self, title, text):
        self.reports.append((title, text, threading.current_thread().name))
        run_errors.record(title, text)


class FakeNoteType(dict):
    pass


class FakeNote:
    def __init__(self, note_id, sort_field=None):
        self.id = note_id
        self.fields = [sort_field or "", "other"]
        self._has_type = sort_field is not None

    def note_type(self):
        if not self._has_type:
            raise RuntimeError("no collection here")
        return FakeNoteType(sortf=0)


def raised(error):
    try:
        raise error
    except Exception as e:
        return e


class ErrorListTests(unittest.TestCase):
    def test_the_same_text_is_counted_with_the_first_and_names_where_else(self):
        errors = run_errors.ErrorList()
        first = errors.add("Note 1", "HTTP 401: bad key")
        for n in range(2, 1001):
            self.assertIsNone(errors.add(f"Note {n}", "HTTP 401: bad key\n"))

        self.assertEqual(errors.entries, [first])
        self.assertEqual((errors.total, first.count), (1000, 1000))
        self.assertEqual(len(first.places), run_errors.MAX_PLACES)
        self.assertTrue(first.again_line().startswith("Also at 999 more: Note 2, Note 3"))
        self.assertTrue(first.again_line().endswith(", …"))
        self.assertEqual(errors.header(), "1000 errors")

    def test_past_the_cap_different_errors_are_only_counted(self):
        errors = run_errors.ErrorList(max_kinds=3)
        for n in range(5):
            errors.add(f"Note {n}", f"error {n}")
        errors.add("Note 9", "error 1")

        self.assertEqual([e.text for e in errors.entries], ["error 0", "error 1", "error 2"])
        self.assertEqual((errors.total, errors.not_listed), (6, 2))
        self.assertIn("2 more errors not listed here", errors.as_text())
        self.assertIn("2 more errors not listed here", errors.as_html())

    def test_the_text_lists_every_error_with_its_traceback(self):
        errors = run_errors.ErrorList()
        errors.add("Step 1/2: Kana · Note 5", "ValueError: boom\n\nTraceback (most recent call)")
        errors.add("Step 2/2: Meanings", "KeyError: 'x'")
        errors.add("Step 2/2: Meanings · Note 6", "KeyError: 'x'")

        text = errors.as_text()
        self.assertTrue(text.startswith("3 errors during the run."), text)
        self.assertIn("Step 1/2: Kana · Note 5\n\nValueError: boom\n\nTraceback", text)
        self.assertIn("Step 2/2: Meanings\nAlso at 1 more: Step 2/2: Meanings · Note 6", text)

    def test_the_html_is_escaped(self):
        errors = run_errors.ErrorList()
        errors.add("<b>t</b>", "a < b")
        self.assertIn("&lt;b&gt;t&lt;/b&gt;", errors.as_html())
        self.assertIn("a &lt; b", errors.as_html())


class RunScopeTests(CatchReports):
    def test_a_run_without_errors_has_none_to_take(self):
        run_errors.start_run()
        self.assertIsNone(run_errors.take_run())

    def test_taking_them_clears_them(self):
        run_errors.start_run()
        run_errors.record("Note 1", "boom")

        errors = run_errors.take_run()

        self.assertEqual([e.title for e in errors.entries], ["Note 1"])
        self.assertIsNone(run_errors.take_run())

    def test_a_report_after_the_end_is_not_kept_for_the_next_run(self):
        """A thread a cancelled run abandoned can still report once the run has ended."""
        run_errors.start_run()
        run_errors.take_run()
        run_errors.record("Note 1", "late")

        run_errors.start_run()
        self.assertIsNone(run_errors.take_run())

    def test_a_new_run_starts_clean_even_if_the_last_was_never_taken(self):
        run_errors.start_run()
        run_errors.record("Note 1", "from a run that raised before its end message")

        run_errors.start_run()
        self.assertIsNone(run_errors.take_run())

    def test_nothing_is_kept_outside_a_run(self):
        run_errors.record("Note 1", "boom")
        self.assertIsNone(run_errors.take_run())


class TitleTests(CatchReports):
    def test_the_step_the_note_and_where(self):
        run_errors.start_run()
        run_errors.set_step("Step 2/4: Make meanings")
        with run_errors.error_subject(run_errors.NoteSubject(FakeNote(12, "<b>食べる</b>&amp;"))):
            run_errors.report_error("boom", where="Saving")

        self.assertEqual(
            self.reports[0][:2], ("Step 2/4: Make meanings · Note 12 (食べる &) · Saving", "boom")
        )

    def test_a_run_from_the_menu_has_no_step(self):
        run_errors.start_run()
        with run_errors.error_subject(run_errors.NoteSubject(FakeNote(12))):
            run_errors.report_error("boom")
        self.assertEqual(self.reports[0][0], "Note 12")

    def test_the_sort_field_is_shortened_and_a_new_note_has_no_id(self):
        long_text = "あ" * 40
        self.assertEqual(
            str(run_errors.NoteSubject(FakeNote(0, long_text))), f"New note ({'あ' * 30}…)"
        )

    def test_the_subject_follows_a_task_into_its_worker_thread(self):
        async def main():
            def in_thread():
                run_errors.report_error("from the thread")

            async def task():
                await asyncio.to_thread(in_thread)

            with run_errors.error_subject(run_errors.NoteSubject(FakeNote(7))):
                created = asyncio.create_task(task())
            # Out of the block before the task has even started
            await created

        asyncio.run(main())

        title, text, thread = self.reports[0]
        self.assertEqual((title, text), ("Note 7", "from the thread"))
        self.assertNotEqual(thread, threading.main_thread().name)

    def test_a_broken_delivery_never_reaches_the_caller(self):
        run_errors.deliver_with(lambda title, text: 1 / 0)
        with self.assertLogs(run_errors.logger, "ERROR"):
            run_errors.report_error("boom")


class ReportExceptionTests(CatchReports):
    def test_the_message_and_the_traceback(self):
        progress_errors.report_exception(raised(ValueError("boom")), "Adding the note")
        text = self.reports[0][1]
        self.assertTrue(text.startswith("Adding the note: ValueError: boom\n\nTraceback"), text)
        self.assertIn("in raised", text)

    def test_interrupted_is_not_reported(self):
        class Interrupted(Exception):
            pass

        saved = progress_errors.Interrupted
        progress_errors.Interrupted = Interrupted
        self.addCleanup(setattr, progress_errors, "Interrupted", saved)

        progress_errors.report_exception(Interrupted())

        self.assertEqual(self.reports, [])

    def test_a_task_abandoned_on_cancel_is_not_reported(self):
        progress_errors.report_exception(progress_errors.RunCancelled("the run was cancelled"))
        self.assertEqual(self.reports, [])


class FakeGate:
    async def acquire(self):
        pass

    def release(self):
        pass


class FakeUpdater:
    def increment_counts(self, **_):
        pass

    def update_progress(self):
        pass


class BaseOpsReportTests(CatchReports):
    def setUp(self):
        super().setUp()
        mw.progress.cancel = False
        self.addCleanup(setattr, mw.progress, "cancel", False)

    def test_a_task_that_raises_is_reported_with_its_note_and_the_run_goes_on(self):
        def op(config, note, notes_to_add_dict, notes_to_update_dict):
            base_ops.report_error("a provider refused it")
            raise ValueError(f"bad note {note.id}")

        handled: list = []
        process = base_ops.make_inner_bulk_op(
            config={},
            op=op,
            gate=FakeGate(),
            progress_updater=FakeUpdater(),
            handle_op_error=handled.append,
            handle_op_result=lambda _: None,
        )

        async def main():
            tasks = []
            for nid in (1, 2):
                with run_errors.error_subject(run_errors.NoteSubject(FakeNote(nid))):
                    tasks.append(asyncio.create_task(process({}, {}, note=FakeNote(nid))))
            return await asyncio.gather(*tasks)

        self.assertEqual(asyncio.run(main()), [False, False])

        self.assertEqual(len(handled), 2)
        titles = [(title, text.split("\n")[0]) for title, text, _ in self.reports]
        self.assertIn(("Note 1", "a provider refused it"), titles)
        self.assertIn(("Note 1", "ValueError: bad note 1"), titles)
        self.assertIn(("Note 2", "ValueError: bad note 2"), titles)

    def test_the_nested_ops_plans_spawn_with_their_note(self):
        seen: list = []

        def spawn(tasks):
            seen.append(run_errors.error_title())

        base_ops._spawning_for(FakeNote(3), spawn)([])
        self.assertEqual(seen, ["Note 3"])

    def test_a_sync_ops_note_that_raises_is_reported_and_the_rest_run(self):
        saved_update = mw.progress.update
        mw.progress.update = lambda **_: None
        self.addCleanup(setattr, mw.progress, "update", saved_update)
        api.begin_run()
        self.addCleanup(api.end_run)
        done: list = []

        def op(config, note, notes_to_add_dict, notes_to_update_dict):
            if note.id == 2:
                raise KeyError("field")
            done.append(note.id)

        base_ops.sync_bulk_notes_op(
            0, None, {}, op, [FakeNote(1), FakeNote(2), FakeNote(3)], [], "Doing",
            notes_to_add_dict={}, notes_to_update_dict={},
        )

        self.assertEqual(done, [1, 3])
        [(title, text, _)] = self.reports
        self.assertEqual(title, "Note 2")
        self.assertTrue(text.startswith("KeyError: 'field'\n\nTraceback"), text)

    def test_a_request_that_got_no_answer_is_reported_but_not_a_cancelled_one(self):
        base_ops.post_with_retry = lambda **_: None
        self.addCleanup(setattr, base_ops, "post_with_retry", api.post_with_retry)
        base_ops.post_to_api("anthropic", "claude-x", "url", {}, {}, {}, base_ops.CancelState())
        cancelled = base_ops.CancelState()
        cancelled.cancel()
        base_ops.post_to_api("anthropic", "claude-x", "url", {}, {}, {}, cancelled)

        self.assertEqual(len(self.reports), 1)
        self.assertTrue(self.reports[0][1].startswith("claude-x: no answer"), self.reports)

    def test_a_refused_request_and_an_unreadable_answer(self):
        response = types.SimpleNamespace(status_code=401, text="x" * 2000)
        base_ops.report_refused("gpt-x", response)
        base_ops.report_unreadable("gpt-x", KeyError("choices"), response)

        refused, unreadable = [text for _, text, _ in self.reports]
        self.assertTrue(refused.startswith("gpt-x: HTTP 401: xxx"))
        # A provider's error page is cut short; the log has it whole
        self.assertLess(len(refused), 600)
        self.assertTrue(unreadable.startswith("gpt-x: could not read the answer (KeyError"))

    def test_an_answer_that_is_not_json_is_reported_unless_the_corrector_fixes_it(self):
        self.assertEqual(base_ops.decode_answer("m", '{"a": 1,}', lambda s: '{"a": 1}'), {"a": 1})
        self.assertEqual(self.reports, [])

        self.assertIsNone(base_ops.decode_answer("m", "no json", None))
        self.assertEqual(self.reports[0][1], "m: the answer was not valid JSON: no json")


class MenuRunEndTests(CatchReports):
    """A run from the menu: its end message lists its errors; with none it is a tooltip."""

    def setUp(self):
        super().setUp()
        self.tooltips: list = []
        self.ends: list = []
        for name, fake in (
            ("tooltip", lambda message, **_: self.tooltips.append(message)),
            ("show_run_end", lambda text, parent, errors: self.ends.append((text, errors))),
            ("showWarning", lambda *a, **k: self.fail("the errors' box is the warning")),
        ):
            patcher = mock.patch.object(base_ops, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def succeed(self):
        base_ops.on_bulk_success(None, "Made meanings", [1], [], [1, 2], None)

    def test_a_run_without_errors_ends_as_before(self):
        run_errors.start_run()
        self.succeed()
        self.assertEqual(len(self.tooltips), 1)
        self.assertEqual(self.ends, [])

    def test_a_run_with_errors_lists_them_at_the_end(self):
        run_errors.start_run()
        with run_errors.error_subject(run_errors.NoteSubject(FakeNote(2))):
            progress_errors.report_exception(raised(ValueError("boom")))

        self.succeed()

        self.assertEqual(self.tooltips, [])
        [(text, errors)] = self.ends
        self.assertIn("Made meanings", text)
        self.assertIn("Note 2\n\nValueError: boom\n\nTraceback", errors.as_text())

    def test_the_next_run_does_not_see_them(self):
        run_errors.start_run()
        run_errors.report_error("boom")
        self.succeed()
        run_errors.start_run()
        self.succeed()
        self.assertEqual((len(self.ends), len(self.tooltips)), (1, 1))

    def test_a_chain_step_leaves_them_for_the_chain(self):
        run_errors.start_run()
        run_errors.set_step("Step 1/2: Op a")
        run_errors.report_error("boom")
        step = mock.Mock(title="Step 1/2: Op a")

        base_ops.on_bulk_success(None, "Made meanings", [1], [], [1, 2], None, chain=step)

        step.on_done.assert_called_once()
        self.assertEqual(self.ends, [])
        self.assertEqual(run_errors.take_run().entries[0].title, "Step 1/2: Op a")


class ChainSummaryTests(CatchReports):
    """The chain's summary: its steps' errors, a failed step's traceback among them, are listed
    from it; with none it is the box it always was."""

    def setUp(self):
        super().setUp()
        self.op_chain = load_ops_module("op_chain")
        self.hooks: dict = {}

        class FakeOpChain:
            def __init__(chain, *args, **hooks):
                self.hooks.update(hooks)

            def start(chain, check):
                pass

        self.boxes: list = []
        patches = [
            mock.patch.object(self.op_chain, "OpChain", FakeOpChain),
            mock.patch.object(mw.progress, "single_shot", lambda ms, f, *a: f(), create=True),
            mock.patch.object(mw.progress, "start", lambda **_: None, create=True),
            mock.patch.object(
                self.op_chain,
                "show_run_end",
                lambda text, parent, errors, **_: self.boxes.append(("errors", errors)),
            ),
            mock.patch.object(
                self.op_chain, "showInfo", lambda text, **_: self.boxes.append(("info", text))
            ),
            mock.patch.object(
                self.op_chain, "showWarning", lambda text, **_: self.boxes.append(("warn", text))
            ),
            mock.patch.object(self.op_chain, "install_idle_run_controls", lambda: None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.op_chain.run_op_chain([], [1], None)

    def test_a_chain_without_errors_shows_its_summary_as_before(self):
        self.hooks["hold_progress"]()
        self.hooks["show_summary"]("All done", False)
        self.assertEqual(self.boxes, [("info", "All done")])

    def test_a_failed_steps_traceback_is_listed_from_the_summary(self):
        self.hooks["hold_progress"]()
        # What failed_step_outcome puts in the pane, which keeps it for the run
        self.deliver("Step 2/2: Op b", "ValueError: boom\n\nTraceback (most recent call last)")

        self.hooks["show_summary"]("Stopped at step 2", True)

        [(kind, errors)] = self.boxes
        self.assertEqual(kind, "errors")
        self.assertIn("Step 2/2: Op b\n\nValueError: boom\n\nTraceback", errors.as_text())
        # Taken: a chain run next starts without them
        self.hooks["hold_progress"]()
        self.hooks["show_summary"]("All done", False)
        self.assertEqual(self.boxes[-1], ("info", "All done"))


if __name__ == "__main__":
    unittest.main()
