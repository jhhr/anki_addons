"""The marks a rename left on a definition, shown at the top of its editor.

The reconcile pass leaves a warning on a definition it could not follow a rename or
deletion for (`logic/rename_warnings.py`), and a definition with a blocking one is not run.
Nothing re-derives a mark: whether the definition now says what it should is the user's
call, so the editor is where they make it, one Dismiss per mark. Dismissing edits the
document only; Save stores the definition without the mark, Cancel throws the document away
with the dismissal.
"""

from __future__ import annotations

import html
from typing import Optional

from aqt.qt import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .discard import discard_widget
from .labels import wrapping
from .stage_document import StageDocument

#: The colour and glyph the picker marks such a definition with
#: (`DefinitionRow._mark_if_broken`), so the banner reads as the same mark.
BROKEN_ICON = "<span style='color: #c0392b'>&#10006;</span>"

BANNER_TEXT = (
    f"{BROKEN_ICON} <b>A rename or deletion in Anki left this definition marked.</b>"
    " It is not run while any mark is left. Dismiss a mark once you have updated the"
    " definition for it."
)


class RenameMarksBanner(QWidget):
    """One row per mark with its message and a Dismiss button, and Dismiss all for two or
    more. Hidden when the definition has no marks, and once the last one is dismissed."""

    def __init__(self, parent: Optional[QWidget], document: StageDocument) -> None:
        super().__init__(parent)
        self.document = document
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # Wrapping, like every other long line in the editor: a message names a field, a
        # note type and sometimes code, and one unwrapped label sets the dialog's minimum
        # width (`labels.py`).
        layout.addWidget(wrapping(QLabel(BANNER_TEXT, self)))
        #: The row for each mark still shown, with the stored entry it dismisses and the
        #: message it shows.
        self.rows: list[tuple[dict, QWidget, str]] = []
        for _location, entry in document.rename_marks():
            message = entry["message"]
            row = self._row(entry, message)
            self.rows.append((entry, row, message))
            layout.addWidget(row)
        self.dismiss_all_button = QPushButton("Dismiss all", self)
        self.dismiss_all_button.clicked.connect(self.dismiss_all)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.dismiss_all_button)
        layout.addLayout(footer)
        self._show_what_is_left()

    def _row(self, entry: dict, message: str) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        # Rich text for the bullet, so the message is escaped: it quotes names from the
        # collection, and a field may well be called "<b>".
        label = wrapping(QLabel(f"&bull; {html.escape(message)}", row))
        layout.addWidget(label, 1)
        button = QPushButton("Dismiss", row)
        button.clicked.connect(lambda _checked=False, entry=entry: self.dismiss(entry))
        layout.addWidget(button)
        return row

    def messages(self) -> list[str]:
        """The messages of the rows still shown."""
        return [message for _entry, _row, message in self.rows]

    def dismiss(self, entry: dict) -> None:
        self.document.dismiss_rename_mark(entry)
        kept = []
        for shown in self.rows:
            if shown[0] is entry:
                discard_widget(shown[1])
            else:
                kept.append(shown)
        self.rows = kept
        self._show_what_is_left()

    def dismiss_all(self) -> None:
        self.document.dismiss_all_rename_marks()
        for _entry, row, _message in self.rows:
            discard_widget(row)
        self.rows = []
        self._show_what_is_left()

    def _show_what_is_left(self) -> None:
        self.dismiss_all_button.setVisible(len(self.rows) >= 2)
        self.setVisible(bool(self.rows))
