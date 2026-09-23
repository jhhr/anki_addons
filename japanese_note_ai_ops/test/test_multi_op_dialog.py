"""The multi-op dialog: which ops run in which order, over which notes.

`OpSelection` is the whole of the choice, so most of it is tested there, without Qt. The
dialog is then tested with real Qt widgets, since what it adds is wiring - a click that moves
an op, numbering that must follow every change, a Run that must stay off with nothing to run.

This suite runs on the Anki stubs, whose `aqt.qt` hands out empty classes. The dialog module
is therefore loaded a second time, with `aqt.qt` standing on PyQt6 for the duration of the
load only; everything else it imports (the registry, the chain runner) is the stubbed copy
the rest of the suite shares. Without PyQt6 the widget tests skip.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import ModuleType
from unittest import mock

from addon_modules import PACKAGE, load_ops_module

op_registry = load_ops_module("op_registry", subdir="")
load_ops_module("op_chain")
# The stubbed copy: its pure parts need no Qt
stubbed_dialog = load_ops_module("multi_op_dialog", subdir="")
OpSelection = stubbed_dialog.OpSelection

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtTest import QTest

    HAVE_QT = True
except ImportError:  # pragma: no cover - the dev requirements install it with aqt
    HAVE_QT = False


def load_with_real_qt() -> ModuleType:
    qt = ModuleType("aqt.qt")
    for source in (QtCore, QtGui, QtWidgets):
        qt.__dict__.update({k: v for k, v in vars(source).items() if not k.startswith("_")})
    qt.qconnect = lambda signal, slot: signal.connect(slot)  # type: ignore[attr-defined]

    names = [
        "aqt.qt",
        f"{PACKAGE}.multi_op_dialog",
        f"{PACKAGE}.shared.ui.note_source_buttons",
    ]
    saved = {name: sys.modules.pop(name, None) for name in names}
    ui_package = sys.modules.get(f"{PACKAGE}.shared.ui")
    saved_attr = getattr(ui_package, "note_source_buttons", None)
    sys.modules["aqt.qt"] = qt
    try:
        return load_ops_module("multi_op_dialog", subdir="")
    finally:
        # The rest of the suite goes on with the stubbed copies
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if ui_package is not None and saved_attr is not None:
            setattr(ui_package, "note_source_buttons", saved_attr)


KEYS = ["a", "b", "c", "d"]


class OpSelectionTests(unittest.TestCase):
    def setUp(self):
        self.sel = OpSelection(KEYS)

    def test_starts_with_every_op_an_option_in_registry_order(self):
        self.assertEqual(self.sel.options(), KEYS)
        self.assertEqual(self.sel.selected(), [])

    def test_select_appends_to_the_end_whatever_the_registry_order(self):
        for key in ("c", "a", "d"):
            self.assertTrue(self.sel.select(key))
        self.assertEqual(self.sel.selected(), ["c", "a", "d"])
        self.assertEqual(self.sel.options(), ["b"])

    def test_selecting_twice_or_an_unknown_key_changes_nothing(self):
        self.sel.select("b")
        self.assertFalse(self.sel.select("b"))
        self.assertFalse(self.sel.select("zzz"))
        self.assertEqual(self.sel.selected(), ["b"])
        self.assertEqual(self.sel.options(), ["a", "c", "d"])

    def test_duplicate_registry_keys_are_one_op(self):
        sel = OpSelection(["a", "b", "a"])
        self.assertEqual(sel.options(), ["a", "b"])
        sel.select("a")
        self.assertEqual(sel.options(), ["b"])

    def test_deselect_puts_the_op_back_at_its_registry_position(self):
        for key in ("d", "b", "a"):
            self.sel.select(key)
        self.assertTrue(self.sel.deselect("b"))
        self.assertEqual(self.sel.selected(), ["d", "a"])
        self.assertEqual(self.sel.options(), ["b", "c"])
        self.sel.deselect("d")
        self.assertEqual(self.sel.options(), ["b", "c", "d"])

    def test_deselect_of_an_option_or_unknown_key_changes_nothing(self):
        self.sel.select("a")
        self.assertFalse(self.sel.deselect("b"))
        self.assertFalse(self.sel.deselect("zzz"))
        self.assertEqual(self.sel.selected(), ["a"])

    def test_reselected_op_goes_to_the_end_again(self):
        for key in ("a", "b", "c"):
            self.sel.select(key)
        self.sel.deselect("a")
        self.sel.select("a")
        self.assertEqual(self.sel.selected(), ["b", "c", "a"])

    def test_move_to_an_index(self):
        for key in KEYS:
            self.sel.select(key)
        self.assertTrue(self.sel.move("d", 1))
        self.assertEqual(self.sel.selected(), ["a", "d", "b", "c"])
        self.assertTrue(self.sel.move("a", 3))
        self.assertEqual(self.sel.selected(), ["d", "b", "c", "a"])

    def test_move_clamps_to_the_ends(self):
        for key in ("a", "b", "c"):
            self.sel.select(key)
        self.assertTrue(self.sel.move("b", 99))
        self.assertEqual(self.sel.selected(), ["a", "c", "b"])
        self.assertTrue(self.sel.move("b", -5))
        self.assertEqual(self.sel.selected(), ["b", "a", "c"])
        self.assertFalse(self.sel.move("b", 0))

    def test_move_of_an_unselected_op_changes_nothing(self):
        self.sel.select("a")
        self.assertFalse(self.sel.move("b", 0))
        self.assertFalse(self.sel.move_up("b"))
        self.assertFalse(self.sel.move_down("zzz"))
        self.assertEqual(self.sel.selected(), ["a"])

    def test_move_up_and_down_stop_at_the_ends(self):
        for key in ("a", "b", "c"):
            self.sel.select(key)
        self.assertFalse(self.sel.move_up("a"))
        self.assertFalse(self.sel.move_down("c"))
        self.assertTrue(self.sel.move_up("c"))
        self.assertEqual(self.sel.selected(), ["a", "c", "b"])
        self.assertTrue(self.sel.move_down("a"))
        self.assertEqual(self.sel.selected(), ["c", "a", "b"])

    def test_clear_returns_every_op_to_the_options(self):
        self.assertFalse(self.sel.clear())
        for key in ("c", "a"):
            self.sel.select(key)
        self.assertTrue(self.sel.clear())
        self.assertEqual(self.sel.selected(), [])
        self.assertEqual(self.sel.options(), KEYS)

    def test_selected_and_options_are_copies(self):
        self.sel.select("a")
        self.sel.selected().append("b")
        self.sel.options().remove("b")
        self.assertEqual(self.sel.selected(), ["a"])
        self.assertEqual(self.sel.options(), ["b", "c", "d"])


class LabelTextTests(unittest.TestCase):
    NoteSource = None

    @classmethod
    def setUpClass(cls):
        source_module = sys.modules[f"{PACKAGE}.shared.ui.note_source_buttons"]
        cls.NoteSource = source_module.NoteSource

    def test_numbered_label(self):
        self.assertEqual(stubbed_dialog.numbered_label(1, "Extract words"), "1. Extract words")

    def test_selection_mode(self):
        source = self.NoteSource([1, 2], "")
        text = stubbed_dialog.count_label_text(2, source)
        self.assertEqual(text, "2 notes will be processed (the selected notes).")
        self.assertFalse(stubbed_dialog.is_whole_collection(source))

    def test_search_mode(self):
        source = self.NoteSource([1], "deck:x", use_selection=False)
        text = stubbed_dialog.count_label_text(1, source)
        self.assertEqual(text, "1 note will be processed (all notes of the current search).")
        self.assertFalse(stubbed_dialog.is_whole_collection(source))

    def test_empty_search_warns_it_is_every_note(self):
        for search in ("", "   "):
            source = self.NoteSource([1], search, use_selection=False)
            text = stubbed_dialog.count_label_text(5000, source)
            self.assertIn("EVERY note in the collection", text)
            self.assertIn("5000 notes will be processed", text)
            self.assertTrue(stubbed_dialog.is_whole_collection(source))

    def test_an_empty_search_does_not_matter_in_selection_mode(self):
        source = self.NoteSource([1], "")
        self.assertNotIn("EVERY", stubbed_dialog.count_label_text(1, source))

    def test_error(self):
        source = self.NoteSource([], "(")
        text = stubbed_dialog.count_label_text(0, source, error="bad search")
        self.assertEqual(text, "The browser's search could not be run: bad search")


def spec(key, label):
    return op_registry.OpSpec(key, label, mock.Mock(), needs_generator=False, group="async")


OPS = [spec("a", "Op A"), spec("b", "Op B"), spec("c", "Op C"), spec("d", "Op D")]


@unittest.skipUnless(HAVE_QT, "needs PyQt6")
class DialogTests(unittest.TestCase):
    dialog_module: ModuleType

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.dialog_module = load_with_real_qt()
        cls.NoteSource = cls.dialog_module.NoteSource

    def make(self, selected=(1, 2, 3), search="deck:x", found=(7, 8), use_selection=None):
        self.find_notes = mock.Mock(return_value=list(found))
        source = self.NoteSource(list(selected), search, use_selection)
        dialog = self.dialog_module.MultiOpDialog(None, source, self.find_notes, ops=OPS)
        self.addCleanup(dialog.deleteLater)
        return dialog

    @staticmethod
    def texts(widget):
        return [widget.item(i).text() for i in range(widget.count())]

    def click_option(self, dialog, label):
        items = dialog.options_list.findItems(label, QtCore.Qt.MatchFlag.MatchExactly)
        self.assertEqual(len(items), 1)
        dialog.options_list.itemClicked.emit(items[0])

    def test_clicking_an_option_moves_it_to_the_end_numbered(self):
        dialog = self.make()
        self.click_option(dialog, "Op C")
        self.click_option(dialog, "Op A")
        self.assertEqual(self.texts(dialog.selected_list), ["1. Op C", "2. Op A"])
        self.assertEqual(self.texts(dialog.options_list), ["Op B", "Op D"])
        self.assertEqual([s.key for s in dialog.chosen_specs()], ["c", "a"])

    def test_a_real_click_on_an_option_moves_it(self):
        dialog = self.make()
        dialog.show()
        rect = dialog.options_list.visualItemRect(dialog.options_list.item(1))
        QTest.mouseClick(
            dialog.options_list.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=rect.center()
        )
        self.assertEqual(self.texts(dialog.selected_list), ["1. Op B"])

    def test_remove_and_double_click_return_the_op_to_its_place(self):
        dialog = self.make()
        for label in ("Op D", "Op B", "Op A"):
            self.click_option(dialog, label)
        dialog.selected_list.setCurrentRow(0)
        dialog.remove_button.click()
        self.assertEqual(self.texts(dialog.selected_list), ["1. Op B", "2. Op A"])
        self.assertEqual(self.texts(dialog.options_list), ["Op C", "Op D"])
        # The op that took its place is current, so Remove can be pressed again
        self.assertEqual(dialog.selected_list.currentRow(), 0)
        dialog.selected_list.itemDoubleClicked.emit(dialog.selected_list.item(1))
        self.assertEqual(self.texts(dialog.selected_list), ["1. Op B"])
        self.assertEqual(self.texts(dialog.options_list), ["Op A", "Op C", "Op D"])

    def test_up_and_down_reorder_renumber_and_keep_the_op_current(self):
        dialog = self.make()
        for label in ("Op A", "Op B", "Op C"):
            self.click_option(dialog, label)
        dialog.selected_list.setCurrentRow(2)
        self.assertFalse(dialog.down_button.isEnabled())
        dialog.up_button.click()
        self.assertEqual(self.texts(dialog.selected_list), ["1. Op A", "2. Op C", "3. Op B"])
        self.assertEqual(dialog.selected_list.currentItem().text(), "2. Op C")
        dialog.up_button.click()
        self.assertEqual(self.texts(dialog.selected_list), ["1. Op C", "2. Op A", "3. Op B"])
        self.assertFalse(dialog.up_button.isEnabled())
        dialog.down_button.click()
        self.assertEqual(self.texts(dialog.selected_list), ["1. Op A", "2. Op C", "3. Op B"])
        self.assertEqual([s.key for s in dialog.chosen_specs()], ["a", "c", "b"])

    def test_a_drop_brings_the_order_in_line_and_renumbers(self):
        # A drop moves the row of the list's own model, as moveRow does here (a real drag
        # under Qt 6.11 was seen to emit exactly this rowsMoved and nothing else)
        dialog = self.make()
        for label in ("Op A", "Op B", "Op C"):
            self.click_option(dialog, label)
        model = dialog.selected_list.model()
        self.assertTrue(model.moveRow(QtCore.QModelIndex(), 0, QtCore.QModelIndex(), 3))
        self.assertEqual(self.texts(dialog.selected_list), ["1. Op B", "2. Op C", "3. Op A"])
        self.assertEqual([s.key for s in dialog.chosen_specs()], ["b", "c", "a"])
        self.assertEqual(self.texts(dialog.options_list), ["Op D"])

    def test_clear(self):
        dialog = self.make()
        for label in ("Op C", "Op A"):
            self.click_option(dialog, label)
        dialog.clear_button.click()
        self.assertEqual(self.texts(dialog.selected_list), [])
        self.assertEqual(self.texts(dialog.options_list), ["Op A", "Op B", "Op C", "Op D"])
        self.assertFalse(dialog.run_button.isEnabled())

    def test_run_needs_an_op_and_a_note(self):
        dialog = self.make()
        self.assertFalse(dialog.run_button.isEnabled())
        self.click_option(dialog, "Op A")
        self.assertTrue(dialog.run_button.isEnabled())
        # A search that finds nothing
        empty = self.make(selected=(), found=())
        self.click_option(empty, "Op A")
        self.assertFalse(empty.run_button.isEnabled())
        self.assertIn("0 notes", empty.count_label.text())

    def test_count_follows_the_note_source(self):
        dialog = self.make(selected=(1, 2, 3), found=(7, 8))
        self.assertIn("3 notes will be processed (the selected notes)", dialog.count_label.text())
        self.find_notes.assert_not_called()
        dialog.note_source_buttons.search_button.click()
        self.find_notes.assert_called_once_with("deck:x")
        self.assertIn("2 notes will be processed (all notes", dialog.count_label.text())

    def test_nothing_selected_starts_on_the_search(self):
        dialog = self.make(selected=(), found=(7, 8, 9))
        self.assertFalse(dialog.note_source_buttons.selection_button.isEnabled())
        self.assertIn("3 notes", dialog.count_label.text())

    def test_empty_search_warns_that_it_is_the_whole_collection(self):
        dialog = self.make(selected=(1,), search="", found=range(5000))
        self.assertNotIn("EVERY", dialog.count_label.text())
        dialog.note_source_buttons.search_button.click()
        self.assertIn("EVERY note in the collection", dialog.count_label.text())
        self.assertIn("5000 notes", dialog.count_label.text())
        self.assertNotEqual(dialog.count_label.styleSheet(), "")
        self.find_notes.assert_called_once_with("")

    def test_a_search_that_fails_disables_run(self):
        dialog = self.make(selected=(1,))
        self.click_option(dialog, "Op A")
        self.assertTrue(dialog.run_button.isEnabled())
        self.find_notes.side_effect = RuntimeError("bad search")
        dialog.note_source_buttons.search_button.click()
        self.assertIn("bad search", dialog.count_label.text())
        self.assertFalse(dialog.run_button.isEnabled())

    def test_run_fixes_the_ids_of_the_selection(self):
        dialog = self.make(selected=(3, 1, 2))
        self.click_option(dialog, "Op B")
        dialog.run_button.click()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.note_ids, [3, 1, 2])

    def test_run_fixes_the_ids_of_the_search(self):
        dialog = self.make(selected=(3,), found=(7, 8), use_selection=False)
        self.click_option(dialog, "Op B")
        self.find_notes.reset_mock()
        dialog.run_button.click()
        self.find_notes.assert_called_once_with("deck:x")
        self.assertEqual(dialog.note_ids, [7, 8])

    def test_close_rejects(self):
        dialog = self.make()
        self.click_option(dialog, "Op B")
        dialog.close_button.click()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.note_ids, [])


class FakeBrowser:
    def __init__(self, selected, search):
        self._selected = selected
        self._search = search

    def selected_notes(self):
        return list(self._selected)

    def current_search(self):
        return self._search


@unittest.skipUnless(HAVE_QT, "needs PyQt6")
class OpenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.module = load_with_real_qt()

    def open(self, browser, found, pick, run=True):
        """Opens the dialog as the opener does; `exec` is replaced by picking the ops by label
        and pressing Run (or Close)."""
        module = self.module
        col = mock.Mock()
        col.find_notes.return_value = list(found)

        def exec_(dialog):
            for label in pick:
                items = dialog.options_list.findItems(label, QtCore.Qt.MatchFlag.MatchExactly)
                dialog.options_list.itemClicked.emit(items[0])
            (dialog.run_button if run else dialog.close_button).click()
            return dialog.result()

        real_init = module.MultiOpDialog.__init__

        def init(dialog, parent, source, find_notes, ops=OPS):
            # No browser widget to parent to here, and the test's small registry
            real_init(dialog, None, source, find_notes, ops=ops)

        with mock.patch.object(module, "mw") as mw, mock.patch.object(
            module, "run_op_chain"
        ) as run_op_chain, mock.patch.object(
            module.MultiOpDialog, "exec", exec_
        ), mock.patch.object(
            module.MultiOpDialog, "__init__", init
        ):
            mw.col = col
            module.show_multi_op_dialog(browser)
        return run_op_chain, col

    def test_run_starts_the_chain_with_the_ops_in_order_over_the_selection(self):
        browser = FakeBrowser([5, 6], "deck:x")
        run_op_chain, col = self.open(browser, found=(7, 8), pick=["Op D", "Op A", "Op C"])
        run_op_chain.assert_called_once()
        specs, nids = run_op_chain.call_args.args
        self.assertEqual([s.key for s in specs], ["d", "a", "c"])
        self.assertEqual(nids, [5, 6])
        self.assertIs(run_op_chain.call_args.kwargs["parent"], browser)
        col.find_notes.assert_not_called()

    def test_with_nothing_selected_the_chain_runs_over_the_search(self):
        browser = FakeBrowser([], "deck:x")
        run_op_chain, col = self.open(browser, found=(7, 8), pick=["Op B"])
        specs, nids = run_op_chain.call_args.args
        self.assertEqual([s.key for s in specs], ["b"])
        self.assertEqual(nids, [7, 8])
        col.find_notes.assert_called_with("deck:x")

    def test_close_starts_nothing(self):
        browser = FakeBrowser([5], "deck:x")
        run_op_chain, _ = self.open(browser, found=(), pick=["Op A"], run=False)
        run_op_chain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
