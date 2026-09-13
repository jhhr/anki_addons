"""Shared state for one top-level copy operation: overlays, caches, trace and counters.

A run has to answer "what does this note say right now" consistently no matter which stage
asks, which note reference it asks through, or how many times the same note comes back from
different queries. That is what the overlays are for: every note, card and file a run
touches is fetched once and kept, so a later stage reading a note an earlier stage wrote
sees the edit, and two references to the same note converge on one working object rather
than on two copies that overwrite each other.

Queries deliberately do *not* go through the overlays. `find_notes` reads the persisted
collection, so which notes a query matches never depends on edits that have not been saved
yet. Without that rule a definition's behaviour would depend on whether a flush happened to
have run, and a preview could not match a real run.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Callable, Optional, Sequence, Union

from anki.cards import Card
from anki.notes import Note
from aqt import mw

from ...shared.utils.logger import Logger
from ...utils.media_files import (
    MediaFileError,
    media_file_exists,
    normalize_media_filename,
    read_media_file,
)

#: The key an unsaved note is held under. A note being added has id 0, so its identity is
#: the object itself; anything else would collide with every other unsaved note.
NoteKey = Union[int, str]


class TriggerSkipped(Exception):
    """This trigger note is not one the definition applies to.

    Raised by the deck whitelist and by a condition that does not match. Benign: the caller
    keeps going with the next note, and nothing is written for this one.
    """


class SkipBlock(Exception):
    """Stop the enclosing block, without failing the definition.

    Raised by `if_empty: skip_block` and `if_missing: skip_block`. The stages after it in
    the block do not run; the definition still commits whatever ran before it.
    """


class StageError(Exception):
    """A structured failure, carrying where it happened (§7.2).

    Every field is optional because the same error type is raised from stage validation,
    from interpolation and from user code, which know different amounts about the context.
    """

    def __init__(
        self,
        message: str,
        definition_guid: Optional[str] = None,
        definition_name: Optional[str] = None,
        stage_guid: Optional[str] = None,
        stage_type: Optional[str] = None,
        note_id: Optional[int] = None,
        loop_path: Sequence[int] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.definition_guid = definition_guid
        self.definition_name = definition_name
        self.stage_guid = stage_guid
        self.stage_type = stage_type
        self.note_id = note_id
        self.loop_path = tuple(loop_path)

    def with_context(
        self,
        definition_guid: Optional[str] = None,
        definition_name: Optional[str] = None,
        stage_guid: Optional[str] = None,
        stage_type: Optional[str] = None,
        note_id: Optional[int] = None,
        loop_path: Optional[Sequence[int]] = None,
    ) -> "StageError":
        """Fill in the context the raising site did not know. Outer frames only add."""
        if self.definition_guid is None:
            self.definition_guid = definition_guid
        if self.definition_name is None:
            self.definition_name = definition_name
        if self.stage_guid is None:
            self.stage_guid = stage_guid
            self.stage_type = stage_type
        if self.note_id is None:
            self.note_id = note_id
        if loop_path and not self.loop_path:
            self.loop_path = tuple(loop_path)
        return self

    def context_description(self) -> str:
        """Where this failed, without the message: definition, stage, note, iteration."""
        parts = []
        if self.definition_name:
            parts.append(f"definition '{self.definition_name}'")
        if self.stage_type:
            parts.append(f"stage {self.stage_type}")
        if self.stage_guid:
            parts.append(f"({self.stage_guid})")
        if self.note_id is not None:
            parts.append(f"note id {self.note_id}")
        if self.loop_path:
            parts.append("iteration " + ".".join(str(index) for index in self.loop_path))
        return ", ".join(parts)

    def describe(self) -> str:
        context = self.context_description()
        return f"{self.message} [{context}]" if context else self.message


class TraceEvent:
    """One stage's execution, for the preview pane and for debugging a run (§9)."""

    __slots__ = (
        "stage_guid",
        "stage_type",
        "stage_name",
        "status",
        "duration_ms",
        "inputs",
        "result",
        "mutations",
        "details",
        "children",
        "error",
        "loop_path",
    )

    def __init__(
        self,
        stage_guid: Optional[str],
        stage_type: Optional[str],
        stage_name: str = "",
        loop_path: Sequence[int] = (),
    ) -> None:
        self.stage_guid = stage_guid
        self.stage_type = stage_type
        self.stage_name = stage_name
        self.status = "running"
        self.duration_ms = 0.0
        self.inputs: dict[str, str] = {}
        self.result: Optional[str] = None
        self.mutations: list[str] = []
        self.details: dict[str, Any] = {}
        self.children: list["TraceEvent"] = []
        self.error: Optional[str] = None
        self.loop_path = tuple(loop_path)

    def __repr__(self) -> str:
        return f"<TraceEvent {self.stage_type} {self.status}>"


#: Trace values longer than this are cut for display; the full value stays out of the trace
#: entirely rather than being kept twice.
TRACE_VALUE_LIMIT = 500


def summarize(value: Any, limit: int = TRACE_VALUE_LIMIT) -> str:
    if isinstance(value, Note):
        return f"<note {value.id}>"
    if isinstance(value, Card):
        return f"<card {value.id}>"
    if isinstance(value, (list, tuple)):
        return f"[{len(value)} items]"
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ExecutionSession:
    """Everything one top-level operation shares, including across nested calls (§3)."""

    def __init__(
        self,
        logger: Logger = Logger("error"),
        is_sync: bool = False,
        field_only: Optional[str] = None,
        deck_id: Optional[int] = None,
        progress_updater: Any = None,
        file_cache: Optional[dict] = None,
        definition_lookup: Optional[Callable[[str], Optional[dict]]] = None,
        want_cancel: Optional[Callable[[], bool]] = None,
        collect_trace: bool = False,
        add_note_compatible_only: bool = False,
    ) -> None:
        self.logger = logger
        self.is_sync = is_sync
        self.field_only = field_only
        self.deck_id = deck_id
        self.progress_updater = progress_updater
        self.file_cache = file_cache if file_cache is not None else {}
        self.definition_lookup = definition_lookup
        self._want_cancel = want_cancel
        self.collect_trace = collect_trace
        #: Set while running against a note that has not been added yet. The commit refuses
        #: any mutation to another note or to a card, whatever the definition's stored
        #: `effects` claimed (§8).
        self.add_note_compatible_only = add_note_compatible_only

        self.notes: dict[NoteKey, Note] = {}
        self.cards: dict[int, Card] = {}
        self.file_overlay: dict[str, str] = {}

        self.modified_notes: dict[NoteKey, Note] = {}
        self.touched_cards: dict[int, Card] = {}
        self.edited_cards: dict[int, Card] = {}
        self.pending_files: list[dict] = []

        self.query_cache: dict[str, list[int]] = {}
        self.call_stack: list[str] = []
        self.trace: list[TraceEvent] = []
        self.cancelled = False

    # -- notes ------------------------------------------------------------------------

    @staticmethod
    def note_key(note: Note) -> NoteKey:
        # A transient add-note trigger has id 0 and no persisted identity, so it is keyed by
        # object instead. Every persisted note is keyed by id, which is what makes two
        # references to it converge on one working note (§7.1).
        return note.id if note.id else f"new-{id(note)}"

    def working_note(self, note: Note) -> Note:
        """Register `note` as this run's working copy of itself, or return the existing one."""
        key = self.note_key(note)
        existing = self.notes.get(key)
        if existing is not None:
            return existing
        self.notes[key] = note
        return note

    def note_by_id(self, note_id: int) -> Note:
        existing = self.notes.get(note_id)
        if existing is not None:
            return existing
        note = mw.col.get_note(note_id)
        self.notes[note_id] = note
        return note

    def mark_note_modified(self, note: Note) -> None:
        self.modified_notes[self.note_key(note)] = note

    def is_modified(self, note: Note) -> bool:
        return self.note_key(note) in self.modified_notes

    # -- cards ------------------------------------------------------------------------

    def cards_of_note(self, note: Note) -> list[Card]:
        """This run's working cards for `note`, in template order.

        Fetched once per card id, so a card action queued by one stage is visible to the
        next stage that reads the same card.
        """
        if not note.id:
            return []
        cards = []
        for card in note.cards():
            cards.append(self.cards.setdefault(card.id, card))
        return cards

    def card_by_id(self, card_id: int) -> Card:
        existing = self.cards.get(card_id)
        if existing is not None:
            return existing
        card = mw.col.get_card(card_id)
        self.cards[card_id] = card
        return card

    def touch_cards(self, cards: Sequence[Card]) -> None:
        """Record cards a stage looked at.

        Format 1 handed the caller every card of every destination note, edited or not,
        because the sync path writes its `fc` custom-data flag onto all of them.
        """
        for card in cards:
            self.touched_cards[card.id] = card

    def mark_card_edited(self, card: Card) -> None:
        self.edited_cards[card.id] = card
        self.touched_cards[card.id] = card

    # -- files ------------------------------------------------------------------------

    def queue_file_write(
        self, filename: str, content: str, overwrite: bool = True, skip_if_exists: bool = False
    ) -> bool:
        """Queue one media write. Returns False when an existing file made it a no-op."""
        name = normalize_media_filename(filename)
        if name not in self.file_overlay and (skip_if_exists or not overwrite):
            if media_file_exists(name):
                if skip_if_exists:
                    return False
                raise MediaFileError(
                    f"File '{name}' already exists and the stage does not overwrite"
                )
        self.file_overlay[name] = content
        self.pending_files.append({"filename": name, "content": content})
        return True

    def read_file(self, filename: str) -> Optional[str]:
        """The file's content, reading through the overlay so a queued write is visible."""
        name = normalize_media_filename(filename)
        if name in self.file_overlay:
            return self.file_overlay[name]
        return read_media_file(name)

    def discard(self) -> None:
        """Throw this trigger's pending plan away without committing any of it.

        Used when a definition fails, is cancelled, or turns out not to apply to the note:
        the working note objects keep whatever was written into them in memory, but nothing
        is handed to `update_notes()` and no file reaches the disk.
        """
        self.modified_notes.clear()
        self.touched_cards.clear()
        self.edited_cards.clear()
        self.pending_files.clear()
        self.file_overlay.clear()

    # -- queries ----------------------------------------------------------------------

    def find_notes(self, query: str) -> list[int]:
        return self._cached_search("notes", query)

    def find_cards(self, query: str) -> list[int]:
        return self._cached_search("cards", query)

    def _cached_search(self, kind: str, query: str) -> list[int]:
        key = base64.b64encode(f"{kind}{query}".encode()).decode()
        cached = self.query_cache.get(key)
        if cached is None:
            finder = mw.col.find_notes if kind == "notes" else mw.col.find_cards
            cached = list(finder(query))
            self.query_cache[key] = cached
        # A copy: selection strategies consume the list, and handing out the cached one
        # would give the next caller a result with entries missing.
        return list(cached)

    # -- progress and cancellation -----------------------------------------------------

    def update_counts(self, **kwargs: Any) -> None:
        if self.progress_updater is not None:
            self.progress_updater.update_counts(**kwargs)

    def render_progress(self) -> None:
        if self.progress_updater is not None:
            self.progress_updater.maybe_render_update()

    def check_cancel(self) -> bool:
        """Checked between triggers, loop iterations, queries and nested calls (§7.1)."""
        if self.cancelled:
            return True
        if self._want_cancel is not None and self._want_cancel():
            self.cancelled = True
        return self.cancelled

    # -- trace ------------------------------------------------------------------------

    def start_event(
        self, stage: dict, loop_path: Sequence[int], parent: Optional[TraceEvent]
    ) -> Optional[TraceEvent]:
        if not self.collect_trace:
            return None
        event = TraceEvent(
            stage.get("guid"), stage.get("type"), stage.get("name", ""), loop_path
        )
        event.details["started"] = time.time()
        if parent is not None:
            parent.children.append(event)
        else:
            self.trace.append(event)
        return event

    @staticmethod
    def finish_event(
        event: Optional[TraceEvent], status: str, result: Any = None, error: Optional[str] = None
    ) -> None:
        if event is None:
            return
        started = event.details.pop("started", None)
        if started is not None:
            event.duration_ms = (time.time() - started) * 1000
        event.status = status
        if result is not None:
            event.result = summarize(result)
        event.error = error


class DefinitionFrame:
    """One invocation of one definition: its trigger note and its own result environment.

    A nested call gets a fresh frame, so it cannot see the caller's variables, lists or loop
    bindings -- only its trigger note and whatever the session shares (§5.9).
    """

    def __init__(
        self,
        definition: dict,
        trigger_note: Note,
        session: ExecutionSession,
        depth: int = 0,
    ) -> None:
        self.definition = definition
        self.trigger_note = trigger_note
        self.session = session
        self.depth = depth
        self.guid = definition.get("guid", "")
        self.name = definition.get("definition_name", "")
        # Multi-note-type definitions omit the card type name from card values, because a
        # definition spanning several note types cannot name one template for all of them.
        self.multiple_note_types = (
            len(definition.get("triggers", {}).get("note_types", []) or []) > 1
        )
        #: Values format-1 expressions expect to find among the variables, filled in by the
        #: stages a migration synthesized: the query's size and the current loop index.
        self.legacy_values: dict[str, Any] = {}
        #: The root block's scope, so `exports` can read the results it left behind.
        self.root_env: Optional[dict] = None
        self.loop_path: list[int] = []

    def mark_root_environment(self, env: dict) -> None:
        self.root_env = env

    def error(self, message: str, stage: Optional[dict] = None) -> StageError:
        return StageError(
            message,
            definition_guid=self.guid,
            definition_name=self.name,
            stage_guid=stage.get("guid") if stage else None,
            stage_type=stage.get("type") if stage else None,
            note_id=self.trigger_note.id if self.trigger_note is not None else None,
            loop_path=tuple(self.loop_path),
        )
