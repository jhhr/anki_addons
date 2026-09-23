"""What one op run as a step of a chain is told, and what it reports back when it is over.

A chain runs several ops one after another over the same notes, each as a full run of its own
through `selected_notes_op`. The op cannot hand its result back by returning: it runs as a
`CollectionOp` and is over only when aqt calls its success or failure handler, on the main
thread, well after `selected_notes_op` has returned. So the chain passes a `ChainStep` in and
`selected_notes_op` calls its `on_done` with a `StepOutcome`, once, from that handler.

Kept free of aqt and anki, like api_client, so the chain's sequencing can be tested with these
types and fake ops alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

STEP_COMPLETED = "completed"
# Cancel or Escape in the progress dialog, a cancel of the cleanup's note adding, or a run left
# paused, which teardown cancels. What the step did is saved all the same.
STEP_CANCELLED = "cancelled"
# The run's own work stopped it (take_stop_reason: a usage limit, an expired login). Wins over
# cancelled: such a stop cancels the run too, and the reason is what the user needs to see.
STEP_STOPPED = "stopped"
# An exception came out of the op; aqt's error dialog has been shown for it.
STEP_FAILED = "failed"


@dataclass(frozen=True)
class StepOutcome:
    status: str
    # The end message the op shows when run from the menu, rich text with <br>s. Without the
    # "Stopped early" line: the stop reason is in `stop_reason`, for the chain to word itself.
    message: str = ""
    stop_reason: Optional[str] = None
    # "<exception type>: <message>" for a failed step
    error: Optional[str] = None

    @property
    def stops_chain(self) -> bool:
        return self.status != STEP_COMPLETED


@dataclass
class ChainStep:
    # "Step i/n"; the step's progress dialog title starts with it
    label: str
    # Called exactly once, on the main thread, after the step's progress dialog is finished.
    # Nothing of the step is left to run after it apart from aqt's own bookkeeping for the
    # operation, so the next step must be started from a later main-loop turn, not from here.
    on_done: Callable[[StepOutcome], None]


def fail_step(chain: Optional[ChainStep], error: str) -> None:
    """Tell `chain` its step failed without starting a run, if there is a chain.

    For an entry function that gives up before `selected_notes_op` (no config, no word array
    resources): nothing else would ever call `on_done`, and the chain would wait forever. A
    run from the menu has no chain and has already said why, so this does nothing for it.

    Unlike a run's, this `on_done` comes synchronously, from inside the call that started the
    step (or from a failed download's handler), with no progress dialog ever shown.
    """
    if chain is not None:
        chain.on_done(StepOutcome(STEP_FAILED, error=error))
