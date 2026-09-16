"""Extract words: a note's sentence becomes a word array, generated locally and then read for
proper nouns by a language model.

Two steps for one note, one API call, through `bulk_notes_op` as before:

1. `word_array.generator.generate` on the context-stripped sentence with the name lexicon, no
   API call and one Sudachi tokenizer at a time (`find_proper_nouns.generate_word_array`).
2. the proper noun call (`find_proper_nouns.add_proper_nouns`), so that a name neither Sudachi
   nor the lexicon knows still comes out as one `proper noun` word.

The words are left unjudged (`match_data` `[]`): which of them are worth a note is the word
matching judge's call, either from its own menu entry or as phase 2 of "Extract words + Judge
matchability". This op replaced the one that asked a model for the whole word list under a long
prompt; the word array is built by rules instead (see `word_array/README.md`).

A note whose word list field already holds an array is skipped, so re-running over a selection
costs nothing. A note still holding an **old** extract_words word list is skipped too:
overwriting it would throw away the note ids of the words already matched, which is
`sync_local_ops/migrate_word_arrays.py`'s job to carry over.

The only model is `proper_nouns_model`, falling back to `extract_words_model`.
"""

import json
import logging
from collections.abc import Sequence
from functools import partial
from typing import Optional

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning

from ..html_stripping import strip_context_sentences
from ..sync_local_ops.migrate_word_arrays import with_generator_resources
from ..utils import get_field_config, print_error_traceback
from ..word_array import names, resources
from ..word_array.match_flags import JUDGE_NEW, decode_word_array
from .base_ops import (
    AsyncTaskProgressUpdater,
    OpPhase,
    bulk_notes_op,
    selected_notes_op,
)
from .find_proper_nouns import add_proper_nouns, generate_word_array
from .word_matching_judgev2 import make_bulk_op as make_judge_bulk_op

logger = logging.getLogger(__name__)


def extract_words_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    name_lexicon: Optional[dict] = None,
) -> bool:
    """Generate the word array for one note's sentence and write it into the word list field.
    False when the note has nothing to extract, or the generator could not run."""
    log_prefix = f"Extract words--nid:{note.id}--"
    note_type = note.note_type()
    if not note_type:
        logger.error(f"{log_prefix}Missing note type")
        return False
    try:
        sentence_field = get_field_config(config, "word_extraction_sentence_field", note_type)
        word_list_field = get_field_config(config, "word_list_field", note_type)
    except Exception as e:
        logger.error(f"{log_prefix}{e}")
        return False
    if sentence_field not in note or word_list_field not in note:
        logger.error(f"{log_prefix}The note is missing the sentence or the word list field")
        return False

    current = note[word_list_field].strip()
    if current:
        if decode_word_array(current) is not None:
            logger.debug(f"{log_prefix}The note already holds a word array")
        else:
            # Generating over it would drop the note ids of its matched words
            logger.info(
                f"{log_prefix}Left alone: the field holds an old word list, migrate it instead"
            )
        return False

    # The sentence without its <i> context, so that the array covers the words the note is
    # about and offers no others to match_words_to_notes
    sentence = strip_context_sentences(note[sentence_field])
    if not sentence:
        logger.debug(f"{log_prefix}No sentence to generate a word array from")
        return False

    try:
        arr = generate_word_array(sentence, name_lexicon)
    except Exception as e:
        logger.error(f"{log_prefix}Could not generate a word array: {e}")
        print_error_traceback(e, logger)
        return False

    add_proper_nouns(config, arr, log_prefix)

    note[word_list_field] = json.dumps(arr, ensure_ascii=False)
    if note.id > 0 and note.id not in notes_to_update_dict:
        notes_to_update_dict[note.id] = note
    return True


def extract_words_op(name_lexicon: Optional[dict] = None):
    """The per-note op with the name lexicon bound, loaded once for a whole run."""
    if name_lexicon is None:
        name_lexicon = names.load_lexicon(resources.NAME_LEXICON)
    return partial(extract_words_in_note, name_lexicon=name_lexicon)


async def bulk_extract_from_notes_op(
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
):
    config = mw.addonManager.getConfig(__name__)
    if not config:
        showWarning("Missing addon configuration")
        return
    return await bulk_notes_op(
        "Extracting words",
        config,
        extract_words_op(),
        col,
        notes,
        edited_nids,
        progress_updater,
        notes_to_add_dict,
        notes_to_update_dict,
    )


def extract_words_from_selected_notes(nids: Sequence[NoteId], parent: Browser):
    """Needs the generator's resources, asked about before any note is touched."""

    def run():
        progress_updater = AsyncTaskProgressUpdater(title="Async AI op: Extracting words")
        done_text = "Extracted words"
        selected_notes_op(done_text, bulk_extract_from_notes_op, nids, parent, progress_updater)

    with_generator_resources(parent, run)


def extract_words_and_judge_from_selected_notes(nids: Sequence[NoteId], parent: Browser):
    """Extract words, then judge the new words' matchability, as one operation. The judge has
    to be a phase of its own: its requests cannot be planned until the words exist."""

    def run():
        progress_updater = AsyncTaskProgressUpdater(
            title="Async AI op: Extracting words + judging matchability"
        )
        done_text = "Extracted and judged words"
        selected_notes_op(
            done_text,
            [
                OpPhase("Extracting words", bulk_extract_from_notes_op),
                OpPhase("Judging words matchability", make_judge_bulk_op(JUDGE_NEW)),
            ],
            nids,
            parent,
            progress_updater,
        )

    with_generator_resources(parent, run)
