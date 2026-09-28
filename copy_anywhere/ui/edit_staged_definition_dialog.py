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

import html
from typing import NamedTuple, Optional, Sequence

from aqt.qt import (
    QGridLayout,
    QGuiApplication,
    QLabel,
    QPushButton,
    QRect,
    QSplitter,
    QTimer,
    QVBoxLayout,
    Qt,
    qtmajor,
)
from aqt.utils import showInfo

from ..logic.definition_schema import CopyDefinitionV2, is_format_2
from ..logic.flow_analysis import find_call_cycles, make_lookup
from ..logic.rename_locations import TRIGGERS_ANCHOR, split_key
from ..logic.rename_reconcile import drop_cleared_warnings
from ..shared.ui.scrollable_dialog import ScrollableQDialog
from .labels import wrapping
from .rename_indicator import RenameIndicator
from .rename_marks_banner import RenameMarksBanner, place_of
from .stage_document import StageDocument
from .stage_editor_context import (
    known_fields_for,
    make_note_types_for,
    trigger_name_blockers,
    unresolved_reference_warnings,
)
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

#: Room left below the dialog for its title bar and frame, which sit outside the size
#: `resize` sets. Without it a dialog as tall as the screen put Save and Cancel below the
#: screen's edge.
TITLE_BAR_ALLOWANCE = 64

#: The narrowest the dialog starts at, as a share of the screen: a definition whose stages
#: need little room still gets a preview wide enough to read a trace in.
MIN_WIDTH_SHARE = 0.7


class InitialSize(NamedTuple):
    width: int
    height: int
    #: How much of the width goes to the stage list, the rest going to the preview.
    stages_width: int


def initial_size(
    available: QRect, stages_width: int, preview_width: int, chrome_width: int
) -> InitialSize:
    """The size the dialog opens at on a screen whose free area is `available`.

    `stages_width` is what the stage list needs to show its widest row without a horizontal
    scroll bar, `preview_width` what the preview asks for, `chrome_width` the margins and
    splitter handle around them. The stage list gets what it needs before the preview gets
    more than it asks for: a width taken from the screen share alone left the list scrolling
    sideways with room to spare on the screen.
    """
    height = max(available.height() - TITLE_BAR_ALLOWANCE, 0)
    wanted = stages_width + preview_width + chrome_width
    width = min(max(wanted, int(available.width() * MIN_WIDTH_SHARE)), available.width())
    # The preview gets what it asks for and the stage list the rest, which the width above
    # already made at least `stages_width` wherever the screen has room for both. On a screen
    # too narrow for both, the stage list is the one that scrolls: the preview is what shows
    # the run, and it has no scroll bar of its own for width.
    stages = width - chrome_width - preview_width
    return InitialSize(width, height, max(stages, 0))


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
            copy_definition,
            lookup=make_lookup(self.all_definitions),
            known_fields=known_fields_for,
            unresolved_refs=unresolved_reference_warnings,
            trigger_names=trigger_name_blockers,
        )

        self.body = QVBoxLayout(self.inner_widget)

        # First, above everything it may be about: a definition with a blocking warning is
        # not run, which matters more than anything else the editor says about it.
        self.marks_banner = RenameMarksBanner(self.inner_widget, self.document)
        self.marks_banner.location_chosen.connect(self.focus_location)
        self.marks_banner.dismissed.connect(self.refresh_rename_indicators)
        self.body.addWidget(self.marks_banner)

        self.triggers_editor = TriggersEditor(
            self.inner_widget, self.document.definition, rename_document=self.document
        )
        self.triggers_editor.changed.connect(self.schedule_refresh)
        self.body.addWidget(self.triggers_editor)

        self.body.addWidget(QLabel("<h2>Stages</h2>", self.inner_widget))
        self.body.addWidget(
            wrapping(
                QLabel(
                    "<small>They run in this order. Each one can use anything named above"
                    " it.</small>",
                    self.inner_widget,
                )
            )
        )
        environment = StageEditorEnvironment(
            make_note_types_for(self.document.definition),
            self.all_definitions,
            self.document.definition.get("guid", ""),
            document=self.document,
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
        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        parent = self.parentWidget()
        screen = (parent.screen() if parent is not None else None) or (
            QGuiApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        vertical_bar = self.scroll_area.verticalScrollBar()
        stages_width = (
            self.inner_widget.sizeHint().width()
            + (vertical_bar.sizeHint().width() if vertical_bar is not None else 0)
            + 2 * self.scroll_area.frameWidth()
        )
        margins = self.main_layout.contentsMargins()
        chrome_width = margins.left() + margins.right() + self.splitter.handleWidth()
        size = initial_size(
            available, stages_width, self.preview.sizeHint().width(), chrome_width
        )
        self.resize(size.width, size.height)
        self.splitter.setSizes([size.stages_width, size.width - chrome_width - size.stages_width])
        # Placed rather than left to Qt, which centres a dialog on its parent: over a parent
        # near a screen edge, a dialog this big would hang off that edge. `move` places the
        # frame, so the top of the free area leaves the title bar inside the allowance.
        self.move(available.x() + (available.width() - size.width) // 2, available.y())

    # -- keeping the analysis current -------------------------------------------------------

    def schedule_refresh(self) -> None:
        self._refresh_timer.start()

    def done(self, result: int) -> None:
        # Both Save and Cancel come through here. A pending re-analysis must not outlive the
        # dialog: it reads the collection and rebuilds every field picker from it, which is
        # not something to be doing once the dialog the pickers belong to is on its way out.
        self._refresh_timer.stop()
        super().done(result)

    def apply_editors(self) -> None:
        """Fold every control in the dialog back into the definition."""
        self.triggers_editor.apply()
        self.stage_tree.apply_editors()
        self.exports_editor.apply()
        self.document.invalidate()

    def refresh_status(self) -> None:
        self.apply_editors()
        # Field pickers and the per-write unfocus boxes list the trigger note type's fields,
        # and the trigger editor that chooses it is in this same dialog, so they have to be
        # relisted here rather than only when a stage itself is edited.
        self.stage_tree.refresh_contexts()
        # The exports panel lists root results, which the stage list decides.
        self.exports_editor.rebuild()
        blockers = list(self.document.save_blockers())
        blockers.extend(self._cycle_blockers())
        self.ok_button.setEnabled(not blockers)
        self.status_label.setText(self._status_html(blockers))
        # Not everything that reaches here is an edit -- the dialog opening and Save come
        # through too -- so the preview is handed the definition and compares it with the
        # one it ran, rather than being told it is out of date (§9).
        self.preview.set_definition(self.document.definition)
        self.preview.mark_stale()
        # Each indicator follows its own part as it is typed in; this catches what changed
        # a part without the user typing, such as a field picker relisted above.
        self.refresh_rename_indicators()

    def refresh_rename_indicators(self) -> None:
        for indicator in self.findChildren(RenameIndicator):
            indicator.refresh()

    def focus_stage(self, stage_guid: str) -> None:
        """Open the stage a trace row stands for and scroll the list to it."""
        row = self.stage_tree.rows.get(stage_guid)
        if row is None:
            return
        self.stage_tree.expand(stage_guid)
        self.scroll_area.ensureWidgetVisible(row)

    def focus_location(self, key: str) -> None:
        """Open and scroll to the part a rename warning's location key names."""
        anchor, path = split_key(key)
        if anchor == TRIGGERS_ANCHOR:
            boxes = {
                "on_unfocus.edit_fields": self.triggers_editor.unfocus_edit,
                "on_unfocus.add_fields": self.triggers_editor.unfocus_add,
            }
            self.scroll_area.ensureWidgetVisible(boxes.get(path, self.triggers_editor))
            return
        stage_guid = place_of(self.document, key).stage_guid
        if stage_guid is None:
            return
        # A stage inside a loop or a condition is out of sight until they are open too.
        for ancestor in self.document.ancestors(stage_guid):
            self.stage_tree.expand(ancestor.get("guid", ""))
        self.focus_stage(stage_guid)

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
        # The document's list, not the analyser's: the add-note case is worded from the
        # triggers and named with stage paths, neither of which the analyser knows about.
        # Escaped: every message is plain text (the same lines go to `showInfo` as they
        # are) and many quote a name from the collection. A card type's is "Note<::>Card",
        # whose `<::>` rich text would swallow.
        warnings = self.document.warnings()
        if warnings:
            lines.append(
                "<span style='color: #b8860b'>Worth knowing:</span><ul>"
                + "".join(f"<li>{html.escape(warning, quote=False)}</li>" for warning in warnings)
                + "</ul>"
            )
        if blockers:
            lines.append(
                "<span style='color: #c0392b'>Cannot be saved yet:</span><ul>"
                + "".join(f"<li>{html.escape(blocker, quote=False)}</li>" for blocker in blockers)
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
        """The definition as it should be stored, with `effects` freshly derived and the
        rename warnings whose text the user fixed dropped (see
        "How a warning goes away" in `docs/staged-definitions.md`).

        Only a Save comes here (the picker asks only an accepted dialog), so a warning
        survives a Cancel however the text was left. The entries a Replace answered in a
        swap go too, unless the text was put back (`StageDocument.settle_rename_marks`).
        """
        self.apply_editors()
        return drop_cleared_warnings(
            self.document.to_definition(), self.document.settled_rename_marks()
        )
