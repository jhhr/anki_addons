import html
from typing import Optional, Dict
import uuid

from aqt import mw
from aqt.qt import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QFrame,
    QLabel,
    QPushButton,
    QComboBox,
    QButtonGroup,
    QRadioButton,
    QDoubleSpinBox,
    QLineEdit,
    QTimer,
    pyqtSignal,
    qtmajor,
)

if qtmajor > 5:
    QFrameStyledPanel = QFrame.Shape.StyledPanel
    QFrameShadowRaised = QFrame.Shadow.Raised
else:
    QFrameStyledPanel = QFrame.StyledPanel  # type: ignore
    QFrameShadowRaised = QFrame.Raised  # type: ignore

from ..configuration import (
    CARD_TYPE_SEPARATOR,
    COPY_MODE_ACROSS_NOTES,
    DIRECTION_SOURCE_TO_DESTINATIONS,
    CardAction,
    CopyDefinition,
)
from ..shared.ui.code_edit_layout import CodeEditLayout
from ..shared.ui.loading_indicator import LoadingIndicator
from .code_notices import CARD_ACTION_CODE_NOTICE
from .stage_edit_state import StageEditState
from ..shared.ui.grouped_combo_box import GroupedComboBox
from ..shared.ui.toggle_switch import ToggleSwitch

INITIAL_ROWS_PER_TICK = 1

base_description = """<p>Configure actions to perform on any destination note's card types.
            Select a card type from the dropdown to configure its action.</p>"""

source_to_destinations_description = (
    "<p><small>Note: Card actions are performed on the queried notes' cards.</small></p>"
)
destination_to_sources_description = (
    "<p><small>Note: Card actions are performed on the trigger note's cards.</small></p>"
)


def _card_action_to_initial_code(
    change_deck=None,
    set_flag=None,
    suspend=None,
    bury=None,
    set_desired_retention=None,
) -> str:
    """Generate initial Python code that returns a CardActionDict from the given form values.

    Produces a ``return { ... }`` block pre-populated with the current form
    values so the user has a ready-made starting point when switching to code
    mode.
    """
    return (
        "return {\n"
        f'    "change_deck": {change_deck!r},\n'
        f'    "set_flag": {set_flag!r},\n'
        f'    "suspend": {suspend!r},\n'
        f'    "bury": {bury!r},\n'
        f'    "set_desired_retention": {set_desired_retention!r},\n'
        "}"
    )


class CardActionsEditor(QWidget):
    """
    Editor for card actions. Shows a dropdown to select card types from the selected note types,
    and inline editors for each CardAction property (change_deck, set_flag, suspend, bury).
    """

    #: Something the user changed here would change the stored card actions.
    changed = pyqtSignal()

    def __init__(
        self,
        parent,
        state: StageEditState,
        copy_definition: Optional[CopyDefinition],
        single_card_mode: bool = False,
    ):
        """
        :param single_card_mode: edit actions that apply to one already-chosen card rather
            than to a note's cards of a given type. A format-2 `edit_card` stage names the
            card itself (§5.12), so there is no card type to pick and the actions are keyed
            by their own guid instead of by a card type name.
        """
        super().__init__(parent)
        self.state = state
        self.copy_definition = copy_definition
        self.single_card_mode = single_card_mode
        self.initialized = False
        self._loading_initial_actions = False
        self._building_initial_actions = False
        self._load_queue: list[tuple[str, CardAction]] = []
        self._load_total = 0
        self.loading_indicator: Optional[LoadingIndicator] = None

        # Both fire when the note these actions reach changes; the first also when the
        # trigger's note types do.
        state.add_selected_model_callback(self.relist_card_types)
        state.add_copy_direction_callback(self.set_description)

        self.vbox = QVBoxLayout()
        self.setLayout(self.vbox)

        # Add description label
        self.description_label = QLabel(self)
        self.vbox.addWidget(self.description_label)

        # Container for all action editors (displayed inline)
        self.actions_container_widget = QWidget(self)
        self.actions_layout = QVBoxLayout(self.actions_container_widget)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.vbox.addWidget(self.actions_container_widget)

        # Add new action button and selector
        self.card_type_selector = GroupedComboBox(self, is_required=False)
        self.card_type_selector.setPlaceholderText("Select a card type to add")
        self.add_action_button = QPushButton("Add Card Action", self)
        self.add_action_button.clicked.connect(self.add_new_action)

        add_action_layout = QHBoxLayout()
        self.card_type_label = QLabel("<h3>Card Type:</h3>")
        add_action_layout.addWidget(self.card_type_label)
        add_action_layout.addWidget(self.card_type_selector)
        add_action_layout.addWidget(self.add_action_button)
        add_action_layout.addStretch()

        self.vbox.addLayout(add_action_layout)

        # Map card type names to their UI components
        self.action_ui_components: Dict[str, Dict] = {}

        # Map card type names to their CardAction definitions
        self.card_actions: Dict[str, CardAction] = {}

        # Load existing card actions from copy_definition
        if copy_definition and copy_definition.get("card_actions"):
            for action in copy_definition["card_actions"]:
                if single_card_mode:
                    key = action.get("guid") or str(uuid.uuid4())
                    action["guid"] = key
                    self.card_actions[key] = action
                    continue
                card_type_name = action.get("card_type_name", "")
                if card_type_name:
                    self.card_actions[card_type_name] = action

        if single_card_mode:
            # Nothing to pick: the stage already named the card these actions apply to.
            self.card_type_selector.hide()
            self.card_type_label.hide()
            self.add_action_button.setText("Add Card Action")

    def _on_changed(self, *_args) -> None:
        if self._building_initial_actions or self._loading_initial_actions:
            # Building the rows a definition arrived with is not the user editing them.
            return
        self.changed.emit()

    def set_description(self):
        """Update the description label based on the current copy mode and direction"""
        if self.single_card_mode:
            self.description_label.setText("")
            return
        if self.state.copy_mode == COPY_MODE_ACROSS_NOTES:
            if self.state.copy_direction == DIRECTION_SOURCE_TO_DESTINATIONS:
                description = source_to_destinations_description
            else:
                description = destination_to_sources_description
        else:  # COPY_MODE_WITHIN_NOTE
            description = ""  # or a specific description for within-note mode
        self.description_label.setText(base_description + description)

    def initialize_ui_state(self):
        """Perform expensive UI state initialization when component is first shown"""
        if self.initialized:
            return

        self.update_card_type_options()
        self.set_description()

        if self.card_actions:
            self._load_queue = list(self.card_actions.items())
            self._load_total = len(self._load_queue)
            self.loading_indicator = LoadingIndicator(
                f"Loading card actions... (0/{self._load_total})",
                self.actions_container_widget,
            )
            self.actions_layout.addWidget(self.loading_indicator)
            self.card_type_selector.setDisabled(True)
            self.add_action_button.setDisabled(True)
            self._start_loading_initial_actions()

        self.initialized = True

    def _start_loading_initial_actions(self):
        if self._loading_initial_actions:
            return
        self._loading_initial_actions = True
        self._building_initial_actions = True
        QTimer.singleShot(0, self._process_load_queue)

    def _process_load_queue(self):
        for _ in range(INITIAL_ROWS_PER_TICK):
            if not self._load_queue:
                break
            card_type_name, action = self._load_queue.pop(0)
            self.create_action_editor(card_type_name, action)

        if self._load_queue:
            if self.loading_indicator is not None:
                loaded_count = self._load_total - len(self._load_queue)
                self.loading_indicator.set_text(
                    f"Loading card actions... ({loaded_count}/{self._load_total})"
                )
            QTimer.singleShot(0, self._process_load_queue)
        else:
            self._finish_loading_initial_actions()

    def _finish_loading_initial_actions(self):
        if self.loading_indicator is not None:
            self.actions_layout.removeWidget(self.loading_indicator)
            self.loading_indicator.deleteLater()
            self.loading_indicator = None
        self._building_initial_actions = False
        self._loading_initial_actions = False
        if self.single_card_mode:
            # `update_card_type_options` returns early in this mode -- there is no card type
            # to pick -- and the re-enable lives after that line, so it would never run. A
            # stage that arrived with an action would be stuck with a greyed-out Add button
            # for the life of the dialog, while one with no actions never takes the loading
            # path and can always add its first.
            self.card_type_selector.setDisabled(False)
            self.add_action_button.setDisabled(False)
            return
        self.update_card_type_options()

    def finish_loading_initial_actions(self):
        if not self._load_queue:
            return
        self._building_initial_actions = True
        while self._load_queue:
            card_type_name, action = self._load_queue.pop(0)
            self.create_action_editor(card_type_name, action)
        self._finish_loading_initial_actions()

    def relist_card_types(self):
        """The note these actions reach changed: keep only what it can have, then relist.

        A stage retargeted from a queried note to the trigger, or whose trigger note type
        was changed at the top of the dialog, may hold an action for a card type the note
        it now edits cannot have. Saved, that action matches nothing at run time, and no
        later edit makes it apply again -- so it is dropped rather than kept and marked.
        A stage editing a queried note offers every card type, and one with no trigger
        note type chosen yet has nothing to check against, so neither drops anything.
        """
        offered = self._offered_card_types()
        if offered is not None:
            for card_type_name in list(self.card_actions):
                if card_type_name not in offered:
                    self._discard_action(card_type_name)
        self.update_card_type_options()

    def update_code_editor_options(self):
        """Give every action's code editor the menu and validation the state has now.

        Each code editor is handed the state's menu when its row is built. The stage above
        replaces the state's context whenever an edit anywhere in the dialog moves the
        stage's scope -- a variable renamed or added upstream, say -- and a row already
        built would go on offering, and validating against, the names it was built with.
        A row built later reads the state as it is then, so rows still in the staged load
        need nothing.
        """
        for ui_components in self.action_ui_components.values():
            ui_components["code_editor"].update_options(
                self.state.post_query_menu_options_dict,
                self.state.post_query_text_edit_validate_dict,
            )

    def _offered_card_types(self) -> Optional[set[str]]:
        """The card types the selector lists, or None when it lists every one there is."""
        if self.single_card_mode or self.state.copy_mode == COPY_MODE_ACROSS_NOTES:
            return None
        if not self.state.selected_models:
            return None
        return {
            f"{model['name']}{CARD_TYPE_SEPARATOR}{template.get('name', '')}"
            for model in self.state.selected_models
            for template in model.get("tmpls", [])
        }

    def update_card_type_options(self):
        """
        Depending on copy_mode, either update the card type dropdown with card types from
        selected note types or all note types in the collection.
        Only shows card types that haven't been added yet.
        """
        if self.single_card_mode:
            return
        current_text = self.card_type_selector.currentText()
        self.card_type_selector.blockSignals(True)
        self.card_type_selector.clear()

        has_available_types = False

        if (
            self.state.copy_mode == COPY_MODE_ACROSS_NOTES
            and self.state.copy_direction == DIRECTION_SOURCE_TO_DESTINATIONS
        ):
            # in this mode, card actions apply to destination notes' cards so show all card types
            # from all existing note types
            all_note_types = mw.col.models.all()
            for model in all_note_types:
                model_name = model["name"]
                templates = model.get("tmpls", [])

                # Collect available templates for this model (not already added)
                available_templates = []
                for template in templates:
                    template_name = template.get("name", "")
                    item_text = f"{model_name}{CARD_TYPE_SEPARATOR}{template_name}"
                    # Only add if not already in card_actions
                    if item_text not in self.card_actions:
                        available_templates.append(item_text)

                # Only add the group if there are available templates
                if available_templates:
                    self.card_type_selector.addGroup(model_name)
                    for item_text in available_templates:
                        self.card_type_selector.addItemToGroup(model_name, item_text)
                        has_available_types = True

            # Restore previous selection if it still exists and is still available
            if current_text and current_text not in self.card_actions:
                index = self.card_type_selector.findText(current_text)
                if index >= 0:
                    self.card_type_selector.setCurrentIndex(index)

            self.card_type_selector.blockSignals(False)

            # Disable if no available types
            if not has_available_types:
                self.card_type_selector.setPlaceholderText("All card types have been configured")
                self.card_type_selector.setDisabled(True)
                self.add_action_button.setDisabled(True)
            else:
                self.card_type_selector.setPlaceholderText("Select a card type to add")
                self.card_type_selector.setDisabled(False)
                self.add_action_button.setDisabled(False)

            return

        if not self.state.selected_models:
            self.card_type_selector.setPlaceholderText("First, select a trigger note type")
            self.card_type_selector.setDisabled(True)
            self.add_action_button.setDisabled(True)
            self.card_type_selector.blockSignals(False)
            return

        # Group card types by note type
        for model in self.state.selected_models:
            model_name = model["name"]
            templates = model.get("tmpls", [])

            # Collect available templates for this model (not already added)
            available_templates = []
            for template in templates:
                template_name = template.get("name", "")
                item_text = f"{model_name}{CARD_TYPE_SEPARATOR}{template_name}"
                # Only add if not already in card_actions
                if item_text not in self.card_actions:
                    available_templates.append(item_text)

            # Only add the group if there are available templates
            if available_templates:
                self.card_type_selector.addGroup(model_name)
                for item_text in available_templates:
                    self.card_type_selector.addItemToGroup(model_name, item_text)
                    has_available_types = True

        # Restore previous selection if it still exists and is still available
        if current_text and current_text not in self.card_actions:
            index = self.card_type_selector.findText(current_text)
            if index >= 0:
                self.card_type_selector.setCurrentIndex(index)

        self.card_type_selector.blockSignals(False)

        # Disable if no available types
        if not has_available_types:
            self.card_type_selector.setPlaceholderText("All card types have been configured")
            self.card_type_selector.setDisabled(True)
            self.add_action_button.setDisabled(True)
        else:
            self.card_type_selector.setPlaceholderText("Select a card type to add")
            self.card_type_selector.setDisabled(False)
            self.add_action_button.setDisabled(False)

    def add_new_action(self):
        """Called when the Add Card Action button is clicked"""
        self.finish_loading_initial_actions()
        if self.single_card_mode:
            self._add_single_card_action()
            return
        card_type_name = self.card_type_selector.currentText()

        if not card_type_name:
            return

        # Check if action already exists
        if card_type_name in self.card_actions:
            return

        # Create new action
        new_action: CardAction = {
            "guid": str(uuid.uuid4()),
            "card_type_name": card_type_name,
            "change_deck": None,
            "set_flag": None,
            "suspend": None,
            "bury": None,
            "set_desired_retention": None,
            "use_code": False,
            "action_code": "",
        }

        self.card_actions[card_type_name] = new_action

        # Create and display the action editor
        self.create_action_editor(card_type_name, new_action)
        self._on_changed()

        # Clear the selector and refresh dropdown to remove the added card type
        self.card_type_selector.setCurrentIndex(-1)
        self.update_card_type_options()

    def _add_single_card_action(self):
        """Add one more action to the card the stage already named."""
        key = str(uuid.uuid4())
        new_action: CardAction = {
            "guid": key,
            "card_type_name": "",
            "change_deck": None,
            "set_flag": None,
            "suspend": None,
            "bury": None,
            "set_desired_retention": None,
            "use_code": False,
            "action_code": "",
        }
        self.card_actions[key] = new_action
        self.create_action_editor(key, new_action)
        self._on_changed()

    def create_action_editor(self, card_type_name: str, action: CardAction):
        """Create the UI for editing a single CardAction and add it inline"""
        # Don't create if already exists
        if card_type_name in self.action_ui_components:
            return

        # Create a frame for the action editor
        frame = QFrame(self.actions_container_widget)
        frame.setFrameShape(QFrameStyledPanel)
        frame.setFrameShadow(QFrameShadowRaised)
        frame_layout = QVBoxLayout(frame)
        self.actions_layout.addWidget(frame)

        # Header
        header = QLabel(
            "<h3>Card action</h3>"
            if self.single_card_mode
            else f"<h3>Actions for card type: <em>{html.escape(card_type_name)}</em></h3>",
            frame,
        )
        frame_layout.addWidget(header)

        # Code mode toggle
        use_code_toggle = ToggleSwitch("Execute as Python code")
        frame_layout.addWidget(use_code_toggle)

        # --- Form mode container ---
        form_mode_container = QWidget(frame)
        form_layout = QFormLayout(form_mode_container)

        # 1. Change Deck dropdown
        deck_combo = QComboBox(form_mode_container)
        deck_combo.addItem("-")
        all_decks = mw.col.decks.all_names_and_ids()
        for deck_name_and_id in all_decks:
            deck_combo.addItem(deck_name_and_id.name)
        current_deck = action.get("change_deck")
        if current_deck:
            index = deck_combo.findText(current_deck)
            if index >= 0:
                deck_combo.setCurrentIndex(index)
        else:
            deck_combo.setCurrentIndex(0)
        form_layout.addRow(QLabel("<b>Move card to deck:</b>", form_mode_container), deck_combo)

        # 2. Set Flag button group
        flag_group = QButtonGroup(form_mode_container)
        flag_layout = QHBoxLayout()
        flag_options = [
            (None, "N/A"),
            (0, "No flag"),
            (1, "Red"),
            (2, "Orange"),
            (3, "Green"),
            (4, "Blue"),
            (5, "Pink"),
            (6, "Turquoise"),
            (7, "Purple"),
        ]
        current_flag = action.get("set_flag")
        for value, text in flag_options:
            radio = QRadioButton(text, form_mode_container)
            radio.setProperty("flag_value", value)
            flag_group.addButton(radio)
            flag_layout.addWidget(radio)
            if current_flag == value or (current_flag is None and value is None):
                radio.setChecked(True)
        form_layout.addRow(QLabel("<b>Set card flag:</b>", form_mode_container), flag_layout)

        # 3. Suspend button group
        suspend_group = QButtonGroup(form_mode_container)
        suspend_layout = QHBoxLayout()
        current_suspend = action.get("suspend")
        for value, text in [(None, "N/A"), (True, "Suspend"), (False, "Unsuspend")]:
            radio = QRadioButton(text, form_mode_container)
            radio.setProperty("suspend_value", value)
            suspend_group.addButton(radio)
            suspend_layout.addWidget(radio)
            if current_suspend == value or (current_suspend is None and value is None):
                radio.setChecked(True)
        form_layout.addRow(QLabel("<b>Suspend card:</b>", form_mode_container), suspend_layout)

        # 4. Bury button group
        bury_group = QButtonGroup(form_mode_container)
        bury_layout = QHBoxLayout()
        current_bury = action.get("bury")
        for value, text in [(None, "N/A"), (True, "Bury"), (False, "Unbury")]:
            radio = QRadioButton(text, form_mode_container)
            radio.setProperty("bury_value", value)
            bury_group.addButton(radio)
            bury_layout.addWidget(radio)
            if current_bury == value or (current_bury is None and value is None):
                radio.setChecked(True)
        form_layout.addRow(QLabel("<b>Bury card:</b>", form_mode_container), bury_layout)

        # 5. Set desired retention
        dr_layout = QHBoxLayout()
        dr_number_input = QDoubleSpinBox(form_mode_container)
        dr_number_input.setRange(0.0, 0.99)
        dr_number_input.setSingleStep(0.01)
        dr_number_input.setDecimals(2)
        dr_number_input.setSpecialValueText(" ")
        dr_number_input.setValue(0.0)
        dr_string_input = QLineEdit(form_mode_container)
        dr_string_input.setPlaceholderText("Custom data property name")
        current_dr = action.get("set_desired_retention")
        if isinstance(current_dr, (float, int)) and not isinstance(current_dr, bool):
            dr_number_input.setValue(float(current_dr))
        elif isinstance(current_dr, str) and current_dr:
            dr_string_input.setText(current_dr)

        def _dr_number_changed(value, _s=dr_string_input, _n=dr_number_input):
            if value >= 0.01:
                _s.blockSignals(True)
                _s.clear()
                _s.blockSignals(False)

        def _dr_string_changed(text, _n=dr_number_input):
            if text:
                _n.blockSignals(True)
                _n.setValue(0.0)
                _n.blockSignals(False)

        dr_number_input.valueChanged.connect(_dr_number_changed)
        dr_string_input.textChanged.connect(_dr_string_changed)
        dr_layout.addWidget(QLabel("Float (0.01\u20130.99):", form_mode_container))
        dr_layout.addWidget(dr_number_input)
        dr_layout.addWidget(QLabel("or custom data property:", form_mode_container))
        dr_layout.addWidget(dr_string_input)
        dr_layout.addStretch()
        form_layout.addRow(QLabel("<b>Set desired retention:</b>", form_mode_container), dr_layout)

        frame_layout.addWidget(form_mode_container)

        # --- Code mode widget ---
        code_editor = CodeEditLayout(
            parent=frame,
            options_dict=self.state.post_query_menu_options_dict,
            is_required=False,
            label=None,
            description=None,
            notice=CARD_ACTION_CODE_NOTICE,
        )
        code_editor.update_options(
            self.state.post_query_menu_options_dict,
            self.state.post_query_text_edit_validate_dict,
        )
        code_editor.set_text(action.get("action_code", "") or "")
        code_editor.hide()
        frame_layout.addWidget(code_editor)

        # Apply initial mode and wire toggle
        initial_use_code = action.get("use_code", False)
        if initial_use_code:
            use_code_toggle.setChecked(True)
            form_mode_container.hide()
            code_editor.show()

        def on_use_code_toggled(checked: bool):
            form_mode_container.setVisible(not checked)
            code_editor.setVisible(checked)
            if checked and not code_editor.get_text().strip():
                deck_text = deck_combo.currentText()
                current_change_deck = None if deck_text == "-" else deck_text
                current_set_flag = next(
                    (b.property("flag_value") for b in flag_group.buttons() if b.isChecked()),
                    None,
                )
                current_suspend = next(
                    (b.property("suspend_value") for b in suspend_group.buttons() if b.isChecked()),
                    None,
                )
                current_bury = next(
                    (b.property("bury_value") for b in bury_group.buttons() if b.isChecked()),
                    None,
                )
                dr_string = dr_string_input.text().strip()
                dr_number = dr_number_input.value()
                current_dr = dr_string or (dr_number if dr_number >= 0.01 else None)
                code_editor.set_text(
                    _card_action_to_initial_code(
                        change_deck=current_change_deck,
                        set_flag=current_set_flag,
                        suspend=current_suspend,
                        bury=current_bury,
                        set_desired_retention=current_dr,
                    )
                )

        use_code_toggle.toggled.connect(on_use_code_toggled)

        # Delete button
        delete_button = QPushButton("Delete this card action", frame)
        delete_button.clicked.connect(lambda: self.delete_action(card_type_name))
        frame_layout.addWidget(delete_button)

        # Every control that can change what `get_card_actions()` returns reports it, so the
        # stage editor above can fold this panel back into the definition and mark the
        # preview's trace stale. Without it an edit here stayed in the widget: the preview
        # re-ran the definition as it was before the change and presented that as current.
        deck_combo.currentIndexChanged.connect(self._on_changed)
        for group in (flag_group, suspend_group, bury_group):
            group.buttonToggled.connect(self._on_changed)
        dr_number_input.valueChanged.connect(self._on_changed)
        dr_string_input.textChanged.connect(self._on_changed)
        use_code_toggle.toggled.connect(self._on_changed)
        code_editor.text_edit.textChanged.connect(self._on_changed)

        # Store UI components for later retrieval
        self.action_ui_components[card_type_name] = {
            "frame": frame,
            "use_code_toggle": use_code_toggle,
            "form_mode_container": form_mode_container,
            "deck_combo": deck_combo,
            "flag_group": flag_group,
            "suspend_group": suspend_group,
            "bury_group": bury_group,
            "dr_number_input": dr_number_input,
            "dr_string_input": dr_string_input,
            "code_editor": code_editor,
        }

    def save_action(self, card_type_name: str):
        """Save a specific action from its UI components"""
        if card_type_name not in self.action_ui_components:
            return

        ui = self.action_ui_components[card_type_name]
        use_code = ui["use_code_toggle"].isChecked()

        # Read form fields (always saved so values are preserved when toggling modes)
        deck_text = ui["deck_combo"].currentText()
        change_deck = None if deck_text == "-" else deck_text

        set_flag = None
        for button in ui["flag_group"].buttons():
            if button.isChecked():
                set_flag = button.property("flag_value")
                break

        suspend = None
        for button in ui["suspend_group"].buttons():
            if button.isChecked():
                suspend = button.property("suspend_value")
                break

        bury = None
        for button in ui["bury_group"].buttons():
            if button.isChecked():
                bury = button.property("bury_value")
                break

        dr_string = ui["dr_string_input"].text().strip()
        dr_number = ui["dr_number_input"].value()
        if dr_string:
            set_desired_retention = dr_string
        elif dr_number >= 0.01:
            set_desired_retention = dr_number
        else:
            set_desired_retention = None

        existing = self.card_actions.get(card_type_name, {})
        self.card_actions[card_type_name] = {
            "guid": existing.get("guid", str(uuid.uuid4())),
            # In single-card mode the key is the action's own guid, not a card type, and
            # the stage's target says which card it applies to.
            "card_type_name": "" if self.single_card_mode else card_type_name,
            "change_deck": change_deck,
            "set_flag": set_flag,
            "suspend": suspend,
            "bury": bury,
            "set_desired_retention": set_desired_retention,
            "use_code": use_code,
            "action_code": ui["code_editor"].get_text(),
        }

    def _discard_action(self, card_type_name: str):
        """Forget one action, its row, and its place in the staged load."""
        self.card_actions.pop(card_type_name, None)
        self._load_queue = [
            (name, action) for name, action in self._load_queue if name != card_type_name
        ]
        ui_components = self.action_ui_components.pop(card_type_name, None)
        if ui_components is not None:
            frame = ui_components["frame"]
            self.actions_layout.removeWidget(frame)
            frame.deleteLater()

    def delete_action(self, card_type_name: str):
        """Delete a card action and its UI"""
        self._discard_action(card_type_name)
        # Refresh dropdown to show the deleted card type as available again
        self.update_card_type_options()
        self._on_changed()

    def get_card_actions(self) -> list[CardAction]:
        """Return the list of card actions"""
        self.finish_loading_initial_actions()
        # Save all actions from their UI components
        for card_type_name in list(self.action_ui_components.keys()):
            self.save_action(card_type_name)

        # Include actions with non-empty code or any non-None form field
        result = []
        for action in self.card_actions.values():
            if (
                (action.get("use_code", False) and action.get("action_code", "").strip())
                or action.get("change_deck") is not None
                or action.get("set_flag") is not None
                or action.get("suspend") is not None
                or action.get("bury") is not None
                or action.get("set_desired_retention") is not None
            ):
                result.append(action)

        return result
