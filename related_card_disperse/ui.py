from __future__ import annotations

from copy import deepcopy
from typing import Optional

from aqt import mw
from aqt.qt import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .configuration import Config, RelatedRule, default_rule
from .shared.interpolate.interpolate_fields import BASE_NOTE_MENU_DICT
from .shared.ui.add_intersecting_model_field_options_to_dict import (
    add_intersecting_model_field_options_to_dict,
)
from .shared.ui.code_edit_layout import (
    CODE_NOTICE_HTML_WARNING,
    CODE_NOTICE_PREFIX,
    CodeEditLayout,
    code_notice_available_names,
)
from .shared.ui.interpolated_text_edit import InterpolatedTextEditLayout, make_validate_dict
from .shared.ui.multi_combo_box import MultiComboBox
from .shared.ui.scrollable_dialog import ScrollableQDialog
from .shared.ui.toggle_switch import ToggleSwitch


QUERY_CODE_NOTICE = (
    CODE_NOTICE_PREFIX
    + "<b>returns a query string or card-id list</b>. "
    + code_notice_available_names(
        extra_names=(("reviewed_card", "the reviewed card"), ("reviewed_note", "the reviewed note"))
    )
    + "<br>"
    + CODE_NOTICE_HTML_WARNING
)

# The cap spin box uses its minimum as "no override": Qt then shows the special
# value text instead of the number, and clamps anything typed into range, so an
# out-of-range cap can never reach the config in the first place.
CAP_USES_DEFAULT = 0
CAP_MAX = 9999

# What an as-yet-unnamed rule is called in the list.
UNNAMED_RULE_LABEL = "New rule"

QUERY_REQUIRED_MESSAGE = (
    "A related card query is required while dispersal is enabled: an empty query matches"
    " the whole collection."
)


class RelatedCardDisperseDialog(ScrollableQDialog):
    """Rule editor.

    The list is the single source of truth: the editor always edits the rule at
    ``_current_index`` and writes back into it before anything moves the
    selection, so there is no such thing as an unsaved rule floating outside the
    list. "New rule" therefore appends a row and selects it rather than blanking
    the form and hoping the next Save figures out what was meant.
    """

    def __init__(self, parent=None):
        footer = QHBoxLayout()
        self.save_all_btn = QPushButton("Save")
        self.cancel_btn = QPushButton("Cancel")
        footer.addStretch(1)
        footer.addWidget(self.save_all_btn)
        footer.addWidget(self.cancel_btn)

        super().__init__(parent or mw, footer_layout=footer)
        self.setWindowTitle("Related Card Disperse")

        self.config = Config()
        self.config.load()
        self.rules: list[RelatedRule] = deepcopy(self.config.rules)

        # Guards the handlers that react to user edits while the form is being
        # populated from a rule, or the selection moved programmatically.
        self._building_ui = False
        # The row the editor is currently editing; -1 when there is no rule.
        self._current_index = -1

        root = QVBoxLayout(self.inner_widget)

        global_box = QWidget(self)
        global_form = QFormLayout(global_box)

        self.default_cap = QSpinBox(self)
        self.default_cap.setMinimum(1)
        self.default_cap.setMaximum(CAP_MAX)
        self.default_cap.setValue(self.config.default_max_related_cards)

        self.show_unchanged = QCheckBox("Report runs that rescheduled nothing", self)
        self.show_unchanged.setToolTip(
            "Also show a tooltip when a rule found nothing to move -- because the cards'"
            " due ranges are too tight to separate, or they are already spread out."
        )
        self.show_unchanged.setChecked(self.config.show_unchanged_outcome)

        self.dedupe_sync_groups = QCheckBox(
            "Dedupe overlapping related-card groups during sync", self
        )
        self.dedupe_sync_groups.setChecked(self.config.dedupe_sync_groups)

        global_form.addRow("Default max related cards per execution", self.default_cap)
        global_form.addRow(self.show_unchanged)
        global_form.addRow(self.dedupe_sync_groups)

        root.addWidget(global_box)

        split = QHBoxLayout()
        root.addLayout(split)

        # Left: rule list + ordering controls
        left = QVBoxLayout()
        split.addLayout(left, 1)

        left.addWidget(QLabel("Rules"))
        self.rule_list = QListWidget(self)
        left.addWidget(self.rule_list)

        order_row = QHBoxLayout()
        self.move_up_btn = QPushButton("Move up", self)
        self.move_down_btn = QPushButton("Move down", self)
        order_row.addWidget(self.move_up_btn)
        order_row.addWidget(self.move_down_btn)
        left.addLayout(order_row)

        # Right: editor
        right = QVBoxLayout()
        split.addLayout(right, 2)

        self.editor = QWidget(self)
        form = QFormLayout(self.editor)

        self.rule_name = QLineEdit(self)
        self.rule_name.setPlaceholderText("Optional name")

        self.enabled = ToggleSwitch("Disperse related cards for these note types", self)

        self.note_types = MultiComboBox(self)
        self._populate_note_types()

        self.on_review = QCheckBox("Run on reviewer_did_answer_card", self)
        self.on_sync = QCheckBox("Run for remotely reviewed cards after sync", self)
        self.use_code = QCheckBox("Use code mode", self)

        self.rule_cap = QSpinBox(self)
        self.rule_cap.setMinimum(CAP_USES_DEFAULT)
        self.rule_cap.setMaximum(CAP_MAX)
        self.rule_cap.setSpecialValueText("Use default")

        self.query_text = InterpolatedTextEditLayout(
            label="Related card query",
            options_dict={},
            description=(
                "Use browser query syntax and interpolate with {{Field}}. Required while"
                " dispersal is enabled. A new rule starts with the reviewed note's own"
                " cards, i.e. plain sibling dispersal."
            ),
            height=120,
            placeholder_text='"deck:My deck" "Front:*{{Front}}*"',
            is_required=False,
        )
        self.query_text_widget = QWidget(self)
        self.query_text_widget.setLayout(self.query_text)

        self.query_code = CodeEditLayout(
            parent=self,
            options_dict={},
            label="Query code",
            description=(
                "Return a query string or list of card ids. Required while dispersal is"
                " enabled."
            ),
            notice=QUERY_CODE_NOTICE,
            is_required=False,
        )
        self.query_code.hide()

        form.addRow("Rule name", self.rule_name)
        form.addRow(self.enabled)
        form.addRow("Target note types", self.note_types)
        form.addRow(self.on_review)
        form.addRow(self.on_sync)
        form.addRow(self.use_code)
        form.addRow("Max related cards (rule override)", self.rule_cap)
        form.addRow(self.query_text_widget)
        form.addRow(self.query_code)

        right.addWidget(self.editor)

        self.validation_label = QLabel(self)
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #d9534f;")
        right.addWidget(self.validation_label)

        action_row = QHBoxLayout()
        self.new_btn = QPushButton("New rule", self)
        self.save_rule_btn = QPushButton("Save rule", self)
        self.remove_rule_btn = QPushButton("Delete rule", self)
        action_row.addWidget(self.new_btn)
        action_row.addWidget(self.save_rule_btn)
        action_row.addWidget(self.remove_rule_btn)
        right.addLayout(action_row)

        self.rule_list.currentRowChanged.connect(self._on_rule_selected)
        self.move_up_btn.clicked.connect(self._move_up)
        self.move_down_btn.clicked.connect(self._move_down)
        self.new_btn.clicked.connect(self._new_rule)
        self.save_rule_btn.clicked.connect(self._save_rule)
        self.remove_rule_btn.clicked.connect(self._delete_rule)
        self.save_all_btn.clicked.connect(self._save_all)
        self.cancel_btn.clicked.connect(self.reject)
        self.use_code.toggled.connect(self._on_use_code_toggled)
        self.enabled.toggled.connect(self._on_enabled_toggled)
        self.query_text.text_edit.textChanged.connect(self._update_validation_state)
        self.query_code.text_edit.textChanged.connect(self._update_validation_state)

        model = self.note_types.model()
        if model is not None:
            model.dataChanged.connect(lambda *_: self._on_note_types_changed())

        self._refresh_rule_list()
        self._select_row(0 if self.rules else -1)

    def _populate_note_types(self) -> None:
        self.note_types.blockSignals(True)
        self.note_types.clear()
        for model in mw.col.models.all():
            self.note_types.addItem(model["name"])
        self.note_types.blockSignals(False)

    def _selected_note_type_names(self) -> list[str]:
        return list(self.note_types.currentData() or [])

    def _serialize_selected_note_types(self) -> str:
        names = self._selected_note_type_names()
        return '"' + '", "'.join(names) + '"' if names else ""

    def _update_query_options(self) -> None:
        selected = self._selected_note_type_names()
        options = BASE_NOTE_MENU_DICT.copy()
        models = [mw.col.models.by_name(name) for name in selected]
        models = [m for m in models if m]
        if models:
            add_intersecting_model_field_options_to_dict(models, options)
        validate = make_validate_dict(options)
        self.query_text.update_options(options, validate)
        self.query_code.update_options(options, validate)

    def _on_note_types_changed(self) -> None:
        if self._building_ui:
            return
        self._update_query_options()
        self._autofill_rule_name()

    def _autofill_rule_name(self) -> None:
        """Name a still-unnamed rule after the note type it was pointed at."""
        if self.rule_name.text().strip():
            return
        names = self._selected_note_type_names()
        if not names:
            return
        self.rule_name.setText(names[0])
        self._relabel_current_row()

    @staticmethod
    def _rule_label(name: str, enabled: bool) -> str:
        label = name or UNNAMED_RULE_LABEL
        return label if enabled else f"{label} (disabled)"

    def _refresh_rule_list(self) -> None:
        self.rule_list.blockSignals(True)
        self.rule_list.clear()
        for rule in self.rules:
            self.rule_list.addItem(
                self._rule_label(rule.get("name", ""), rule.get("enabled", True))
            )
        self.rule_list.blockSignals(False)

    def _relabel_current_row(self) -> None:
        item = self.rule_list.item(self._current_index)
        if item is not None:
            item.setText(
                self._rule_label(self.rule_name.text().strip(), self.enabled.isChecked())
            )

    def _on_use_code_toggled(self, checked: bool) -> None:
        self.query_text_widget.setVisible(not checked)
        self.query_code.setVisible(checked)
        if checked and not self.query_code.get_text().strip():
            self.query_code.set_text(f"return {repr(self.query_text.get_text())}")
        self._update_validation_state()

    def _on_enabled_toggled(self, _checked: bool) -> None:
        self._relabel_current_row()
        self._update_validation_state()

    def _active_query_text(self) -> str:
        if self.use_code.isChecked():
            return self.query_code.get_text()
        return self.query_text.get_text()

    def _rule_is_valid(self) -> bool:
        """An enabled rule needs a query; a disabled one is never run."""
        if self._current_index < 0 or not self.enabled.isChecked():
            return True
        return bool(self._active_query_text().strip())

    def _update_validation_state(self) -> None:
        required = self.enabled.isChecked() and self._current_index >= 0
        in_code_mode = self.use_code.isChecked()
        self.query_text.text_edit.set_required(required and not in_code_mode)
        self.query_code.text_edit.set_required(required and in_code_mode)

        valid = self._rule_is_valid()
        self.validation_label.setText("" if valid else QUERY_REQUIRED_MESSAGE)
        self.save_all_btn.setEnabled(valid)
        self.save_rule_btn.setEnabled(valid and self._current_index >= 0)

    def _set_form_from_rule(self, rule: RelatedRule) -> None:
        self.rule_name.setText(rule.get("name", ""))
        self.enabled.setChecked(rule.get("enabled", True))
        self.note_types.setCurrentText("")
        for note_type_name in self._decode_note_types(rule.get("target_note_types", "")):
            self.note_types.addSelectedItem(note_type_name)
        self.note_types.updateText()
        self.on_review.setChecked(rule.get("on_review", True))
        self.on_sync.setChecked(rule.get("on_sync", True))
        self.use_code.setChecked(rule.get("use_code", False))
        self.rule_cap.setValue(rule.get("max_related_cards") or CAP_USES_DEFAULT)
        self.query_text.set_text(rule.get("related_card_query", ""))
        self.query_code.set_text(rule.get("query_code", ""))
        self._on_use_code_toggled(self.use_code.isChecked())

    @staticmethod
    def _decode_note_types(target_note_types: str) -> list[str]:
        if not target_note_types:
            return []
        return [n for n in target_note_types.strip('"').split('", "') if n]

    def _form_to_rule(self, guid: str) -> RelatedRule:
        cap = self.rule_cap.value()
        cap_value: Optional[int] = None if cap == CAP_USES_DEFAULT else cap

        return RelatedRule(
            guid=guid,
            name=self.rule_name.text().strip(),
            enabled=self.enabled.isChecked(),
            target_note_types=self._serialize_selected_note_types(),
            related_card_query=self.query_text.get_text(),
            use_code=self.use_code.isChecked(),
            query_code=self.query_code.get_text(),
            on_review=self.on_review.isChecked(),
            on_sync=self.on_sync.isChecked(),
            max_related_cards=cap_value,
        )

    # -------------------------------------------------------------------------
    # Selection / editing
    # -------------------------------------------------------------------------

    def _commit_form(self) -> None:
        """Write the editor's contents back into the rule it is editing."""
        index = self._current_index
        if 0 <= index < len(self.rules):
            self.rules[index] = self._form_to_rule(self.rules[index]["guid"])

    def _load_row(self, row: int) -> None:
        self._current_index = row
        has_rule = 0 <= row < len(self.rules)
        self._building_ui = True
        try:
            self._set_form_from_rule(self.rules[row] if has_rule else default_rule())
        finally:
            self._building_ui = False
        self._update_query_options()
        self.editor.setEnabled(has_rule)
        self.remove_rule_btn.setEnabled(has_rule)
        self.move_up_btn.setEnabled(has_rule)
        self.move_down_btn.setEnabled(has_rule)
        self._update_validation_state()

    def _select_row(self, row: int) -> None:
        """Move the selection ourselves, without the commit-on-leave handler."""
        self._building_ui = True
        try:
            self.rule_list.setCurrentRow(row)
        finally:
            self._building_ui = False
        self._load_row(row)

    def _on_rule_selected(self, row: int) -> None:
        if self._building_ui:
            return
        # _current_index still points at the row being left.
        self._commit_form()
        self._load_row(row)

    def _new_rule(self) -> None:
        self._commit_form()
        self.rules.append(default_rule())
        self._refresh_rule_list()
        self._select_row(len(self.rules) - 1)

    def _save_rule(self) -> None:
        if self._current_index < 0:
            return
        row = self._current_index
        self._commit_form()
        self._refresh_rule_list()
        self._select_row(row)

    def _delete_rule(self) -> None:
        row = self._current_index
        if row < 0 or row >= len(self.rules):
            return
        self.rules.pop(row)
        # Nothing to commit into any more: the edited rule is gone.
        self._current_index = -1
        self._refresh_rule_list()
        self._select_row(min(row, len(self.rules) - 1) if self.rules else -1)

    def _move_up(self) -> None:
        row = self._current_index
        if row <= 0 or row >= len(self.rules):
            return
        self._commit_form()
        self.rules[row - 1], self.rules[row] = self.rules[row], self.rules[row - 1]
        self._refresh_rule_list()
        self._select_row(row - 1)

    def _move_down(self) -> None:
        row = self._current_index
        if row < 0 or row >= len(self.rules) - 1:
            return
        self._commit_form()
        self.rules[row + 1], self.rules[row] = self.rules[row], self.rules[row + 1]
        self._refresh_rule_list()
        self._select_row(row + 1)

    def _save_all(self) -> None:
        self._commit_form()
        self.config.update_global(
            default_cap=self.default_cap.value(),
            show_unchanged=self.show_unchanged.isChecked(),
            dedupe_sync=self.dedupe_sync_groups.isChecked(),
        )
        self.config.replace_rules(self.rules)
        self.accept()


def show_config_dialog(parent=None) -> None:
    dialog = RelatedCardDisperseDialog(parent)
    dialog.exec()
