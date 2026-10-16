import functools
import logging

from typing import Callable, Sequence, Any, Optional

from aqt import mw

from anki.notes import Note, NoteId
from aqt.utils import showWarning

from anki.collection import Collection


from ..utils import get_field_config

from ..async_api_ops.chain_types import ChainStep
from ..async_api_ops.base_ops import (
    AsyncTaskProgressUpdater,
    bulk_notes_op,
    selected_notes_op,
)
from ..async_api_ops.link_lookup import link_index
from ..async_api_ops.match_words_to_notes import decode_word_array_field
from ..async_api_ops.role_gate import notes_of_role
from ..link_ids import LinkIndex
from ..note_roles import SENTENCE_ROLE
from ..word_array.match_flags import format_word_array
from ..word_array.match_targets import unlink_missing_notes

logger = logging.getLogger(__name__)


def find_missing_matched_note_ids_for_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    *,
    held: Callable[[int], bool],
) -> bool:
    """Put every word of the note's array linked to an ID no note holds back to `["match"]`.
    `held` says whether a note holds a link ID (link_ids.py): never a `nid:` search, which
    finds a note by its note id whatever its link ID field holds, and no note at all for a
    generated ID."""
    note_type = note.note_type()
    log_prefix = f"Find missing matched note ids--nid:{note.id}--"
    if note_type is None:
        logger.error(f"{log_prefix}Error: Note has no note type")
        return False
    word_list_field = get_field_config(config, "word_list_field", note_type)
    if word_list_field not in note:
        logger.error(f"{log_prefix}Error: Note is missing the word list field '{word_list_field}'")
        return False
    arr = decode_word_array_field(note, word_list_field, notes_to_update_dict, log_prefix)
    if arr is None:
        return False
    unlinked = unlink_missing_notes(arr, held)
    if unlinked:
        logger.debug(f"{log_prefix}No notes found for {unlinked}, words set to be rematched")
        note[word_list_field] = format_word_array(arr)
        if note.id > 0 and note.id not in notes_to_update_dict:
            notes_to_update_dict[note.id] = note
    return True


def bulk_find_missing_matched_note_ids_op(
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]] = {},
    notes_to_update_dict: dict[NoteId, Note] = {},
):
    config = mw.addonManager.getConfig(__name__)
    if not config:
        showWarning("Missing addon configuration")
        return
    message = "Finding missing matched note ids"
    # The link IDs of every note type, read once for the run when the first note asks. Every
    # type, not just the vocab type: in the one-type layout a word can be linked to a note of
    # another type (the match op's candidates are the notes of every type with the vocab type's
    # word fields, and a CREATE NEW copy takes its candidate's type), and the `nid:` search this
    # replaces kept a link to a note of any type
    indexes: Optional[list[LinkIndex]] = None

    def held(held_id: int) -> bool:
        nonlocal indexes
        if indexes is None:
            indexes = [
                link_index(config, notetype.name, col=mw.col)
                for notetype in mw.col.models.all_names_and_ids()
            ]
        return any(held_id in index for index in indexes)

    op = functools.partial(find_missing_matched_note_ids_for_note, held=held)
    return bulk_notes_op(
        message,
        config,
        op,
        col,
        notes_of_role(config, notes, SENTENCE_ROLE),
        edited_nids,
        progress_updater,
        notes_to_add_dict,
        notes_to_update_dict,
        is_sync_op=True,
    )


def find_missing_matched_note_ids_selected_notes(
    nids: Sequence[NoteId], parent: Any, chain: Optional[ChainStep] = None
):
    progress_updater = AsyncTaskProgressUpdater(title="Sync op: Finding missing matched note ids")
    done_text = "Updated missing matched note ids"
    bulk_op = bulk_find_missing_matched_note_ids_op
    return selected_notes_op(done_text, bulk_op, nids, parent, progress_updater, chain=chain)
