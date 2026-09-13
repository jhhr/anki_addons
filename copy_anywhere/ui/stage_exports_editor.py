"""The definition-level Exports panel.

A definition's exports are its interface to any definition that calls it (§5.9): a caller
binds an export under a local name and gets that value in its own scope. Only root-block
results can be exported, because a branch- or loop-local result might not run at all, so
the panel lists exactly the root results and lets each one be given an export name.
"""

from typing import Optional

from aqt.qt import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)

from ..shared.ui.required_text_input import RequiredLineEdit
from .stage_document import StageDocument


class ExportsEditor(QWidget):
    """Picks which root results this definition offers to its callers."""

    changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget], document: StageDocument) -> None:
        super().__init__(parent)
        self.document = document
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.addWidget(
            QLabel(
                "<small>A definition that is called by another can hand back any result"
                " produced at its top level. Nothing is handed back when it runs on its"
                " own.</small>",
                self,
            )
        )
        self.rows_container = QWidget(self)
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.box.addWidget(self.rows_container)
        self.rows: list[tuple[str, QCheckBox, RequiredLineEdit]] = []
        self.rebuild()

    def rebuild(self) -> None:
        """Relist the root results. Called whenever the stage list changes shape."""
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.deleteLater()
        self.rows = []
        exported = {
            export.get("stage_guid"): export.get("name", "")
            for export in self.document.exports()
            if isinstance(export, dict)
        }
        candidates = self.document.exportable_stages()
        if not candidates:
            self.rows_layout.addWidget(
                QLabel(
                    "<small>No stage at the top level produces a result yet.</small>",
                    self.rows_container,
                )
            )
            return
        for guid, result_name in candidates:
            row = QWidget(self.rows_container)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            keep = QCheckBox(result_name, row)
            keep.setChecked(guid in exported)
            name = RequiredLineEdit(row, placeholder_text=f"export it as {result_name}")
            name.setText(exported.get(guid, "") or "")
            name.setEnabled(keep.isChecked())
            keep.toggled.connect(name.setEnabled)
            keep.toggled.connect(self._on_changed)
            name.textChanged.connect(self._on_changed)
            layout.addWidget(keep)
            layout.addWidget(QLabel("as", row))
            layout.addWidget(name)
            layout.addStretch()
            self.rows_layout.addWidget(row)
            self.rows.append((guid, keep, name))

    def _on_changed(self, *_args) -> None:
        self.changed.emit()

    def apply(self) -> None:
        """Write the panel back. An export left unnamed takes the result's own name.

        Requiring a second name for the common case -- export `H1` as `H1` -- would be
        ceremony; the analyser still rejects a name that is not an identifier.
        """
        exports = []
        for guid, keep, name in self.rows:
            if not keep.isChecked():
                continue
            exports.append({"name": name.text().strip() or keep.text(), "stage_guid": guid})
        self.document.set_exports(exports)
