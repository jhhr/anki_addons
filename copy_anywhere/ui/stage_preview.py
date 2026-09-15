"""The preview pane: pick a trigger note, run the definition read-only, read the trace.

An operational tool, not an explanation of the feature. The question it answers is "what
would this definition do to that note", so it is a note picker, a run button, and the trace
the run left behind.

Nothing here decides anything about the run. `logic/preview.py` owns that, and this pane
only shows what it produced -- which is what keeps "the preview matches a real run" a
property of the executor rather than of a widget.
"""

from typing import Optional, Sequence

from aqt.qt import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    Qt,
    pyqtSignal,
    qtmajor,
)

from ..logic.definition_schema import CopyDefinitionV2
from ..logic.preview import PreviewRun, find_trigger_notes, preview_note, run_preview
from .stage_document import STAGE_TYPE_ICONS, STAGE_TYPE_LABELS

if qtmajor > 5:
    UserRole = Qt.ItemDataRole.UserRole
    SingleSelection = QAbstractItemView.SelectionMode.SingleSelection
else:  # pragma: no cover -- Anki 2.1.49 and older
    UserRole = Qt.UserRole  # type: ignore[attr-defined]
    SingleSelection = QAbstractItemView.SingleSelection  # type: ignore[attr-defined]

#: What each status looks like in the trace. A run that failed halfway is the common case
#: while a definition is being written, so the failing stage has to be the obvious one.
STATUS_MARKS = {
    "ok": ("✓", "#2d8a4a"),
    "failed": ("✗", "#c0392b"),
    "skipped": ("–", "#7f8c8d"),
    "cancelled": ("–", "#7f8c8d"),
    "running": ("…", "#7f8c8d"),
}

#: Said instead of a trace whenever the definition changed after the last run. The preview
#: is not rerun on every keystroke: a query or a nested call is not cheap enough for that.
STALE_TEXT = "Edited since this ran — run it again to see what it does now."


def _escape(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


class PreviewPane(QWidget):
    """Picks a trigger note, runs the definition against it, and shows what it would do."""

    #: A trace row was chosen: the stage list should open and show that stage.
    stage_selected = pyqtSignal(str)

    def __init__(
        self,
        parent: Optional[QWidget],
        definition: CopyDefinitionV2,
        all_definitions: Optional[Sequence[dict]] = None,
    ) -> None:
        super().__init__(parent)
        self.definition = definition
        self.all_definitions = list(all_definitions or [])
        self.run: Optional[PreviewRun] = None
        self.stale = True
        #: Every event the tree shows, in build order. A row carries its index rather than
        #: its guid alone, because a stage inside a loop has one row per iteration and they
        #: all share the guid.
        self._events: list = []
        #: Set while the pane is selecting a row itself, so telling the stage list about a
        #: selection the stage list asked for does not bounce back.
        self._selecting = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel("<h2>Preview</h2>", self))
        self.stale_label = QLabel("", self)
        self.stale_label.setWordWrap(True)
        self.stale_label.setStyleSheet("color: #b8860b;")
        header.addWidget(self.stale_label, 1)
        layout.addLayout(header)

        search = QHBoxLayout()
        self.query_edit = QLineEdit(self)
        self.query_edit.setPlaceholderText("Extra search terms, e.g. deck:Japanese")
        self.query_edit.returnPressed.connect(self.search_notes)
        search.addWidget(self.query_edit, 1)
        self.search_button = QPushButton("Find notes", self)
        self.search_button.clicked.connect(self.search_notes)
        search.addWidget(self.search_button)
        layout.addLayout(search)

        self.note_list = QListWidget(self)
        self.note_list.setSelectionMode(SingleSelection)
        self.note_list.setMaximumHeight(120)
        self.note_list.itemSelectionChanged.connect(self._note_chosen)
        self.note_list.itemDoubleClicked.connect(lambda _item: self.run_preview())
        layout.addWidget(self.note_list)

        controls = QHBoxLayout()
        self.run_button = QPushButton("Run preview", self)
        self.run_button.setToolTip(
            "Runs the definition against the selected note without saving anything: the"
            " searches are real, the changes are only reported."
        )
        self.run_button.clicked.connect(self.run_preview)
        controls.addWidget(self.run_button)
        self.summary_label = QLabel("", self)
        self.summary_label.setWordWrap(True)
        controls.addWidget(self.summary_label, 1)
        layout.addLayout(controls)

        self.trace_tree = QTreeWidget(self)
        self.trace_tree.setHeaderLabels(["Stage", "Result"])
        self.trace_tree.setColumnWidth(0, 220)
        self.trace_tree.itemSelectionChanged.connect(self._trace_row_chosen)
        layout.addWidget(self.trace_tree, 2)

        self.details = QTextBrowser(self)
        self.details.setMinimumHeight(120)
        layout.addWidget(self.details, 1)

        self.search_notes()
        self.mark_stale()

    # -- picking a note -------------------------------------------------------------------

    def search_notes(self) -> None:
        """Fill the list with notes the definition's triggers would consider."""
        self.note_list.clear()
        try:
            found = find_trigger_notes(self.definition, self.query_edit.text())
        except Exception as error:  # noqa: BLE001 -- an invalid search is the user's typing
            self.summary_label.setText(f"<span style='color: #c0392b'>{_escape(error)}</span>")
            return
        for note_id, label in found:
            item = QListWidgetItem(label, self.note_list)
            item.setData(UserRole, note_id)
        if not found:
            self.summary_label.setText("No notes match. Adjust the search or the triggers.")
        elif self.note_list.count():
            self.note_list.setCurrentRow(0)

    def selected_note_id(self) -> Optional[int]:
        item = self.note_list.currentItem()
        return item.data(UserRole) if item is not None else None

    def _note_chosen(self) -> None:
        # A different note makes the trace on screen about the wrong note, which is the same
        # problem an edit causes and deserves the same answer.
        if self.run is not None and self.selected_note_id() != self.run.trigger_id:
            self.mark_stale()

    # -- running --------------------------------------------------------------------------

    def mark_stale(self) -> None:
        """Say the trace on screen no longer describes the definition as it now is (§9)."""
        self.stale = True
        self.stale_label.setText(STALE_TEXT if self.run is not None else "")

    def set_definition(self, definition: CopyDefinitionV2) -> None:
        self.definition = definition

    def run_preview(self) -> None:
        note_id = self.selected_note_id()
        if note_id is None:
            self.summary_label.setText("Choose a note to run against.")
            return
        try:
            self.run = run_preview(
                self.definition, preview_note(note_id), self.all_definitions
            )
        except Exception as error:  # noqa: BLE001 -- a bug here must not close the dialog
            self.run = None
            self.summary_label.setText(
                f"<span style='color: #c0392b'>The preview failed to run:"
                f" {_escape(error)}</span>"
            )
            return
        self.stale = False
        self.stale_label.setText("")
        self._show_run()

    def _show_run(self) -> None:
        run = self.run
        if run is None:
            return
        self.summary_label.setText(self._summary_html(run))
        self.trace_tree.clear()
        self._events = []
        for event in run.trace:
            self.trace_tree.addTopLevelItem(self._build_item(event))
        self.trace_tree.expandAll()
        self.details.setHtml(
            "<p>Select a stage to see what it saw and what it would change.</p>"
        )

    def _summary_html(self, run: PreviewRun) -> str:
        if not run.succeeded:
            head = "<span style='color: #c0392b'>The definition failed.</span>"
        else:
            head = "Would change {} note(s), {} card(s), {} file(s).".format(
                len(run.notes), len(run.cards), len(run.files)
            )
        if run.messages:
            head += "<br><small>" + "<br>".join(_escape(m) for m in run.messages) + "</small>"
        return head

    # -- the trace tree -------------------------------------------------------------------

    def _build_item(self, event) -> QTreeWidgetItem:
        mark, colour = STATUS_MARKS.get(event.status, ("?", "#7f8c8d"))
        icon = STAGE_TYPE_ICONS.get(event.stage_type, "•")
        label = event.stage_name or STAGE_TYPE_LABELS.get(event.stage_type, event.stage_type)
        item = QTreeWidgetItem([f"{mark} {icon} {label}", event.result or ""])
        item.setToolTip(0, event.error or "")
        item.setData(0, UserRole, event.stage_guid)
        item.setData(1, UserRole, len(self._events))
        self._events.append(event)

        # A loop's body events all carry the loop's stage as their parent but belong to
        # different passes, so they are grouped by iteration rather than run together (§9).
        depth = len(event.loop_path)
        groups: dict[int, QTreeWidgetItem] = {}
        for child in event.children:
            child_item = self._build_item(child)
            if len(child.loop_path) > depth:
                index = child.loop_path[depth]
                group = groups.get(index)
                if group is None:
                    group = QTreeWidgetItem([f"Iteration {index}", ""])
                    groups[index] = group
                    item.addChild(group)
                group.addChild(child_item)
            else:
                item.addChild(child_item)
        return item

    def _trace_row_chosen(self) -> None:
        items = self.trace_tree.selectedItems()
        if not items:
            return
        index = items[0].data(1, UserRole)
        if index is None:
            # An "Iteration n" grouping row, which stands for no single stage.
            return
        self.details.setHtml(self._details_html(self._events[index]))
        guid = items[0].data(0, UserRole)
        if guid and not self._selecting:
            self.stage_selected.emit(guid)

    def _details_html(self, event) -> str:
        parts = [f"<h3>{_escape(event.stage_name or event.stage_type or '')}</h3>"]
        if event.iteration_label():
            parts.append(f"<p><b>Iteration</b> {event.iteration_label()}</p>")
        if event.error:
            parts.append(f"<p style='color: #c0392b'><b>{_escape(event.error)}</b></p>")
        if event.result is not None:
            parts.append(f"<p><b>Result:</b> {_escape(event.result)}</p>")
        parts.append(self._table("What it could see", event.inputs.items()))
        details = {
            key: value for key, value in event.details.items() if key != "started"
        }
        parts.append(self._table("Details", details.items()))
        if event.mutations:
            parts.append(
                "<p><b>Would change</b></p><ul>"
                + "".join(f"<li>{_escape(line)}</li>" for line in event.mutations)
                + "</ul>"
            )
        parts.append(f"<p><small>Took {event.duration_ms:.1f} ms</small></p>")
        return "".join(parts)

    @staticmethod
    def _table(title: str, rows) -> str:
        rows = list(rows)
        if not rows:
            return ""
        cells = "".join(
            f"<tr><td><b>{_escape(name)}</b></td><td>{_escape(value)}</td></tr>"
            for name, value in rows
        )
        return f"<p><b>{title}</b></p><table>{cells}</table>"

    # -- correlating with the stage list ----------------------------------------------------

    def show_stage(self, stage_guid: str) -> None:
        """Select this stage's latest trace event, if the last run reached it (§9).

        The latest rather than the first, because a stage in a loop body ran once per
        iteration: the last one is the state the run ended in. The other iterations are
        still there under their own headings for the user to pick.
        """
        if self.run is None:
            return
        found: list[QTreeWidgetItem] = []
        for index in range(self.trace_tree.topLevelItemCount()):
            self._collect_items(self.trace_tree.topLevelItem(index), stage_guid, found)
        if not found:
            return
        self._selecting = True
        try:
            self.trace_tree.setCurrentItem(found[-1])
        finally:
            self._selecting = False

    def _collect_items(
        self, item: QTreeWidgetItem, stage_guid: str, found: list
    ) -> None:
        if item.data(0, UserRole) == stage_guid:
            found.append(item)
        for index in range(item.childCount()):
            self._collect_items(item.child(index), stage_guid, found)
