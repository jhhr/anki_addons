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

The part builds each key through `rename_locations` from the guid of the object it edits,
never by hand: a key spelled differently from the pass's matches nothing and shows nothing,
with no error to say so.
"""

from __future__ import annotations

from typing import Any, Callable, NamedTuple, Optional, Sequence

from aqt.qt import QHBoxLayout, QLabel, QWidget

from ..logic.rename_scan import still_spelled
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


class RenameIndicator(QWidget):
    """✖ or ⓘ with the warnings' messages on hover; hidden when none of them is live.

    `locations` is asked again on every `refresh`, because a part's mode can change which
    of its locations is in effect and how its text is read (a condition switched to an Anki
    search). The part calls `refresh` on its own change signal; the dialog also refreshes
    every indicator after a dismissal in the banner and on each re-analysis.

    `row` is the layout the icon sits in, left open for the Replace button that goes beside
    it.
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
        #: The stored entries shown, with the location each is filed under.
        self.shown: list[tuple[LiveLocation, dict]] = []
        self.refresh()

    def refresh(self, *_args) -> None:
        self.shown = [
            (location, entry)
            for location in self._locations()
            for entry in self.document.rename_marks_at(location.key)
            if still_spelled(location.read_as, location.value, entry)
        ]
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

    def entries(self) -> list[dict]:
        return [entry for _location, entry in self.shown]

    def messages(self) -> list[str]:
        return [entry["message"] for entry in self.entries()]

    def is_blocking(self) -> bool:
        return any(entry.get("blocks_run") is True for entry in self.entries())


__all__ = [
    "BLOCKING_ICON",
    "LiveLocation",
    "RenameIndicator",
    "WARNING_ICON",
]
