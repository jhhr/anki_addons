"""Which notes a browser-launched dialog acts on: the selection, or the whole current search.

Used by copy_anywhere's definition picker and japanese_note_ai_ops' multi-op dialog. Both
exist so that the user can run over thousands of notes without selecting them in the
browser, where a large selection makes Anki lag and the right-click menu slower still.
The user leaves one row selected, opens the dialog and picks "current search". Shared so
that the two dialogs label, highlight and resolve that choice the same way.

`NoteSource` is the choice itself and touches neither Qt nor a collection: the caller
passes `find_notes`, so the resolution rules can be tested without either.
`NoteSourceButtons` is the pair of toggle buttons that edits one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Sequence

from anki.notes import NoteId
from aqt.qt import QHBoxLayout, QPushButton, QWidget

if TYPE_CHECKING:
    from aqt.browser import Browser

SEARCH_BUTTON_LABEL = "Use all notes from current search"
ACTIVE_BUTTON_STYLE = "background-color: #e0e0e0; color: black;"


def selection_button_label(count: int) -> str:
    return f"Use selected notes ({count})"


class NoteSource:
    """The browser selection and search as they were when a dialog opened, and which to use.

    Both are captured up front because the dialog is window-modal over the browser: what the
    user saw when they opened it is what they meant, and the search is resolved only when it
    is needed (`note_ids`), so a long search costs nothing until the user commits to it.

    It starts on the selection, even an empty one. The search has to be picked by a click:
    it can be thousands of notes, or with an empty search box the whole collection, and a
    dialog that started there ran on all of them at a stray Enter.
    """

    def __init__(
        self,
        selected_nids: Sequence[NoteId],
        search: str,
        use_selection: bool = True,
    ) -> None:
        self.selected_nids: list[NoteId] = list(selected_nids)
        self.search = search
        self.use_selection = use_selection

    def browser_query(self) -> Optional[str]:
        """A search string for the notes this source means, to combine with other terms, or
        None for no notes at all: selection mode with nothing selected.

        Not "" for that: an empty search matches every note, and an empty `nid:` is a syntax
        error.
        """
        if self.use_selection:
            if not self.selected_nids:
                return None
            return f"nid:{','.join(map(str, self.selected_nids))}"
        return self.search

    def note_ids(self, find_notes: Callable[[str], Sequence[NoteId]]) -> list[NoteId]:
        """The ids to act on. Selection mode never widens to the search, even when empty:
        running over a whole search the user did not pick is the worse failure.

        An empty `search` is passed to `find_notes` as it is; Anki matches the whole
        collection for it. `Browser.current_search()` is the search box text, which is empty
        while the browser shows its default search, so callers that act on the ids should
        check what they got.
        """
        if self.use_selection:
            return list(self.selected_nids)
        return list(find_notes(self.search))


def browser_note_source(browser: Optional[Browser]) -> NoteSource:
    """Capture the browser's selection and search. No browser means nothing selected and an
    empty search, rather than an error, since the dialogs can be opened without one."""
    if not browser:
        return NoteSource([], "")
    return NoteSource(browser.selected_notes(), browser.current_search())


class NoteSourceButtons(QWidget):
    """The "Use selected notes (N)" / "Use all notes from current search" toggle pair.

    Edits `note_source.use_selection` in place and calls `on_change` after the mode changes
    (not during construction: the caller is usually still building the widgets its callback
    updates). Margins are zero so that it sits in a caller's button row like two plain
    buttons would.
    """

    def __init__(
        self,
        note_source: NoteSource,
        on_change: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.note_source = note_source
        self.on_change = on_change

        self.selection_button = QPushButton(
            selection_button_label(len(note_source.selected_nids))
        )
        self.search_button = QPushButton(SEARCH_BUTTON_LABEL)
        self.selection_button.clicked.connect(lambda: self.set_use_selection(True))
        self.search_button.clicked.connect(lambda: self.set_use_selection(False))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.selection_button)
        layout.addWidget(self.search_button)

        self._update_highlight()

    def set_use_selection(self, use_selection: bool) -> None:
        if use_selection == self.note_source.use_selection:
            return
        self.note_source.use_selection = use_selection
        self._update_highlight()
        if self.on_change is not None:
            self.on_change()

    def _update_highlight(self) -> None:
        use_selection = self.note_source.use_selection
        self.selection_button.setStyleSheet(ACTIVE_BUTTON_STYLE if use_selection else "")
        self.search_button.setStyleSheet("" if use_selection else ACTIVE_BUTTON_STYLE)
