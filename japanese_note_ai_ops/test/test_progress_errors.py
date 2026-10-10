"""A run's errors go into a pane beside the progress, in Anki's own progress dialog.

With no error the dialog keeps its size; the first one widens it and puts a list of errors on
the right, the progress and its Pause/Cancel buttons on the left.

Qt layout and sizing, so like `test_cancel_keys` these load the modules a second time with
`aqt.qt` standing on PyQt6, and build the dialog from Anki's own designer form when the
installed aqt has it. Without PyQt6 they skip.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

from addon_modules import PACKAGE, load_ops_module, mw

try:
    from PyQt6 import QtCore, QtGui, QtWidgets, sip

    HAVE_QT = True
except ImportError:  # pragma: no cover - the dev requirements install it with aqt
    HAVE_QT = False


def load_with_real_qt() -> tuple[ModuleType, ModuleType]:
    """progress_errors and the progress_controls it imports, both on real Qt."""
    qt = ModuleType("aqt.qt")
    for source in (QtCore, QtGui, QtWidgets):
        qt.__dict__.update({k: v for k, v in vars(source).items() if not k.startswith("_")})
    qt.qconnect = lambda signal, slot: signal.connect(slot)  # type: ignore[attr-defined]
    qt.sip = sip  # type: ignore[attr-defined]
    controls_name = f"{PACKAGE}.async_api_ops.progress_controls"
    names = ["aqt.qt", controls_name, f"{PACKAGE}.async_api_ops.progress_errors"]
    saved = {name: sys.modules.pop(name, None) for name in names}
    sys.modules["aqt.qt"] = qt
    run_errors = load_ops_module("run_errors")
    deliver = run_errors._deliver
    try:
        errors = load_ops_module("progress_errors")
        return errors, sys.modules[controls_name]
    finally:
        # Loading it pointed the reports at this copy; the rest of the suite reports to the other
        run_errors.deliver_with(deliver)
        # The rest of the suite goes on with the stubbed copies
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def anki_form():
    """Anki's designer form for the progress dialog, or a copy of it (as of Anki 26.09)."""
    try:
        from _aqt.forms.progress_qt6 import Ui_Dialog

        return Ui_Dialog()
    except ImportError:
        return None


if HAVE_QT:

    class FakeProgressDialog(QtWidgets.QDialog):
        """Anki's ProgressDialog as ProgressManager.start builds it, without aqt."""

        def __init__(self):
            super().__init__()
            self.wantCancel = False
            form = anki_form()
            if form is not None:
                form.setupUi(self)
            else:
                layout = QtWidgets.QVBoxLayout(self)
                layout.setContentsMargins(6, 6, 6, 6)
                label = QtWidgets.QLabel(self)
                bar = QtWidgets.QProgressBar(self)
                layout.addWidget(label)
                layout.addWidget(bar)
                self.resize(310, 69)
                form = SimpleNamespace(verticalLayout=layout, label=label, progressBar=bar)
            self.form = form
            self.form.label.setText("Processing notes 3/10")
            self.setMinimumWidth(300)

        def closeEvent(self, evt):
            self.wantCancel = True
            evt.ignore()

        def keyPressEvent(self, evt):
            if evt.key() == QtCore.Qt.Key.Key_Escape:
                evt.ignore()
                self.wantCancel = True


@unittest.skipUnless(HAVE_QT, "needs PyQt6")
class ErrorPaneTests(unittest.TestCase):
    errors_module: ModuleType
    controls_module: ModuleType

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.errors_module, cls.controls_module = load_with_real_qt()

    def setUp(self):
        self.win = self.open_dialog()
        p = mock.patch.object(self.controls_module, "pause_state", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def open_dialog(self):
        """A dialog as Anki opens it for a run, the buttons in, shown and laid out."""
        win = FakeProgressDialog()
        self.addCleanup(lambda: sip.isdeleted(win) or win.deleteLater())
        patcher = mock.patch.object(mw.progress, "_win", win, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controls_module.start_run_controls()
        win.show()
        self.settle()
        return win

    def settle(self):
        QtWidgets.QApplication.processEvents()

    def report(self, title="Step 2/4: Make meanings", text="Traceback:\n  boom"):
        shown = self.errors_module.report_run_error(title, text)
        self.settle()
        return shown

    def pane(self, win=None):
        return getattr(win or self.win, self.errors_module._PANE_ATTR, None)

    def redraw_now(self, pane):
        """The redraw the pane has due, now. The timer is stopped rather than left to fire: the
        test's parentless dialog is freed by Python's cycle collector, which could come round
        inside that timer's own timeout in a later test, and did (a segfault)."""
        self.assertTrue(pane.redraw.isActive(), "a repeat puts off the redraw")
        pane.redraw.stop()
        self.errors_module._redraw(pane)
        self.settle()

    def controls(self):
        return getattr(self.win, self.controls_module._CONTROLS_ATTR)

    def test_without_an_error_the_dialog_is_as_anki_made_it(self):
        size = self.win.size()
        self.controls_module.refresh_run_controls()
        self.settle()

        self.assertEqual(self.win.size(), size)
        self.assertIsNone(self.pane())
        self.assertFalse(hasattr(self.win, self.controls_module.PROGRESS_COLUMN_ATTR))

    def test_the_first_error_widens_the_dialog_and_lists_it_on_the_right(self):
        before = self.win.size()

        self.assertTrue(self.report())

        pane = self.pane()
        self.assertIsNotNone(pane)
        self.assertTrue(pane.pane.isVisible())
        self.assertGreaterEqual(self.win.width(), before.width() * 2 - 1)
        self.assertGreaterEqual(self.win.height(), before.height())
        text = pane.browser.toPlainText()
        self.assertIn("Step 2/4: Make meanings", text)
        self.assertIn("Traceback:\n  boom", text)
        self.assertEqual(pane.header.text(), "1 error")
        # Progress on the left half, errors on the right
        bar = self.win.form.progressBar
        bar_right = bar.mapTo(self.win, QtCore.QPoint(bar.width(), 0)).x()
        pane_left = pane.pane.mapTo(self.win, QtCore.QPoint(0, 0)).x()
        self.assertLessEqual(bar_right, pane_left)
        self.assertLess(abs(pane_left - self.win.width() // 2), 20)

    def test_a_second_error_is_added_below_without_widening_again(self):
        self.report("Step 1/2: Kana", "first")
        size = self.win.size()

        self.assertTrue(self.report("Step 2/2: Meanings", "second"))

        self.assertEqual(self.win.size(), size)
        pane = self.pane()
        text = pane.browser.toPlainText()
        self.assertLess(text.index("first"), text.index("Step 2/2: Meanings"))
        self.assertIn("second", text)
        self.assertEqual(pane.header.text(), "2 errors")

    def test_the_text_is_shown_as_written_not_as_html(self):
        self.report("<b>title</b>", "a < b & <i>c</i>")

        text = self.pane().browser.toPlainText()
        self.assertIn("<b>title</b>", text)
        self.assertIn("a < b & <i>c</i>", text)

    def test_the_buttons_stay_in_the_progress_column_and_still_work(self):
        self.report()
        controls = self.controls()
        column = getattr(self.win, self.controls_module.PROGRESS_COLUMN_ATTR)

        self.assertIs(controls.cancel.parentWidget(), column.parentWidget())
        self.assertTrue(controls.cancel.isVisible())
        with mock.patch.object(self.controls_module, "pause_run") as pause_run:
            controls.toggle.click()
        pause_run.assert_called_once()
        controls.cancel.click()
        self.assertTrue(self.win.wantCancel)

    def test_buttons_built_after_the_pane_go_into_the_progress_column(self):
        """A chain opens its dialog before step 1; were its buttons missing when an error came,
        they would still be built beside the bar, not under both halves."""
        win = FakeProgressDialog()
        self.addCleanup(win.deleteLater)
        with mock.patch.object(mw.progress, "_win", win, create=True):
            self.errors_module.report_run_error("Chain", "failed")
            self.controls_module.start_run_controls()
            column = getattr(win, self.controls_module.PROGRESS_COLUMN_ATTR)
            controls = getattr(win, self.controls_module._CONTROLS_ATTR)

        self.assertIs(controls.cancel.parentWidget(), column.parentWidget())

    def test_the_pane_stays_across_the_steps_of_a_chain(self):
        self.report("Step 1/2: Kana", "first")
        self.controls_module.disable_run_controls()
        # The next step reuses the dialog
        self.controls_module.start_run_controls()
        self.report("Step 2/2: Meanings", "second")

        self.assertEqual(self.pane().errors.total, 2)
        self.assertTrue(self.controls().cancel.isEnabled())

    def test_a_new_dialog_starts_without_the_pane(self):
        self.report()
        self.win.hide()

        win = self.open_dialog()

        self.assertIsNone(self.pane(win))
        self.assertLess(win.width(), self.win.width())

    def test_escape_in_the_pane_cancels_as_it_does_in_the_dialog(self):
        self.report()
        browser = self.pane().browser
        escape = QtGui.QKeyEvent(
            QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Escape, QtCore.Qt.KeyboardModifier(0)
        )

        self.controls_module.disable_run_controls()
        QtWidgets.QApplication.sendEvent(browser, escape)
        self.assertFalse(self.win.wantCancel, "Cancel is greyed, so Escape is dropped")

        self.controls_module.start_run_controls()
        QtWidgets.QApplication.sendEvent(browser, escape)
        self.assertTrue(self.win.wantCancel)

    def test_the_pane_never_takes_the_keyboard_on_its_own(self):
        self.report()

        self.assertFalse(
            self.pane().browser.focusPolicy() & QtCore.Qt.FocusPolicy.TabFocus,
        )

    def test_the_widening_stops_at_the_screen(self):
        screen = self.win.screen().availableGeometry()
        self.win.resize(screen.width() * 3 // 4, self.win.height())
        self.settle()

        self.report()

        self.assertLessEqual(self.win.width(), screen.width())
        frame = self.win.frameGeometry()
        self.assertGreaterEqual(frame.left(), screen.left())
        self.assertLessEqual(frame.right(), screen.right())

    def test_no_dialog_is_not_shown(self):
        with mock.patch.object(mw.progress, "_win", None):
            self.assertFalse(self.errors_module.report_run_error("t", "x"))

    def test_a_deleted_dialog_is_not_shown(self):
        win = FakeProgressDialog()
        sip.delete(win)
        with mock.patch.object(mw.progress, "_win", win):
            self.assertFalse(self.errors_module.report_run_error("t", "x"))

    def test_off_the_main_thread_nothing_is_touched(self):
        results = []
        worker = threading.Thread(
            target=lambda: results.append(self.errors_module.report_run_error("t", "x"))
        )
        with self.assertLogs(self.errors_module.logger, "ERROR"):
            worker.start()
            worker.join()

        self.assertEqual(results, [False])
        self.assertIsNone(self.pane())

    def test_a_worker_threads_report_is_shown_once_it_reaches_the_main_thread(self):
        queued: list = []
        with mock.patch.object(mw.taskman, "run_on_main", queued.append):
            worker = threading.Thread(
                target=self.errors_module.report_run_error_from_any_thread, args=("Note 5", "boom")
            )
            worker.start()
            worker.join()

        self.assertIsNone(self.pane(), "nothing is touched on the worker thread")
        self.assertEqual(len(queued), 1)
        queued[0]()
        self.settle()
        self.assertIn("Note 5", self.pane().browser.toPlainText())

    def test_on_the_main_thread_it_is_shown_at_once(self):
        self.errors_module.report_run_error_from_any_thread("Note 5", "boom")
        self.assertIn("boom", self.pane().browser.toPlainText())

    def test_one_error_for_every_note_is_listed_once_with_a_count(self):
        for n in range(1, 1001):
            self.report(f"Note {n}", "HTTP 401: invalid key")
        pane = self.pane()
        self.redraw_now(pane)

        text = pane.browser.toPlainText()
        self.assertEqual(text.count("HTTP 401: invalid key"), 1)
        self.assertIn("Also at 999 more: Note 2, Note 3", text)
        self.assertEqual(pane.header.text(), "1000 errors")

    def test_a_new_error_while_a_redraw_is_due_is_listed_once(self):
        self.report("Note 1", "first")
        self.report("Note 2", "first")
        self.report("Note 3", "second")
        pane = self.pane()
        # Listed by the redraw, not appended while one is due, or it would be there twice
        self.assertNotIn("second", pane.browser.toPlainText())
        self.redraw_now(pane)

        text = pane.browser.toPlainText()
        self.assertEqual((text.count("first"), text.count("second")), (1, 1))

    def test_what_the_pane_shows_is_kept_for_the_run(self):
        run_errors = load_ops_module("run_errors")
        run_errors.start_run()
        self.addCleanup(run_errors.take_run)
        self.report("Step 1/2: Kana", "ValueError: boom\n\nTraceback ...")
        with mock.patch.object(mw.progress, "_win", None):
            # Not shown, so not kept: whoever reported it showed it its own way
            self.errors_module.report_run_error("Step 2/2", "not shown")

        errors = run_errors.take_run()
        self.assertEqual([e.title for e in errors.entries], ["Step 1/2: Kana"])
        self.assertIsNone(run_errors.take_run(), "taken once, gone for the next run")


@unittest.skipUnless(HAVE_QT, "needs PyQt6")
class RunEndTests(unittest.TestCase):
    """A run that met errors ends with one box saying so, whose button lists them all."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.errors_module, _ = load_with_real_qt()

    def errors(self):
        errors = load_ops_module("run_errors").ErrorList()
        errors.add("Step 1/2: Kana · Note 5", "ValueError: boom\n\nTraceback (most recent call)")
        errors.add("Step 2/2: Meanings", "failed")
        return errors

    def test_the_box_says_how_many_and_offers_the_list(self):
        box, show = self.errors_module.end_message_box(
            "Done: 3 notes", None, self.errors(), "AI ops", warning=True
        )
        self.addCleanup(box.deleteLater)

        self.assertIn("Done: 3 notes", box.text())
        self.assertIn("2 errors", box.text())
        self.assertEqual(show.text(), "Show errors")
        self.assertIsNot(box.defaultButton(), show)

    def run_end(self, clicked_show):
        shown = mock.Mock()

        class FakeBox:
            def exec(self):
                pass

            def clickedButton(self):
                return show if clicked_show else None

        show = object()
        with mock.patch.object(
            self.errors_module, "end_message_box", return_value=(FakeBox(), show)
        ), mock.patch.object(self.errors_module, "showText", shown):
            self.errors_module.show_run_end("Done", None, self.errors(), title="AI ops")
        return shown

    def test_show_errors_lists_them_with_the_traceback(self):
        shown = self.run_end(clicked_show=True)

        text = shown.call_args.args[0]
        self.assertIn("Step 1/2: Kana · Note 5\n\nValueError: boom\n\nTraceback", text)
        self.assertIn("Step 2/2: Meanings", text)

    def test_ok_lists_nothing(self):
        self.run_end(clicked_show=False).assert_not_called()


if __name__ == "__main__":
    unittest.main()
