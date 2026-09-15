"""What happens to the mutations a run produced.

Evaluation never writes to the database. It edits the working note and card objects the
session holds and queues file writes; committing is what hands those notes and cards to the
caller's batched `update_notes()`/`update_cards()` and puts the files on disk. Keeping the
two apart is what lets a failure anywhere in a definition leave the collection alone, and
what lets preview run the real evaluator and simply not commit.

File writes are applied after the collection changes and are *not* covered by Anki's undo.
The commit result says whether any were written so the caller can say so too.
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
        return result

    def write_files(self, session: ExecutionSession, result: CommitResult) -> None:
        for pending in session.pending_files:
            try:
                write_media_file(pending["filename"], pending["content"])
                result.files.append(pending["filename"])
            except Exception as error:  # noqa: BLE001 -- reported, never raised past here
                # The collection changes are already committed and file writes are outside
                # undo, so a failure here is reported rather than unwinding anything.
                result.file_error = f"Error in writing to file: {error}"
                session.logger.error(result.file_error)
                return
        session.file_overlay.clear()


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
