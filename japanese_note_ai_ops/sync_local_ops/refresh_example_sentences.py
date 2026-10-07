"""The op "Refresh example sentences": the selected vocab notes of the two-type layout copy their
example sentence note again (note_roles.copy_example: the fields the vocab type keeps a copy of,
usually the translation and the audio, and the example's id).

A vocab note's copy goes stale when its example sentence note is edited or retranslated, and its
link breaks when that note is deleted. For a note whose example is gone (or never was, or names
a note of another type) the op takes the oldest sentence note (lowest id) whose word array links
it; with none, it clears the id field and tags the note `example-sentence-missing`, which a
later run takes off again once it finds one.

Only the two-type layout has example sentence notes: in the one-type layout a vocab note holds
its own sentence, and the op refuses to start.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from anki.collection import Collection
from anki.errors import NotFoundError
from anki.notes import Note, NoteId
from anki.utils import ids2str
from aqt import mw
from aqt.utils import showWarning

from ..async_api_ops.base_ops import (
    AsyncTaskProgressUpdater,
    BulkOpResult,
    NotesRunSpec,
    bulk_notes_op,
    selected_notes_op,
)
from ..async_api_ops.chain_types import ChainStep, fail_step
from ..async_api_ops.role_gate import notes_passing
from ..note_roles import (
    VOCAB_ROLE,
    copy_example,
    example_id_field,
    layout_error,
    note_type_search,
    role_error,
    sentence_type_of,
)
from ..utils import get_field_config
from ..word_array.match_flags import decode_word_array, iter_words, matched_note_id

logger = logging.getLogger(__name__)

LABEL = "Refresh example sentences"
# The bulk op's message, which also names the run's undo entry
MESSAGE = "Refreshing example sentences"
MISSING_TAG = "example-sentence-missing"


def refresh_error(config: Mapping[str, Any], note_type_name: str) -> Optional[str]:
    """Why the op cannot refresh this type's notes, naming the type; None when it can: the
    vocab type of a two-type layout whose block names `example_sentence_id_field`."""
    error = layout_error(config, note_type_name) or role_error(config, note_type_name, VOCAB_ROLE)
    if error:
        return error
    if sentence_type_of(config, note_type_name) == note_type_name:
        return (
            f'{LABEL} needs the two-type layout, and note type "{note_type_name}" names no'
            " sentence_note_type in the settings: its notes hold their own sentences."
        )
    if example_id_field(config, note_type_name) is None:
        return (
            f'Note type "{note_type_name}" has no example_sentence_id_field in the settings,'
            " so nothing says which sentence note is its notes' example."
        )
    return None


def _selected_types(col: Collection, nids: Sequence[NoteId]) -> list[str]:
    db = col.db
    assert db is not None
    names = []
    for mid in db.list(f"select distinct mid from notes where id in {ids2str(nids)}"):
        notetype = col.models.get(mid)
        names.append(notetype["name"] if notetype is not None else str(mid))
    return names


def refresh_preflight_error(
    col: Collection, config: Mapping[str, Any], nids: Sequence[NoteId]
) -> Optional[str]:
    """Why the run over these notes would do nothing: none of their types can be refreshed (the
    first type's reason). A run over some types that can leaves the others out, reported."""
    errors = [refresh_error(config, name) for name in _selected_types(col, nids)]
    if not errors or None in errors:
        return None
    return errors[0]


def _example_note(id_text: str, sentence_type: str) -> Optional[Note]:
    """The sentence note an example id field names, None when it names none of that type."""
    id_text = id_text.strip()
    if not id_text.isdigit():
        return None
    try:
        note = mw.col.get_note(NoteId(int(id_text)))
    except NotFoundError:
        return None
    note_type = note.note_type()
    return note if note_type is not None and note_type["name"] == sentence_type else None


def _oldest_linking_note(config: dict, sentence_type: str, vocab_id: int) -> Optional[Note]:
    """The sentence note of lowest id whose word array links `vocab_id`, sub-words included.
    The search's `*<id>*` also finds a longer id holding it, so each found array is read."""
    word_list_field = get_field_config(config, "word_list_field", {"name": sentence_type})
    query = f'{note_type_search(sentence_type)} "{word_list_field}:*{vocab_id}*"'
    for nid in sorted(mw.col.find_notes(query)):
        note = mw.col.get_note(nid)
        arr = decode_word_array(note[word_list_field])
        if arr and any(matched_note_id(elem) == vocab_id for _, elem in iter_words(arr)):
            return note
    return None


def refresh_example_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
) -> bool:
    """Copy the note's example sentence note into it again, finding another when it is gone;
    writes nothing to the collection. True when the note changed."""
    note_type = note.note_type()
    assert note_type is not None, "the bulk op lets through only notes of a refreshable type"
    vocab_type = note_type["name"]
    sentence_type = sentence_type_of(config, vocab_type)
    id_field = example_id_field(config, vocab_type)
    assert id_field is not None, "the bulk op lets through only notes of a refreshable type"
    before = (list(note.fields), list(note.tags))
    example = _example_note(note[id_field], sentence_type) or _oldest_linking_note(
        config, sentence_type, note.id
    )
    if example is None:
        logger.info("%s: note %s has no example sentence note", LABEL, note.id)
        note[id_field] = ""
        # add_tag appends a duplicate (Anki strips it on save), which would read as a change
        if not note.has_tag(MISSING_TAG):
            note.add_tag(MISSING_TAG)
    else:
        copy_example(
            config, vocab_type, sentence_note=example, sentence_id=example.id, vocab_note=note
        )
        note.remove_tag(MISSING_TAG)
    changed = (list(note.fields), list(note.tags)) != before
    if changed and note.id > 0:
        notes_to_update_dict[note.id] = note
    return changed


async def bulk_refresh_examples_op(
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
) -> BulkOpResult:
    config = mw.addonManager.getConfig(__name__) or {}
    return await bulk_notes_op(
        MESSAGE,
        config,
        refresh_example_in_note,
        col,
        # The notes of a type it cannot refresh (a sentence type, the one-type layout's) are
        # left out, reported once per type
        notes_passing(notes, lambda name: refresh_error(config, name)),
        edited_nids,
        progress_updater,
        notes_to_add_dict,
        notes_to_update_dict,
        is_sync_op=True,
    )


def refresh_spec() -> NotesRunSpec:
    """The op's run, as the menu starts it and as a script does (`NotesRunSpec`)."""
    return NotesRunSpec(
        done_text="Refreshed example sentences",
        title=f"Sync op: {MESSAGE}",
        bulk_op=bulk_refresh_examples_op,
    )


def refresh_example_sentences_from_selected(
    nids: Sequence[NoteId], parent: Any, chain: Optional[ChainStep] = None
):
    """Refresh the selected vocab notes' example sentences. Refused here, before any progress
    dialog, when no selected note is of a type it can refresh: the one-type layout's."""
    config = mw.addonManager.getConfig(__name__) or {}
    error = refresh_preflight_error(mw.col, config, nids)
    if error:
        logger.error("%s: %s", LABEL, error)
        showWarning(error, parent=parent, title=LABEL)
        fail_step(chain, error)
        return None
    spec = refresh_spec()
    return selected_notes_op(
        spec.done_text,
        spec.bulk_op,
        nids,
        parent,
        AsyncTaskProgressUpdater(title=spec.title),
        chain=chain,
    )
