"""Pause/Resume and Cancel buttons for a bulk run, in Anki's own progress dialog.

Anki's progress dialog has no buttons: a run is cancelled by Escape or the close box, and
there was no way to pause one at all. The buttons go into that dialog rather than a window of
our own because it is application-modal for the whole run, so nothing outside it could be
clicked, and because it already comes and goes with the run.

Everything here runs on the main thread, which belongs to no run: it reads the pause through
`pause_state()` and acts through `pause_run`/`resume_run`, all of which fall back to the run in
progress. Nothing here may raise into its caller: it is called from every progress redraw, and
a broken button must not take the progress display, or the run, down with it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from aqt import mw
from aqt.qt import QHBoxLayout, QPushButton, Qt, qconnect, sip

from .api_client import pause_run, pause_state, resume_run

logger = logging.getLogger(__name__)

PAUSE_REASON = "paused by user"

# The controls are kept on the dialog object itself, not in a module global. Anki builds a
# new dialog for every operation and deletes it at the end, so each run gets fresh buttons
# and a reference to a deleted dialog's buttons is never left behind to be touched.
_CONTROLS_ATTR = "_japanese_note_ai_ops_run_controls"


class _RunControls:
    __slots__ = ("toggle", "cancel", "disabled")

    def __init__(self, toggle: QPushButton, cancel: QPushButton) -> None:
        self.toggle = toggle
        self.cancel = cancel
        # Once the run is being cancelled; a later refresh must not bring the buttons back
        self.disabled = False


def _progress_window() -> Optional[tuple[Any, Any]]:
    """Anki's progress dialog for the operation in progress and its layout, or None.

    Private Anki API, all of it, kept in this one place: `mw.progress._win` (the dialog),
    `win.form.verticalLayout` (the designer form's only layout, holding the label and the
    bar) and `win.wantCancel` (the flag Escape and the close box set, which
    `mw.progress.want_cancel()` reads). Checked against Anki 26.09. If a later Anki renames
    or drops any of them, the dialog simply shows no buttons: the run still pauses itself at
    a usage limit and resumes at the reset time, and Escape still cancels.
    """
    win = getattr(mw.progress, "_win", None)
    if win is None:
        return None
    layout = getattr(getattr(win, "form", None), "verticalLayout", None)
    if layout is None or not hasattr(win, "wantCancel"):
        return None
    # ProgressManager.finish deletes the dialog later rather than at once, so a redraw queued
    # before it can still find the Python object of a dialog Qt has already destroyed
    if sip.isdeleted(win):
        return None
    return win, layout


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
        # Escape keeps cancelling through the dialog itself.
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
    qconnect(toggle.clicked, lambda: _on_toggle(win))
    qconnect(cancel.clicked, lambda: _on_cancel(win))
    return controls


def _refresh(win: Any, controls: _RunControls) -> None:
    if controls.disabled or win.wantCancel:
        # Escape or the close box: the run is being cancelled, and a Resume now would only
        # look like it undid that
        _disable(controls)
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


def refresh_run_controls() -> None:
    """Bring the buttons in line with the run's pause, adding them if they are missing.

    Main thread, after every progress redraw. The install is repeated here so that the
    buttons still appear if a later Anki creates its dialog only after
    `CollectionOp.run_in_background()` has returned; today it exists by then.
    """
    install_run_controls()


def disable_run_controls() -> None:
    """Grey the buttons out for good, once the run is being cancelled. Main thread."""
    try:
        found = _progress_window()
        if found is None:
            return
        controls = _controls_of(found[0])
        if controls is not None:
            _disable(controls)
    except Exception as e:
        logger.error("Could not disable the run controls: %s", e)
