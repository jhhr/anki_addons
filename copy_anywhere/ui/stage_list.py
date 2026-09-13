"""The ordered, single-column stage list that replaced the mode and section tabs.

One `StageTreeWidget` renders a whole definition. Structural stages render their children
as an indented block inside their own row, so the nesting the JSON has is the nesting the
user sees, and the visual order is the serialized order.

Editing a stage's *contents* never rebuilds anything: the editors write straight into the
stage dicts. Editing the *shape* -- adding, deleting, duplicating, moving, or enabling a
stage -- rebuilds the tree from the document, because every scope below the change has
moved and every menu built from one is now wrong.
"""

from typing import Optional

from aqt.qt import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
    qtmajor,
)

from ..logic.definition_schema import Stage, stage_body_blocks
from .stage_document import (
    BODY_KEY_LABELS,
    STAGE_TYPE_ICONS,
    STAGE_TYPE_LABELS,
    StageDocument,
    stage_label,
    stage_summary,
)
from .stage_editor_context import (
    StageEditorContext,
    build_contexts,
    context_after,
    make_note_types_for,
)
from .stage_editors import StageEditorEnvironment, make_stage_editor

if qtmajor > 5:
    QFrameStyledPanel = QFrame.Shape.StyledPanel
    QSizePolicyPreferred = QSizePolicy.Policy.Preferred
    QSizePolicyMinimum = QSizePolicy.Policy.Minimum
else:  # pragma: no cover -- Anki 2.1.49 and older
    QFrameStyledPanel = QFrame.StyledPanel  # type: ignore[attr-defined]
    QSizePolicyPreferred = QSizePolicy.Preferred  # type: ignore[attr-defined]
    QSizePolicyMinimum = QSizePolicy.Minimum  # type: ignore[attr-defined]

#: How far one nesting level is indented, in pixels.
INDENT = 18


class StageRow(QFrame):
    """One stage: a header that says what it does, and a body that lets you change it."""

    def __init__(
        self,
        parent: QWidget,
        tree: "StageTreeWidget",
        stage: Stage,
        context: StageEditorContext,
    ) -> None:
        super().__init__(parent)
        self.tree = tree
        self.stage = stage
        self.guid = stage.get("guid", "")
        self.setFrameShape(QFrameStyledPanel)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        header = QHBoxLayout()
        outer.addLayout(header)

        self.expand_button = QPushButton("▸", self)
        self.expand_button.setFlat(True)
        self.expand_button.setMaximumWidth(24)
        self.expand_button.clicked.connect(self._toggle)
        header.addWidget(self.expand_button)

        stage_type = stage.get("type", "")
        header.addWidget(QLabel(STAGE_TYPE_ICONS.get(stage_type, "•"), self))
        self.title = QLabel(f"<b>{stage_label(stage)}</b>", self)
        header.addWidget(self.title)
        self.summary = QLabel(stage_summary(stage), self)
        self.summary.setStyleSheet("color: palette(mid);")
        self.summary.setSizePolicy(QSizePolicyPreferred, QSizePolicyMinimum)
        header.addWidget(self.summary, 1)

        self.problem_label = QLabel("", self)
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #c0392b;")
        header.addWidget(self.problem_label)

        self.enabled_box = QCheckBox("On", self)
        self.enabled_box.setChecked(stage.get("enabled", True))
        self.enabled_box.setToolTip(
            "A stage that is off stays here and stays editable, but does not run and"
            " produces no result."
        )
        self.enabled_box.toggled.connect(self._on_enabled)
        header.addWidget(self.enabled_box)

        self.up_button = QPushButton("↑", self)
        self.up_button.setMaximumWidth(28)
        self.up_button.clicked.connect(lambda: tree.move_stage(self.guid, -1))
        header.addWidget(self.up_button)
        self.down_button = QPushButton("↓", self)
        self.down_button.setMaximumWidth(28)
        self.down_button.clicked.connect(lambda: tree.move_stage(self.guid, 1))
        header.addWidget(self.down_button)

        self.menu_button = QPushButton("⋮", self)
        self.menu_button.setMaximumWidth(28)
        self.menu_button.clicked.connect(self._show_menu)
        header.addWidget(self.menu_button)

        self.body = QWidget(self)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(INDENT, 0, 0, 0)
        outer.addWidget(self.body)

        self.editor = make_stage_editor(self.body, stage, context, tree.environment)
        if self.editor is not None:
            self.editor.changed.connect(tree.contents_changed)
            body_layout.addWidget(self.editor)
        else:
            body_layout.addWidget(
                QLabel(
                    f"<b>Unknown stage type '{stage_type}'.</b> This definition cannot run"
                    " until it is removed.",
                    self.body,
                )
            )

        self.child_blocks: list[StageBlockWidget] = []
        for key, _block in stage_body_blocks(stage):
            label = QLabel(f"<b>{BODY_KEY_LABELS.get(key, key)}</b>", self.body)
            body_layout.addWidget(label)
            child = StageBlockWidget(self.body, tree, self.guid, key)
            body_layout.addWidget(child)
            self.child_blocks.append(child)

        self.body.setVisible(self.guid in tree.expanded)
        self.expand_button.setText("▾" if self.body.isVisible() else "▸")
        self.refresh_problems()

    # -- interaction ---------------------------------------------------------------------

    def _toggle(self) -> None:
        # `isHidden` rather than `isVisible`: a widget inside a window that has not been
        # shown yet reports `isVisible() == False` whatever it was asked to do, which would
        # make the first click on every row expand it again.
        visible = self.body.isHidden()
        self.body.setVisible(visible)
        self.expand_button.setText("▾" if visible else "▸")
        if visible:
            self.tree.expanded.add(self.guid)
        else:
            self.tree.expanded.discard(self.guid)
            # Collapsing is the natural moment to fold a finished edit back in.
            self.tree.contents_changed()

    def _on_enabled(self, checked: bool) -> None:
        self.tree.set_enabled(self.guid, checked)

    def _show_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("Duplicate", lambda: self.tree.duplicate_stage(self.guid))
        move_menu = menu.addMenu("Move into")
        targets = self.tree.document.move_targets(self.guid)
        if not targets:
            move_menu.setEnabled(False)
        for parent_guid, body_key, label in targets:
            move_menu.addAction(
                label,
                lambda p=parent_guid, k=body_key: self.tree.move_stage_into(self.guid, p, k),
            )
        menu.addSeparator()
        menu.addAction("Delete", lambda: self.tree.remove_stage(self.guid))
        menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft()))

    # -- refresh -------------------------------------------------------------------------

    def refresh_summary(self) -> None:
        self.title.setText(f"<b>{stage_label(self.stage)}</b>")
        self.summary.setText(stage_summary(self.stage))

    def refresh_problems(self) -> None:
        problems = self.tree.document.problems_for(self.guid)
        warnings = self.tree.document.warnings_for(self.guid)
        if problems:
            self.problem_label.setText(
                "⚠ " + "; ".join(problem.message for problem in problems)
            )
        elif warnings:
            self.problem_label.setText("• " + "; ".join(one.message for one in warnings))
            self.problem_label.setStyleSheet("color: #b8860b;")
        else:
            self.problem_label.setText("")

    def set_context(self, context: StageEditorContext) -> None:
        if self.editor is not None:
            self.editor.set_context(context)

    def apply(self) -> None:
        if self.editor is not None:
            self.editor.apply()


class StageBlockWidget(QWidget):
    """One ordered block of stages, with the Add Stage button that appends to it."""

    def __init__(
        self,
        parent: QWidget,
        tree: "StageTreeWidget",
        parent_guid: Optional[str],
        body_key: Optional[str],
    ) -> None:
        super().__init__(parent)
        self.tree = tree
        self.parent_guid = parent_guid
        self.body_key = body_key
        self.rows: list[StageRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        for stage in tree.document.block(parent_guid, body_key) or []:
            context = tree.contexts.get(stage.get("guid", ""))
            if context is None:
                continue
            row = StageRow(self, tree, stage, context)
            layout.addWidget(row)
            self.rows.append(row)
            tree.rows[row.guid] = row

        self.add_button = QPushButton("Add stage…", self)
        self.add_button.clicked.connect(self._show_add_menu)
        layout.addWidget(self.add_button)
        layout.addStretch()

    def _show_add_menu(self) -> None:
        context = context_after(
            self.tree.document, self.tree.contexts, self.parent_guid, self.body_key
        )
        menu = QMenu(self)
        for stage_type in context.available_stage_types():
            menu.addAction(
                STAGE_TYPE_LABELS[stage_type],
                lambda t=stage_type: self.tree.add_stage(t, self.parent_guid, self.body_key),
            )
        menu.exec(self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))


class StageTreeWidget(QWidget):
    """The whole ordered stage list, and the only thing that changes its shape."""

    #: The definition changed in a way that affects whether it can be saved.
    definition_changed = pyqtSignal()

    def __init__(
        self,
        parent: Optional[QWidget],
        document: StageDocument,
        environment: StageEditorEnvironment,
    ) -> None:
        super().__init__(parent)
        self.document = document
        self.environment = environment
        self.expanded: set[str] = set()
        self.rows: dict[str, StageRow] = {}
        self.contexts: dict[str, StageEditorContext] = {}
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(0, 0, 0, 0)
        self.root_block: Optional[StageBlockWidget] = None
        self.rebuild()

    # -- building ------------------------------------------------------------------------

    def rebuild(self) -> None:
        """Throw every row away and build them again from the document.

        Cheaper alternatives exist, but a stage's editors are built from the scope in front
        of it, and any change to the shape of the definition moves that scope for
        everything below. Rebuilding is how the menus stay true.
        """
        if self.root_block is not None:
            self.layout_box.removeWidget(self.root_block)
            self.root_block.deleteLater()
        self.rows = {}
        self.contexts = build_contexts(
            self.document, make_note_types_for(self.document.definition)
        )
        self.root_block = StageBlockWidget(self, self, None, None)
        self.layout_box.addWidget(self.root_block)
        self.definition_changed.emit()

    def apply_editors(self) -> None:
        """Fold every open editor's widgets back into the stage dicts."""
        for row in self.rows.values():
            row.apply()
        self.document.invalidate()

    def contents_changed(self) -> None:
        """A stage's contents were edited: re-analyse and refresh, but keep the widgets.

        Menus are updated in place rather than rebuilt because the user is very likely
        still typing into one of them.
        """
        self.apply_editors()
        self.contexts = build_contexts(
            self.document, make_note_types_for(self.document.definition)
        )
        for guid, row in self.rows.items():
            row.refresh_summary()
            row.refresh_problems()
            context = self.contexts.get(guid)
            if context is not None:
                row.set_context(context)
        self.definition_changed.emit()

    # -- structural edits ----------------------------------------------------------------

    def add_stage(self, stage_type: str, parent_guid, body_key) -> None:
        self.apply_editors()
        stage = self.document.add_stage(stage_type, parent_guid, body_key)
        if stage is not None:
            # A stage the user just asked for should be open: it is empty, and every one of
            # them needs something filled in.
            self.expanded.add(stage.get("guid", ""))
        self.rebuild()

    def remove_stage(self, guid: str) -> None:
        self.apply_editors()
        self.document.remove_stage(guid)
        self.expanded.discard(guid)
        self.rebuild()

    def duplicate_stage(self, guid: str) -> None:
        self.apply_editors()
        copy = self.document.duplicate_stage(guid)
        if copy is not None:
            self.expanded.add(copy.get("guid", ""))
        self.rebuild()

    def move_stage(self, guid: str, offset: int) -> None:
        self.apply_editors()
        if self.document.move_within_block(guid, offset):
            self.rebuild()

    def move_stage_into(self, guid: str, parent_guid, body_key) -> None:
        self.apply_editors()
        if self.document.move_to(guid, parent_guid, body_key):
            self.rebuild()

    def set_enabled(self, guid: str, enabled: bool) -> None:
        self.apply_editors()
        self.document.set_enabled(guid, enabled)
        self.rebuild()
