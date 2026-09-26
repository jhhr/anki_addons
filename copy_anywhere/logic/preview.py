"""Running a definition without writing anything, and saying what it would have done (§9).

This is the same evaluator the real thing uses, with two differences: the committer records
the plan instead of publishing it, and the trace is collected. Everything else -- queries,
interpolation, code, process chains, called definitions, the depth and cycle checks -- runs
exactly as it does in a real run, because a preview that took a different path through the
code would be answering a different question.

Nothing here writes to the collection or to the media folder. Notes are re-fetched from the
collection so the objects the evaluator edits in memory are this preview's own copies, and
are thrown away with it: an editor that left the browser's note objects modified would be
worse than no preview at all.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

from anki.notes import Note, NoteId
from aqt import mw

from ..logging_setup import ADDON_MODULE, SHARED_LOGGER_NAME
from .definition_migration import MigrationError
from .definition_schema import CopyDefinitionV2
from .execution.commit import PreviewCommitter
from .execution.context import ExecutionSession, TraceEvent

#: How many notes the trigger-note browser offers at once. The list is there to pick one
#: note out of, not to be a second card browser.
TRIGGER_NOTE_LIMIT = 50


class _MessageCollector(logging.Handler):
    """Keeps the errors a previewed run reports, for the pane to show under its summary.

    Only errors, as the debug-level chatter of a run is the trace's job here: the pane shows
    what each stage did, and a message is for what went wrong.
    """

    def __init__(self, messages: list[str]) -> None:
        super().__init__(level=logging.ERROR)
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _collecting_messages(messages: list[str]) -> Iterator[None]:
    """Route what the addon and `jp_text_processing` log during the block into `messages`.

    The same two loggers an operation's file is attached to, for the same reason: a furigana
    process complaining about a reading is part of what the previewed definition did.
    """
    handler = _MessageCollector(messages)
    loggers = [logging.getLogger(ADDON_MODULE), logging.getLogger(SHARED_LOGGER_NAME)]
    for target in loggers:
        target.addHandler(handler)
    try:
        yield
    finally:
        for target in loggers:
            target.removeHandler(handler)


class PreviewRun:
    """What one previewed run of one definition against one note produced."""

    __slots__ = ("trace", "notes", "cards", "files", "messages", "succeeded", "trigger_id")

    def __init__(self) -> None:
        self.trace: list[TraceEvent] = []
        self.notes: list[dict] = []
        self.cards: list[dict] = []
        self.files: list[dict] = []
        self.messages: list[str] = []
        self.succeeded = True
        self.trigger_id: Optional[int] = None

    def __repr__(self) -> str:
        return (
            f"<PreviewRun {'ok' if self.succeeded else 'failed'}"
            f" notes={len(self.notes)} cards={len(self.cards)} files={len(self.files)}>"
        )

    # -- reading the trace ---------------------------------------------------------------

    def all_events(self) -> list[TraceEvent]:
        """Every event in the run, parents before their children."""
        found: list[TraceEvent] = []

        def walk(events: Sequence[TraceEvent]) -> None:
            for event in events:
                found.append(event)
                walk(event.children)

        walk(self.trace)
        return found

    def events_for(self, stage_guid: str) -> list[TraceEvent]:
        """Every time that stage ran, in order.

        A stage inside a loop body has one event per iteration, all carrying the same guid;
        which one the pane shows is the user's choice, so they all come back (§9).
        """
        return [event for event in self.all_events() if event.stage_guid == stage_guid]

    def changed_note_ids(self) -> list[int]:
        return [note["note_id"] for note in self.notes]


def run_preview(
    definition: CopyDefinitionV2,
    trigger_note: Note,
    definitions_for_calls: Optional[Sequence[dict]] = None,
    is_sync: bool = False,
    deck_id: Optional[int] = None,
) -> PreviewRun:
    """Evaluate `definition` against `trigger_note` and record what it would have done."""
    run = PreviewRun()
    run.trigger_id = trigger_note.id or None
    with _collecting_messages(run.messages):
        _preview_into(run, definition, trigger_note, definitions_for_calls, is_sync, deck_id)
    return run


def _preview_into(
    run: PreviewRun,
    definition: dict,
    trigger_note: Note,
    definitions_for_calls: Optional[Sequence[dict]],
    is_sync: bool,
    deck_id: Optional[int],
) -> None:
    # Imported here rather than at module level: `copy_fields` is the operation boundary and
    # already imports the executor, so importing it from a module the executor's own package
    # can reach would close a cycle.
    from .copy_fields import (
        definitions_a_call_may_reach,
        make_definition_lookup,
        note_passes_deck_whitelist,
    )
    from .execution.runner import as_format_2, run_definition_for_trigger_note

    try:
        staged = as_format_2(definition)
    except MigrationError as error:
        run.succeeded = False
        run.messages.append(str(error))
        return

    triggers = staged.get("triggers", {}) or {}
    if not note_passes_deck_whitelist(
        deck_names=triggers.get("deck_names") or [],
        include_subdecks=bool(triggers.get("include_subdecks", False)),
        trigger_note=trigger_note,
        deck_id=deck_id,
    ):
        run.messages.append(
            "This note is not in a deck the definition's trigger settings allow, so nothing"
            " would run for it."
        )
        return

    reachable = definitions_a_call_may_reach(staged, definitions_for_calls)
    committer = PreviewCommitter()
    session = ExecutionSession(
        is_sync=is_sync,
        deck_id=deck_id,
        definition_lookup=make_definition_lookup(reachable) if reachable else None,
        collect_trace=True,
    )
    run.succeeded = run_definition_for_trigger_note(
        definition=staged,
        trigger_note=trigger_note,
        session=session,
        committer=committer,
    )
    run.trace = session.trace
    run.notes = committer.planned_notes
    run.cards = committer.planned_cards
    run.files = committer.planned_files


def preview_note(note_id: int) -> Note:
    """A copy of the note for the preview to edit, so the real one is left alone.

    `get_note` builds a new object from the database on every call, which is all the
    isolation a preview needs: the evaluator writes into this object and nobody ever hands
    it to `update_notes()`.
    """
    return mw.col.get_note(NoteId(note_id))


def trigger_note_query(definition: CopyDefinitionV2, extra: str = "") -> str:
    """An Anki search for the notes this definition would consider, plus the user's terms.

    The note types come from the definition's own trigger settings, so the browser starts by
    offering notes the definition actually applies to rather than the whole collection.
    """
    triggers = definition.get("triggers", {}) or {}
    parts = []
    note_types = [name for name in (triggers.get("note_types") or []) if name]
    if note_types:
        joined = " OR ".join(f'note:"{name}"' for name in note_types)
        parts.append(f"({joined})")
    decks = [name for name in (triggers.get("deck_names") or []) if name]
    if decks:
        joined = " OR ".join(f'deck:"{name}"' for name in decks)
        parts.append(f"({joined})")
    extra = (extra or "").strip()
    if extra:
        parts.append(f"({extra})")
    return " ".join(parts)


def find_trigger_notes(
    definition: CopyDefinitionV2, extra: str = "", limit: int = TRIGGER_NOTE_LIMIT
) -> list[tuple[int, str]]:
    """Candidate trigger notes as `(note id, label)`, capped at `limit`.

    A definition with no note types and no deck restriction matches everything, so the cap
    is what keeps "show me something to preview against" from listing the collection.
    """
    query = trigger_note_query(definition, extra)
    ids = list(mw.col.find_notes(query)) if query else list(mw.col.find_notes(""))
    found = []
    for note_id in ids[:limit]:
        note = mw.col.get_note(NoteId(note_id))
        first = note.fields[0] if note.fields else ""
        label = re.sub(r"<[^>]+>", " ", first).strip() or f"note {note_id}"
        found.append((int(note_id), label))
    return found
