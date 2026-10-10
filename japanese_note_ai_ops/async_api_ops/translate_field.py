import logging
from typing import Optional, Union
from anki.notes import Note, NoteId
from anki.collection import Collection
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning
from collections.abc import Sequence

from .chain_types import ChainStep
from .base_ops import (
    get_response,
    bulk_notes_op,
    selected_notes_op,
    AsyncTaskProgressUpdater,
)
from .collection_access import (
    find_notes as col_find_notes,
    get_notes as col_get_notes,
)
from .role_gate import notes_of_role
from ..note_roles import (
    SENTENCE_ROLE,
    example_field_pairs,
    example_id_field,
    note_type_search,
    vocab_type_of,
)
from ..utils import get_field_config

logger = logging.getLogger(__name__)

TRANSLATED_SENTENCE_RETURN_FIELD = "english_sentence"


def translate_sentence_prompt(sentence: str) -> str:
    # HTML-keeping prompt
    return (
        f"sentence_to_translate_into_english: {sentence}\n\nIgnore any HTML in the"
        " sentence.\nReturn an HTML-free English translation of the sentence in a JSON string as"
        f' the value of the key "{TRANSLATED_SENTENCE_RETURN_FIELD}".'
    )


def get_translated_field_from_model(config: dict[str, str], sentence: str) -> Union[str, None]:
    return_field = TRANSLATED_SENTENCE_RETURN_FIELD
    inputs = {"sentence": sentence}
    no_html_prompt = translate_sentence_prompt(**inputs)
    model = config.get("translate_sentence_model", "")
    result = get_response(model, no_html_prompt, kind="translate.sentence", inputs=inputs)
    if result is None:
        # If translation failed, return nothing
        return None
    try:
        return result[return_field]
    except KeyError:
        return None


def copy_translation_to_vocab_notes(
    config: dict,
    sentence_note: Note,
    vocab_type: str,
    translated_sentence_field: str,
    notes_to_update_dict: dict[NoteId, Note],
) -> None:
    """Give the vocab notes whose example `sentence_note` is (two-type layout) its new
    translation, in the vocab type's translation field: their copy of it (note_roles,
    `copy_example`) would otherwise keep the old one until "Refresh example sentences".

    Reads through collection_access, as on the run's worker threads; registers each vocab note
    it changes, and the run's own version of one already registered.
    """
    id_field = example_id_field(config, vocab_type)
    vocab_field = dict(example_field_pairs(config, vocab_type)).get(translated_sentence_field)
    if not id_field or not vocab_field or sentence_note.id <= 0:
        return
    translation = sentence_note[translated_sentence_field]
    nids = col_find_notes(f'{note_type_search(vocab_type)} "{id_field}:{sentence_note.id}"')
    registered = [notes_to_update_dict[nid] for nid in nids if nid in notes_to_update_dict]
    fetched = col_get_notes([nid for nid in nids if nid not in notes_to_update_dict])
    for vocab_note in [*registered, *fetched]:
        if vocab_field in vocab_note and vocab_note[vocab_field] != translation:
            vocab_note[vocab_field] = translation
            notes_to_update_dict[vocab_note.id] = vocab_note


def translate_sentence_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    *,
    copy_to_vocab_notes: bool = True,
) -> bool:
    """Translate the note's sentence into its translation field. In the two-type layout a
    sentence note's vocab notes get the translation too, unless `copy_to_vocab_notes` is off:
    the editor's hook translates only the note it shows, which the editor saves."""
    note_type = note.note_type()
    if not note_type:
        logger.error(f"note_type() call failed for note {note.id}")
        return False
    try:
        sentence_field = get_field_config(config, "sentence_field", note_type)
        translated_sentence_field = get_field_config(config, "translated_sentence_field", note_type)
        # Read before the request, so a broken layout costs none: the type itself outside the
        # two-type layout, where there is nothing to copy to
        vocab_type = vocab_type_of(config, note_type["name"]) if copy_to_vocab_notes else None
    except Exception as e:
        logger.error(str(e))
        return False

    logger.debug(f"sentence_field in note: {sentence_field in note}")
    logger.debug(f"translated_sentence_field in note: {translated_sentence_field in note}")
    # Check if the note has the required fields
    if sentence_field in note and translated_sentence_field in note:
        logger.debug("note has fields")
        # Get the values from fields
        sentence = note[sentence_field]
        logger.debug(f"sentence: {sentence}")
        # Check if the value is non-empty
        if sentence:
            # Call API to get translation
            translated_sentence = get_translated_field_from_model(config, sentence)
            logger.debug(f"translated_sentence: {translated_sentence}")
            if translated_sentence is not None:
                # Update the note with the new value
                note[translated_sentence_field] = translated_sentence
                if note.id != 0 and note.id not in notes_to_update_dict:
                    notes_to_update_dict[note.id] = note
                if vocab_type is not None and vocab_type != note_type["name"]:
                    copy_translation_to_vocab_notes(
                        config, note, vocab_type, translated_sentence_field, notes_to_update_dict
                    )
                return True
            return False
        return False
    else:
        logger.error("note is missing fields")
    return False


def bulk_translate_notes_op(
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
    message = "Translating sentences"
    op = translate_sentence_in_note
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
    )


def translate_selected_notes(
    nids: Sequence[NoteId], parent: Browser, chain: Optional[ChainStep] = None
):
    progress_updater = AsyncTaskProgressUpdater(title="Async AI op: Translating sentences")
    done_text = "Updated translation"
    bulk_op = bulk_translate_notes_op
    return selected_notes_op(done_text, bulk_op, nids, parent, progress_updater, chain=chain)
