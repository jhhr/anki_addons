"""The format-2 definition editor: trigger settings, an ordered stage list, exports.

There are no mode tabs and no section tabs. A format-1 definition had to put its variables,
its condition, its query, its field writes and its file writes in fixed places because the
mode decided what each of those places meant; a staged definition says so itself, in order,
so the editor is the order.

Save is blocked while the analyser has anything to complain about (§10). The blockers are
listed rather than hidden behind the button: a definition that cannot be saved should say
which stage is why.

The right half is the preview (§9): the same definition run against a note the user picks,
committing nothing. It is not rerun as the definition is edited -- a query or a nested call
is far too expensive to run on a keystroke -- so an edit marks it stale and the user reruns
it when they want the answer.
"""

from typing import Optional, Sequence

from aqt.qt import (
    QGridLayout,
    QGuiApplication,
    QLabel,
    QPushButton,
    QSplitter,
    QTimer,
    QVBoxLayout,
    Qt,
    qtmajor,
)
from aqt.utils import showInfo

from ..logic.definition_schema import CopyDefinitionV2, is_format_2
from ..logic.flow_analysis import find_call_cycles, make_lookup
from ..shared.ui.scrollable_dialog import ScrollableQDialog
from .stage_document import StageDocument
from .stage_editor_context import make_note_types_for
from .stage_editors import StageEditorEnvironment
from .stage_exports_editor import ExportsEditor
from .stage_list import StageTreeWidget
from .stage_preview import PreviewPane
from .stage_triggers_editor import TriggersEditor

if qtmajor > 5:
    WindowModal = Qt.WindowModality.WindowModal
    Horizontal = Qt.Orientation.Horizontal
else:  # pragma: no cover -- Anki 2.1.49 and older
    WindowModal = Qt.WindowModal  # type: ignore[attr-defined]
    Horizontal = Qt.Horizontal  # type: ignore[attr-defined]

#: How long after the last keystroke the definition is re-analysed. Analysis is pure and
#: cheap, but rebuilding an interpolation menu walks every note type, so it is not free
#: enough to do on every character.
REANALYSE_DELAY_MS = 300


class EditStagedDefinitionDialog(ScrollableQDialog):
    """Edits one format-2 copy definition."""

    def __init__(
        self,
        parent,
        copy_definition: Optional[CopyDefinitionV2] = None,
        all_definitions: Optional[Sequence[CopyDefinitionV2]] = None,
    ) -> None:
        self.ok_button = QPushButton("Save")
        self.close_button = QPushButton("Cancel")
        self.ok_button.clicked.connect(self.check_fields)
        self.close_button.clicked.connect(self.reject)

        footer = QGridLayout()
        footer.setColumnMinimumWidth(0, 150)
        footer.setColumnMinimumWidth(2, 150)
        footer.addWidget(self.ok_button, 0, 0)
        footer.addWidget(self.close_button, 0, 2)
        super().__init__(parent, footer_layout=footer)
        self.setWindowModality(WindowModal)

        self.all_definitions = [
            definition for definition in (all_definitions or []) if is_format_2(definition)
        ]
        self.document = StageDocument(
            copy_definition, lookup=make_lookup(self.all_definitions)
        )

        self.body = QVBoxLayout(self.inner_widget)

        self.triggers_editor = TriggersEditor(self.inner_widget, self.document.definition)
        self.triggers_editor.changed.connect(self.schedule_refresh)
        self.body.addWidget(self.triggers_editor)

        self.body.addWidget(QLabel("<h2>Stages</h2>", self.inner_widget))
        self.body.addWidget(
            QLabel(
                "<small>They run in this order. Each one can use anything named above"
                " it.</small>",
                self.inner_widget,
            )
        )
        environment = StageEditorEnvironment(
            make_note_types_for(self.document.definition),
            self.all_definitions,
            self.document.definition.get("guid", ""),
        )
        self.stage_tree = StageTreeWidget(self.inner_widget, self.document, environment)
        self.stage_tree.definition_changed.connect(self.schedule_refresh)
        self.body.addWidget(self.stage_tree)

        self.body.addWidget(QLabel("<h2>Exports</h2>", self.inner_widget))
        self.exports_editor = ExportsEditor(self.inner_widget, self.document)
        self.exports_editor.changed.connect(self.schedule_refresh)
        self.body.addWidget(self.exports_editor)

        self.status_label = QLabel("", self.inner_widget)
        self.status_label.setWordWrap(True)
        self.body.addWidget(self.status_label)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REANALYSE_DELAY_MS)
        self._refresh_timer.timeout.connect(self.refresh_status)

        # The stage list keeps the scroll area it was written for; the preview sits beside
        # it in a splitter so a long definition and a long trace do not fight for height.
        self.preview = PreviewPane(
            self, self.document.definition, self.all_definitions
        )
        self.preview.stage_selected.connect(self.focus_stage)
        self.stage_tree.stage_expanded.connect(self.preview.show_stage)
        self.splitter = QSplitter(Horizontal, self)
        self.main_layout.removeWidget(self.scroll_area)
        self.splitter.addWidget(self.scroll_area)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.main_layout.insertWidget(0, self.splitter)

        self.refresh_status()

        screen = QGuiApplication.primaryScreen()
        if screen:
            geometry = screen.availableGeometry()
            self.resize(
                max(self.sizeHint().width(), int(geometry.width() * 0.7)),
                int(min(self.sizeHint().height() * 1.5, geometry.height())),
            )

    # -- keeping the analysis current -------------------------------------------------------

    def schedule_refresh(self) -> None:
        self._refresh_timer.start()

    def apply_editors(self) -> None:
        """Fold every control in the dialog back into the definition."""
        self.triggers_editor.apply()
        self.stage_tree.apply_editors()
        self.exports_editor.apply()
        self.document.invalidate()

    def refresh_status(self) -> None:
        self.apply_editors()
        # The exports panel lists root results, which the stage list decides.
        self.exports_editor.rebuild()
        blockers = list(self.document.save_blockers())
        blockers.extend(self._cycle_blockers())
        self.ok_button.setEnabled(not blockers)
        self.status_label.setText(self._status_html(blockers))
        # Anything that reached here changed the definition, so whatever the preview last
        # ran is no longer what this definition does (§9).
        self.preview.set_definition(self.document.definition)
        self.preview.mark_stale()

    def focus_stage(self, stage_guid: str) -> None:
        """Open the stage a trace row stands for and scroll the list to it."""
        row = self.stage_tree.rows.get(stage_guid)
        if row is None:
            return
        self.stage_tree.expand(stage_guid)
        self.scroll_area.ensureWidgetVisible(row)

    def _cycle_blockers(self) -> list[str]:
        """Call cycles, checked across the whole config rather than one definition.

        `analyze_definition` already refuses to follow a cycle, but it reports it from
        wherever it noticed; naming the whole path is what lets the user break it.
        """
        definitions = [
            definition
            for definition in self.all_definitions
            if definition.get("guid") != self.document.definition.get("guid")
        ]
        definitions.append(self.document.to_definition())
        names = {
            definition.get("guid"): definition.get("definition_name") or definition.get("guid")
            for definition in definitions
        }
        own_guid = self.document.definition.get("guid")
        blockers = []
        for cycle in find_call_cycles(definitions):
            if own_guid in cycle:
                path = " → ".join(str(names.get(guid, guid)) for guid in cycle)
                blockers.append(f"These definitions call each other in a circle: {path}")
        return blockers

    def _status_html(self, blockers: Sequence[str]) -> str:
        compatible = self.document.add_note_compatible()
        lines = [
            "Can run while a note is being added: <b>{}</b>".format("yes" if compatible else "no")
        ]
        warnings = [problem.message for problem in self.document.analysis.warnings]
        if warnings:
            lines.append(
                "<span style='color: #b8860b'>Worth knowing:</span><ul>"
                + "".join(f"<li>{warning}</li>" for warning in warnings)
                + "</ul>"
            )
        if blockers:
            lines.append(
                "<span style='color: #c0392b'>Cannot be saved yet:</span><ul>"
                + "".join(f"<li>{blocker}</li>" for blocker in blockers)
                + "</ul>"
            )
        return "<br>".join(lines)

    # -- saving ------------------------------------------------------------------------------

    def check_fields(self) -> None:
        self.refresh_status()
        blockers = list(self.document.save_blockers()) + self._cycle_blockers()
        if blockers:
            showInfo("This definition cannot be saved yet:\n\n" + "\n".join(blockers))
            return
        name = self.document.definition.get("definition_name", "")
        clashes = [
            definition
            for definition in self.all_definitions
            if definition.get("definition_name") == name
            and definition.get("guid") != self.document.definition.get("guid")
        ]
        if clashes:
            showInfo(
                "There is another copy definition with the same name. Please choose a unique"
                " name."
            )
            return
        self.accept()

    def get_copy_definition(self) -> CopyDefinitionV2:
        """The definition as it should be stored, with `effects` freshly derived."""
        self.apply_editors()
        return self.document.to_definition()
