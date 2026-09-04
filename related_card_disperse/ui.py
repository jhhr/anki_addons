from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Optional

from aqt import mw
from aqt.utils import showWarning
from aqt.qt import (
    QCheckBox,
    QDialog,
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


QUERY_CODE_NOTICE = (
    CODE_NOTICE_PREFIX
    + "<b>returns a query string or card-id list</b>. "
    + code_notice_available_names(
        extra_names=(("reviewed_card", "the reviewed card"), ("reviewed_note", "the reviewed note"))
    )
    + "<br>"
    + CODE_NOTICE_HTML_WARNING
)


class RelatedCardDisperseDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Related Card Disperse")
        self.resize(1000, 700)

        self.config = Config()
        self.config.load()
        self.rules: list[RelatedRule] = deepcopy(self.config.rules)

        self._building_ui = False

        root = QVBoxLayout(self)

        global_box = QWidget(self)
        global_form = QFormLayout(global_box)

        self.default_cap = QSpinBox(self)
        self.default_cap.setMinimum(1)
        self.default_cap.setMaximum(9999)
        self.default_cap.setValue(self.config.default_max_related_cards)

        self.show_no_overlap = QCheckBox("Show skipped outcome when due ranges do not overlap", self)
        self.show_no_overlap.setChecked(self.config.show_no_overlap_outcome)

        self.dedupe_sync_groups = QCheckBox(
            "Dedupe overlapping related-card groups during sync", self
        )
        self.dedupe_sync_groups.setChecked(self.config.dedupe_sync_groups)

        global_form.addRow("Default max related cards per execution", self.default_cap)
        global_form.addRow(self.show_no_overlap)
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

        editor = QWidget(self)
        form = QFormLayout(editor)

        self.rule_name = QLineEdit(self)
        self.rule_name.setPlaceholderText("Optional name")

        self.note_types = MultiComboBox(self)
        self._populate_note_types()

        self.on_review = QCheckBox("Run on reviewer_did_answer_card", self)
        self.on_sync = QCheckBox("Run for remotely reviewed cards after sync", self)
        self.use_code = QCheckBox("Use code mode", self)

        self.rule_cap = QLineEdit(self)
        self.rule_cap.setPlaceholderText("Optional override (positive integer)")

        self.query_text = InterpolatedTextEditLayout(
            label="Related card query",
            options_dict={},
            description="Use browser query syntax and interpolate with {{Field}}.",
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
            description="Return a query string or list of card ids.",
            notice=QUERY_CODE_NOTICE,
            is_required=False,
        )
        self.query_code.hide()

        form.addRow("Rule name", self.rule_name)
        form.addRow("Target note types", self.note_types)
        form.addRow(self.on_review)
        form.addRow(self.on_sync)
        form.addRow(self.use_code)
        form.addRow("Max related cards (rule override)", self.rule_cap)
        form.addRow(self.query_text_widget)
        form.addRow(self.query_code)

        right.addWidget(editor)

        action_row = QHBoxLayout()
        self.new_btn = QPushButton("New rule", self)
        self.save_rule_btn = QPushButton("Save rule", self)
        self.remove_rule_btn = QPushButton("Delete rule", self)
        action_row.addWidget(self.new_btn)
        action_row.addWidget(self.save_rule_btn)
        action_row.addWidget(self.remove_rule_btn)
        right.addLayout(action_row)

        footer = QHBoxLayout()
        self.save_all_btn = QPushButton("Save", self)
        self.cancel_btn = QPushButton("Cancel", self)
        footer.addStretch(1)
        footer.addWidget(self.save_all_btn)
        footer.addWidget(self.cancel_btn)
        root.addLayout(footer)

        self.rule_list.currentRowChanged.connect(self._on_rule_selected)
        self.move_up_btn.clicked.connect(self._move_up)
        self.move_down_btn.clicked.connect(self._move_down)
        self.new_btn.clicked.connect(self._new_rule)
        self.save_rule_btn.clicked.connect(self._save_rule)
        self.remove_rule_btn.clicked.connect(self._delete_rule)
        self.save_all_btn.clicked.connect(self._save_all)
        self.cancel_btn.clicked.connect(self.reject)
        self.use_code.toggled.connect(self._on_use_code_toggled)

        model = self.note_types.model()
        if model is not None:
            model.dataChanged.connect(lambda *_: self._update_query_options())

        self._refresh_rule_list()
        if self.rules:
            self.rule_list.setCurrentRow(0)
        else:
            self._building_ui = True
            self._set_form_from_rule(default_rule())
            self._building_ui = False
            self._update_query_options()

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

    def _refresh_rule_list(self) -> None:
        self.rule_list.blockSignals(True)
        self.rule_list.clear()
        for idx, rule in enumerate(self.rules, start=1):
            name = rule.get("name") or f"Rule {idx}"
            self.rule_list.addItem(name)
        self.rule_list.blockSignals(False)

    def _on_use_code_toggled(self, checked: bool) -> None:
        self.query_text_widget.setVisible(not checked)
        self.query_code.setVisible(checked)
        if checked and not self.query_code.get_text().strip():
            self.query_code.set_text(f"return {repr(self.query_text.get_text())}")

    def _set_form_from_rule(self, rule: RelatedRule) -> None:
        self.rule_name.setText(rule.get("name", ""))
        self.note_types.setCurrentText("")
        for note_type_name in self._decode_note_types(rule.get("target_note_types", "")):
            self.note_types.addSelectedItem(note_type_name)
        self.note_types.updateText()
        self.on_review.setChecked(rule.get("on_review", True))
        self.on_sync.setChecked(rule.get("on_sync", True))
        self.use_code.setChecked(rule.get("use_code", False))
        self.rule_cap.setText("" if rule.get("max_related_cards") is None else str(rule["max_related_cards"]))
        self.query_text.set_text(rule.get("related_card_query", ""))
        self.query_code.set_text(rule.get("query_code", ""))
        self._on_use_code_toggled(self.use_code.isChecked())

    @staticmethod
    def _decode_note_types(target_note_types: str) -> list[str]:
        if not target_note_types:
            return []
        return [n for n in target_note_types.strip('"').split('", "') if n]

    def _current_rule(self) -> RelatedRule:
        cap_raw = self.rule_cap.text().strip()
        cap_value: Optional[int]
        if not cap_raw:
            cap_value = None
        else:
            cap = int(cap_raw)
            if cap <= 0:
                raise ValueError("max_related_cards must be positive")
            cap_value = cap

        return RelatedRule(
            guid="",
            name=self.rule_name.text().strip(),
            target_note_types=self._serialize_selected_note_types(),
            related_card_query=self.query_text.get_text(),
            use_code=self.use_code.isChecked(),
            query_code=self.query_code.get_text(),
            on_review=self.on_review.isChecked(),
            on_sync=self.on_sync.isChecked(),
            max_related_cards=cap_value,
        )

    def _on_rule_selected(self, row: int) -> None:
        if row < 0 or row >= len(self.rules):
            return
        self._building_ui = True
        try:
            self._set_form_from_rule(self.rules[row])
        finally:
            self._building_ui = False
        self._update_query_options()

    def _new_rule(self) -> None:
        self._building_ui = True
        try:
            self._set_form_from_rule(default_rule())
            self.note_types.setCurrentText("")
        finally:
            self._building_ui = False
        self._update_query_options()
        self.rule_list.clearSelection()

    def _save_rule(self) -> None:
        try:
            rule = self._current_rule()
        except ValueError:
            showWarning("Max related cards must be a positive integer.")
            return

        current_row = self.rule_list.currentRow()
        if current_row < 0:
            rule["guid"] = str(uuid.uuid4())
            self.rules.append(rule)
            current_row = len(self.rules) - 1
        else:
            existing = self.rules[current_row]
            rule["guid"] = existing["guid"]
            self.rules[current_row] = rule

        self._refresh_rule_list()
        self.rule_list.setCurrentRow(current_row)

    def _delete_rule(self) -> None:
        row = self.rule_list.currentRow()
        if row < 0 or row >= len(self.rules):
            return
        self.rules.pop(row)
        self._refresh_rule_list()
        if self.rules:
            self.rule_list.setCurrentRow(max(0, row - 1))
        else:
            self._new_rule()

    def _move_up(self) -> None:
        row = self.rule_list.currentRow()
        if row <= 0:
            return
        self.rules[row - 1], self.rules[row] = self.rules[row], self.rules[row - 1]
        self._refresh_rule_list()
        self.rule_list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self.rule_list.currentRow()
        if row < 0 or row >= len(self.rules) - 1:
            return
        self.rules[row + 1], self.rules[row] = self.rules[row], self.rules[row + 1]
        self._refresh_rule_list()
        self.rule_list.setCurrentRow(row + 1)

    def _save_all(self) -> None:
        # Persist currently edited unsaved rule if list selection is active.
        if self.rule_list.currentRow() >= 0:
            self._save_rule()

        self.config.update_global(
            default_cap=self.default_cap.value(),
            show_no_overlap=self.show_no_overlap.isChecked(),
            dedupe_sync=self.dedupe_sync_groups.isChecked(),
        )
        self.config.replace_rules(self.rules)
        self.accept()


def show_config_dialog(parent=None) -> None:
    dialog = RelatedCardDisperseDialog(parent)
    dialog.exec()
