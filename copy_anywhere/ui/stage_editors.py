"""One editor per stage type.

Each class edits the stage dict it was handed, in place, when `apply()` is called. None of
them owns any state the stage does not: the dialog can throw a whole row away and rebuild
it from the definition, which is what happens whenever a stage moves.

The controls themselves are the format-1 ones wherever there is one to reuse -- the
interpolated text edit, the code editor, the process chain, the tag editor, the card action
editor -- reached through `StageEditState`, which is what those widgets think an `EditState`
is. What is new here is only the arrangement: a stage names the note it reads and the note
it writes, so every editor starts with the binding it acts on.
"""

from typing import Callable, Optional, Sequence

from anki.models import NotetypeDict
from aqt import mw
from aqt.qt import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)

from ..configuration import (
    ALL_FIELD_TO_FIELD_PROCESS_NAMES,
    ALL_FIELD_TO_VARIABLE_PROCESS_NAMES,
)
from ..logic.definition_schema import (
    CopyDefinitionV2,
    IF_EMPTY_POLICIES,
    IF_MISSING_POLICIES,
    LIST_ITEM_TYPE_NAMES,
    SELECTION_STRATEGIES,
    SORT_ORDERS,
    STAGE_CALL_DEFINITION,
    STAGE_CARD_QUERY,
    STAGE_CONDITION,
    STAGE_EDIT_CARD,
    STAGE_EDIT_NOTE,
    STAGE_FOR_EACH_CARD,
    STAGE_FOR_EACH_NOTE,
    STAGE_LIST_VARIABLE,
    STAGE_NOTE_QUERY,
    STAGE_READ_FILE,
    STAGE_REDUCE,
    STAGE_STORE,
    STAGE_VARIABLE,
    STAGE_WRITE_FILE,
    Stage,
    TEXT,
    WRITE_IF_POLICIES,
    value_expression,
)
from ..shared.ui.grouped_combo_box import GroupedComboBox
from ..shared.ui.multi_combo_box import MultiComboBox
from ..shared.ui.required_combobox import RequiredCombobox
from ..shared.ui.required_text_input import RequiredLineEdit
from .card_actions_editor import CardActionsEditor
from .code_notices import FILE_CODE_NOTICE
from .stage_edit_state import StageEditState
from .stage_editor_context import NoteTypesFor, StageEditorContext
from .stage_triggers_editor import quoted_items, selected_names
from .tag_editor import TagEditor
from .value_expression_editor import ValueExpressionEditor

#: What the write_if choices are called in the editor.
WRITE_IF_LABELS = {"always": "always", "empty": "only if the field is empty"}

#: What a reduce does with its list. A join is what the migrator builds for format 1's
#: implicit many-notes join; a fold is what format 2 adds.
REDUCE_OPERATIONS = ("join", "fold")
REDUCE_OPERATION_LABELS = {
    "join": "join them into one text",
    "fold": "fold them with an expression",
}

#: The empty-result policies, in menu order, with what they mean spelled out.
IF_EMPTY_LABELS = {
    "continue": "carry on with an empty result",
    "skip_block": "stop running the rest of this block",
    "error": "fail the definition",
}

IF_MISSING_LABELS = {
    "empty": "use an empty string",
    "skip_block": "stop running the rest of this block",
    "error": "fail the definition",
}

SELECTION_LABELS = {
    "all": "all of them",
    "first": "the first",
    "random": "a random",
}


class StageEditorEnvironment:
    """What every stage editor needs beyond its own stage.

    `definitions` holds the other definitions in the config, which only the call stage
    needs -- but it needs their exports, so it needs the definitions themselves.
    """

    def __init__(
        self,
        note_types_for: NoteTypesFor,
        definitions: Optional[Sequence[CopyDefinitionV2]] = None,
        own_guid: str = "",
    ) -> None:
        self.note_types_for = note_types_for
        self.definitions = list(definitions or [])
        self.own_guid = own_guid

    def definition(self, guid: str) -> Optional[CopyDefinitionV2]:
        for definition in self.definitions:
            if definition.get("guid") == guid:
                return definition
        return None

    def callable_definitions(self) -> list[CopyDefinitionV2]:
        """Every definition a call stage may name: all of them but this one.

        Deeper cycles are not filtered here. The analyser reports them with the whole path,
        which says more than a silently shorter menu would.
        """
        return [
            definition
            for definition in self.definitions
            if definition.get("guid") and definition.get("guid") != self.own_guid
        ]


# --------------------------------------------------------------------------------------
# Small shared controls
# --------------------------------------------------------------------------------------


def binding_combo(
    parent: QWidget, names: Sequence[str], current: str, placeholder: str
) -> RequiredCombobox:
    """A combo over binding names.

    A binding the stage already names but that is no longer in scope is still listed, and
    still selected: §6 says a reference that broke stays visible and marked rather than
    being silently swapped for something else.
    """
    combo = RequiredCombobox(parent, placeholder_text=placeholder, is_required=True)
    options = list(names)
    if current and current not in options:
        options.append(current)
    combo.addItems(options)
    if current:
        combo.setCurrentText(current)
    else:
        combo.setCurrentIndex(-1)
    return combo


def labelled_combo(
    parent: QWidget, values: Sequence[str], labels: dict, current: str
) -> QComboBox:
    """A combo whose items show a phrase but carry the persisted value."""
    combo = QComboBox(parent)
    for value in values:
        combo.addItem(labels.get(value, value), value)
    index = combo.findData(current)
    combo.setCurrentIndex(index if index >= 0 else 0)
    return combo


def combo_value(combo: QComboBox) -> str:
    data = combo.currentData()
    return data if isinstance(data, str) else combo.currentText()


def note_types_of(binding: str, note_types_for: NoteTypesFor) -> list[NotetypeDict]:
    """The note types a binding may hold, with None ("any") expanded to all of them."""
    resolved = note_types_for(binding)
    if resolved is not None:
        return list(resolved)
    assert mw is not None and mw.col is not None
    return [model for model in mw.col.models.all()]


def field_combo(
    parent: QWidget, binding: str, note_types_for: NoteTypesFor, current: str
) -> GroupedComboBox:
    """A field picker for whichever note types the target binding may hold."""
    combo = GroupedComboBox(parent, placeholder_text="Select a field", is_required=True)
    fill_field_combo(combo, binding, note_types_for, current)
    return combo


def fill_field_combo(
    combo: GroupedComboBox, binding: str, note_types_for: NoteTypesFor, current: str
) -> None:
    """(Re)list the fields on offer, keeping the current choice if it is still one of them.

    Refillable because the trigger note type is chosen in the same dialog, at the top of the
    same scroll area: a picker built once goes on offering the fields of whichever note type
    the definition happened to name when the stage's editor was created, grouped under that
    note type's name, with a selection the trigger note may no longer have. Saving that
    fails the run per note with "Field 'X' not found in note", reported against the stage
    rather than against the change that caused it.
    """
    combo.blockSignals(True)
    try:
        combo.clear()
        offered: list[str] = []
        for model in note_types_of(binding, note_types_for):
            assert mw is not None and mw.col is not None
            names = mw.col.models.field_names(model)
            if not names:
                continue
            combo.addGroup(model["name"])
            for name in names:
                combo.addItemToGroup(model["name"], name)
                offered.append(name)
        # A name no longer on offer is dropped rather than kept: unlike a deck whitelist,
        # this one is checked against the note at run time, so keeping it would preserve a
        # write that cannot work. Blanking it makes the analyser ask for a field instead.
        combo.setCurrentText(current if current in offered else "")
    finally:
        combo.blockSignals(False)


def name_edit(parent: QWidget, current: str, placeholder: str) -> RequiredLineEdit:
    edit = RequiredLineEdit(parent, is_required=True, placeholder_text=placeholder)
    edit.setText(current or "")
    edit.update_required_style()
    return edit


def tags_to_list(text: str) -> list[str]:
    """Split the tag editor's comma-joined string into the JSON array format 2 stores (§4).

    `MultiComboBox` joins the item texts with ", " and `TagEditor` fills it with bare tag
    names, so what comes back is unquoted -- but a name that already carries quotes is
    accepted too, because a definition written by hand may well have them.
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    return [tag.strip().strip('"') for tag in stripped.split(",") if tag.strip().strip('"')]


def tags_to_text(tags: Sequence[str]) -> str:
    """The quoted, comma-joined form `TagEditor` stores and selects on."""
    return ", ".join(f'"{tag}"' for tag in tags)


# --------------------------------------------------------------------------------------
# The base
# --------------------------------------------------------------------------------------


class StageEditor(QWidget):
    """The body of one expanded stage row."""

    changed = pyqtSignal()

    def __init__(
        self,
        parent: QWidget,
        stage: Stage,
        context: StageEditorContext,
        environment: StageEditorEnvironment,
        target_is_trigger: bool = True,
    ) -> None:
        super().__init__(parent)
        self.stage = stage
        self.context = context
        self.environment = environment
        self.state = StageEditState(
            context,
            selected_models=list(environment.note_types_for("trigger") or []),
            target_is_trigger=target_is_trigger,
        )
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self._expression_editors: list[ValueExpressionEditor] = []
        #: The label widget of each row added through `add_row`, keyed by its text, so an
        #: editor whose shape depends on a choice can hide a row whole rather than leaving a
        #: caption over nothing.
        self.row_labels: dict[str, QLabel] = {}

    # -- helpers for subclasses ----------------------------------------------------------

    def expression_editor(self, expression, label: str, **kwargs) -> ValueExpressionEditor:
        editor = ValueExpressionEditor(self, expression, self.context, self.state, label, **kwargs)
        editor.changed.connect(self.changed)
        self._expression_editors.append(editor)
        return editor

    def add_row(self, label, widget) -> QLabel:
        made = QLabel(label, self) if isinstance(label, str) else label
        self.form.addRow(made, widget)
        if isinstance(label, str):
            self.row_labels[label] = made
        return made

    def notify(self, *_args) -> None:
        self.changed.emit()

    # -- contract ------------------------------------------------------------------------

    def apply(self) -> None:
        """Write every control back into the stage dict."""
        for editor in self._expression_editors:
            editor.apply()

    def set_context(self, context: StageEditorContext) -> None:
        self.context = context
        for editor in self._expression_editors:
            editor.set_context(context)


# --------------------------------------------------------------------------------------
# Leaf stages
# --------------------------------------------------------------------------------------


class VariableStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        self.result = name_edit(self, stage.get("result", ""), "A name, e.g. M")
        self.result.textChanged.connect(self.notify)
        self.add_row("Call the result", self.result)
        self.value = self.expression_editor(
            stage.setdefault("value", value_expression()),
            "Its value",
            description="Anything in scope above this stage can be referenced here.",
            process_names=ALL_FIELD_TO_VARIABLE_PROCESS_NAMES,
        )
        self.form.addRow(self.value)

    def apply(self):
        super().apply()
        self.stage["result"] = self.result.text().strip()


class QueryStageEditor(StageEditor):
    """Note and card queries differ only in what they count, so they share an editor."""

    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        self.is_cards = stage.get("type") == STAGE_CARD_QUERY
        what = "cards" if self.is_cards else "notes"
        self.result = name_edit(self, stage.get("result", ""), f"A name for the {what}")
        self.result.textChanged.connect(self.notify)
        self.add_row("Call the result", self.result)

        self.query = self.expression_editor(
            stage.setdefault("query", value_expression()),
            "Search",
            description=(
                "An ordinary Anki search. It runs against the collection as saved:"
                " edits earlier stages made are not searchable, though their values can be"
                " used to build the search."
            ),
            process_names=ALL_FIELD_TO_VARIABLE_PROCESS_NAMES,
        )
        self.form.addRow(self.query)

        selection = stage.setdefault("selection", {})
        self.strategy = labelled_combo(
            self, SELECTION_STRATEGIES, SELECTION_LABELS, selection.get("strategy", "all")
        )
        self.strategy.currentIndexChanged.connect(self._on_strategy_changed)
        self.strategy.currentIndexChanged.connect(self.notify)
        self.count = QSpinBox(self)
        self.count.setRange(1, 9999)
        self.count.setValue(selection.get("count") or 1)
        self.count.setSuffix(f" {what}")
        self.count.valueChanged.connect(self.notify)
        selection_row = QHBoxLayout()
        selection_row.addWidget(self.strategy)
        selection_row.addWidget(self.count)
        selection_row.addStretch()
        self.add_row("Take", self._wrap(selection_row))

        self.sort_field = field_combo(
            self, "", environment.note_types_for, selection.get("sort_field") or ""
        )
        self.sort_field.setPlaceholderText("Do not sort")
        self.sort_order = labelled_combo(
            self,
            SORT_ORDERS,
            {"ascending": "ascending", "descending": "descending"},
            selection.get("sort_order", "descending"),
        )
        # Format 1 could sort a field as numbers, and the migrator carries that over as
        # `sort_numeric`. It is a checkbox here rather than a hidden key, because an editor
        # that cannot show a setting ends up destroying it on the next save.
        self.sort_numeric = QCheckBox("as numbers", self)
        self.sort_numeric.setToolTip(
            "Sort on the field read as a number, with anything unparsable counting as 0."
            " Without this, 9 sorts above 10."
        )
        self.sort_numeric.setChecked(bool(selection.get("sort_numeric", False)))
        self.sort_numeric.stateChanged.connect(self.notify)
        sort_row = QHBoxLayout()
        sort_row.addWidget(self.sort_field)
        sort_row.addWidget(self.sort_order)
        sort_row.addWidget(self.sort_numeric)
        sort_row.addStretch()
        self.add_row("Then sort by", self._wrap(sort_row))

        # A definition format 1 refused to select for keeps refusing until someone says
        # otherwise here. Saving the stage must not be what says otherwise: the stage would
        # go from selecting nothing to selecting everything the query matched, silently.
        self.selection_error = selection.get("selection_error") or ""
        self.keep_refusing = QCheckBox("Use the settings above instead", self)
        if self.selection_error:
            self.add_row(
                "",
                QLabel(
                    "<span style='color: #c0392b'>This stage selects nothing:</span> "
                    f"{self.selection_error}",
                    self,
                ),
            )
            self.keep_refusing.stateChanged.connect(self.notify)
            self.add_row("", self.keep_refusing)

        self.if_empty = labelled_combo(
            self, IF_EMPTY_POLICIES, IF_EMPTY_LABELS, stage.get("if_empty", "continue")
        )
        self.if_empty.currentIndexChanged.connect(self.notify)
        self.add_row("If nothing matches", self.if_empty)
        self._on_strategy_changed()

    def _wrap(self, layout) -> QWidget:
        container = QWidget(self)
        container.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return container

    def _on_strategy_changed(self, *_args) -> None:
        # "all" has no count to give, and a count box that does nothing invites the reader
        # to believe it does.
        self.count.setVisible(combo_value(self.strategy) != "all")

    def apply(self):
        super().apply()
        self.stage["result"] = self.result.text().strip()
        strategy = combo_value(self.strategy)
        # Updated rather than replaced: a selection carries keys this editor does not own,
        # and a save that rebuilt the dict from the four controls would drop them.
        selection = self.stage.setdefault("selection", {})
        selection.update({
            "strategy": strategy,
            "count": None if strategy == "all" else self.count.value(),
            "sort_field": self.sort_field.currentText() or None,
            "sort_order": combo_value(self.sort_order),
            "sort_numeric": self.sort_numeric.isChecked(),
        })
        if self.selection_error and self.keep_refusing.isChecked():
            selection.pop("selection_error", None)
        self.stage["if_empty"] = combo_value(self.if_empty)


class FieldWriteRow(QFrame):
    """One `field = value` line inside an Edit Note stage."""

    changed = pyqtSignal()
    removed = pyqtSignal(object)

    def __init__(self, parent: "EditNoteStageEditor", field_write: dict) -> None:
        super().__init__(parent)
        self.field_write = field_write
        self.owner = parent
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.field = field_combo(
            self,
            (parent.stage.get("target") or {}).get("binding", "trigger"),
            parent.environment.note_types_for,
            field_write.get("field", ""),
        )
        self.field.currentTextChanged.connect(self.changed)
        self.write_if = labelled_combo(
            self, WRITE_IF_POLICIES, WRITE_IF_LABELS, field_write.get("write_if", "always")
        )
        self.write_if.currentIndexChanged.connect(self.changed)
        remove = QPushButton("Remove", self)
        remove.clicked.connect(lambda: self.removed.emit(self))
        header.addWidget(QLabel("Write", self))
        header.addWidget(self.field)
        header.addWidget(self.write_if)
        header.addStretch()
        header.addWidget(remove)
        layout.addLayout(header)

        self.value = ValueExpressionEditor(
            self,
            field_write.setdefault("value", value_expression()),
            parent.context,
            parent.state,
            label="to",
            process_names=ALL_FIELD_TO_FIELD_PROCESS_NAMES,
        )
        self.value.changed.connect(self.changed)
        layout.addWidget(self.value)

        # Only a migrated write has one of these. Format 1 asked per field write which editor
        # fields trigger it, and `run_edit_note` still honours the answer, so leaving it off
        # the row made it the one thing about a write that could not be changed: adding a
        # field to the definition's own unfocus list widened the gate that starts a run and
        # not this one, so the definition ran and every migrated write in it was skipped --
        # silently, because the tags and card actions beside them are not gated.
        self.unfocus_fields: Optional[MultiComboBox] = None
        if "unfocus_trigger_fields" in field_write:
            self.unfocus_fields = MultiComboBox(
                self, placeholder_text="No fields (this write never runs on unfocus)"
            )
            self._fill_unfocus_fields(parent.context)
            self.unfocus_fields.currentTextChanged.connect(self.changed)
            watched = QHBoxLayout()
            watched.addWidget(QLabel("only when leaving", self))
            watched.addWidget(self.unfocus_fields)
            watched.addStretch()
            layout.addLayout(watched)

    def _fill_unfocus_fields(self, context: StageEditorContext) -> None:
        """Offer the trigger note's fields: these name editor fields, not the write's target.

        A `MultiComboBox` can only offer what is in it, so a stored name the trigger note
        type no longer has would be dropped on the way through. It is added as its own item
        instead, the way the trigger editor keeps a whitelisted deck it cannot offer.
        """
        box = self.unfocus_fields
        if box is None:
            return
        stored = [name for name in self.field_write.get("unfocus_trigger_fields") or [] if name]
        offered: list[str] = []
        assert mw is not None and mw.col is not None
        for model in note_types_of("trigger", self.owner.environment.note_types_for):
            for name in mw.col.models.field_names(model):
                if name not in offered:
                    offered.append(name)
        for name in stored:
            if name not in offered:
                offered.append(name)
        # Quoted item texts, as every other name box in this editor holds them: that is the
        # form `selected_names` reads back, and a field name can contain a comma.
        box.blockSignals(True)
        box.clear()
        box.addItems(quoted_items(offered))
        box.setCurrentText(", ".join(quoted_items(stored)))
        box.blockSignals(False)

    def apply(self) -> dict:
        self.field_write["field"] = self.field.currentText()
        self.field_write["write_if"] = combo_value(self.write_if)
        if self.unfocus_fields is not None:
            self.field_write["unfocus_trigger_fields"] = selected_names(self.unfocus_fields)
        self.value.apply()
        return self.field_write

    def set_context(self, context: StageEditorContext) -> None:
        self.value.set_context(context)
        fill_field_combo(
            self.field,
            (self.owner.stage.get("target") or {}).get("binding", "trigger"),
            self.owner.environment.note_types_for,
            self.field.currentText(),
        )
        self._fill_unfocus_fields(context)


class EditNoteStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        target = (stage.get("target") or {}).get("binding", "trigger")
        super().__init__(
            parent, stage, context, environment, target_is_trigger=target == "trigger"
        )
        self.target = binding_combo(
            self, context.note_bindings, target, "Which note to edit"
        )
        self.target.currentTextChanged.connect(self._on_target_changed)
        self.add_row("Edit", self.target)

        self.fields_container = QWidget(self)
        self.fields_layout = QVBoxLayout(self.fields_container)
        self.fields_layout.setContentsMargins(0, 0, 0, 0)
        self.form.addRow(self.fields_container)
        self.field_rows: list[FieldWriteRow] = []
        for field_write in stage.setdefault("fields", []):
            self._add_field_row(field_write)

        add_field = QPushButton("Add a field to write", self)
        add_field.clicked.connect(self._on_add_field)
        self.form.addRow(add_field)

        tags = stage.setdefault("tags", {"add": [], "remove": []})
        self.tag_editor = TagEditor(
            self,
            self.state,  # type: ignore[arg-type]
            {  # type: ignore[arg-type]
                "add_tags": tags_to_text(tags.get("add", [])),
                "remove_tags": tags_to_text(tags.get("remove", [])),
            },
            self.state.copy_mode,
        )
        self.tag_editor.initialize_ui_state()
        self.tag_editor.changed.connect(self.changed)
        self.form.addRow(self.tag_editor)

        self.card_actions = CardActionsEditor(
            self,
            self.state,  # type: ignore[arg-type]
            {"card_actions": stage.setdefault("card_actions", [])},  # type: ignore[arg-type]
        )
        self.card_actions.initialize_ui_state()
        self.card_actions.changed.connect(self.changed)
        self.form.addRow(self.card_actions)

    def _on_target_changed(self, name: str) -> None:
        # The card types on offer are the trigger note type's or every note type's, and
        # which of those depends on the note this stage now edits. The field pickers follow
        # the same change through `set_context`, which the refresh below reaches.
        self.state.set_target_is_trigger(name == "trigger")
        self.notify()

    def _add_field_row(self, field_write: dict) -> FieldWriteRow:
        row = FieldWriteRow(self, field_write)
        row.changed.connect(self.changed)
        row.removed.connect(self._on_remove_field)
        self.fields_layout.addWidget(row)
        self.field_rows.append(row)
        return row

    def _on_add_field(self) -> None:
        field_write = {"field": "", "value": value_expression(), "write_if": "always"}
        self.stage.setdefault("fields", []).append(field_write)
        self._add_field_row(field_write)
        self.changed.emit()

    def _on_remove_field(self, row: FieldWriteRow) -> None:
        self.field_rows.remove(row)
        fields = self.stage.get("fields") or []
        if row.field_write in fields:
            fields.remove(row.field_write)
        self.fields_layout.removeWidget(row)
        row.deleteLater()
        self.changed.emit()

    def apply(self):
        super().apply()
        self.stage["target"] = {"binding": self.target.currentText()}
        self.stage["fields"] = [row.apply() for row in self.field_rows]
        self.stage["tags"] = {
            "add": tags_to_list(self.tag_editor.get_add_tags()),
            "remove": tags_to_list(self.tag_editor.get_remove_tags()),
        }
        self.stage["card_actions"] = self.card_actions.get_card_actions()

    def set_context(self, context):
        super().set_context(context)
        for row in self.field_rows:
            row.set_context(context)


class EditCardStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment, target_is_trigger=False)
        self.target = binding_combo(
            self,
            context.card_bindings,
            (stage.get("target") or {}).get("binding", ""),
            "Which card to act on",
        )
        self.target.currentTextChanged.connect(self.notify)
        self.add_row("Act on", self.target)
        self.form.addRow(
            QLabel(
                "<small>These actions apply to exactly that card, so there is no card type"
                " to choose.</small>",
                self,
            )
        )
        self.card_actions = CardActionsEditor(
            self,
            self.state,  # type: ignore[arg-type]
            {"card_actions": stage.setdefault("card_actions", [])},  # type: ignore[arg-type]
            single_card_mode=True,
        )
        self.card_actions.initialize_ui_state()
        self.card_actions.changed.connect(self.changed)
        self.form.addRow(self.card_actions)

    def apply(self):
        super().apply()
        self.stage["target"] = {"binding": self.target.currentText()}
        self.stage["card_actions"] = self.card_actions.get_card_actions()


class ReadFileStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        self.result = name_edit(self, stage.get("result", ""), "A name for the contents")
        self.result.textChanged.connect(self.notify)
        self.add_row("Call the result", self.result)
        self.filename = self.expression_editor(
            stage.setdefault("filename", value_expression()),
            "File in the media folder",
            description="Path separators and '..' are refused; the file must be UTF-8.",
            process_names=ALL_FIELD_TO_VARIABLE_PROCESS_NAMES,
        )
        self.form.addRow(self.filename)
        self.if_missing = labelled_combo(
            self, IF_MISSING_POLICIES, IF_MISSING_LABELS, stage.get("if_missing", "empty")
        )
        self.if_missing.currentIndexChanged.connect(self.notify)
        self.add_row("If the file is not there", self.if_missing)

    def apply(self):
        super().apply()
        self.stage["result"] = self.result.text().strip()
        self.stage["if_missing"] = combo_value(self.if_missing)


class WriteFileStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        self.filename = self.expression_editor(
            stage.setdefault("filename", value_expression()),
            "File in the media folder",
            description=(
                "Leave this empty only if the content is code that returns its own"
                " (filename, content) pairs."
            ),
            is_required=False,
            process_names=ALL_FIELD_TO_VARIABLE_PROCESS_NAMES,
        )
        self.form.addRow(self.filename)
        self.content = self.expression_editor(
            stage.setdefault("content", value_expression()),
            "Its whole contents",
            description=(
                "A write replaces the file. To append, read it into a variable first and"
                " build the new contents from that."
            ),
            notice=FILE_CODE_NOTICE,
            process_names=ALL_FIELD_TO_FIELD_PROCESS_NAMES,
        )
        self.form.addRow(self.content)
        self.overwrite = QCheckBox("Replace the file if it already exists", self)
        self.overwrite.setChecked(bool(stage.get("overwrite")))
        self.overwrite.toggled.connect(self.notify)
        self.form.addRow(self.overwrite)
        self.form.addRow(
            QLabel(
                "<small>File writes happen after the collection changes and are not undone"
                " by Anki's undo.</small>",
                self,
            )
        )

    def apply(self):
        super().apply()
        self.stage["overwrite"] = self.overwrite.isChecked()


class ListVariableStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        self.result = name_edit(self, stage.get("result", ""), "A name, e.g. L1")
        self.result.textChanged.connect(self.notify)
        self.add_row("Call the list", self.result)
        self.item_type = labelled_combo(
            self, LIST_ITEM_TYPE_NAMES, {}, stage.get("item_type", TEXT)
        )
        self.item_type.currentIndexChanged.connect(self.notify)
        self.add_row("Holding", self.item_type)

    def apply(self):
        super().apply()
        self.stage["result"] = self.result.text().strip()
        self.stage["item_type"] = combo_value(self.item_type)


class StoreStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        self.target = binding_combo(
            self,
            context.list_bindings,
            (stage.get("target") or {}).get("binding", ""),
            "Which list to append to",
        )
        self.target.currentTextChanged.connect(self.notify)
        self.add_row("Append to", self.target)
        self.value = self.expression_editor(
            stage.setdefault("value", value_expression()), "The value to append"
        )
        self.form.addRow(self.value)

    def apply(self):
        super().apply()
        self.stage["target"] = {"kind": "list", "binding": self.target.currentText()}


# --------------------------------------------------------------------------------------
# Structural stages
# --------------------------------------------------------------------------------------


class ForEachNoteStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        self.input = binding_combo(
            self,
            context.note_list_bindings,
            (stage.get("input") or {}).get("binding", ""),
            "Which list of notes",
        )
        self.input.currentTextChanged.connect(self.notify)
        self.add_row("For each note in", self.input)
        self.item_binding = name_edit(self, stage.get("item_binding", "note"), "note")
        self.item_binding.textChanged.connect(self.notify)
        self.add_row("Call each one", self.item_binding)

    def apply(self):
        super().apply()
        self.stage["input"] = {"binding": self.input.currentText()}
        self.stage["item_binding"] = self.item_binding.text().strip() or "note"


class ForEachCardStageEditor(StageEditor):
    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        self.input = binding_combo(
            self,
            context.card_list_bindings,
            (stage.get("input") or {}).get("binding", ""),
            "Which list of cards",
        )
        self.input.currentTextChanged.connect(self.notify)
        self.add_row("For each card in", self.input)
        self.item_binding = name_edit(self, stage.get("item_binding", "card"), "card")
        self.item_binding.textChanged.connect(self.notify)
        self.add_row("Call each card", self.item_binding)
        self.note_binding = name_edit(self, stage.get("note_binding", "note"), "note")
        self.note_binding.textChanged.connect(self.notify)
        self.add_row("Call its note", self.note_binding)

    def apply(self):
        super().apply()
        self.stage["input"] = {"binding": self.input.currentText()}
        self.stage["item_binding"] = self.item_binding.text().strip() or "card"
        self.stage["note_binding"] = self.note_binding.text().strip() or "note"


class ReduceStageEditor(StageEditor):
    """A reduce is one of two things, and only one of them reads the expressions below.

    `join` returns `separator.join(...)` and ignores `initial`, `item_binding`,
    `accumulator_binding` and `value` entirely; `fold` runs `value` once per item with an
    accumulator. Which one it is lives in `operation`, and every reduce a migration produces
    is a join carrying format 1's `select_card_separator`.

    Both keys used to be invisible. A stored join kept working -- `apply()` did not write
    `operation`, so it survived -- but everything shown for it was dead: two expression
    editors and two binding names the executor would not read, presented exactly as they are
    on a fold that does read them. The separator, the one part of a join a user has reason to
    change, could not be seen at all, though it was an ordinary text field in the format-1
    editor it came from.
    """

    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        sources = (
            context.list_bindings + context.note_list_bindings + context.card_list_bindings
        )
        self.input = binding_combo(
            self, sources, (stage.get("input") or {}).get("binding", ""), "Which list to fold"
        )
        self.input.currentTextChanged.connect(self.notify)
        self.add_row("Fold", self.input)
        self.result = name_edit(self, stage.get("result", ""), "A name for the result")
        self.result.textChanged.connect(self.notify)
        self.add_row("Call the result", self.result)
        self.operation = labelled_combo(
            self,
            REDUCE_OPERATIONS,
            REDUCE_OPERATION_LABELS,
            stage.get("operation", "fold") or "fold",
        )
        self.operation.currentIndexChanged.connect(self._on_operation_changed)
        self.add_row("By", self.operation)
        self.separator = QLineEdit(self)
        self.separator.setText(stage.get("separator", ", ") or "")
        self.separator.setPlaceholderText("nothing between the values")
        self.separator.textChanged.connect(self.notify)
        self.separator_row = self.add_row("With this between them", self.separator)
        self.initial = self.expression_editor(
            stage.setdefault("initial", value_expression()),
            "Starting from",
            is_required=False,
            allow_process_chain=False,
        )
        self.form.addRow(self.initial)
        self.item_binding = name_edit(self, stage.get("item_binding", "item"), "item")
        self.item_binding.textChanged.connect(self.notify)
        self.add_row("Call each item", self.item_binding)
        self.accumulator_binding = name_edit(
            self, stage.get("accumulator_binding", "accumulator"), "accumulator"
        )
        self.accumulator_binding.textChanged.connect(self.notify)
        self.add_row("Call the running value", self.accumulator_binding)
        self.value = self.expression_editor(
            stage.setdefault("value", value_expression(mode="code")),
            "The next running value",
            description=(
                "Runs once per item and returns the next running value. It cannot change"
                " any note: a fold that has to write is a loop with a Store in it."
            ),
        )
        self.form.addRow(self.value)
        self._apply_operation()

    def _apply_operation(self) -> None:
        """Show only the controls the chosen operation actually reads."""
        is_join = combo_value(self.operation) == "join"
        self.separator_row.setVisible(is_join)
        self.separator.setVisible(is_join)
        for widget in (self.initial, self.value):
            widget.setVisible(not is_join)
        for label_text, widget in (
            ("Call each item", self.item_binding),
            ("Call the running value", self.accumulator_binding),
        ):
            widget.setVisible(not is_join)
            self.row_labels[label_text].setVisible(not is_join)

    def _on_operation_changed(self, *_args) -> None:
        self._apply_operation()
        self.notify()

    def apply(self):
        super().apply()
        self.stage["input"] = {"binding": self.input.currentText()}
        self.stage["result"] = self.result.text().strip()
        self.stage["operation"] = combo_value(self.operation)
        self.stage["separator"] = self.separator.text()
        self.stage["item_binding"] = self.item_binding.text().strip() or "item"
        self.stage["accumulator_binding"] = (
            self.accumulator_binding.text().strip() or "accumulator"
        )


class ConditionStageEditor(StageEditor):
    """A condition runs its first branch when the predicate holds -- in one of two ways.

    A migrated copy condition is an Anki search run against one note, which is how format 1
    ran it (§11); anything authored here is boolean code, or a value the branch takes the
    truthiness of. `predicate_kind` is what tells the executor which, so it belongs on the
    editor: while it was a hidden key, a migrated condition was shown as an ordinary
    expression, code toggle and all, and the code the toggle invited was never run -- the
    executor still took the search branch and still ran the untouched text.
    """

    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        is_search = stage.get("predicate_kind") == "note_query"
        self.match_as_search = QCheckBox("Match it as an Anki search against a note", self)
        self.match_as_search.setChecked(is_search)
        self.form.addRow(self.match_as_search)
        self.target = binding_combo(
            self,
            context.note_bindings,
            (stage.get("predicate_target") or {}).get("binding", "") or "trigger",
            "Which note the search has to match",
        )
        self.target.currentTextChanged.connect(self.notify)
        self.add_row("Against", self.target)
        self.only_on_sync = QCheckBox("Only check it during a sync", self)
        self.only_on_sync.setChecked(bool(stage.get("only_on_sync", False)))
        self.only_on_sync.setToolTip(
            "Outside a sync the condition is not checked at all and the branch runs."
        )
        self.only_on_sync.toggled.connect(self.notify)
        self.form.addRow(self.only_on_sync)
        self.predicate = self.expression_editor(
            stage.setdefault("predicate", value_expression(mode="code")),
            "Run the first branch when",
            description=(
                "Code returning true or false, or text that counts as true when it is not"
                " empty."
            ),
            allow_process_chain=False,
        )
        self.form.addRow(self.predicate)
        self.match_as_search.toggled.connect(self._on_kind_changed)
        self._apply_kind(is_search)

    def _apply_kind(self, is_search: bool) -> None:
        self.target.setEnabled(is_search)
        # A search is text run through `find_notes`; there is no code form of it, and an
        # expression has no note to be matched against.
        self.predicate.set_code_allowed(not is_search)
        self.predicate.set_label(
            "An Anki search the note has to match" if is_search else "Run the first branch when"
        )

    def _on_kind_changed(self, is_search: bool) -> None:
        self._apply_kind(is_search)
        self.notify()

    def apply(self):
        super().apply()
        self.stage["only_on_sync"] = self.only_on_sync.isChecked()
        if self.match_as_search.isChecked():
            self.stage["predicate_kind"] = "note_query"
            self.stage["predicate_target"] = {"binding": self.target.currentText()}
        else:
            self.stage.pop("predicate_kind", None)
            self.stage.pop("predicate_target", None)
            # A condition that no longer matches a search cannot be format 1's copy
            # condition either, and the marker is what makes a non-match end the whole
            # definition rather than take the empty branch. Leaving it behind would keep
            # that on a stage whose editor shows nothing about it.
            self.stage.pop("unmatched_skips_trigger", None)


class CallDefinitionStageEditor(StageEditor):
    """Binds another definition's exports into this one's scope.

    The list of exports comes from the callee as it is stored, so a callee that has never
    been analysed still offers its declared exports and the analyser is left to complain
    about the types.
    """

    def __init__(self, parent, stage, context, environment):
        super().__init__(parent, stage, context, environment)
        # Definitions are referenced by guid, never by name (§5.9), so the combo shows
        # names and this list carries the guid for each row.
        self.definition_guids: list[str] = []
        self.definition_combo = RequiredCombobox(
            self, placeholder_text="Which definition to run", is_required=True
        )
        for definition in environment.callable_definitions():
            self.definition_guids.append(definition["guid"])
            self.definition_combo.addItem(
                definition.get("definition_name") or definition["guid"]
            )
        current = stage.get("definition_guid", "")
        if current and current not in self.definition_guids:
            # The callee was deleted or is not in this config. Keep naming it: the analyser
            # reports it as missing, which is more use than silently calling something else.
            self.definition_guids.append(current)
            self.definition_combo.addItem(f"{current} (not found)")
        self.definition_combo.setCurrentIndex(
            self.definition_guids.index(current) if current in self.definition_guids else -1
        )
        self.definition_combo.currentIndexChanged.connect(self._on_definition_changed)
        self.add_row("Run", self.definition_combo)

        self.trigger = binding_combo(
            self,
            context.note_bindings,
            (stage.get("trigger") or {}).get("binding", "trigger"),
            "Which note it should treat as its trigger",
        )
        self.trigger.currentTextChanged.connect(self.notify)
        self.add_row("With this note as its trigger", self.trigger)

        self.outputs_container = QWidget(self)
        self.outputs_layout = QVBoxLayout(self.outputs_container)
        self.outputs_layout.setContentsMargins(0, 0, 0, 0)
        self.form.addRow(QLabel("<h4>Results to keep</h4>", self))
        self.form.addRow(self.outputs_container)
        self.output_rows: list[tuple[str, QCheckBox, RequiredLineEdit]] = []
        self._rebuild_outputs()

    def selected_guid(self) -> str:
        index = self.definition_combo.currentIndex()
        if 0 <= index < len(self.definition_guids):
            return self.definition_guids[index]
        return ""

    def _callee(self) -> Optional[CopyDefinitionV2]:
        guid = self.selected_guid()
        return self.environment.definition(guid) if guid else None

    def _rebuild_outputs(self) -> None:
        while self.outputs_layout.count():
            item = self.outputs_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.deleteLater()
        self.output_rows = []
        callee = self._callee()
        bound = {
            output.get("export"): output.get("result", "")
            for output in self.stage.get("outputs") or []
            if isinstance(output, dict)
        }
        exports = [
            export.get("name", "")
            for export in (callee or {}).get("exports", []) or []
            if isinstance(export, dict) and export.get("name")
        ]
        if not exports:
            self.outputs_layout.addWidget(
                QLabel(
                    "<small>That definition exports nothing, so there is nothing to keep."
                    "</small>"
                    if callee is not None
                    else "<small>That definition is not in this collection, so its exports"
                    " cannot be listed. What this stage binds is kept until it can be.</small>",
                    self.outputs_container,
                )
            )
            return
        for name in exports:
            row = QWidget(self.outputs_container)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            keep = QCheckBox(name, row)
            keep.setChecked(name in bound)
            local = RequiredLineEdit(row, placeholder_text=f"call it {name} here")
            local.setText(bound.get(name, "") or "")
            local.setEnabled(keep.isChecked())
            keep.toggled.connect(local.setEnabled)
            keep.toggled.connect(self.notify)
            local.textChanged.connect(self.notify)
            layout.addWidget(keep)
            layout.addWidget(QLabel("as", row))
            layout.addWidget(local)
            layout.addStretch()
            self.outputs_layout.addWidget(row)
            self.output_rows.append((name, keep, local))

    def _on_definition_changed(self, *_args) -> None:
        # Exports belong to the callee, so changing which definition is called invalidates
        # every binding the user made against the old one.
        self.stage["outputs"] = []
        self._rebuild_outputs()
        self.changed.emit()

    def apply(self):
        super().apply()
        self.stage["definition_guid"] = self.selected_guid()
        self.stage["trigger"] = {"binding": self.trigger.currentText()}
        if self._callee() is None and self.selected_guid():
            # Nothing was listed, so there is nothing to read back, and rebuilding `outputs`
            # from no rows would delete the bindings rather than leave them alone. `apply` is
            # not a user action -- `refresh_status` runs it while the dialog is still being
            # built -- so the bindings would be gone before the stage was ever on screen, and
            # gone from the saved definition. The combo goes on naming the missing callee, so
            # putting it back has to be the fix, and it only is if these survive.
            return
        self.stage["outputs"] = [
            {"export": name, "result": local.text().strip()}
            for name, keep, local in self.output_rows
            if keep.isChecked()
        ]


STAGE_EDITOR_CLASSES: dict[str, Callable[..., StageEditor]] = {
    STAGE_VARIABLE: VariableStageEditor,
    STAGE_NOTE_QUERY: QueryStageEditor,
    STAGE_CARD_QUERY: QueryStageEditor,
    STAGE_EDIT_NOTE: EditNoteStageEditor,
    STAGE_EDIT_CARD: EditCardStageEditor,
    STAGE_READ_FILE: ReadFileStageEditor,
    STAGE_WRITE_FILE: WriteFileStageEditor,
    STAGE_LIST_VARIABLE: ListVariableStageEditor,
    STAGE_STORE: StoreStageEditor,
    STAGE_FOR_EACH_NOTE: ForEachNoteStageEditor,
    STAGE_FOR_EACH_CARD: ForEachCardStageEditor,
    STAGE_REDUCE: ReduceStageEditor,
    STAGE_CONDITION: ConditionStageEditor,
    STAGE_CALL_DEFINITION: CallDefinitionStageEditor,
}


def make_stage_editor(
    parent: QWidget,
    stage: Stage,
    context: StageEditorContext,
    environment: StageEditorEnvironment,
) -> Optional[StageEditor]:
    """The editor for this stage's type, or None when the type is not one we know.

    A row for an unknown type still renders -- it just cannot be edited -- because the
    definition is invalid and the user needs to see which row to delete.
    """
    factory = STAGE_EDITOR_CLASSES.get(stage.get("type", ""))
    if factory is None:
        return None
    return factory(parent, stage, context, environment)


