"""What happens to the mutations a run produced.

Evaluation never writes to the database. It edits the working note and card objects the
session holds and queues file writes; committing is what hands those notes and cards to the
caller's batched `update_notes()`/`update_cards()` and puts the files on disk. Keeping the
two apart is what lets a failure anywhere in a definition leave the collection alone, and
what lets preview run the real evaluator and simply not commit.

File writes are applied after the collection changes and are *not* covered by Anki's undo.
A write that fails stops the rest: the files before it are on disk, it and the ones queued
after it are not, and the commit result carries the reason so the runner can fail the run
the way it fails a stage error -- the notes and cards are already the caller's by then.
"""

from __future__ import annotations

from typing import Optional

from anki.cards import Card
from anki.notes import Note

from ...utils.media_files import write_media_file
from .context import ExecutionSession


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
    ) -> CommitResult:
        result = CommitResult()
        for note in session.modified_notes.values():
            result.notes.append(note)
            if copied_into_notes is not None:
                copied_into_notes.append(note)
        for card in session.touched_cards.values():
            result.cards.append(card)
            if copied_into_cards_dict is not None:
                copied_into_cards_dict[card.id] = card
        self.write_files(session, result)
        session.modified_notes.clear()
        session.touched_cards.clear()
        session.edited_cards.clear()
        session.pending_files.clear()
        # Cleared whether or not every file made it: the overlay is what a later read sees
        # "as if the file were written", and once the queue is empty a file is either on
        # disk, where the read finds it anyway, or abandoned, in which case the overlay
        # would be the only thing still claiming it exists.
        session.file_overlay.clear()
        return result

    def write_files(self, session: ExecutionSession, result: CommitResult) -> None:
        for index, pending in enumerate(session.pending_files):
            try:
                write_media_file(pending["filename"], pending["content"])
                result.files.append(pending["filename"])
            except Exception as error:  # noqa: BLE001 -- reported, never raised past here
                # The collection changes are already committed and file writes are outside
                # undo, so a failure here is recorded rather than unwinding anything. The
                # files after it are not attempted: the definition wrote them in this order
                # for a reason, and a later one may well fail the same way.
                remaining = len(session.pending_files) - index - 1
                result.file_error = (
                    f"Error in writing to file '{pending['filename']}': {error}."
                    " The note and card changes were saved; this file"
                    + (f" and the {remaining} queued after it" if remaining else "")
                    + " were not written."
                )
                return


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
        session.touched_cards.clear()
        session.edited_cards.clear()
        session.pending_files.clear()
        session.file_overlay.clear()
        return result

    def write_files(self, session: ExecutionSession, result: CommitResult) -> None:
        # Deliberately nothing: a preview that touched the media folder would not be one.
        return
