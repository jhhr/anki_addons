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
    try:
        errors = load_ops_module("progress_errors")
        return errors, sys.modules[controls_name]
    finally:
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

        self.assertEqual(self.pane().count, 2)
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


if __name__ == "__main__":
    unittest.main()
