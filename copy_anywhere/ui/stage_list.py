"""The ordered, single-column stage list that replaced the mode and section tabs.

One `StageTreeWidget` renders a whole definition. Structural stages render their children
as an indented block inside their own row, so the nesting the JSON has is the nesting the
user sees, and the visual order is the serialized order.

Editing a stage's *contents* never rebuilds anything: the editors write straight into the
stage dicts. Editing the *shape* -- adding, deleting, duplicating, moving, wrapping or
enabling a stage -- rebuilds the tree from the document, because every scope below the
change has moved and every menu built from one is now wrong.

Every row has a checkbox, and the bar above the list acts on the checked rows together:
wrapping a run of stages in a condition or a loop, moving them in or out of one, turning
them on or off, deleting them. The `⋮` menu offers the same moves for its own row alone.
Which rows are checked is kept by guid, like which are expanded, so it survives a rebuild.
A checked row is always one that can be seen: closing a block unchecks what is inside it,
and a move opens every block around where the stages land. The bar acts on what is checked
without asking, and that must not include a stage the user is not looking at.
"""

from typing import Iterable, Optional

from aqt.qt import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    Qt,
    pyqtSignal,
    qtmajor,
)

from ..logic.definition_schema import STAGE_CONDITION, Stage, stage_body_blocks, walk_stages
from ..shared.utils.block_signals import block_signals
from .discard import discard_widget
from .labels import ElidedLabel
from .stage_document import (
    BODY_KEY_LABELS,
    STAGE_TYPE_ICONS,
    STAGE_TYPE_LABELS,
    WRAP_STAGE_TYPES,
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
from .outline import outline_frame
from .stage_editors import StageEditorEnvironment, make_stage_editor

if qtmajor > 5:
    QSizePolicyPreferred = QSizePolicy.Policy.Preferred
    QSizePolicyMinimum = QSizePolicy.Policy.Minimum
    PlainText = Qt.TextFormat.PlainText
else:  # pragma: no cover -- Anki 2.1.49 and older
    QSizePolicyPreferred = QSizePolicy.Preferred  # type: ignore[attr-defined]
    QSizePolicyMinimum = QSizePolicy.Minimum  # type: ignore[attr-defined]
    PlainText = Qt.PlainText  # type: ignore[attr-defined]

#: How far one nesting level is indented, in pixels.
INDENT = 18


def add_submenu(menu: QMenu, title: str) -> QMenu:
    submenu = menu.addMenu(title)
    # addMenu(str) always creates and returns the submenu; the stubs type it optional.
    assert submenu is not None
    return submenu


def menu_text(text: str) -> str:
    """`text` as a menu entry that shows all of it.

    A menu reads `&x` as "x is this entry's shortcut" and draws no `&`. The entries here are
    built from what the user wrote, and a condition testing for `'&nbsp;'` was listed as one
    testing for `'nbsp;'`.
    """
    return text.replace("&", "&&")


def show_menu_below(anchor: QWidget, menu: QMenu) -> None:
    """Show a menu built for this one click, and let go of it afterwards.

    A menu is a child of the widget that built it and lives as long as that does. The bar
    above the list is there for as long as the dialog is, so without the delete it kept
    every menu it had ever opened, each with the stages it was built for.
    """
    menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
    menu.deleteLater()


def wrapper_noun(stage: Stage) -> str:
    """What a structural stage is called in a sentence: "the condition", "the loop"."""
    return "condition" if stage.get("type") == STAGE_CONDITION else "loop"


def fill_wrap_menu(menu: QMenu, tree: "StageTreeWidget", guids: list[str]) -> None:
    for stage_type in WRAP_STAGE_TYPES:
        menu.addAction(
            STAGE_TYPE_LABELS[stage_type],
            lambda t=stage_type: tree.wrap_stages(guids, t),
        )
    menu.setEnabled(tree.document.can_wrap(guids))


def fill_move_out_menu(menu: QMenu, tree: "StageTreeWidget", guids: list[str]) -> None:
    group = tree.document.sibling_group(guids)
    owner = tree.document.stage(group.parent_guid) if group and group.parent_guid else None
    if owner is None:
        menu.setEnabled(False)
        return
    noun = wrapper_noun(owner)
    menu.addAction(f"Above the {noun}", lambda: tree.move_stages_out(guids, below=False))
    menu.addAction(f"Below the {noun}", lambda: tree.move_stages_out(guids, below=True))


def fill_move_into_menu(menu: QMenu, tree: "StageTreeWidget", guids: list[str]) -> None:
    targets = tree.document.move_targets(*guids)
    for parent_guid, body_key, label in targets:
        menu.addAction(
            menu_text(label),
            lambda p=parent_guid, k=body_key: tree.move_stages_into(guids, p, k),
        )
    menu.setEnabled(bool(targets))


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
        outline_frame(self, "stageRow")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        header = QHBoxLayout()
        outer.addLayout(header)

        # A checkbox rather than a click-to-select row, so that being able to pick several
        # stages is visible before anyone goes looking for it.
        self.select_box = QCheckBox(self)
        self.select_box.setChecked(self.guid in tree.selected)
        self.select_box.setToolTip(
            "Check stages to wrap them in a condition or a loop, move them, turn them on or"
            " off, or delete them together, with the bar above the stages."
        )
        self.select_box.toggled.connect(lambda checked: tree.set_selected(self.guid, checked))
        header.addWidget(self.select_box)

        # The row's small buttons are QToolButtons sized by their one-character label, not
        # QPushButtons with a capped width: Anki's stylesheet pads every QPushButton by 15px a
        # side (25px on Windows), which inside a 28px button left no room for the label, so
        # in Anki they showed as blank squares.
        self.expand_button = QToolButton(self)
        self.expand_button.setText("▸")
        self.expand_button.setAutoRaise(True)
        self.expand_button.setToolTip("Show or hide this stage's settings")
        self.expand_button.clicked.connect(self._toggle)
        header.addWidget(self.expand_button)

        stage_type = stage.get("type", "")
        header.addWidget(QLabel(STAGE_TYPE_ICONS.get(stage_type, "•"), self))
        # The title stays on one line; a wrapped label reports a narrow preferred width, and
        # the layout then broke even a two-word stage name.
        self.title = QLabel(f"<b>{stage_label(stage)}</b>", self)
        header.addWidget(self.title)
        self.summary = ElidedLabel(stage_summary(stage), self)
        self.summary.setStyleSheet("color: palette(mid);")
        self.summary.setTextFormat(PlainText)
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

        self.up_button = QToolButton(self)
        self.up_button.setText("↑")
        self.up_button.setToolTip("Move this stage up")
        self.up_button.clicked.connect(lambda: tree.move_stage(self.guid, -1))
        header.addWidget(self.up_button)
        self.down_button = QToolButton(self)
        self.down_button.setText("↓")
        self.down_button.setToolTip("Move this stage down")
        self.down_button.clicked.connect(lambda: tree.move_stage(self.guid, 1))
        header.addWidget(self.down_button)

        self.menu_button = QToolButton(self)
        self.menu_button.setText("⋮")
        self.menu_button.setToolTip(
            "Duplicate, wrap in a condition or a loop, move into another block, or delete"
        )
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
        self.expand_button.setText("▾" if self.guid in tree.expanded else "▸")
        self.refresh_problems()

    # -- interaction ---------------------------------------------------------------------

    def _toggle(self) -> None:
        # `isHidden` rather than `isVisible`: a widget inside a window that has not been
        # shown yet reports `isVisible() == False` whatever it was asked to do, which would
        # make the first click on every row expand it again.
        self.set_expanded(self.body.isHidden())
        if self.guid not in self.tree.expanded:
            # Collapsing is the natural moment to fold a finished edit back in.
            self.tree.contents_changed()

    def set_expanded(self, expanded: bool, announce: bool = True) -> None:
        """Open or close this row's body.

        `announce` is off when something other than the user did the opening: the preview
        opens a stage when its trace row is chosen, and a row that answered by announcing
        itself would send the preview straight back to the row it just came from.
        """
        self.body.setVisible(expanded)
        self.expand_button.setText("▾" if expanded else "▸")
        if expanded:
            self.tree.expanded.add(self.guid)
            if announce:
                # Opening a stage is how the user says which one they are looking at, which
                # is also the question the preview's trace answers (§9).
                self.tree.stage_expanded.emit(self.guid)
        else:
            self.tree.expanded.discard(self.guid)
            # The stages in this row's blocks have just gone out of sight, and the bar acts
            # on whatever is checked: left checked, they were still counted, and Delete took
            # stages nobody was looking at.
            self.tree.deselect_inside(self.guid)

    def _on_enabled(self, checked: bool) -> None:
        self.tree.set_enabled(self.guid, checked)

    def build_menu(self) -> QMenu:
        """This row's own menu: what it does to this stage alone, checked or not."""
        menu = QMenu(self)
        guids = [self.guid]
        menu.addAction("Duplicate", lambda: self.tree.duplicate_stage(self.guid))
        fill_wrap_menu(add_submenu(menu, "Wrap in"), self.tree, guids)
        parent = self.tree.document.ancestors(self.guid)
        if parent:
            fill_move_out_menu(
                add_submenu(menu, menu_text(f"Move out of {stage_label(parent[-1])}")),
                self.tree,
                guids,
            )
        fill_move_into_menu(add_submenu(menu, "Move into"), self.tree, guids)
        if stage_body_blocks(self.stage):
            menu.addAction(
                f"Remove this {wrapper_noun(self.stage)}, keep its stages",
                lambda: self.tree.unwrap_stage(self.guid),
            )
        menu.addSeparator()
        menu.addAction("Delete", lambda: self.tree.remove_stage(self.guid))
        return menu

    def _show_menu(self) -> None:
        show_menu_below(self.menu_button, self.build_menu())

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
        show_menu_below(self.add_button, menu)


class StageSelectionBar(QWidget):
    """What can be done to the checked stages together.

    Always shown, with its buttons off until something is checked, so the list says what
    the checkboxes are for. A button that cannot act on what is checked says why in its
    tooltip rather than leaving the user to guess at the rule.
    """

    def __init__(self, parent: QWidget, tree: "StageTreeWidget") -> None:
        super().__init__(parent)
        self.tree = tree
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.count_label = QLabel("", self)
        layout.addWidget(self.count_label)

        def button(text: str, action) -> QToolButton:
            made = QToolButton(self)
            made.setText(text)
            made.clicked.connect(action)
            layout.addWidget(made)
            return made

        def menu_button(text: str, build_menu) -> QToolButton:
            made = QToolButton(self)
            made.setText(text)
            # Built at the click rather than once: what a menu offers depends on which
            # stages are checked and where they are now. The button is handed over rather
            # than read from `sender()`, which a lambda slot does not get to see.
            made.clicked.connect(lambda: show_menu_below(made, build_menu()))
            layout.addWidget(made)
            return made

        self.wrap_button = menu_button("Wrap in ▾", self.build_wrap_menu)
        self.move_out_button = menu_button("Move out ▾", self.build_move_out_menu)
        self.move_into_button = menu_button("Move into ▾", self.build_move_into_menu)
        self.on_button = button(
            "Turn on", lambda: tree.set_stages_enabled(tree.selected_guids(), True)
        )
        self.off_button = button(
            "Turn off", lambda: tree.set_stages_enabled(tree.selected_guids(), False)
        )
        self.delete_button = button("Delete", lambda: tree.remove_stages(tree.selected_guids()))
        self.clear_button = button("✕", tree.clear_selection)
        self.clear_button.setToolTip("Uncheck all")
        layout.addStretch()
        self.refresh()

    def build_wrap_menu(self) -> QMenu:
        menu = QMenu(self)
        fill_wrap_menu(menu, self.tree, self.tree.selected_guids())
        return menu

    def build_move_out_menu(self) -> QMenu:
        menu = QMenu(self)
        fill_move_out_menu(menu, self.tree, self.tree.selected_guids())
        return menu

    def build_move_into_menu(self) -> QMenu:
        menu = QMenu(self)
        fill_move_into_menu(menu, self.tree, self.tree.selected_guids())
        return menu

    def refresh(self) -> None:
        guids = self.tree.selected_guids()
        document = self.tree.document
        count = len(guids)
        # Short, like the button labels: this bar is the widest thing in the stage list, and
        # its width is what the dialog opens at.
        self.count_label.setText(f"{count} checked" if count else "None checked")
        group = document.sibling_group(guids) if guids else None
        nothing = "Check the stages to act on first."
        spread = "Only stages in the same block can be moved or wrapped together."
        if not guids:
            wrap_why = move_why = out_why = nothing
        elif group is None:
            wrap_why = move_why = out_why = spread
        else:
            wrap_why = (
                ""
                if document.can_wrap(guids)
                else "Only stages next to each other can be wrapped together."
            )
            move_why = "" if document.move_targets(*guids) else "There is no block to move into."
            out_why = (
                "" if group.parent_guid else "The checked stages are already at the top level."
            )
        for one, why, tip in (
            (
                self.wrap_button,
                wrap_why,
                "Put the checked stages inside a new condition or loop, where they stand.",
            ),
            (
                self.move_out_button,
                out_why,
                "Move the checked stages out of the condition or loop they are in.",
            ),
            (
                self.move_into_button,
                move_why,
                "Move the checked stages into a condition or loop, next to where they are.",
            ),
        ):
            one.setEnabled(not why)
            one.setToolTip(why or tip)
        for one in (self.on_button, self.off_button, self.delete_button, self.clear_button):
            one.setEnabled(bool(guids))


class StageTreeWidget(QWidget):
    """The whole ordered stage list, and the only thing that changes its shape."""

    #: The definition changed in a way that affects whether it can be saved.
    definition_changed = pyqtSignal()
    #: A stage was opened. The preview pane uses this to show that stage's trace.
    stage_expanded = pyqtSignal(str)

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
        self.selected: set[str] = set()
        self.rows: dict[str, StageRow] = {}
        self.contexts: dict[str, StageEditorContext] = {}
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(0, 0, 0, 0)
        self.selection_bar = StageSelectionBar(self, self)
        self.layout_box.addWidget(self.selection_bar)
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
            discard_widget(self.root_block)
        self.rows = {}
        self.selected &= {
            stage.get("guid", "") for stage in walk_stages(self.document.root_block())
        }
        self.contexts = build_contexts(
            self.document, make_note_types_for(self.document.definition)
        )
        self.root_block = StageBlockWidget(self, self, None, None)
        self.layout_box.addWidget(self.root_block)
        self.selection_bar.refresh()
        self.definition_changed.emit()

    def expand(self, guid: str) -> None:
        """Open one stage because something other than the user asked for it."""
        row = self.rows.get(guid)
        if row is not None:
            row.set_expanded(True, announce=False)

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
        self.refresh_contexts()
        self.definition_changed.emit()

    def refresh_contexts(self) -> None:
        """Relist every open editor's menus against the definition as it now is.

        Separate from `contents_changed` because the definition can change from outside the
        stage list -- the trigger note type is chosen at the top of the same dialog, and the
        field pickers below it are built from exactly that.
        """
        self.contexts = build_contexts(
            self.document, make_note_types_for(self.document.definition)
        )
        for guid, row in self.rows.items():
            row.refresh_summary()
            row.refresh_problems()
            context = self.contexts.get(guid)
            if context is not None:
                row.set_context(context)
        # Relisting a menu can change what it holds -- a field the new trigger note type does
        # not have is dropped rather than left selected -- so the stage dicts are written
        # again from the widgets as they now are. Without it the stage would keep naming a
        # field the picker no longer offers, which is the state this exists to prevent.
        self.apply_editors()

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

    def set_enabled(self, guid: str, enabled: bool) -> None:
        self.apply_editors()
        self.document.set_enabled(guid, enabled)
        self.rebuild()

    # -- several stages at once ----------------------------------------------------------
    #
    # A move keeps the stages checked: they are still there, and what to do with them next
    # is often another move. Moving them into a block opens it and every block around it,
    # and wrapping them opens the new stage, since checked stages hidden inside a closed
    # block would leave the bar counting stages nobody can see. Closing a block by hand
    # unchecks what is in it, for the same reason.

    def selected_guids(self) -> list[str]:
        """The checked stages, in the order they run."""
        return [
            stage.get("guid", "")
            for stage in walk_stages(self.document.root_block())
            if stage.get("guid") in self.selected
        ]

    def set_selected(self, guid: str, selected: bool) -> None:
        if selected:
            self.selected.add(guid)
        else:
            self.selected.discard(guid)
        self.selection_bar.refresh()

    def deselect(self, guids: Iterable[str]) -> None:
        """Uncheck these stages, in what is remembered and in their rows."""
        for guid in guids:
            self.selected.discard(guid)
            row = self.rows.get(guid)
            if row is not None:
                # Blocked so that the bar is refreshed once, below, and not once per box.
                with block_signals(row.select_box):
                    row.select_box.setChecked(False)
        self.selection_bar.refresh()

    def clear_selection(self) -> None:
        self.deselect(list(self.selected))

    def deselect_inside(self, guid: str) -> None:
        """Uncheck every stage in this stage's blocks, at any depth. The stage itself keeps
        its check: its row is still on screen when its blocks are closed."""
        stage = self.document.stage(guid)
        if stage is None:
            return
        inside = {
            child.get("guid", "")
            for _key, block in stage_body_blocks(stage)
            for child in walk_stages(block)
        }
        if inside & self.selected:
            self.deselect(inside)

    def wrap_stages(self, guids: list[str], stage_type: str) -> None:
        self.apply_editors()
        wrapper = self.document.wrap(guids, stage_type)
        if wrapper is not None:
            # The new stage is empty and needs a predicate or a list before it can run.
            self.expanded.add(wrapper.get("guid", ""))
            self.rebuild()

    def unwrap_stage(self, guid: str) -> None:
        self.apply_editors()
        if self.document.unwrap(guid):
            self.expanded.discard(guid)
            self.rebuild()

    def move_stages_out(self, guids: list[str], below: bool) -> None:
        self.apply_editors()
        if self.document.move_out(guids, below):
            self.rebuild()

    def move_stages_into(self, guids: list[str], parent_guid, body_key) -> None:
        self.apply_editors()
        if self.document.move_into(guids, parent_guid, body_key):
            if parent_guid is not None:
                # Not the block alone: the menu lists it whether or not it is on screen, and
                # inside a closed block the stages moved out of sight, still checked.
                self.expanded.add(parent_guid)
                self.expanded.update(
                    stage.get("guid", "") for stage in self.document.ancestors(parent_guid)
                )
            self.rebuild()

    def remove_stages(self, guids: list[str]) -> None:
        self.apply_editors()
        self.document.remove_stages(guids)
        self.rebuild()

    def set_stages_enabled(self, guids: list[str], enabled: bool) -> None:
        self.apply_editors()
        for guid in guids:
            self.document.set_enabled(guid, enabled)
        self.rebuild()
