"""Taking a widget off screen for good, at once.

`deleteLater` alone does not: the deferred delete runs only when control returns to the
event loop it was posted from. A dialog's rows rebuilt while it was still being built were
posted from the loop outside the dialog, whose own `exec()` then ran on top of it, so the
old rows stayed on screen under the new ones until the dialog closed. The exports panel
showed one row over another, and every rebuilt stage list had its rows twice.
"""

from __future__ import annotations

from aqt.qt import QWidget


def discard_widget(widget: QWidget) -> None:
    """Hide `widget` now and delete it when Qt next can."""
    widget.hide()
    widget.deleteLater()
