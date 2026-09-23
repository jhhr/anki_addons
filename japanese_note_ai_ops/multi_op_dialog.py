"""The browser's "Japanese AI ops..." dialog: pick several ops in an order, pick the notes, run.

Running an op from the "AI helper" context menu means selecting its notes in the browser, and
Anki lags badly with thousands of rows selected, worse again on the right click. Here the
note source is the shared selection / current-search pair copy_anywhere's picker uses, so one
selected row and a search are enough, and the chosen ops run one after another as one chain
(`async_api_ops/op_chain.py`), each a full run of its own with its own undo entry.

`OpSelection` is the choice of ops and their order, kept free of Qt so that it can be tested
on its own. Each op can be chosen once: clicking an option moves it to the end of the run
order, and removing it puts it back among the options where the registry has it.

Imports the op modules through `op_registry`, so like `ai_helper_menu` it may be imported
only inside `__init__.py`'s guarded `try/except ImportError` block.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Callable, Optional

from anki.notes import NoteId
from aqt import mw
from aqt.qt import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    Qt,
    QVBoxLayout,
    QWidget,
    qconnect,
)

from .async_api_ops.op_chain import run_op_chain
from .op_registry import OPS, OpSpec
from .shared.ui.note_source_buttons import NoteSource, NoteSourceButtons, browser_note_source

if TYPE_CHECKING:
    from aqt.browser import Browser

DIALOG_TITLE = "Japanese AI ops"
WHOLE_COLLECTION_STYLE = "color: #c62828; font-weight: bold;"

FindNotes = Callable[[str], Sequence[NoteId]]


class OpSelection:
    """Which ops run, in which order; the rest are the options, in registry order.

    Every change is by key and a change that makes no sense - an unknown key, choosing an op
    that is already chosen, moving one that is not - is ignored and returns False: a click
    that lands twice, or a double click on an item a single click has already moved, must not
    queue an op twice or raise.
    """

    def __init__(self, keys: Sequence[str]) -> None:
        # Registry order, which is also where a deselected op goes back to
        self._keys: list[str] = list(dict.fromkeys(keys))
        self._selected: list[str] = []

    def options(self) -> list[str]:
        return [key for key in self._keys if key not in self._selected]

    def selected(self) -> list[str]:
        return list(self._selected)

    def select(self, key: str) -> bool:
        """Add `key` at the end of the run order: the op clicked last runs last."""
        if key not in self._keys or key in self._selected:
            return False
        self._selected.append(key)
        return True

    def deselect(self, key: str) -> bool:
        if key not in self._selected:
            return False
        self._selected.remove(key)
        return True

    def move(self, key: str, new_index: int) -> bool:
        """Put `key` at `new_index` of the run order, clamped to the list's ends."""
        if key not in self._selected:
            return False
        new_index = max(0, min(new_index, len(self._selected) - 1))
        old_index = self._selected.index(key)
        if new_index == old_index:
            return False
        self._selected.pop(old_index)
        self._selected.insert(new_index, key)
        return True

    def move_up(self, key: str) -> bool:
        if key not in self._selected:
            return False
        return self.move(key, self._selected.index(key) - 1)

    def move_down(self, key: str) -> bool:
        if key not in self._selected:
            return False
        return self.move(key, self._selected.index(key) + 1)

    def clear(self) -> bool:
        if not self._selected:
            return False
        self._selected.clear()
        return True


def numbered_label(position: int, label: str) -> str:
    """`position` counts from 1, as the run order is read."""
    return f"{position}. {label}"


def is_whole_collection(note_source: NoteSource) -> bool:
    """Whether the notes to run on are every note in the collection.

    `Browser.current_search()` is the search box text, which is empty while the browser
    shows its default search - the current deck, say - and an empty search matches every
    note there is. What the browser shows is then not what would be run on.
    """
    return not note_source.use_selection and not note_source.search.strip()


def count_label_text(count: int, note_source: NoteSource, error: Optional[str] = None) -> str:
    if error is not None:
        return f"The browser's search could not be run: {error}"
    notes = "1 note" if count == 1 else f"{count} notes"
    if note_source.use_selection:
        return f"{notes} will be processed (the selected notes)."
    if is_whole_collection(note_source):
        return (
            f"The browser's search box is empty, so this is EVERY note in the collection, not"
            f" just what the browser shows: {notes} will be processed."
        )
    return f"{notes} will be processed (all notes of the current search)."


class MultiOpDialog(QDialog):
    """Options on the left, the run order on the right, the note source and Run below.

    Accepting it leaves the ops in `chosen_specs()` and the note ids, fixed when Run was
    pressed, in `note_ids`; the caller starts the chain once the dialog has closed.
    """

    def __init__(
        self,
        parent: Optional[QWidget],
        note_source: NoteSource,
        find_notes: FindNotes,
        ops: Sequence[OpSpec] = OPS,
    ) -> None:
        super().__init__(parent)
        self.note_source = note_source
        self.find_notes = find_notes
        self.ops_by_key: dict[str, OpSpec] = {spec.key: spec for spec in ops}
        self.selection = OpSelection([spec.key for spec in ops])
        self.note_count = 0
        self.note_ids: list[NoteId] = []

        self.setWindowTitle(DIALOG_TITLE)
        # Modal to the browser alone, like copy_anywhere's picker: the main window stays usable
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(760, 480)

        self.options_list = QListWidget()
        self.options_list.setToolTip("Click an op to add it to the end of the run order")
        # itemClicked only, not itemActivated: a double click would first move the clicked
        # op out and then activate whichever op slid under the pointer
        qconnect(self.options_list.itemClicked, self._on_option_clicked)

        self.selected_list = QListWidget()
        self.selected_list.setToolTip(
            "Ops run from top to bottom. Drag to reorder, double-click to remove"
        )
        self.selected_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.selected_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.selected_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        qconnect(self.selected_list.itemDoubleClicked, self._on_selected_double_clicked)
        qconnect(self.selected_list.currentRowChanged, lambda _row: self._update_buttons())
        # A drop inside the list moves the item itself (QListWidget moves the row of its model,
        # checked by a real drag under Qt 6.11), and rowsMoved fires once it has moved. The
        # model is brought in line from the items there; the lists are not rebuilt then, as
        # the drop is still being handled, only renumbered.
        model = self.selected_list.model()
        if model is not None:
            qconnect(model.rowsMoved, lambda *_args: self._on_rows_dropped())

        self.up_button = QPushButton("Up")
        self.down_button = QPushButton("Down")
        self.remove_button = QPushButton("Remove")
        self.clear_button = QPushButton("Clear")
        qconnect(self.up_button.clicked, self._move_current_up)
        qconnect(self.down_button.clicked, self._move_current_down)
        qconnect(self.remove_button.clicked, self._remove_current)
        qconnect(self.clear_button.clicked, self._clear)

        self.count_label = QLabel()
        self.count_label.setWordWrap(True)
        self.note_source_buttons = NoteSourceButtons(note_source, on_change=self._update_count)
        self.run_button = QPushButton("Run")
        self.run_button.setDefault(True)
        self.close_button = QPushButton("Close")
        qconnect(self.run_button.clicked, self._run)
        qconnect(self.close_button.clicked, self.reject)

        self._build_layout()
        self._rebuild_lists()
        self._update_count()

    def _build_layout(self) -> None:
        options_column = QVBoxLayout()
        options_column.addWidget(QLabel("Available ops"))
        options_column.addWidget(self.options_list)

        selected_column = QVBoxLayout()
        selected_column.addWidget(QLabel("Run in this order"))
        selected_column.addWidget(self.selected_list)

        order_buttons = QVBoxLayout()
        order_buttons.addStretch()
        for button in (self.up_button, self.down_button, self.remove_button, self.clear_button):
            order_buttons.addWidget(button)
        order_buttons.addStretch()

        lists = QHBoxLayout()
        lists.addLayout(options_column, 1)
        lists.addLayout(selected_column, 1)
        lists.addLayout(order_buttons)

        footer = QHBoxLayout()
        footer.addWidget(self.note_source_buttons)
        footer.addStretch()
        footer.addWidget(self.run_button)
        footer.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(lists, 1)
        layout.addWidget(self.count_label)
        layout.addLayout(footer)

    def chosen_specs(self) -> list[OpSpec]:
        return [self.ops_by_key[key] for key in self.selection.selected()]

    def select_op(self, key: str) -> None:
        if self.selection.select(key):
            self._rebuild_lists(current_key=key)

    def deselect_op(self, key: str) -> None:
        if not self.selection.deselect(key):
            return
        row = self.selected_list.currentRow()
        selected = self.selection.selected()
        # The one that took its place stays current, so Remove can be pressed repeatedly
        next_key = selected[min(row, len(selected) - 1)] if selected and row >= 0 else None
        self._rebuild_lists(current_key=next_key)

    def _current_key(self) -> Optional[str]:
        item = self.selected_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_option_clicked(self, item: QListWidgetItem) -> None:
        self.select_op(item.data(Qt.ItemDataRole.UserRole))

    def _on_selected_double_clicked(self, item: QListWidgetItem) -> None:
        self.deselect_op(item.data(Qt.ItemDataRole.UserRole))

    def _move_current_up(self) -> None:
        key = self._current_key()
        if key is not None and self.selection.move_up(key):
            self._rebuild_lists(current_key=key)

    def _move_current_down(self) -> None:
        key = self._current_key()
        if key is not None and self.selection.move_down(key):
            self._rebuild_lists(current_key=key)

    def _remove_current(self) -> None:
        key = self._current_key()
        if key is not None:
            self.deselect_op(key)

    def _clear(self) -> None:
        if self.selection.clear():
            self._rebuild_lists()

    def _on_rows_dropped(self) -> None:
        for index in range(self.selected_list.count()):
            item = self.selected_list.item(index)
            if item is not None:
                self.selection.move(item.data(Qt.ItemDataRole.UserRole), index)
        self._renumber()
        self._update_buttons()

    def _item(self, key: str, text: str) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, key)
        return item

    def _rebuild_lists(self, current_key: Optional[str] = None) -> None:
        self.options_list.clear()
        for key in self.selection.options():
            self.options_list.addItem(self._item(key, self.ops_by_key[key].label))
        self.selected_list.clear()
        for position, key in enumerate(self.selection.selected(), start=1):
            label = numbered_label(position, self.ops_by_key[key].label)
            self.selected_list.addItem(self._item(key, label))
            if key == current_key:
                self.selected_list.setCurrentRow(position - 1)
        self._update_buttons()

    def _renumber(self) -> None:
        for index in range(self.selected_list.count()):
            item = self.selected_list.item(index)
            if item is not None:
                key = item.data(Qt.ItemDataRole.UserRole)
                item.setText(numbered_label(index + 1, self.ops_by_key[key].label))

    def _update_count(self) -> None:
        error: Optional[str] = None
        try:
            self.note_count = len(self.note_source.note_ids(self.find_notes))
        except Exception as e:
            # The browser ran this search already, so this is unlikely; a count that cannot be
            # had disables Run rather than leaving the dialog unable to open
            self.note_count = 0
            error = str(e)
        self.count_label.setText(count_label_text(self.note_count, self.note_source, error))
        warn = error is not None or is_whole_collection(self.note_source)
        self.count_label.setStyleSheet(WHOLE_COLLECTION_STYLE if warn else "")
        self._update_buttons()

    def _update_buttons(self) -> None:
        selected = self.selection.selected()
        key = self._current_key()
        row = selected.index(key) if key in selected else -1
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < len(selected) - 1)
        self.remove_button.setEnabled(row >= 0)
        self.clear_button.setEnabled(bool(selected))
        # A chain given no notes only warns that none of them exist
        self.run_button.setEnabled(bool(selected) and self.note_count > 0)

    def _run(self) -> None:
        if not self.selection.selected() or self.note_count <= 0:
            return
        # Resolved again rather than reusing the count's: the ids are fixed now, at Run
        try:
            note_ids = self.note_source.note_ids(self.find_notes)
        except Exception:
            note_ids = []
        if not note_ids:
            # The collection changed since the count, or the search now fails. Closing would
            # start nothing and say nothing; staying open shows why through the count label.
            self._update_count()
            return
        self.note_ids = note_ids
        self.accept()


def show_multi_op_dialog(browser: Browser) -> None:
    """Open the dialog over `browser` and, if Run was pressed, start the chain once it has
    closed, so that the chain's first progress dialog is not opened under a modal one."""
    col = mw.col
    if col is None:
        return
    dialog = MultiOpDialog(browser, browser_note_source(browser), col.find_notes)
    if dialog.exec() and dialog.note_ids:
        run_op_chain(dialog.chosen_specs(), dialog.note_ids, parent=browser)
