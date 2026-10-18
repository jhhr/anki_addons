"""The op "Refresh example sentences": the selected vocab notes of the two-type layout copy their
example sentence note again (note_roles.copy_example: the fields the vocab type keeps a copy of,
usually the translation and the audio, and the example's link ID, link_ids.py).

A vocab note's copy goes stale when its example sentence note is edited or retranslated, and its
link breaks when that note is deleted. For a note whose example is gone (or never was, or names
a note of another type) the op takes the oldest sentence note (lowest id) whose word array links
it; with none, it clears the id field and tags the note `example-sentence-missing`, which a
later run takes off again once it finds one.

Only the two-type layout has example sentence notes: in the one-type layout a vocab note holds
its own sentence, and the op refuses to start.
"""

from __future__ import annotations

import functools
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
from ..async_api_ops.link_lookup import link_index
from ..async_api_ops.role_gate import notes_passing
from ..link_ids import LinkIndex, field_search, link_id, parse_link_id
from ..note_roles import (
    VOCAB_ROLE,
    copy_example,
    example_id_field,
    layout_error,
    link_id_field,
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


def _link_id_of(config: Mapping[str, Any], note: Note, note_type_name: str) -> int:
    """The link ID (link_ids.py) of a note of the collection, which links to it hold: its type's
    link ID field's ID, else its note id."""
    field = link_id_field(config, note_type_name)
    return link_id(note[field] if field and field in note else "", note.id)


def _example_note(id_text: str, sentence_index: LinkIndex) -> Optional[Note]:
    """The sentence note an example id field names by its link ID, None when it names none of
    the sentence type: no link ID, or one no note of that type (`sentence_index`) holds. Read
    as a note id, a sentence note's generated ID would find no note, and the note id of one
    holding another ID could find a note of any type."""
    held = parse_link_id(id_text.strip())
    note_id = sentence_index.note_id(held) if held is not None else None
    if note_id is None:
        return None
    try:
        return mw.col.get_note(NoteId(note_id))
    except NotFoundError:
        return None


def _oldest_linking_note(config: dict, sentence_type: str, vocab_link_id: int) -> Optional[Note]:
    """The sentence note of lowest id whose word array links the vocab note of this link ID,
    sub-words included. The search's `*<id>*` also finds a longer ID holding it, so each found
    array is read."""
    word_list_field = get_field_config(config, "word_list_field", {"name": sentence_type})
    links = field_search(word_list_field, f"*{vocab_link_id}*")
    for nid in sorted(mw.col.find_notes(f"{note_type_search(sentence_type)} {links}")):
        note = mw.col.get_note(nid)
        arr = decode_word_array(note[word_list_field])
        if arr and any(matched_note_id(elem) == vocab_link_id for _, elem in iter_words(arr)):
            return note
    return None


def refresh_example_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    *,
    sentence_indexes: dict[str, LinkIndex],
) -> bool:
    """Copy the note's example sentence note into it again, finding another when it is gone;
    writes nothing to the collection. True when the note changed.

    Every link is by link ID (link_ids.py): the example id field names the sentence note by its
    link ID, and that is what is written for a replacement; the arrays are searched for the
    vocab note's. `sentence_indexes` holds the run's index of each sentence type, each read once
    (link_lookup.link_index) the first time a note needs it."""
    note_type = note.note_type()
    assert note_type is not None, "the bulk op lets through only notes of a refreshable type"
    vocab_type = note_type["name"]
    sentence_type = sentence_type_of(config, vocab_type)
    id_field = example_id_field(config, vocab_type)
    assert id_field is not None, "the bulk op lets through only notes of a refreshable type"
    sentence_index = sentence_indexes.get(sentence_type)
    if sentence_index is None:
        sentence_index = link_index(config, sentence_type, col=mw.col)
        sentence_indexes[sentence_type] = sentence_index
    before = (list(note.fields), list(note.tags))
    example = _example_note(note[id_field], sentence_index) or _oldest_linking_note(
        config, sentence_type, _link_id_of(config, note, vocab_type)
    )
    if example is None:
        logger.info("%s: note %s has no example sentence note", LABEL, note.id)
        note[id_field] = ""
        # add_tag appends a duplicate (Anki strips it on save), which would read as a change
        if not note.has_tag(MISSING_TAG):
            note.add_tag(MISSING_TAG)
    else:
        copy_example(
            config,
            vocab_type,
            sentence_note=example,
            sentence_id=_link_id_of(config, example, sentence_type),
            vocab_note=note,
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
    # One index per sentence type for the run: a search per note, through thousands of notes,
    # would read every note of the type each time
    sentence_indexes: dict[str, LinkIndex] = {}
    return await bulk_notes_op(
        MESSAGE,
        config,
        functools.partial(refresh_example_in_note, sentence_indexes=sentence_indexes),
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
