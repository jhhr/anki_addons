"""A pane of errors beside the progress, in Anki's own progress dialog.

A run's errors were shown with a modal error box. Over the progress dialog, which is
application-modal for the whole run (and for the whole of a chain), that box competed with it
for the user's input, and every failed step of a chain stacked another one. Here they go into
the dialog instead: with no error it keeps its usual size; the first one widens it, the progress
moving to the left half and a scrollable list of errors taking the right.

The pane is kept on the dialog object, like the buttons in `progress_controls`. Anki builds a
new dialog for every top-level operation and deletes it at the end, so a new run starts with no
pane and no errors, while a chain, which holds one dialog open across its steps, keeps its
errors from step to step.

Main thread only. Nothing here may raise into its caller: it is called while a run is failing,
and the caller's fallback (a modal error box) is better than a second error.
"""

from __future__ import annotations

import html
import logging
import threading
from typing import Any

from aqt.qt import QHBoxLayout, QLabel, QTextBrowser, QTextCursor, QVBoxLayout, QWidget, Qt

from .progress_controls import PROGRESS_COLUMN_ATTR, _dialog, _progress_window

logger = logging.getLogger(__name__)

_PANE_ATTR = "_japanese_note_ai_ops_error_pane"

# Tall enough for a short traceback. Anki's dialog is about 80 pixels high, which would leave
# the list three lines.
PANE_MIN_HEIGHT = 260


class _ErrorPane:
    __slots__ = ("column", "header", "pane", "browser", "count")

    def __init__(self, column: Any, header: Any, pane: Any, browser: Any) -> None:
        self.column = column
        self.header = header
        self.pane = pane
        self.browser = browser
        self.count = 0


def _pane_of(win: Any) -> _ErrorPane | None:
    pane = getattr(win, _PANE_ATTR, None)
    return pane if isinstance(pane, _ErrorPane) else None


def _split(win: Any, layout: Any) -> _ErrorPane:
    """Move everything the dialog's layout holds into a left column and put the pane on the
    right, both in that same layout.

    The contents are moved rather than the layout: Qt will not move a widget's own layout to
    another widget. The Pause/Cancel row moves with them, and `progress_controls` finds the
    column through PROGRESS_COLUMN_ATTR, so buttons built after this land there as well.
    """
    left = QWidget()
    column = QVBoxLayout(left)
    column.setContentsMargins(0, 0, 0, 0)
    while layout.count():
        item = layout.takeAt(0)
        # addWidget and addLayout reparent the widgets to the column; addItem would leave them
        # children of the dialog, drawn at the column's offsets from the wrong origin
        if item.widget() is not None:
            column.addWidget(item.widget())
        elif item.layout() is not None:
            column.addLayout(item.layout())
        else:
            column.addItem(item)

    pane = QWidget()
    pane_layout = QVBoxLayout(pane)
    pane_layout.setContentsMargins(0, 0, 0, 0)
    header = QLabel()
    browser = QTextBrowser()
    browser.setOpenLinks(False)
    # Selectable, so a traceback can be copied, but never focused unless clicked: the dialog
    # pops up over whatever the user was typing into (see the buttons in progress_controls).
    # Escape pressed in it still reaches the dialog, which cancels or not as before.
    browser.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
    browser.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    pane_layout.addWidget(header)
    pane_layout.addWidget(browser)

    row = QHBoxLayout()
    row.addWidget(left, 1)
    row.addWidget(pane, 1)
    layout.addLayout(row)

    errors = _ErrorPane(column, header, pane, browser)
    setattr(win, _PANE_ATTR, errors)
    setattr(win, PROGRESS_COLUMN_ATTR, column)
    return errors


def _widen(win: Any, before: Any) -> None:
    """Double the dialog's width from `before`, its size without the pane; never make it
    shorter, nor bigger than the screen it is on."""
    width = before.width() * 2
    height = max(before.height(), PANE_MIN_HEIGHT)
    screen = win.screen()
    available = screen.availableGeometry() if screen is not None else None
    if available is not None:
        # The screen holds the window frame as well
        frame = win.frameGeometry()
        room_w = available.width() - (frame.width() - before.width())
        room_h = available.height() - (frame.height() - before.height())
        width = max(before.width(), min(width, room_w))
        height = max(before.height(), min(height, room_h))
    win.resize(width, height)
    if available is None or not win.isVisible():
        # Not shown yet: it is placed when it is
        return
    # Grow about the centre, as the dialog was placed over its parent, then keep it on screen
    frame = win.frameGeometry()
    x = frame.x() - (width - before.width()) // 2
    y = frame.y()
    x = max(available.left(), min(x, available.right() + 1 - frame.width()))
    y = max(available.top(), min(y, available.bottom() + 1 - frame.height()))
    win.move(x, y)


def _append(errors: _ErrorPane, title: str, text: str) -> None:
    browser = errors.browser
    bar = browser.verticalScrollBar()
    # Follow the newest error unless the user has scrolled up to read an earlier one
    follow = bar is None or bar.value() == bar.maximum()
    cursor = browser.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    separator = "<hr>" if errors.count else ""
    # pre-wrap keeps a traceback's lines and indentation and still wraps the long ones
    cursor.insertHtml(
        f"{separator}<p><b>{html.escape(title)}</b></p>"
        f'<p style="white-space: pre-wrap;">{html.escape(text.rstrip())}</p>'
    )
    errors.count += 1
    errors.header.setText("1 error" if errors.count == 1 else f"{errors.count} errors")
    if follow and bar is not None:
        bar.setValue(bar.maximum())


def report_run_error(title: str, text: str) -> bool:
    """Show an error in the pane of the progress dialog open now, making the pane on the first.

    `title` names where it happened ("Step 2/4: Make meanings"), `text` is the message or the
    traceback, both plain text. Main thread only.

    Returns whether it was shown. False when there is no progress dialog (the run's has
    closed, or a later Anki renamed it), when called off the main thread (nothing is touched
    then), or when the pane could not be built; the caller then shows the error its own way.
    Never raises.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.error("report_run_error called off the main thread; not shown: %s", title)
        return False
    try:
        win = _dialog()
        if win is None:
            return False
        errors = _pane_of(win)
        if errors is None:
            found = _progress_window()
            if found is None:
                return False
            # Before the split: the pane's minimum size may grow the dialog at the next layout
            before = win.size()
            errors = _split(win, found[1])
            _widen(win, before)
        _append(errors, title, text)
        return True
    except Exception as e:
        logger.error("Could not show an error in the progress dialog: %s", e)
        return False
