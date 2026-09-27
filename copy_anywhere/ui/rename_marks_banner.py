"""The marks a rename left on a definition, shown at the top of its editor.

The reconcile pass leaves a warning on a definition under each location that still spells a
renamed or deleted name (`logic/rename_warnings.py`), and a definition with a blocking one is
not run. The banner lists them grouped by location, each group named the way the user knows
the part -- the stage, and what in it -- with a click on the name opening that stage and
scrolling to it, since the part itself shows the warning too (`rename_indicator.py`).

Nothing re-derives a mark: whether the definition now says what it should is the user's
call, so the editor is where they make it. A mark goes when the user dismisses it, or on
Save once its location no longer spells the old name
(`rename_reconcile.drop_cleared_warnings`). Dismissing edits the document only; Save stores
the definition without the mark, Cancel throws the document away with the dismissal.
"""

from __future__ import annotations

import html
from typing import NamedTuple, Optional

from aqt.qt import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget, pyqtSignal

from ..logic.definition_schema import walk_stages
from ..logic.object_refs import card_action_card_type
from ..logic.rename_locations import TRIGGERS_ANCHOR, split_key
from .discard import discard_widget
from .labels import wrapping
from .rename_indicator import BLOCKING_ICON, WARNING_ICON
from .stage_document import StageDocument

#: The colour and glyph the picker marks such a definition with
#: (`DefinitionRow._mark_if_broken`), so the banner reads as the same mark.
BROKEN_ICON = BLOCKING_ICON

BANNER_TEXT = (
    f"<b>A rename or deletion in Anki left this definition marked.</b> It is not run while"
    f" any {BLOCKING_ICON} mark is left; an {WARNING_ICON} mark does not stop it. Dismiss a"
    " mark once you have updated the definition for it; one whose text no longer spells the"
    " old name goes when you save."
)

#: What the banner calls a part of a stage, by the path of its location key. An expression
#: is named by the key it is stored under, with "(code)" for its code side.
_EXPRESSION_PARTS = {
    "query": "query",
    "value": "value",
    "index": "which one",
    "predicate": "condition",
    "filename": "file name",
    "content": "contents",
    "initial": "starting value",
}
_SLOT_PARTS = {
    "selection.sort_field": "sort field",
    "unfocus_trigger_fields": "unfocus fields",
    "write_if_field": "write-if field",
    "field": "target field",
    "action_code": "code",
    "change_deck": "deck",
}
_TRIGGER_PARTS = {
    "on_unfocus.edit_fields": "unfocus fields (editing a note)",
    "on_unfocus.add_fields": "unfocus fields (adding a note)",
}
ORPHANED = "(no longer in this definition)"


class Place(NamedTuple):
    """Where a location key points in the definition as it is now."""

    label: str
    #: The stage to open for it; None for the triggers and for a location that is gone.
    stage_guid: Optional[str]
    #: The anchor is not in the definition any more: its stage, write or action was deleted.
    orphaned: bool = False


def _part(path: str) -> str:
    if path in _SLOT_PARTS:
        return _SLOT_PARTS[path]
    name, _dot, side = path.rpartition(".")
    if name in _EXPRESSION_PARTS and side in ("text", "code"):
        return _EXPRESSION_PARTS[name] + (" (code)" if side == "code" else "")
    return path


def place_of(document: StageDocument, key: str) -> Place:
    """A readable name for a location key, and which stage holds it."""
    anchor, path = split_key(key)
    if anchor == TRIGGERS_ANCHOR:
        return Place(f"Triggers → {_TRIGGER_PARTS.get(path, path)}", None)
    if document.stage(anchor) is not None:
        return Place(f"{document.path_of(anchor)} → {_part(path)}", anchor)
    for stage in walk_stages(document.definition.get("stages") or []):
        guid = stage.get("guid")
        if not isinstance(guid, str):
            continue
        for write in stage.get("fields") or []:
            if isinstance(write, dict) and write.get("guid") == anchor:
                field = write.get("field") or "?"
                return Place(
                    f"{document.path_of(guid)} → field write '{field}' → {_part(path)}", guid
                )
        for action in stage.get("card_actions") or []:
            if isinstance(action, dict) and action.get("guid") == anchor:
                name = card_action_card_type(action)["name"]
                which = f"card action '{name}'" if name else "card action"
                return Place(f"{document.path_of(guid)} → {which} → {_part(path)}", guid)
    return Place(ORPHANED, None, orphaned=True)


class RenameMarksBanner(QWidget):
    """The marks grouped by location: a group's name opens its stage, each mark has its
    message and a Dismiss button, and Dismiss all for two or more. Hidden when the
    definition has no marks, and once the last one is dismissed."""

    #: A group's name was clicked: the location key it stands for.
    location_chosen = pyqtSignal(str)
    #: A mark was dismissed, so the indicators showing it have to look again.
    dismissed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget], document: StageDocument) -> None:
        super().__init__(parent)
        self.document = document
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # Wrapping, like every other long line in the editor: a message names a field, a
        # note type and sometimes code, and one unwrapped label sets the dialog's minimum
        # width (`labels.py`).
        self.header = wrapping(QLabel("", self))
        layout.addWidget(self.header)
        #: The row for each mark still shown, with the stored entry it dismisses and the
        #: message it shows, in the order the groups show them.
        self.rows: list[tuple[dict, QWidget, str]] = []
        #: Each location still shown: its group widget, the label naming it, and its rows.
        self.groups: dict[str, tuple[QWidget, QLabel, list[QWidget]]] = {}
        grouped: dict[str, list[dict]] = {}
        for key, entry in document.rename_marks():
            grouped.setdefault(key, []).append(entry)
        for key, entries in grouped.items():
            layout.addWidget(self._group(key, entries))
        self.dismiss_all_button = QPushButton("Dismiss all", self)
        self.dismiss_all_button.clicked.connect(self.dismiss_all)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.dismiss_all_button)
        layout.addLayout(footer)
        self._show_what_is_left()

    def _group(self, key: str, entries: list[dict]) -> QWidget:
        group = QWidget(self)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 4, 0, 0)
        place = place_of(self.document, key)
        # Escaped: a stage name and a field name are the user's, and may look like tags.
        text = html.escape(place.label)
        if place.orphaned:
            label = wrapping(QLabel(f"<i>{text}</i>", group))
        else:
            label = wrapping(QLabel(f"<a href='#'>{text}</a>", group))
            label.setToolTip("Open this part of the definition")
            label.linkActivated.connect(lambda _link, key=key: self.location_chosen.emit(key))
        layout.addWidget(label)
        rows = []
        for entry in entries:
            message = entry["message"]
            row = self._row(group, entry, message)
            self.rows.append((entry, row, message))
            rows.append(row)
            layout.addWidget(row)
        self.groups[key] = (group, label, rows)
        return group

    def _row(self, parent: QWidget, entry: dict, message: str) -> QWidget:
        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        # Rich text for the icon, so the message is escaped: it quotes names from the
        # collection, and a field may well be called "<b>".
        icon = BLOCKING_ICON if entry.get("blocks_run") is True else WARNING_ICON
        label = wrapping(QLabel(f"{icon} {html.escape(message)}", row))
        layout.addWidget(label, 1)
        button = QPushButton("Dismiss", row)
        button.clicked.connect(lambda _checked=False, entry=entry: self.dismiss(entry))
        layout.addWidget(button)
        return row

    def messages(self) -> list[str]:
        """The messages of the rows still shown."""
        return [message for _entry, _row, message in self.rows]

    def group_labels(self) -> dict[str, str]:
        """The name each location still shown is listed under, by its key."""
        return {key: label.text() for key, (_group, label, _rows) in self.groups.items()}

    def dismiss(self, entry: dict) -> None:
        self.document.dismiss_rename_mark(entry)
        kept = []
        for shown in self.rows:
            if shown[0] is entry:
                self._discard_row(shown[1])
            else:
                kept.append(shown)
        self.rows = kept
        self._show_what_is_left()
        self.dismissed.emit()

    def dismiss_all(self) -> None:
        self.document.dismiss_all_rename_marks()
        for group, _label, _rows in self.groups.values():
            discard_widget(group)
        self.groups = {}
        self.rows = []
        self._show_what_is_left()
        self.dismissed.emit()

    def _discard_row(self, row: QWidget) -> None:
        for key, (group, _label, rows) in list(self.groups.items()):
            if any(shown is row for shown in rows):
                rows[:] = [shown for shown in rows if shown is not row]
                if rows:
                    discard_widget(row)
                else:
                    # A location with nothing left to say goes with its last row.
                    discard_widget(group)
                    del self.groups[key]
                return
        discard_widget(row)

    def _show_what_is_left(self) -> None:
        blocking = any(entry.get("blocks_run") is True for entry, _row, _message in self.rows)
        self.header.setText(f"{BLOCKING_ICON if blocking else WARNING_ICON} {BANNER_TEXT}")
        self.dismiss_all_button.setVisible(len(self.rows) >= 2)
        self.setVisible(bool(self.rows))


__all__ = ["BANNER_TEXT", "BROKEN_ICON", "Place", "RenameMarksBanner", "place_of"]
