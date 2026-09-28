"""The mark beside an editor part whose text a rename or deletion in Anki left a warning on.

The reconcile pass files each warning under the location that spells the old name
(`logic/rename_locations.py`), so the editor can show it on the part that holds that text
instead of leaving the user to hunt for it from a banner. Each part owns one indicator and
tells it which location keys it holds, how each is read and what it says right now; the
indicator shows ✖ when any warning there stops the definition from running and ⓘ when none
does, with the messages on hover.

It follows the live text rather than the store: once the part no longer spells the old name,
as `rename_scan.still_spelled` reads it, the indicator hides, although the entry stays
stored until Save drops it (`rename_reconcile.drop_cleared_warnings`, the same rule) or the
user dismisses it. So what the indicator shows while the user types is what a save would
keep.

Where a warning there has a new name and the scanner can spell it in (`Hit.replaceable`),
a Replace button beside the icon opens `rename_replace_dialog`, which shows the change and
hands it back to the part through the location's `replace`: a text box takes it through
its undo stack (`replace_text`), a picker selects the new name (`select_name`).

The part builds each key through `rename_locations` from the guid of the object it edits,
never by hand: a key spelled differently from the pass's matches nothing and shows nothing,
with no error to say so.
"""

from __future__ import annotations

from typing import Any, Callable, NamedTuple, Optional, Sequence

from aqt.qt import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextCursor,
    QWidget,
)

from ..logic.rename_warnings import entry_blocks_run
from .rename_replace_dialog import RenameReplaceDialog, Replacement, plan_replacement
from .stage_document import StageDocument

#: The picker's mark for a definition that is not run (`DefinitionRow._mark_if_broken`), so
#: a part reads as the reason for the same mark.
BLOCKING_ICON = "<span style='color: #c0392b'>&#10006;</span>"
#: The picker's mark for a definition that runs but whose searches deserve a look
#: (`DefinitionRow._mark_stale_search_terms`).
WARNING_ICON = "<span style='color: #2471a3'>&#9432;</span>"

# The tooltip's first line is fixed on purpose: Qt guesses whether a tooltip is rich text
# from what stands before its first line break, and a message quotes names from the
# collection, which may look like tags.
BLOCKING_HEADER = "The definition is not run while this is left:"
WARNING_HEADER = "The definition still runs, but check this:"


class LiveLocation(NamedTuple):
    """One location an editor part holds, as the part reads it right now."""

    #: Built with `rename_locations`, from the guid of the object the part edits.
    key: str
    #: How the value is read, one of `rename_scan`'s `READ_AS_*`: the scanner the pass
    #: found the name with.
    read_as: str
    #: What the location says now. None for a location that is not in effect, such as the
    #: code side of an expression in text mode: the pass does not read it, and a save drops
    #: its warnings, so the indicator does not show them either.
    value: Any
    #: How the part takes a new value, which the Replace button's Apply calls with what
    #: the dialog showed; None for a part that offers no Replace.
    replace: Optional[Callable[[Any], None]] = None


def replace_text(edit: QPlainTextEdit, text: str) -> None:
    """Put `text` in `edit` as one step of its undo stack, so Ctrl+Z brings the old back.

    `setPlainText`, which the layouts' `set_text` uses, empties the undo stack instead. The
    whole text is swapped rather than each span: the scanners count in Python characters and
    a text cursor in UTF-16 units, which part ways at the first character outside the Basic
    Multilingual Plane, such as a rare kanji. `textChanged` fires as for typing, so the
    part's own change signal carries the edit on to the definition and the indicator.
    """
    document = edit.document()
    if document is None:
        return
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    cursor.select(QTextCursor.SelectionType.Document)
    cursor.insertText(text)
    cursor.endEditBlock()


def select_name(combo: QComboBox, name: str) -> None:
    """Select `name` in a picker, adding it first when the picker does not offer it.

    A renamed object's new name is normally on offer already; it is not when the picker
    lists the fields of other note types than the one renamed, and the user asked for the
    name by pressing Apply.
    """
    index = combo.findText(name)
    if index < 0:
        combo.addItem(name)
        index = combo.findText(name)
    combo.setCurrentIndex(index)


class RenameIndicator(QWidget):
    """✖ or ⓘ with the warnings' messages on hover; hidden when none of them is live.

    `locations` is asked again on every `refresh`, because a part's mode can change which
    of its locations is in effect and how its text is read (a condition switched to an Anki
    search). The part calls `refresh` on its own change signal; the dialog also refreshes
    every indicator after a dismissal in the banner and on each re-analysis.

    `row` is the layout the icon and the Replace button sit in.

    The button shows when some location with a `replace` has a warning Replace can act on;
    it applies every such warning at once, so a search naming both a renamed deck and a
    renamed note type is fixed by one Apply.
    """

    def __init__(
        self,
        parent: Optional[QWidget],
        document: StageDocument,
        locations: Callable[[], Sequence[LiveLocation]],
    ) -> None:
        super().__init__(parent)
        self.document = document
        self._locations = locations
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.icon = QLabel(self)
        self.row.addWidget(self.icon)
        self.replace_button = QPushButton("Replace…", self)
        self.replace_button.setToolTip(
            "Show this text with the new name in place of the old, and apply it if it reads"
            " right"
        )
        self.replace_button.clicked.connect(self._on_replace)
        self.row.addWidget(self.replace_button)
        #: The stored entries shown, with the location each is filed under.
        self.shown: list[tuple[LiveLocation, dict]] = []
        #: What Replace would do, per location it can act on, with how that location takes it.
        self.replacements: list[tuple[Replacement, Callable[[Any], None]]] = []
        #: The location key of each of `replacements`, in the same order.
        self.replacement_keys: list[str] = []
        self.refresh()

    def refresh(self, *_args) -> None:
        self.shown = [
            (location, entry)
            for location in self._locations()
            for entry in self.document.rename_marks_at(location.key)
            if self.document.rename_mark_is_live(
                location.key, location.read_as, location.value, entry
            )
        ]
        planned = self._replacements()
        self.replacements = [(replacement, write) for _key, replacement, write in planned]
        self.replacement_keys = [key for key, _replacement, _write in planned]
        self.replace_button.setVisible(bool(self.replacements))
        self.setVisible(bool(self.shown))
        if not self.shown:
            self.icon.setText("")
            self.icon.setToolTip("")
            return
        blocking = self.is_blocking()
        self.icon.setText(BLOCKING_ICON if blocking else WARNING_ICON)
        self.icon.setToolTip(
            "\n".join([BLOCKING_HEADER if blocking else WARNING_HEADER] + self.messages())
        )

    def _replacements(self) -> list[tuple[str, Replacement, Callable[[Any], None]]]:
        by_key: dict[str, tuple[LiveLocation, list[dict]]] = {}
        for location, entry in self.shown:
            by_key.setdefault(location.key, (location, []))[1].append(entry)
        planned = []
        for location, entries in by_key.values():
            if location.replace is None:
                continue
            replacement = plan_replacement(location.read_as, location.value, entries)
            if replacement.changes:
                planned.append((location.key, replacement, location.replace))
        return planned

    def replace_dialog(self) -> RenameReplaceDialog:
        """The dialog the Replace button opens, for what the part says right now.

        Parented to the window, not to this widget: Apply hides the indicator, and the
        dialog must not go with it. Its Apply also tells the document which entries it
        answered that a scan of the new text cannot see answered (`Replacement.settled`),
        from the plan it showed: Apply's own edit refreshes this indicator first.
        """
        self.refresh()
        planned = [
            (key, replacement)
            for key, (replacement, _write) in zip(self.replacement_keys, self.replacements)
            if replacement.settled
        ]
        dialog = RenameReplaceDialog(self.window(), self.replacements)

        def settle() -> None:
            for key, replacement in planned:
                self.document.settle_rename_marks(key, replacement.before, replacement.settled)
            self.refresh()

        dialog.accepted.connect(settle)
        return dialog

    def _on_replace(self) -> None:
        self.replace_dialog().exec()
        # A part whose change signal does not reach `refresh` still shows what it now says.
        self.refresh()

    def entries(self) -> list[dict]:
        return [entry for _location, entry in self.shown]

    def messages(self) -> list[str]:
        return [entry["message"] for entry in self.entries()]

    def is_blocking(self) -> bool:
        return any(entry_blocks_run(entry) for entry in self.entries())


__all__ = [
    "BLOCKING_ICON",
    "LiveLocation",
    "RenameIndicator",
    "replace_text",
    "select_name",
    "WARNING_ICON",
]
