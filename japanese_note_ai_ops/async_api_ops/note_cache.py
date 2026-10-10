"""Every note the run fetches, kept for as long as the run lasts.

The matching op retrieves the same notes over and over. A word's notes are fetched once per
section that mentions the word, and the hot words appear in dozens of sentences, so one run
measured 2,398 fetches totalling 255,831s of waiting - a median of 123.4s per fetch once the
word index had freed enough capacity for the tasks to pile up behind the collection's one
permit. The notes behind those fetches are a much smaller set, and the second fetch of one
can only ever return what the first did.

That is the same argument `word_index.py` rests on, and it is the collection's own design
rather than a new assumption: a run does not write to the collection. Every update_notes,
remove_notes and add_note happens in base_ops' cleanup phase after the ops have finished, so
`get_note` returns the same row from the first turn of a run to the last.

What the cache changes, and what it does not:

- **Identity, not content.** Two calls now get the same Note object rather than two objects
  holding identical fields. Every reader in the matching op already prefers the object in
  `notes_to_update_dict` when there is one, and every writer puts a note it edits there, so a
  note that has been edited is served from that dict and the cached object is only ever handed
  out for notes nobody has touched. Callers that deliberately want a pristine copy of an
  already-edited note - `get_other_meaning_notes` under `allow_reupdate_existing` - do not go
  through here, so their semantics are unchanged.
- **Tag edits that skip `notes_to_update_dict` now persist within the run.** A tag put on a
  note that is not then registered (`clean_meaning` used to add `updated_jp_meaning`, and
  sometimes `MEANING_MAPPED_TAG`, and register the note only if its meanings changed) was
  discarded with a fresh copy per fetch; on a cached object a later reader such as
  `needs_meaning_mapping` sees it. Nothing is written to the collection either way, because
  writing is what `notes_to_update_dict` decides.

Bounded by the run rather than by a size cap. A whole run's distinct notes are a subset of one
notetype's notes, which the word index already holds the fields of, so the ceiling is a known
few thousand Note objects and the cache dies with the run that made it.
"""

import logging
from typing import TYPE_CHECKING, Iterable, Optional

from anki.errors import NotFoundError

from .collection_access import get_notes, get_notes_async

if TYPE_CHECKING:
    from anki.notes import Note, NoteId

logger = logging.getLogger(__name__)

# How often to log the running hit rate, in notes asked for. INFO so the rate shows up in a
# run logged at any level, the way the MDX memo's line does.
REPORT_EVERY = 500


class NoteCache:
    """The notes this run has fetched, by id.

    Not locked. Its callers are the event loop and, through `get_notes_blocking`, the pool
    threads the synchronous ops run on; the only shared state is one dict whose reads and
    writes are single bytecodes, so two callers racing for the same missing note is at worst
    one duplicate fetch - and `_store` keeps the object the first of them cached, so no reader
    ever sees its note swapped out underneath it. The counters can lose a count to the same
    race, which costs a hit rate a rounding error and nothing else.
    """

    __slots__ = ("_notes", "asked", "hits", "fetched", "_next_report")

    def __init__(self) -> None:
        self._notes: "dict[NoteId, Note]" = {}
        self.asked = 0
        self.hits = 0
        self.fetched = 0
        self._next_report = REPORT_EVERY

    def __len__(self) -> int:
        return len(self._notes)

    def peek(self, note_id: "NoteId") -> "Optional[Note]":
        """This note if the run has already fetched it, without asking the collection."""
        return self._notes.get(note_id)

    def _store(self, note: "Note") -> "Note":
        """Cache a note, or hand back the copy already cached for its id.

        The already-cached object wins so that a note handed to a caller stays the note that
        caller keeps seeing, even if a concurrent fetch of the same id lands in between.
        """
        return self._notes.setdefault(note.id, note)

    async def get_notes(self, note_ids: "Iterable[NoteId]") -> "dict[NoteId, Note]":
        """The notes for these ids, taking one turn with the collection for the ones missing.

        Ids the collection has no note for are absent from the result rather than raising, which
        every caller is written for: a word linked to a note deleted since, say, is left
        unrated. Anki's `get_note` raises `NotFoundError` for such an id, and with it the whole
        batch `get_notes_async` fetches in one turn, so then each is fetched alone: a turn per
        note, but only after a fetch has met a gone note.
        """
        ids = list(note_ids)
        if not ids:
            return {}
        self.asked += len(ids)

        found: "dict[NoteId, Note]" = {}
        missing: "list[NoteId]" = []
        for note_id in ids:
            note = self._notes.get(note_id)
            if note is None:
                missing.append(note_id)
            else:
                found[note_id] = note
        self.hits += len(found)

        if missing:
            # One turn for all of them, as before: taking and releasing the collection per note
            # lets every other waiting caller in between.
            for note in await _fetch_each_async(missing):
                found[note.id] = self._store(note)
            self.fetched += len(missing)
        self._report()
        return found

    def get_notes_blocking(self, note_ids: "Iterable[NoteId]") -> "dict[NoteId, Note]":
        """`get_notes` for a caller that is not on the event loop.

        The synchronous ops run in `asyncio.to_thread` workers and reach the collection
        through the blocking wrappers, so they cannot await this. Same cache, same one turn
        for whatever is missing, and an id with no note left out the same way.
        """
        ids = list(note_ids)
        if not ids:
            return {}
        self.asked += len(ids)

        found: "dict[NoteId, Note]" = {}
        missing: "list[NoteId]" = []
        for note_id in ids:
            note = self._notes.get(note_id)
            if note is None:
                missing.append(note_id)
            else:
                found[note_id] = note
        self.hits += len(found)

        if missing:
            for note in _fetch_each(missing):
                found[note.id] = self._store(note)
            self.fetched += len(missing)
        self._report()
        return found

    def _report(self) -> None:
        if self.asked < self._next_report:
            return
        self._next_report = self.asked + REPORT_EVERY
        logger.info(
            "Note cache: %d asked, %d fetched, %d from cache (%.1f%% avoided), %d notes held",
            self.asked,
            self.fetched,
            self.hits,
            100 * self.hits / self.asked,
            len(self._notes),
        )


async def _fetch_each_async(note_ids: "list[NoteId]") -> "list[Note]":
    """`get_notes_async`, leaving out the ids the collection has no note for: one turn for all
    of them, and a turn each only once that has raised (a lone id is then known to be gone)."""
    try:
        return await get_notes_async(note_ids)
    except NotFoundError:
        if len(note_ids) == 1:
            _log_gone(note_ids[0])
            return []
    notes: "list[Note]" = []
    for note_id in note_ids:
        try:
            notes.extend(await get_notes_async([note_id]))
        except NotFoundError:
            _log_gone(note_id)
    return notes


def _fetch_each(note_ids: "list[NoteId]") -> "list[Note]":
    """`_fetch_each_async` for a caller off the event loop."""
    try:
        return get_notes(note_ids)
    except NotFoundError:
        if len(note_ids) == 1:
            _log_gone(note_ids[0])
            return []
    notes: "list[Note]" = []
    for note_id in note_ids:
        try:
            notes.extend(get_notes([note_id]))
        except NotFoundError:
            _log_gone(note_id)
    return notes


def _log_gone(note_id: "NoteId") -> None:
    logger.warning("Note %s is not in the collection, left out of the fetch", note_id)
