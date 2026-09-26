"""Pause/Resume and Cancel buttons for a bulk run, in Anki's own progress dialog.

Anki's progress dialog has no buttons: a run is cancelled by Escape or the close box, and
there was no way to pause one at all. The buttons go into that dialog rather than a window of
our own because it is application-modal for the whole run, so nothing outside it could be
clicked, and because it already comes and goes with the run. Once the buttons are in, Escape
and the close box cancel only while Cancel is enabled (`swallows_cancel`).

Everything here runs on the main thread, which belongs to no run: it reads the pause through
`pause_state()` and acts through `pause_run`/`resume_run`, all of which fall back to the run in
progress. Nothing here may raise into its caller: it is called from every progress redraw, and
a broken button must not take the progress display, or the run, down with it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from aqt import mw
from aqt.qt import QEvent, QHBoxLayout, QObject, QPushButton, Qt, qconnect, sip

from .api_client import pause_run, pause_state, resume_run

logger = logging.getLogger(__name__)

PAUSE_REASON = "paused by user"

# The controls are kept on the dialog object itself, not in a module global. Anki builds a
# new dialog for every operation and deletes it at the end, so each run gets fresh buttons
# and a reference to a deleted dialog's buttons is never left behind to be touched.
_CONTROLS_ATTR = "_japanese_note_ai_ops_run_controls"
# Set by `progress_errors` once it has split the dialog into the progress column and its error
# pane: the layout that now holds the label and the bar, where the buttons belong too
PROGRESS_COLUMN_ATTR = "_japanese_note_ai_ops_progress_column"


def swallows_cancel(event_type: Any, key: Any, cancel_enabled: bool) -> bool:
    """Whether a key press or close of the dialog is a cancel to be dropped: Escape and the
    close box cancel only while the Cancel button could, so a greyed Cancel means neither does.

    Otherwise a press while the cleanup writes what cannot be stopped set the flag all the same,
    and the step it ended read as cancelled though it had saved everything, stopping a chain.
    """
    if cancel_enabled:
        return False
    if event_type == QEvent.Type.Close:
        return True
    return event_type == QEvent.Type.KeyPress and key == Qt.Key.Key_Escape


class _CancelKeyFilter(QObject):
    """Drops Escape and the close box while Cancel is greyed (`swallows_cancel`).

    On the dialog rather than a subclass of it: the dialog is Anki's. aqt closes it with
    hide() and deleteLater(), never close(), so no Close this drops is one of Anki's own.
    """

    def __init__(self, win: Any) -> None:
        super().__init__(win)
        self.controls: Optional[_RunControls] = None

    def eventFilter(self, obj: Any, event: Any) -> bool:  # type: ignore[override]
        try:
            controls = self.controls
            if controls is None or event is None:
                return False
            key = event.key() if event.type() == QEvent.Type.KeyPress else None
            return swallows_cancel(event.type(), key, controls.cancel.isEnabled())
        except Exception as e:
            logger.error("Could not filter the progress dialog's cancel keys: %s", e)
            return False


class _RunControls:
    __slots__ = ("toggle", "cancel", "disabled", "cleanup", "key_filter")

    def __init__(self, toggle: QPushButton, cancel: QPushButton) -> None:
        self.toggle = toggle
        self.cancel = cancel
        # Kept alive with the controls; parented to the dialog as well
        self.key_filter: Optional[_CancelKeyFilter] = None
        # Once the run is being cancelled; a later refresh must not bring the buttons back
        self.disabled = False
        # Once the cleanup has re-armed Cancel to stop its note adding (rearm_cleanup_cancel):
        # only Cancel is live then, whatever the run's pause says
        self.cleanup = False


def _progress_window() -> Optional[tuple[Any, Any]]:
    """Anki's progress dialog for the operation in progress and its layout, or None.

    Private Anki API, all of it, kept in this one place: `mw.progress._win` (the dialog),
    `win.form.verticalLayout` (the designer form's only layout, holding the label and the
    bar; once an error pane is shown, the column they were moved into, see
    `progress_errors`) and `win.wantCancel` (the flag Escape and the close box set, which
    `mw.progress.want_cancel()` reads). Checked against Anki 26.09. If a later Anki renames
    or drops any of them, the dialog simply shows no buttons: the run still pauses itself at
    a usage limit and resumes at the reset time, and Escape still cancels.
    """
    win = _dialog()
    if win is None:
        return None
    layout = getattr(win, PROGRESS_COLUMN_ATTR, None)
    if layout is None:
        layout = getattr(getattr(win, "form", None), "verticalLayout", None)
    if layout is None:
        return None
    return win, layout


def _dialog() -> Any:
    """The progress dialog and its cancel flag, or None; its layout may not be as expected."""
    win = getattr(mw.progress, "_win", None)
    if win is None or not hasattr(win, "wantCancel"):
        return None
    # ProgressManager.finish deletes the dialog later rather than at once, so a redraw queued
    # before it can still find the Python object of a dialog Qt has already destroyed
    if sip.isdeleted(win):
        return None
    return win


def _controls_of(win: Any) -> Optional[_RunControls]:
    controls = getattr(win, _CONTROLS_ATTR, None)
    return controls if isinstance(controls, _RunControls) else None


def _install(win: Any, layout: Any) -> _RunControls:
    toggle = QPushButton("Pause")
    toggle.setToolTip(
        "Start no new request until resumed. Requests already sent run to completion."
    )
    cancel = QPushButton("Cancel")
    for button in (toggle, cancel):
        # Never keyboard-activated: the dialog pops up over whatever the user was typing
        # into, and Space or Enter landing on a focused button would pause or cancel the run.
        # Escape keeps cancelling through the dialog itself, while Cancel is enabled.
        button.setAutoDefault(False)
        button.setDefault(False)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    row = QHBoxLayout()
    row.addStretch()
    row.addWidget(toggle)
    row.addWidget(cancel)
    layout.addLayout(row)
    controls = _RunControls(toggle, cancel)
    setattr(win, _CONTROLS_ATTR, controls)
    key_filter = _CancelKeyFilter(win)
    key_filter.controls = controls
    win.installEventFilter(key_filter)
    controls.key_filter = key_filter
    qconnect(toggle.clicked, lambda: _on_toggle(win))
    qconnect(cancel.clicked, lambda: _on_cancel(win))
    return controls


def _refresh(win: Any, controls: _RunControls) -> None:
    if controls.disabled or win.wantCancel:
        # Escape or the close box: the run is being cancelled, and a Resume now would only
        # look like it undid that
        _disable(controls)
        return
    if controls.cleanup:
        # Cancel now stops the note adding. Pause stays grey: the cleanup never waits for a
        # resume, so a Pause would only look like it held the adding.
        controls.toggle.setEnabled(False)
        controls.cancel.setEnabled(True)
        return
    pause = pause_state()
    if pause is None:
        text = "Pause"
    elif pause.automatic:
        # Retries the requests that hit the limit now instead of at the reset time; if the
        # limit still holds they pause the run again
        text = "Resume now"
    else:
        text = "Resume"
    if controls.toggle.text() != text:
        controls.toggle.setText(text)


def _disable(controls: _RunControls) -> None:
    controls.disabled = True
    controls.toggle.setEnabled(False)
    controls.cancel.setEnabled(False)


def _on_toggle(win: Any) -> None:
    try:
        if pause_state() is None:
            pause_run(PAUSE_REASON)
        else:
            resume_run()
        controls = _controls_of(win)
        if controls is not None and not sip.isdeleted(win):
            _refresh(win, controls)
    except Exception as e:
        logger.error("Pause button failed: %s", e)


def _on_cancel(win: Any) -> None:
    try:
        # What Escape does. The run notices it the way it notices Escape, including while it
        # is paused, so there is nothing else to stop here.
        win.wantCancel = True
        controls = _controls_of(win)
        if controls is not None:
            _disable(controls)
    except Exception as e:
        logger.error("Cancel button failed: %s", e)


def install_run_controls() -> None:
    """Add the Pause/Resume and Cancel buttons to the progress dialog, once per dialog.

    Main thread. Quietly does nothing when there is no dialog, or not the dialog expected.
    """
    try:
        found = _progress_window()
        if found is None:
            return
        win, layout = found
        controls = _controls_of(win)
        if controls is None:
            controls = _install(win, layout)
        _refresh(win, controls)
    except Exception as e:
        logger.error("Could not add the run controls to the progress dialog: %s", e)


def start_run_controls() -> None:
    """The buttons for a run that is starting. Main thread, after `run_in_background`.

    A run has its dialog to itself unless it is a step of a chain, whose dialog stays open
    across steps: there the previous step left the buttons greyed for good, and this run
    brings them back. Never over a cancel the dialog's flag still holds.
    """
    try:
        found = _progress_window()
        if found is None:
            return
        win, layout = found
        controls = _controls_of(win)
        if controls is None:
            controls = _install(win, layout)
        elif not win.wantCancel:
            controls.disabled = False
            controls.cleanup = False
            controls.toggle.setEnabled(True)
            controls.cancel.setEnabled(True)
        _refresh(win, controls)
    except Exception as e:
        logger.error("Could not add the run controls to the progress dialog: %s", e)


def install_idle_run_controls() -> None:
    """The buttons, greyed, for a dialog open with no run in it yet: a chain's, before its
    first step. Greyed Cancel also keeps Escape and the close box from cancelling (see
    `swallows_cancel`). Main thread."""
    install_run_controls()
    disable_run_controls()


def refresh_run_controls() -> None:
    """Bring the buttons in line with the run's pause, adding them if they are missing.

    Main thread, after every progress redraw. The install is repeated here so that the
    buttons still appear if a later Anki creates its dialog only after
    `CollectionOp.run_in_background()` has returned; today it exists by then.
    """
    install_run_controls()


def rearm_cleanup_cancel() -> bool:
    """Give the cleanup a cancel of its own, to stop its note adding. Main thread.

    The dialog's flag cannot tell a second press from the first: once a cancel of the API work
    has set it, it stays set for the rest of the run, so a cleanup reading it would stop adding
    before it began. It is reset here, and Cancel enabled again, so that from now on the flag
    means the adding is to stop. The flag is reset even when the buttons could not be built,
    since Escape still sets it. The run's own cancel (`run_cancelled()`) stays as it is.

    Returns whether the flag was reset. When it was not (no dialog, or not the one expected)
    the caller must not read the flag as a cancel of the adding: it may still hold the first.
    """
    try:
        win = _dialog()
        if win is None:
            return False
        win.wantCancel = False
    except Exception as e:
        logger.error("Could not reset the cancel for the cleanup: %s", e)
        return False
    try:
        controls = _controls_of(win)
        if controls is not None:
            controls.cleanup = True
            # The latch the first cancel set; a later cancel of the adding sets it again
            controls.disabled = False
            _refresh(win, controls)
    except Exception as e:
        # The flag is reset, so Escape and the close box still stop the adding
        logger.error("Could not enable Cancel again for the cleanup: %s", e)
    return True


def disable_run_controls() -> None:
    """Grey the buttons out for good, once the run is being cancelled, or once the cleanup has
    nothing left that a cancel stops. Main thread."""
    try:
        found = _progress_window()
        if found is None:
            return
        controls = _controls_of(found[0])
        if controls is not None:
            _disable(controls)
    except Exception as e:
        logger.error("Could not disable the run controls: %s", e)
