"""What happens to the mutations a run produced.

Evaluation never writes to the database. It edits the working note and card objects the
session holds and queues file writes; committing is what hands those notes and cards to the
caller's batched `update_notes()`/`update_cards()` and puts the files on disk. Keeping the
two apart is what lets a failure anywhere in a definition leave the collection alone, and
what lets preview run the real evaluator and simply not commit.

File writes are *not* covered by Anki's undo, and they go to disk only once the note and card
changes they belong with are saved. A caller that saves notes -- a bulk run once per
definition, the hooks after each definition -- passes `copied_into_files`, which collects the
files as `copied_into_notes` collects the notes, and writes them with `write_queued_files`
after its `update_notes()`. Written at commit instead, a bulk run that failed on a later
trigger note left files on disk whose notes were never saved. A caller that saves nothing
itself passes no dict, and the files are written at commit.

A write that fails stops the rest: the files before it are on disk, it and the ones queued
after it are not, and the reason is returned so the caller can fail the run the way it fails
a stage error -- the notes and cards are the caller's by then.
"""

from __future__ import annotations

from typing import Optional

from anki.cards import Card
from anki.notes import Note

from ...utils.media_files import file_key, write_media_file
from .context import ExecutionSession, QueuedFiles


class CommitResult:
    __slots__ = ("notes", "cards", "files", "file_error")

    def __init__(self) -> None:
        self.notes: list[Note] = []
        self.cards: list[Card] = []
        self.files: list[str] = []
        self.file_error: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"<CommitResult notes={len(self.notes)} cards={len(self.cards)}"
            f" files={len(self.files)}>"
        )


class CollectionCommitter:
    """Publishes a run's mutations: notes and cards to the caller, files to disk."""

    def commit(
        self,
        session: ExecutionSession,
        copied_into_notes: Optional[list] = None,
        copied_into_cards_dict: Optional[dict] = None,
        copied_into_files: Optional[QueuedFiles] = None,
    ) -> CommitResult:
        result = CommitResult()
        for note in session.modified_notes.values():
            result.notes.append(note)
            if copied_into_notes is not None:
                copied_into_notes.append(note)
        # Only the cards a card action changed. The caller's dict outlives this trigger note,
        # so an unchanged copy of a card put there would replace the copy an earlier trigger
        # note edited, and that edit would be lost. Format 1 handed over every card of every
        # note it wrote, for the sync tail's `fc` flag; the tail's sweep searches for every
        # card still waiting, so it needs none of them from here.
        #
        # This does not stop two trigger notes of one bulk run that both *change* a card, or
        # both write a note, from losing the earlier change: each run starts from the saved
        # collection, the caller saves once per definition, and the later copy replaces the
        # earlier one here and in `copied_into_notes`. That is an accepted limitation, not
        # an oversight -- merging two copies of a note or card edit by edit is more than is
        # worth maintaining. See "Only edited cards are handed over" in
        # `docs/staged-definitions.md`.
        for card in session.edited_cards.values():
            result.cards.append(card)
            if copied_into_cards_dict is not None:
                copied_into_cards_dict[card.id] = card
        if copied_into_files is not None:
            for pending in session.pending_files:
                # Moved to the end: the file lands with the content, and in the order, of
                # its last write.
                key = file_key(pending["filename"])
                copied_into_files.pop(key, None)
                copied_into_files[key] = (pending["filename"], pending["content"])
                result.files.append(pending["filename"])
        else:
            self.write_files(session, result)
        session.modified_notes.clear()
        session.edited_cards.clear()
        session.pending_files.clear()
        # Cleared whether or not every file made it: the overlay is what a later read sees
        # "as if the file were written", and once the queue is empty a file is either on
        # disk or in the caller's queue, where a read finds it anyway, or abandoned, in which
        # case the overlay would be the only thing still claiming it exists.
        session.file_overlay.clear()
        return result

    def write_files(self, session: ExecutionSession, result: CommitResult) -> None:
        written, result.file_error = _write(
            [(pending["filename"], pending["content"]) for pending in session.pending_files]
        )
        result.files.extend(written)


def write_queued_files(copied_into_files: QueuedFiles) -> Optional[str]:
    """Write what the runs queued into `copied_into_files`, once their notes are saved.

    Returns why a write failed, for the caller to log as it logs a failed run, or None. The
    dict is emptied either way: what did not make it is abandoned, as a failed run's is.
    """
    _written, error = _write(list(copied_into_files.values()))
    copied_into_files.clear()
    return error


def _write(files: list[tuple[str, str]]) -> tuple[list[str], Optional[str]]:
    written: list[str] = []
    for index, (filename, content) in enumerate(files):
        try:
            write_media_file(filename, content)
            written.append(filename)
        except Exception as error:  # noqa: BLE001 -- reported, never raised past here
            # The collection changes are the caller's already and file writes are outside
            # undo, so a failure here is recorded rather than unwinding anything. The files
            # after it are not attempted: the definition wrote them in this order for a
            # reason, and a later one may well fail the same way.
            remaining = len(files) - index - 1
            not_written = (
                f"this file and the {remaining} queued after it were not written"
                if remaining
                else "this file was not written"
            )
            return written, (
                f"Error in writing to file '{filename}': {error}."
                f" The note and card changes are kept; {not_written}."
            )
    return written, None


class PreviewCommitter(CollectionCommitter):
    """Records what a run would do and persists none of it (§9).

    Queries are real and read-only; note, card and file mutations are captured. Note objects
    are still edited in memory, which is how a later stage sees an earlier one's write, but
    nothing is ever handed to `update_notes()` and nothing is written to the media folder.
    """

    def __init__(self) -> None:
        self.planned_notes: list[dict] = []
        self.planned_cards: list[dict] = []
        self.planned_files: list[dict] = []

    def commit(
        self,
        session: ExecutionSession,
        copied_into_notes: Optional[list] = None,
        copied_into_cards_dict: Optional[dict] = None,
        copied_into_files: Optional[QueuedFiles] = None,
    ) -> CommitResult:
        result = CommitResult()
        for note in session.modified_notes.values():
            self.planned_notes.append({
                "note_id": note.id,
                "fields": dict(zip(note.keys(), note.values())),
                "tags": list(note.tags),
            })
            result.notes.append(note)
        for card in session.edited_cards.values():
            self.planned_cards.append({
                "card_id": card.id,
                "deck_id": card.odid or card.did,
                "queue": card.queue,
                "flag": card.user_flag(),
            })
            result.cards.append(card)
        self.planned_files.extend(dict(pending) for pending in session.pending_files)
        result.files = [pending["filename"] for pending in session.pending_files]
        session.modified_notes.clear()
        session.edited_cards.clear()
        session.pending_files.clear()
        session.file_overlay.clear()
        return result

    def write_files(self, session: ExecutionSession, result: CommitResult) -> None:
        # Deliberately nothing: a preview that touched the media folder would not be one.
        return
