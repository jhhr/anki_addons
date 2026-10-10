"""Escape and the close box cancel a run only while its Cancel button is enabled.

Anki's progress dialog sets its cancel flag on either. While the cleanup writes what cannot be
stopped the buttons are grey, but a press there still set the flag, and the finished run then
read as cancelled: a chain stopped at a step that had saved everything.

The filter is Qt event handling, so these build a real dialog shaped like Anki's
`ProgressDialog` and load `progress_controls` a second time with `aqt.qt` standing on PyQt6,
as `test_multi_op_dialog` does for the dialog. Without PyQt6 they skip.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from types import ModuleType
from unittest import mock

from addon_modules import PACKAGE, load_ops_module, mw

try:
    from PyQt6 import QtCore, QtGui, QtWidgets, sip

    HAVE_QT = True
except ImportError:  # pragma: no cover - the dev requirements install it with aqt
    HAVE_QT = False


def load_with_real_qt() -> ModuleType:
    qt = ModuleType("aqt.qt")
    for source in (QtCore, QtGui, QtWidgets):
        qt.__dict__.update({k: v for k, v in vars(source).items() if not k.startswith("_")})
    qt.qconnect = lambda signal, slot: signal.connect(slot)  # type: ignore[attr-defined]
    qt.sip = sip  # type: ignore[attr-defined]
    names = ["aqt.qt", f"{PACKAGE}.async_api_ops.progress_controls"]
    saved = {name: sys.modules.pop(name, None) for name in names}
    sys.modules["aqt.qt"] = qt
    try:
        return load_ops_module("progress_controls")
    finally:
        # The rest of the suite goes on with the stubbed copies
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


if HAVE_QT:

    class FakeProgressDialog(QtWidgets.QDialog):
        """What Anki's ProgressDialog does with Escape and the close box."""

        def __init__(self):
            super().__init__()
            self.wantCancel = False
            self.form = types.SimpleNamespace(verticalLayout=QtWidgets.QVBoxLayout(self))

        def closeEvent(self, evt):
            self.wantCancel = True
            evt.ignore()

        def keyPressEvent(self, evt):
            if evt.key() == QtCore.Qt.Key.Key_Escape:
                evt.ignore()
                self.wantCancel = True


@unittest.skipUnless(HAVE_QT, "needs PyQt6")
class CancelKeyTests(unittest.TestCase):
    controls_module: ModuleType

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.controls_module = load_with_real_qt()

    def setUp(self):
        self.win = FakeProgressDialog()
        self.addCleanup(self.win.deleteLater)
        patcher = mock.patch.object(mw.progress, "_win", self.win, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No run: the toggle reads the pause through these
        for name in ("pause_state",):
            p = mock.patch.object(self.controls_module, name, lambda: None)
            p.start()
            self.addCleanup(p.stop)

    def escape(self):
        event = QtGui.QKeyEvent(
            QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Escape, QtCore.Qt.KeyboardModifier(0)
        )
        QtWidgets.QApplication.sendEvent(self.win, event)

    def close_box(self):
        QtWidgets.QApplication.sendEvent(self.win, QtGui.QCloseEvent())

    def controls(self):
        return getattr(self.win, self.controls_module._CONTROLS_ATTR)

    def test_escape_cancels_while_cancel_is_enabled(self):
        self.controls_module.start_run_controls()
        self.escape()
        self.assertTrue(self.win.wantCancel)

    def test_the_close_box_cancels_while_cancel_is_enabled(self):
        self.controls_module.start_run_controls()
        self.close_box()
        self.assertTrue(self.win.wantCancel)

    def test_neither_cancels_while_the_cleanup_has_cancel_greyed(self):
        self.controls_module.start_run_controls()
        self.controls_module.disable_run_controls()
        self.escape()
        self.close_box()
        self.assertFalse(self.win.wantCancel)

    def test_the_adding_s_re_armed_cancel_brings_escape_back(self):
        self.controls_module.start_run_controls()
        self.controls_module.disable_run_controls()
        self.assertTrue(self.controls_module.rearm_cleanup_cancel())
        self.escape()
        self.assertTrue(self.win.wantCancel)

    def test_a_chain_s_idle_dialog_ignores_escape_until_a_step_starts(self):
        self.controls_module.install_idle_run_controls()
        self.escape()
        self.assertFalse(self.win.wantCancel)
        self.controls_module.start_run_controls()
        self.assertTrue(self.controls().cancel.isEnabled())
        self.escape()
        self.assertTrue(self.win.wantCancel)

    def test_the_next_step_brings_back_the_buttons_the_last_left_grey(self):
        self.controls_module.start_run_controls()
        # The step's cleanup ends what a cancel stops
        self.controls_module.disable_run_controls()
        self.controls_module.start_run_controls()
        controls = self.controls()
        self.assertTrue(controls.cancel.isEnabled())
        self.assertTrue(controls.toggle.isEnabled())
        self.assertFalse(controls.cleanup)

    def test_a_cancel_already_made_keeps_the_buttons_grey_for_the_next_run(self):
        self.controls_module.start_run_controls()
        self.controls().cancel.click()
        self.controls_module.start_run_controls()
        self.assertFalse(self.controls().cancel.isEnabled())
        self.assertTrue(self.win.wantCancel)

    def test_the_buttons_go_in_once(self):
        self.controls_module.start_run_controls()
        first = self.controls()
        self.controls_module.start_run_controls()
        self.assertIs(self.controls(), first)
        self.assertEqual(len(self.win.findChildren(QtWidgets.QPushButton)), 2)
