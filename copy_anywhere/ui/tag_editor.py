from typing import Optional
from aqt import mw

from aqt.qt import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QFormLayout,
    pyqtSignal,
)

from ..configuration import (
    COPY_MODE_WITHIN_NOTE,
    DIRECTION_DESTINATION_TO_SOURCES,
    CopyDefinition,
    CopyModeType,
    split_tags,
)

from ..shared.ui.multi_combo_box import MultiComboBox


from .stage_edit_state import StageEditState


class TagEditor(QWidget):
    """
    Class for editing tags to add/remove for a note or notes
    """

    #: Something the user changed here would change the stored tags.
    changed = pyqtSignal()

    def __init__(
        self,
        parent,
        state: StageEditState,
        copy_definition: Optional[CopyDefinition],
        copy_mode: CopyModeType,
    ):
        super().__init__(parent)
        self.state = state
        if copy_definition is None:
            self.add_tags_str = ""
            self.remove_tags_str = ""
        else:
            self.add_tags_str = copy_definition.get("add_tags", "")
            self.remove_tags_str = copy_definition.get("remove_tags", "")
        self.copy_definition = copy_definition
        self.copy_mode = copy_mode

        self.all_tags: list[str] = mw.col.tags.all()

        self.layout = QVBoxLayout()
        self.setLayout(self.layout)

        self.direction_callback = state.add_copy_direction_callback(
            self.update_direction_labels, is_visible=False
        )

        # Show two combo boxes for adding/removing tags
        self.form_layout = QFormLayout()
        self.layout.addLayout(self.form_layout)
        self.add_tags_label = QLabel("Tags to add")
        self.add_tags_combo_box = MultiComboBox(self)
        self.form_layout.addRow(self.add_tags_label, self.add_tags_combo_box)

        self.remove_tags_label = QLabel("Tags to remove")
        self.remove_tags_combo_box = MultiComboBox(self)
        self.form_layout.addRow(self.remove_tags_label, self.remove_tags_combo_box)

        # `dataChanged` on the model, not `currentTextChanged` on the box: ticking an item is
        # what a user does here, and `MultiComboBox.setCurrentText` blocks the model's signals
        # on purpose so that filling a box from stored text does not read as an edit.
        for box in (self.add_tags_combo_box, self.remove_tags_combo_box):
            box.model().dataChanged.connect(self._on_changed)

    def _fill_tag_box(self, box: MultiComboBox, stored: str):
        """Offer every tag in the collection, plus any this definition names, and select."""
        chosen = split_tags(stored)
        names = list(self.all_tags)
        for tag in chosen:
            if tag not in names:
                names.append(tag)
        box.addItems([f'"{name}"' for name in names])
        box.setCurrentText(", ".join(f'"{tag}"' for tag in chosen))

    def _on_changed(self, *_args) -> None:
        self.changed.emit()

    def get_add_tags(self) -> str:
        return self.add_tags_combo_box.currentText()

    def get_remove_tags(self) -> str:
        return self.remove_tags_combo_box.currentText()

    def update_direction_labels(self):
        if self.state.copy_mode == COPY_MODE_WITHIN_NOTE:
            add_tag_label_clarification = "to the trigger note"
            remove_tag_label_clarification = "from the trigger note"
        elif self.state.copy_direction == DIRECTION_DESTINATION_TO_SOURCES:
            add_tag_label_clarification = "to the trigger note"
            remove_tag_label_clarification = "from the trigger note"
        else:
            add_tag_label_clarification = "from the searched note"
            remove_tag_label_clarification = "to the searched note"

        self.add_tags_label.setText(f"Tags to add {add_tag_label_clarification}")
        self.remove_tags_label.setText(f"Tags to remove {remove_tag_label_clarification}")

    def enable_callbacks(self):
        self.direction_callback.is_visible = True

    def disable_callbacks(self):
        self.direction_callback.is_visible = False

    def initialize_ui_state(self):
        # Items carry the quotes, because that is the form `split_tags()` reads back and
        # the form `MultiComboBox.setCurrentText` has to match item for item: it splits the
        # stored string on ", " and looks for an item of exactly that text. With bare items
        # a two-tag selection matched nothing and silently loaded as no tags at all.
        self._fill_tag_box(self.remove_tags_combo_box, self.remove_tags_str)
        self._fill_tag_box(self.add_tags_combo_box, self.add_tags_str)

        self.update_direction_labels()
        self.enable_callbacks()
