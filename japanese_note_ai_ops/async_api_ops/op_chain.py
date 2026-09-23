"""Running several ops one after another over the same notes: a chain.

Each step is a full, ordinary run of its op through `selected_notes_op`, with its own cleanup
and undo entry; nothing is merged across steps. The chain only sequences them: it starts a step,
waits for the step's `ChainStep.on_done`, and starts the next one or stops, and once it is over
it shows one summary of every step instead of each step's own tooltip.

The progress dialog is the chain's, not the steps': the chain holds a progress level open from
before its first step to after its last, so each step's `CollectionOp` nests in it and reuses
its dialog. Were each step to open and close its own, the application-modal dialog would be gone
between two steps, and the browser the chain runs over could be closed or edited in that gap.

`OpChain` is the sequencing, with everything that needs Anki handed in as a callable, so the
order, the stops and the summary can be tested with fake ops and no Qt. `run_op_chain` wires in
the real collection, timers and dialogs.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Callable, Optional

from anki.notes import NoteId
from aqt import mw
from aqt.qt import QTimer
from aqt.utils import showInfo, showWarning

from ..generator_resources import with_generator_resources
from .base_ops import failed_step_outcome
from .progress_controls import install_idle_run_controls
from .chain_types import (
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_STOPPED,
    ChainStep,
    StepOutcome,
)

if TYPE_CHECKING:
    # Only for the annotation: the registry imports every op module, and a chain is handed its
    # specs by whoever built them
    from ..op_registry import OpSpec

logger = logging.getLogger(__name__)

SUMMARY_TITLE = "Japanese AI ops"

# The ids are inlined, not bound: a bound list hits SQLite's variable limit (999 in older
# builds), and ints cannot inject anything. Chunking keeps each statement small even for a
# chain over a whole collection.
EXISTING_IDS_CHUNK = 500

def existing_note_ids(col: Any, nids: Sequence[NoteId]) -> list[NoteId]:
    """`nids` without the ids whose notes no longer exist, in the order given.

    A step's cleanup can remove notes (the match op's duplicate-meaning cleanup), and
    `selected_notes_op` gets every id's note, which raises for a removed one.
    """
    ids = list(nids)
    found: set[int] = set()
    for start in range(0, len(ids), EXISTING_IDS_CHUNK):
        chunk = ids[start : start + EXISTING_IDS_CHUNK]
        found.update(
            col.db.list(
                f"select id from notes where id in ({','.join(str(int(n)) for n in chunk)})"
            )
        )
    return [nid for nid in ids if int(nid) in found]


class OpChain:
    """One run of a chain: `start` it once; it then drives itself from the steps' `on_done`s.

    The hooks:
    - `existing_ids(nids)`: those of `nids` whose notes still exist, order kept.
    - `schedule(func)`: call `func` later, from a fresh main-loop turn.
    - `hold_progress()` / `release_progress()`: open the chain's progress dialog before the
      first step, and let it go after the last (see the module docstring).
    - `failed_outcome(error)`: show `error` to the user and return the failed step's outcome.
    - `show_summary(text, stopped_early)`: show the rich-text summary once the progress dialog
      has gone.

    A step's `on_done` can come synchronously from inside `spec.start` (`fail_step`), and a
    run's comes from inside aqt's success handler, before aqt has finished with the operation.
    So nothing is started from inside `on_done`: the next step and the end both go through
    `schedule`.
    """

    def __init__(
        self,
        specs: Sequence[OpSpec],
        nids: Sequence[NoteId],
        parent: Any,
        *,
        existing_ids: Callable[[Sequence[NoteId]], list[NoteId]],
        schedule: Callable[[Callable[[], None]], None],
        hold_progress: Callable[[], None],
        release_progress: Callable[[], None],
        failed_outcome: Callable[[Exception], StepOutcome],
        show_summary: Callable[[str, bool], None],
    ):
        self.specs = list(specs)
        # Fixed now; notes a step adds never join them (a later step that searches the
        # collection sees those notes all the same)
        self.nids = list(nids)
        self.parent = parent
        self._existing_ids = existing_ids
        self._schedule = schedule
        self._hold_progress = hold_progress
        self._release_progress = release_progress
        self._holding = False
        self._failed_outcome = failed_outcome
        self._show_summary = show_summary
        # (step index, outcome) of every step that was started, in order
        self.outcomes: list[tuple[int, StepOutcome]] = []
        # Index of the step that found no notes left to run on
        self.notes_gone_at: Optional[int] = None
        self.ended = False
        self._started = False
        # Index of the step whose on_done is awaited, None when none is
        self._awaiting: Optional[int] = None

    def start(self, with_resources: Callable[[Any, Callable[[], None]], None]) -> None:
        """Begin the chain. `with_resources(parent, then)` is `with_generator_resources`.

        When any op needs the word array generator, its downloads are asked about once, here,
        rather than by the first such step in the middle of the chain. Declined, or with
        SudachiPy missing, `then` never runs and the chain never starts; the question or the
        warning has said why, so there is no summary. The steps' own checks then find
        everything present and go straight on.
        """
        if self._started:
            raise RuntimeError("A chain is started once")
        self._started = True
        if not self.specs:
            self.ended = True
            return
        if any(spec.needs_generator for spec in self.specs):
            # The download has a progress dialog of its own, done before the chain's opens
            with_resources(self.parent, self._begin)
        else:
            self._begin()

    def _begin(self) -> None:
        self._hold_progress()
        self._holding = True
        self._schedule(lambda: self._run_step(0))

    def label(self, index: int) -> str:
        return f"Step {index + 1}/{len(self.specs)}"

    def _run_step(self, index: int) -> None:
        spec = self.specs[index]
        self._awaiting = index
        try:
            ids = self._existing_ids(self.nids)
            if not ids:
                self._awaiting = None
                self.notes_gone_at = index
                self._end()
                return
            spec.start(ids, self.parent, ChainStep(self.label(index), self._on_done_for(index)))
        except Exception as e:
            if self._awaiting == index:
                # Nothing reported this step, and nothing of it will run now
                self._on_done_for(index)(self._failed_outcome(e))
            else:
                # The step had already said how it ended (a `fail_step` before raising); what
                # comes of that stands, but the error is not swallowed
                logger.exception("%s raised after it reported its end", self.label(index))
                self._failed_outcome(e)

    def _on_done_for(self, index: int) -> Callable[[StepOutcome], None]:
        def on_done(outcome: StepOutcome) -> None:
            if self.ended or self._awaiting != index:
                # A second call, or one after the chain ended: the step has been counted once
                logger.warning(
                    "%s reported its end again (%s); ignored", self.label(index), outcome.status
                )
                return
            self._awaiting = None
            self.outcomes.append((index, outcome))
            if outcome.stops_chain or index + 1 >= len(self.specs):
                self._end()
            else:
                self._schedule(lambda: self._run_step(index + 1))

        return on_done

    def _end(self) -> None:
        self.ended = True
        text, stopped_early = self.summary()

        def finish() -> None:
            if self._holding:
                self._holding = False
                self._release_progress()
            self._show_summary(text, stopped_early)

        # Not from inside on_done either: that is aqt's success handler, or a step's start
        self._schedule(finish)

    def summary(self) -> tuple[str, bool]:
        """The rich-text summary, and whether the chain stopped before its last step."""
        esc = html.escape
        parts: list[str] = []
        for index, outcome in self.outcomes:
            head = f"<b>{esc(self.label(index))}: {esc(self.specs[index].label)}</b>"
            if outcome.status == STEP_CANCELLED:
                head += " (cancelled; what it had finished is saved)"
            elif outcome.status == STEP_STOPPED:
                head += " (stopped)"
            elif outcome.status == STEP_FAILED:
                head += " (failed)"
            # The step's message is rich text already, as its tooltip would have shown it
            parts.append(f"{head}<br>{outcome.message}" if outcome.message else head)

        stop: Optional[str] = None
        not_run_from: Optional[int] = None
        last = self.outcomes[-1] if self.outcomes else None
        if self.notes_gone_at is not None:
            index = self.notes_gone_at
            stop = (
                f"The chain stopped before {esc(self.label(index))}"
                f" ({esc(self.specs[index].label)}): none of its notes exist any more."
            )
            not_run_from = index
        elif last is not None and last[1].stops_chain:
            index, outcome = last
            stop = f"The chain stopped at {esc(self.label(index))} ({esc(self.specs[index].label)})"
            if outcome.status == STEP_CANCELLED:
                stop += ": it was cancelled."
                done = [o for i, o in self.outcomes if i < index]
                if done:
                    stop += (
                        f" The {len(done)} step{'s' if len(done) > 1 else ''} before it"
                        " ran to the end."
                    )
                stop += (
                    " What the cancelled step had finished is saved, as its own undo entry"
                    " like every step's."
                )
            elif outcome.status == STEP_STOPPED:
                stop += f": {esc(outcome.stop_reason or 'it stopped itself')}"
            else:
                stop += f": it failed with {esc(outcome.error or 'an error')}"
                if outcome.stop_reason:
                    stop += f"<br>It also stopped: {esc(outcome.stop_reason)}"
            not_run_from = index + 1

        if stop is not None:
            parts.append(stop)
            assert not_run_from is not None
            not_run = [
                f"{esc(self.label(i))}: {esc(self.specs[i].label)}"
                for i in range(not_run_from, len(self.specs))
            ]
            if not_run:
                parts.append("<b>Did not run:</b><br>" + "<br>".join(not_run))
        return "<br><br>".join(parts), stop is not None


def run_op_chain(specs: Sequence[OpSpec], nids: Sequence[NoteId], parent: Any) -> None:
    """Run `specs` in order over `nids`, each as a step of one chain, and sum them up at the
    end. Returns at once; the steps run later, from the main loop."""
    def schedule(func: Callable[[], None]) -> None:
        # Not aqt's single_shot, which holds a call back for as long as a progress is open:
        # the chain's own is, from its first step to its last
        QTimer.singleShot(0, func)

    def hold_progress() -> None:
        mw.progress.start(parent=parent, immediate=True, title=SUMMARY_TITLE)
        # Greyed until a step's run brings them in, so Escape cannot cancel the gap before it
        install_idle_run_controls()

    def show_summary(text: str, stopped_early: bool) -> None:
        def show() -> None:
            show_dialog = showWarning if stopped_early else showInfo
            show_dialog(text, parent=parent, title=SUMMARY_TITLE, textFormat="rich")

        # aqt's timer this time: it fires once no progress is open, so the summary is not
        # shown under the chain's dialog while that is still closing
        mw.progress.single_shot(0, show)

    OpChain(
        specs,
        nids,
        parent,
        existing_ids=lambda ids: existing_note_ids(mw.col, ids),
        schedule=schedule,
        hold_progress=hold_progress,
        release_progress=mw.progress.finish,
        failed_outcome=lambda error: failed_step_outcome(parent, error),
        show_summary=show_summary,
    ).start(with_generator_resources)
