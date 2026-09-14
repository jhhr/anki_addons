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

from ..logic.definition_schema import stage_result_name
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
        self.rows: list[tuple[str, str, QCheckBox, RequiredLineEdit]] = []
        self.rebuild()

    def rebuild(self) -> None:
        """Relist the root results. Called whenever the stage list changes shape."""
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.deleteLater()
        self.rows = []
        # Keyed by stage and result, not by stage alone: a call stage binds one result per
        # output, so each is its own row and its own export.
        exported = {
            (export.get("stage_guid"), export.get("result") or ""): export.get("name", "")
            for export in self.document.exports()
            if isinstance(export, dict)
        }
        candidates = self.document.exportable_stages()
        stray = self._stray_exports(candidates, exported)
        candidates.extend(sorted(stray))
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
            # An export stored before `result` existed named only its stage, and its stage
            # could only bind the one result, so it belongs to this row.
            stored = exported.get((guid, result_name))
            if stored is None:
                stored = exported.get((guid, ""))
            keep.setChecked(stored is not None)
            name = RequiredLineEdit(row, placeholder_text=f"export it as {result_name}")
            name.setText(stored or "")
            name.setEnabled(keep.isChecked())
            keep.toggled.connect(name.setEnabled)
            keep.toggled.connect(self._on_changed)
            name.textChanged.connect(self._on_changed)
            layout.addWidget(keep)
            layout.addWidget(QLabel("as", row))
            layout.addWidget(name)
            if (guid, result_name) in stray:
                marker = QLabel(
                    "<span style='color: orange'>no longer at the top level</span>", row
                )
                marker.setToolTip(
                    "Only a result produced at the top level can be exported. Move the"
                    " stage back out, or untick this to drop the export."
                )
                layout.addWidget(marker)
            layout.addStretch()
            self.rows_layout.addWidget(row)
            self.rows.append((guid, result_name, keep, name))

    def _stray_exports(self, candidates, exported) -> set[tuple[str, str]]:
        """Stored exports whose stage no longer offers the result they name.

        A stage moved into a loop or a branch is still in the definition, and still holds
        the export the user gave it, but it is no longer exportable: §5.9 keeps exports free
        of values that might not be produced. Without a row the panel would drop the export
        the next time it wrote itself back, so the choice would disappear on the way past
        rather than when the user made it -- and moving the stage back out would not bring
        it back. A row keeps it visible and, since the analyser reports it, fixable either
        way: untick it, or move the stage back to the top level.
        """
        offered = set(candidates)
        stray: set[tuple[str, str]] = set()
        for (guid, result_name), name in exported.items():
            if not guid or (guid, result_name) in offered:
                continue
            stage = self.document.stage(guid)
            if stage is None:
                # Really gone: `remove_stage` drops these, and nothing puts them back.
                continue
            # An export stored before `result` existed names only its stage, so the row is
            # keyed by what that stage actually produces -- which is also the key it will
            # have once it is back at the top level, so the row does not split in two.
            stray.add((guid, result_name or stage_result_name(stage) or name))
        return stray

    def _on_changed(self, *_args) -> None:
        self.changed.emit()

    def apply(self) -> None:
        """Write the panel back. An export left unnamed takes the result's own name.

        Requiring a second name for the common case -- export `H1` as `H1` -- would be
        ceremony; the analyser still rejects a name that is not an identifier.
        """
        exports = []
        for guid, result_name, keep, name in self.rows:
            if not keep.isChecked():
                continue
            exports.append({
                "name": name.text().strip() or keep.text(),
                "stage_guid": guid,
                "result": result_name,
            })
        self.document.set_exports(exports)
