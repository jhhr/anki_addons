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

The errors that do not fail a run come here too, from whichever thread met them:
`run_errors` collects them and names the note and the chain step, and hands them to
`report_run_error_from_any_thread`, which hops to the main thread. The pane groups repeats of
one error, so one cause failing every note does not list thousands. What the pane shows is also
kept for the run (`run_errors.record`), and `show_run_end` makes it readable once the dialog is
gone: a failed chain step closes it a moment after its traceback arrives.

Main thread only, but for `report_run_error_from_any_thread` and `report_exception`. Nothing
here may raise into its caller: it is called while a run is failing, and the caller's fallback
(a modal error box) is better than a second error.
"""

from __future__ import annotations

import html
import logging
import threading
from functools import partial
from typing import Any

from anki.errors import Interrupted
from aqt import mw
from aqt.qt import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTextBrowser,
    QTextCursor,
    QTimer,
    QVBoxLayout,
    QWidget,
    Qt,
)
from aqt.utils import showText

from . import run_errors
from .collection_access import RunCancelled
from .progress_controls import PROGRESS_COLUMN_ATTR, _dialog, _progress_window
from .run_errors import ErrorList, entry_html

logger = logging.getLogger(__name__)

_PANE_ATTR = "_japanese_note_ai_ops_error_pane"

# Tall enough for a short traceback. Anki's dialog is about 80 pixels high, which would leave
# the list three lines.
PANE_MIN_HEIGHT = 260


# How long repeats of an error may wait before the pane redraws their counts: one cause can
# fail every note of a run, and redrawing the whole list for each would keep the main thread
# busy with it
REDRAW_DELAY_MS = 300


class _ErrorPane:
    __slots__ = ("column", "header", "pane", "browser", "errors", "redraw")

    def __init__(self, column: Any, header: Any, pane: Any, browser: Any) -> None:
        self.column = column
        self.header = header
        self.pane = pane
        self.browser = browser
        self.errors = ErrorList()
        # Parented to the browser, so it dies with the dialog instead of firing into it
        self.redraw = QTimer(browser)
        self.redraw.setSingleShot(True)
        self.redraw.setInterval(REDRAW_DELAY_MS)
        self.redraw.timeout.connect(partial(_redraw, self))


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


def _follow_start(browser: Any) -> tuple[Any, bool]:
    bar = browser.verticalScrollBar()
    # Follow the newest error unless the user has scrolled up to read an earlier one
    return bar, bar is None or bar.value() == bar.maximum()


def _append(pane: _ErrorPane, title: str, text: str) -> None:
    entry = pane.errors.add(title, text)
    pane.header.setText(pane.errors.header())
    if entry is None or pane.redraw.isActive():
        # A repeat, or one past the listed kinds: only counts change, and a redraw shows them.
        # A new error while one is pending waits for it too, or it would be listed twice.
        if not pane.redraw.isActive():
            pane.redraw.start()
        return
    browser = pane.browser
    bar, follow = _follow_start(browser)
    cursor = browser.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertHtml(entry_html(entry, len(pane.errors.entries) > 1))
    if follow and bar is not None:
        bar.setValue(bar.maximum())


def _redraw(pane: _ErrorPane) -> None:
    try:
        browser = pane.browser
        bar, follow = _follow_start(browser)
        at = bar.value() if bar is not None else 0
        browser.setHtml(pane.errors.as_html())
        if bar is not None:
            bar.setValue(bar.maximum() if follow else at)
    except Exception as e:
        # The dialog may be going; the errors are in the run's list all the same
        logger.error("Could not redraw the error pane: %s", e)


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
    except Exception as e:
        logger.error("Could not show an error in the progress dialog: %s", e)
        return False
    run_errors.record(title, text)
    return True


def report_run_error_from_any_thread(title: str, text: str) -> None:
    """`report_run_error` from any thread: off the main thread it is run there later. Nothing
    falls back to another box: an error that does not fail the run is not worth a modal one,
    and the log has it. Never raises."""
    try:
        if threading.current_thread() is threading.main_thread():
            report_run_error(title, text)
        else:

            def show() -> None:
                report_run_error(title, text)

            mw.taskman.run_on_main(show)
    except Exception as e:
        logger.error("Could not pass an error to the progress dialog: %s", e)


run_errors.deliver_with(report_run_error_from_any_thread)


def report_exception(error: BaseException, what: str = "", where: str = "") -> None:
    """Report an exception a task or note ran into, when the run goes on without it; titled by
    `run_errors.error_title(where)`, from any thread. Anki's `Interrupted` is not an error but
    an interrupted backend call, and aqt's error box skips it too; nor is `RunCancelled`, which
    only says the run was cancelled under the task."""
    if isinstance(error, (Interrupted, RunCancelled)):
        return
    run_errors.report_error(run_errors.exception_text(error, what), where)


def show_run_end(
    text: str, parent: Any, errors: ErrorList, title: str = "Anki", warning: bool = True
) -> None:
    """The end message of a run that met errors: `text` (rich text) and how many errors there
    were, with a button that lists them, tracebacks and all, in a box they can be copied from.
    One box, however many errors: the list opens only when asked for."""
    box, show = end_message_box(text, parent, errors, title, warning)
    box.exec()
    if box.clickedButton() is show:
        show_error_list(errors, parent, title)


def end_message_box(
    text: str, parent: Any, errors: ErrorList, title: str, warning: bool
) -> tuple[Any, Any]:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"{text}<br><br><b>{html.escape(errors.header())}</b> during the run, listed"
        " under Show errors."
    )
    show = box.addButton("Show errors", QMessageBox.ButtonRole.ActionRole)
    ok = box.addButton(QMessageBox.StandardButton.Ok)
    box.setDefaultButton(ok)
    return box, show


def show_error_list(errors: ErrorList, parent: Any, title: str = "Anki") -> None:
    showText(
        errors.as_text(),
        parent=parent,
        title=f"{title}: errors",
        copyBtn=True,
        minWidth=700,
        minHeight=500,
    )
