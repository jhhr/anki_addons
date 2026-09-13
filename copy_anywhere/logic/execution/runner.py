"""Running one definition against one trigger note, and reporting what happened.

This is the boundary `copy_fields.py` calls: it owns the session, decides what a failure,
a skip and a cancellation each mean for the caller's bulk loop, keeps the progress counters
format 1 reported, and commits the run's mutations.

The return value is the same three-way answer format 1 gave: `True` means "this note is
done, keep going" whether or not anything was written, and `False` means the definition
failed and the bulk loop must stop so the user can see why.
"""

from __future__ import annotations

from typing import Optional

from anki.notes import Note

from ..definition_schema import is_format_2
from ..definition_migration import MigrationError, migrate_definition_v1_to_v2
from .commit import CollectionCommitter
from .context import DefinitionFrame, ExecutionSession, StageError, TriggerSkipped
from .evaluator import Cancelled, execute_definition


def as_format_2(definition: dict) -> dict:
    """The definition as stages, migrating a stored format-1 one on the way in.

    Migration is pure and cheap, and doing it here means the editor, the hooks and the
    stored config can stay on format 1 until the format-2 editor lands, while everything
    that actually runs is one executor over one format.
    """
    if is_format_2(definition):
        return definition
    return migrate_definition_v1_to_v2(definition)


def run_definition_for_trigger_note(
    definition: dict,
    trigger_note: Note,
    session: ExecutionSession,
    committer: Optional[CollectionCommitter] = None,
    copied_into_notes: Optional[list] = None,
    copied_into_cards_dict: Optional[dict] = None,
) -> bool:
    """Evaluate `definition` for one trigger note and commit what it produced."""
    committer = committer or CollectionCommitter()
    logger = session.logger
    try:
        staged = as_format_2(definition)
    except MigrationError as error:
        logger.error(str(error))
        return False

    frame = DefinitionFrame(staged, session.working_note(trigger_note), session)
    try:
        execute_definition(frame)
    except TriggerSkipped:
        # The deck whitelist or a copy condition said this note is not one the definition
        # applies to. Benign: nothing is written, nothing is committed, the loop goes on.
        session.update_counts(skipped_note_cnt_inc=1)
        session.discard()
        return True
    except Cancelled:
        # A half-evaluated trigger frame is never committed (§7.1).
        session.discard()
        return True
    except StageError as error:
        logger.error(error.message)
        context = error.context_description()
        if context:
            logger.debug(f"copy_for_single_trigger_note: failed at {context}")
        session.discard()
        return False

    session.update_counts(note_cnt_inc=1)
    legacy = staged.get("legacy") or {}
    if legacy.get("trigger_is_source"):
        # Format 1 counted the trigger note as the one source in every mode but
        # Destination-to-sources, where the query result was the source list.
        session.update_counts(processed_sources_inc=1)
    committer.commit(session, copied_into_notes, copied_into_cards_dict)
    return True
