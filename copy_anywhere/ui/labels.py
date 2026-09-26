"""Labels that wrap instead of widening the editor.

A QLabel does not wrap by default, and its minimum width is then its whole text on one line.
In the definition editor that meant a stage summary listing a dozen field names, or one
line of help text, set the minimum width of the whole scroll area: the stage list grew
wider than its half of the dialog and scrolled sideways.
"""

from __future__ import annotations

from typing import Optional

from aqt.qt import QLabel, QResizeEvent, QSize, Qt, QWidget, qtmajor

if qtmajor > 5:
    ElideRight = Qt.TextElideMode.ElideRight
else:  # pragma: no cover -- Anki 2.1.49 and older
    ElideRight = Qt.ElideRight  # type: ignore[attr-defined]


def wrapping(label: QLabel) -> QLabel:
    """`label`, set to wrap its text at the width it is given."""
    label.setWordWrap(True)
    return label


class ElidedLabel(QLabel):
    """One line of plain text that ends in "…" where the space runs out, whole on hover.

    For a line that sits beside other widgets in a row, such as a stage's summary. A wrapped
    label there needs the row to grow taller for it, and nested layouts do not pass that on,
    so the extra lines were cut off. This one asks for no width it does not get: its minimum
    is a sliver, and what it is given decides how much of the text shows.
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setText(text)

    def setText(self, text: Optional[str]) -> None:  # noqa: N802 -- Qt's name
        self._full_text = text or ""
        self._elide()

    def text(self) -> str:
        """The whole text, as set. Only the painting is shortened: code and tests reading the
        label get what it says, not how much of it fits."""
        return self._full_text

    def minimumSizeHint(self) -> QSize:  # noqa: N802 -- Qt's name
        hint = super().minimumSizeHint()
        return QSize(self.fontMetrics().horizontalAdvance("…") * 3, hint.height())

    def sizeHint(self) -> QSize:  # noqa: N802 -- Qt's name
        hint = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full_text), hint.height())

    def resizeEvent(self, event: Optional[QResizeEvent]) -> None:  # noqa: N802 -- Qt's name
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full_text, ElideRight, self.width())
        super().setText(shown)
        self.setToolTip(self._full_text if shown != self._full_text else "")
