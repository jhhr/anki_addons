"""Showing the exception that failed a chain's step, and the step's failed outcome.

Four places end a step on an exception: an op's `CollectionOp` failure handler, an error in its
success handler, a step's start raising inside the chain, and the word array download before a
step (failing, or the step's start raising after it). Each used to show the error and build the
outcome itself, and they drifted: two left a stop reason behind for the next run, and two let
an error from the error box itself escape, which left the chain waiting for an `on_done` that
never came. `failed_step_outcome` is the one way now.

Every one of them runs while the chain holds its progress dialog open (a step's own progress
level is finished by then, the chain's is not), so the error goes into that dialog's error pane
rather than a message box competing with the application-modal dialog. A failed step stops the
chain and the dialog closes right after, so the pane is not where the user reads it: the
chain's summary names the step and carries the `error` text, and lists what the pane showed,
traceback and all, under its Show errors button (`run_errors` keeps it). The log has it too.
aqt's `Interrupted` is not shown, as aqt's own error box does not show it.

Not in `chain_types`, which stays free of aqt.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Optional

from anki.errors import Interrupted
from aqt.errors import show_exception

from .api_client import take_stop_reason
from .chain_types import STEP_FAILED, StepOutcome
from .progress_errors import report_run_error

logger = logging.getLogger(__name__)


def error_summary(error: BaseException) -> str:
    """ "<exception type>: <message>", or the type alone for an exception without one."""
    detail = str(error)
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def failed_step_outcome(
    parent: Any, error: Exception, title: str, context: Optional[str] = None
) -> StepOutcome:
    """Show `error` and return the outcome of a step it failed. Never raises.

    `title` names the step as the chain does ("Step 2/4: Make meanings"). `context` says what
    the step was doing when it is not the op's own run ("downloading the word array
    resources"); it ends up in the outcome's error, and so in the chain's summary.

    Shown in the progress dialog's error pane when one is open, else as aqt shows an op's
    exception. A `CollectionOp` or `QueryOp` given a failure handler no longer shows it itself.
    """
    summary = error_summary(error)
    if context:
        summary += f" (while {context})"
    logger.error("%s failed: %s", title, summary, exc_info=error)
    # Interrupted is not an error to show but an interrupted backend call; aqt's box skips it too
    if not isinstance(error, Interrupted):
        _show(parent, error, title, summary)
    return StepOutcome(
        STEP_FAILED,
        # Not left for the next run to find; a run that raised may still have set one
        stop_reason=take_stop_reason(),
        error=summary,
    )


def _show(parent: Any, error: Exception, title: str, summary: str) -> None:
    try:
        text = summary
        if error.__traceback__ is not None:
            # The three-argument form: the one-argument one is 3.10+, and Anki runs on 3.9
            tb = traceback.format_exception(type(error), error, error.__traceback__)
            text += "\n\n" + "".join(tb)
        if not report_run_error(title, text):
            show_exception(parent=parent, exception=error)
    except Exception as e:
        # Still a failed step: the chain has to hear of it whether or not anything was shown
        logger.error("Could not show the error of %s: %s", title, e)
