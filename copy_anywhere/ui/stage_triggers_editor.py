"""The trigger controls at the top of a format-2 definition.

Trigger filtering stays outside the stage interpreter (§8): which notes a definition even
considers is decided by note type, deck and the event booleans, before any stage runs. So
these controls are a plain editor over `definition["triggers"]` rather than anything the
stage list knows about.

They were written from scratch rather than reusing the format-1 editor's trigger form: that
one wrote through `EditState` into a definition's flat, comma-joined keys, while format 2
keeps the same settings as JSON arrays under one `triggers` object (§4). Both it and
`EditState` were removed with the rest of the format-1 editor.
"""

from typing import Optional

from anki.decks import DeckId
from anki.utils import ids2str
from aqt import mw
from aqt.qt import (
    QCheckBox,
    QFormLayout,
    QLabel,
    QWidget,
    pyqtSignal,
)

from ..logic.definition_schema import CopyDefinitionV2, Triggers
from ..shared.ui.multi_combo_box import MultiComboBox
from ..shared.ui.required_text_input import RequiredLineEdit


def quoted_items(names) -> list[str]:
    return [f'"{name}"' for name in names]


def selected_names(box: MultiComboBox) -> list[str]:
    """The MultiComboBox keeps its selection as one quoted, comma-joined string."""
    text = (box.currentText() or "").strip()
    if not text:
        return []
    return [name for name in text.strip('""').split('", "') if name]


def decks_of_note_types(note_type_names) -> list[dict]:
    """Every deck holding a card of one of these note types, plus their parents.

    Same rule as format 1's deck limit box: offering every deck in the collection buries
    the handful that could ever match.
    """
    assert mw is not None and mw.col is not None and mw.col.db is not None
    models = [mw.col.models.by_name(name) for name in note_type_names]
    mids = [model["id"] for model in models if model is not None]
    if not mids:
        return []
    dids: list[DeckId] = mw.col.db.list(f"""
            SELECT DISTINCT CASE WHEN odid==0 THEN did ELSE odid END
            FROM cards c, notes n
            WHERE n.mid IN {ids2str(mids)}
            AND c.nid = n.id
        """)
    decks = [deck for deck in (mw.col.decks.get(did) for did in dids) if deck is not None]
    seen = {deck["name"] for deck in decks}
    for deck in list(decks):
        for parent in mw.col.decks.parents(deck["id"]):
            if parent["name"] not in seen:
                decks.append(parent)
                seen.add(parent["name"])
    decks.sort(key=lambda deck: deck["name"])
    return decks


def field_names_of(note_type_names) -> list[str]:
    assert mw is not None and mw.col is not None
    names: list[str] = []
    for name in note_type_names:
        model = mw.col.models.by_name(name)
        if model is None:
            continue
        for field in mw.col.models.field_names(model):
            if field not in names:
                names.append(field)
    return names


class TriggersEditor(QWidget):
    """Name, note types, deck limit and the events a definition runs on."""

    changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget], definition: CopyDefinitionV2) -> None:
        super().__init__(parent)
        self.definition = definition
        triggers: Triggers = definition.setdefault(
            "triggers",
            {
                "note_types": [],
                "deck_names": [],
                "include_subdecks": False,
                "on_sync": False,
                "on_add": False,
                "on_review": False,
                "on_unfocus": {"edit_fields": [], "add_fields": []},
            },
        )
        self.triggers = triggers
        form = QFormLayout(self)
        self.form = form

        self.name_edit = RequiredLineEdit(self, is_required=True)
        self.name_edit.setText(definition.get("definition_name", "") or "")
        self.name_edit.update_required_style()
        self.name_edit.textChanged.connect(self._on_changed)
        form.addRow(QLabel("<h3>Name for this copy definition</h3>", self), self.name_edit)

        assert mw is not None and mw.col is not None
        self.note_types_box = MultiComboBox(
            self, placeholder_text="Select note types", is_required=True
        )
        self.note_types_box.setMinimumWidth(300)
        self.note_types_box.addItems(
            quoted_items(model.name for model in mw.col.models.all_names_and_ids())
        )
        self.note_types_box.setCurrentText(
            ", ".join(quoted_items(triggers.get("note_types", []) or []))
        )
        self.note_types_box.currentTextChanged.connect(self._on_note_types_changed)
        form.addRow(QLabel("<h3>Trigger note type</h3>", self), self.note_types_box)

        self.note_type_warning = QLabel("", self)
        self.note_type_warning.setWordWrap(True)
        form.addRow("", self.note_type_warning)

        self.decks_box = MultiComboBox(
            self, placeholder_text="First, select a trigger note type"
        )
        self.decks_box.setMinimumWidth(300)
        self.decks_box.currentTextChanged.connect(self._on_changed)
        form.addRow(QLabel("<h4>Trigger deck limit</h4>", self), self.decks_box)
        form.addRow(
            "",
            QLabel(
                "<small>Cards belong to decks, not notes. A note passes the limit when any"
                " of its cards is in a listed deck.</small>",
                self,
            ),
        )

        self.include_subdecks = QCheckBox("Include subdecks of selected decks", self)
        self.include_subdecks.setChecked(bool(triggers.get("include_subdecks")))
        self.include_subdecks.toggled.connect(self._on_changed)
        form.addRow("", self.include_subdecks)

        self.on_sync = QCheckBox("Run on sync for reviewed cards", self)
        self.on_sync.setChecked(bool(triggers.get("on_sync")))
        self.on_sync.toggled.connect(self._on_changed)
        form.addRow("", self.on_sync)

        self.on_add = QCheckBox("Run when adding a new note", self)
        self.on_add.setChecked(bool(triggers.get("on_add")))
        self.on_add.toggled.connect(self._on_changed)
        form.addRow("", self.on_add)

        self.on_review = QCheckBox("Run on review", self)
        self.on_review.setChecked(bool(triggers.get("on_review")))
        self.on_review.toggled.connect(self._on_changed)
        form.addRow("", self.on_review)

        unfocus = triggers.setdefault("on_unfocus", {"edit_fields": [], "add_fields": []})
        self.unfocus_edit = MultiComboBox(self, placeholder_text="No fields (never)")
        self.unfocus_edit.currentTextChanged.connect(self._on_changed)
        form.addRow(
            QLabel("<h4>Run when leaving a field, editing a note</h4>", self),
            self.unfocus_edit,
        )
        self.unfocus_add = MultiComboBox(self, placeholder_text="No fields (never)")
        self.unfocus_add.currentTextChanged.connect(self._on_changed)
        form.addRow(
            QLabel("<h4>Run when leaving a field, adding a note</h4>", self), self.unfocus_add
        )
        form.addRow(
            "",
            QLabel(
                "<small>Unfocus runs the whole definition, not just the stages that mention"
                " the field you left.</small>",
                self,
            ),
        )
        # What the user has chosen, as opposed to what the boxes can currently offer. The
        # boxes are rebuilt from the note types on every change, so their contents cannot be
        # the record: a name that the current note types do not have would be read back as
        # "not chosen" and lost. Kept here instead, and narrowed only by what the user does.
        self._chosen_decks = list(triggers.get("deck_names", []) or [])
        self._chosen_unfocus = [
            list(unfocus.get("edit_fields", []) or []),
            list(unfocus.get("add_fields", []) or []),
        ]
        #: What each box was last filled with, or None before it has been filled at all. An
        #: empty box means nothing until this is known: it is empty to begin with because
        #: nothing has been put in it, not because the user emptied it.
        self._offered_decks: Optional[list[str]] = None
        self._offered_unfocus: list[Optional[list[str]]] = [None, None]
        self._refresh_dependent_boxes()

    # -- reacting ------------------------------------------------------------------------

    def _on_changed(self, *_args) -> None:
        self.changed.emit()

    def _on_note_types_changed(self, *_args) -> None:
        self._refresh_dependent_boxes()
        self.changed.emit()

    def _refresh_dependent_boxes(self) -> None:
        note_types = selected_names(self.note_types_box)
        self._refresh_decks(note_types)
        self._refresh_unfocus(note_types)
        self._refresh_warning(note_types)

    def _take_choice(
        self, box: MultiComboBox, chosen: list[str], offered: Optional[list[str]]
    ) -> None:
        """Fold what `box` is showing back into `chosen`, in place, before it is refilled.

        The box only ever offered `offered`, the names of the note types selected when it
        was last filled, so that is all it can answer for. Names outside that list belong to
        a note type that was not selected then and are left alone -- deselecting a note type
        and selecting it again must not lose its fields or decks. Within it the box is the
        whole answer, so a name the user unticked is gone, including the last one.

        `offered` is None before the box has been filled at all, and then it answers for
        nothing: it is empty because nothing has been put in it.
        """
        if offered is None:
            return
        live = selected_names(box)
        chosen[:] = [name for name in chosen if name not in offered] + live

    def _refresh_decks(self, note_types) -> None:
        decks = decks_of_note_types(note_types)
        self._take_choice(self.decks_box, self._chosen_decks, self._offered_decks)
        self._offered_decks = [deck["name"] for deck in decks]
        chosen = self._chosen_decks
        self.decks_box.blockSignals(True)
        self.decks_box.clear()
        for deck in decks:
            self.decks_box.addItem(f'"{deck["name"]}"')
            if deck["name"] in chosen:
                self.decks_box.addSelectedItem(f'"{deck["name"]}"')
        self.decks_box.set_popup_and_box_width()
        self.decks_box.blockSignals(False)
        if not note_types:
            self.decks_box.setPlaceholderText("First, select a trigger note type")
            self.decks_box.setDisabled(True)
        elif decks:
            self.decks_box.setPlaceholderText("Select decks (optional)")
            self.decks_box.setDisabled(False)
        else:
            self.decks_box.setPlaceholderText("No decks found for these note types")
            self.decks_box.setDisabled(True)

    def _refresh_unfocus(self, note_types) -> None:
        fields = field_names_of(note_types)
        boxes = (self.unfocus_edit, self.unfocus_add)
        for index, (box, chosen) in enumerate(zip(boxes, self._chosen_unfocus)):
            self._take_choice(box, chosen, self._offered_unfocus[index])
            self._offered_unfocus[index] = list(fields)
            box.blockSignals(True)
            box.clear()
            for field in fields:
                box.addItem(f'"{field}"')
                if field in chosen:
                    box.addSelectedItem(f'"{field}"')
            box.set_popup_and_box_width()
            box.blockSignals(False)
            box.setDisabled(not fields)

    def _refresh_warning(self, note_types) -> None:
        if len(note_types) > 1:
            self.note_type_warning.setText(
                "<span style='color: orange'>Note:</span> with several trigger note types,"
                " only the fields they all share are offered by name. The rest are still"
                " reachable, under 'All fields'."
            )
        else:
            self.note_type_warning.setText("")

    # -- writing back ---------------------------------------------------------------------

    def apply(self) -> None:
        self.definition["definition_name"] = self.name_edit.text().strip()
        self.triggers["note_types"] = selected_names(self.note_types_box)
        self.triggers["deck_names"] = selected_names(self.decks_box)
        self.triggers["include_subdecks"] = self.include_subdecks.isChecked()
        self.triggers["on_sync"] = self.on_sync.isChecked()
        self.triggers["on_add"] = self.on_add.isChecked()
        self.triggers["on_review"] = self.on_review.isChecked()
        self.triggers["on_unfocus"] = {
            "edit_fields": selected_names(self.unfocus_edit),
            "add_fields": selected_names(self.unfocus_add),
        }
