"""Find proper nouns: a language model names the proper nouns of a note's word array sentence.

One request per note, to `proper_nouns_model`. The names come back as a list and
`word_array.proper_noun_llm.fix_array` makes each one, where it starts and ends on word boundaries,
a single `proper noun` word without sub-words. A note whose field holds no word array is skipped,
and note ids a merge dropped are logged, so a link lost to a bad call can be put back. Run it
before the word matching judge: a merged name is left with the match_data of its first word.
"""

import json
import logging
import threading
from collections.abc import Sequence
from typing import Optional

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning

from ..sync_local_ops.migrate_word_arrays import with_generator_resources
from ..utils import get_field_config
from ..word_array import generator, proper_noun_llm
from ..word_array.match_flags import decode_word_array
from .base_ops import AsyncTaskProgressUpdater, bulk_notes_op, get_response, selected_notes_op

logger = logging.getLogger(__name__)

# Notes are handled on several threads at once; one Sudachi tokenizer serves them all
_generator_lock = threading.Lock()


def _name_rest_word(word: list, rest_raw: str) -> Optional[list]:
    """`generator.name_rest_word`, one call at a time: splits a word the generator cut across a
    name (凛と -> 凛 + と) where the rest is a word of its own."""
    with _generator_lock:
        return generator.name_rest_word(word, rest_raw)


def proper_nouns_model(config: dict) -> str:
    return config.get("proper_nouns_model") or config.get("extract_words_model", "")


def find_proper_nouns_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
) -> bool:
    log_prefix = f"Find proper nouns--nid:{note.id}--"
    note_type = note.note_type()
    if not note_type:
        logger.error(f"{log_prefix}Missing note type")
        return False
    try:
        word_list_field = get_field_config(config, "word_list_field", note_type)
    except Exception as e:
        logger.error(f"{log_prefix}{e}")
        return False
    arr = decode_word_array(note[word_list_field]) if word_list_field in note else None
    if arr is None:
        logger.debug(f"{log_prefix}The word list field holds no word array")
        return False

    response = get_response(
        proper_nouns_model(config),
        proper_noun_llm.prompt(arr),
        response_schema=proper_noun_llm.RESPONSE_SCHEMA,
    )
    if response is None:
        logger.error(f"{log_prefix}No response from the model")
        return False
    try:
        names = proper_noun_llm.names_from_response(response)
    except ValueError as e:
        logger.error(f"{log_prefix}{e}")
        return False
    fix = proper_noun_llm.fix_array(arr, names, _name_rest_word)
    logger.debug(f"{log_prefix}names {names}, changed {fix.changed}")
    if fix.unaligned:
        logger.info(f"{log_prefix}names not on word boundaries: {fix.unaligned}")
    for note_id in fix.unlinked:
        logger.warning(f"{log_prefix}unlinked note {note_id} merging a proper noun")
    if fix.changed:
        current_note = notes_to_update_dict.get(note.id, note)
        current_note[word_list_field] = json.dumps(arr, ensure_ascii=False)
        if current_note.id > 0:
            notes_to_update_dict[current_note.id] = current_note
    return True


def bulk_find_proper_nouns_op(
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
    return bulk_notes_op(
        "Finding proper nouns",
        config,
        find_proper_nouns_in_note,
        col,
        notes,
        edited_nids,
        progress_updater,
        notes_to_add_dict,
        notes_to_update_dict,
    )


def find_proper_nouns_from_selected_notes(nids: Sequence[NoteId], parent: Browser):
    """Needs the generator's resources: a name the generator cut across is split with it."""

    def run():
        progress_updater = AsyncTaskProgressUpdater(title="Async AI op: Finding proper nouns")
        done_text = "Found proper nouns"
        selected_notes_op(done_text, bulk_find_proper_nouns_op, nids, parent, progress_updater)

    with_generator_resources(parent, run)
