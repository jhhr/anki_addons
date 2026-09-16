import logging
from collections.abc import Sequence
from typing import Callable

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning

from ..configuration import GeneratedMeaningsDictType
from ..sync_local_ops.migrate_word_arrays import with_generator_resources
from ..word_array.match_flags import JUDGE_NEW
from .base_ops import AsyncTaskProgressUpdater, OpPhase, bulk_notes_op, selected_notes_op
from .clean_meaning import clean_meaning_in_note
from .extract_words import extract_words_op
from .kanjify_sentence import kanjify_sentence_in_note
from .make_all_meanings import (
    load_meanings_dict_from_file,
    make_meanings_in_note,
    write_meanings_dict_to_file,
)
from .word_matching_judgev2 import make_bulk_op as make_judge_bulk_op

logger = logging.getLogger(__name__)


def new_note_all_ops_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    processed_words_set: set[str],
    all_generated_meanings_dict: GeneratedMeaningsDictType,
    extract_words: Callable[..., bool],
) -> bool:
    changed = False

    changed |= make_meanings_in_note(
        config,
        note,
        processed_words_set,
        all_generated_meanings_dict,
        notes_to_add_dict,
        notes_to_update_dict,
    )

    changed |= clean_meaning_in_note(
        config,
        note,
        notes_to_add_dict,
        notes_to_update_dict,
        all_generated_meanings_dict,
        allow_reupdate_existing=True,
    )

    changed |= kanjify_sentence_in_note(
        config,
        note,
        notes_to_add_dict,
        notes_to_update_dict,
    )

    changed |= extract_words(
        config,
        note,
        notes_to_add_dict,
        notes_to_update_dict,
    )

    return changed


async def bulk_new_note_all_ops(
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

    message = "Running new note all ops"
    processed_words_set: set[str] = set()
    all_generated_meanings_dict = load_meanings_dict_from_file()
    extract_words = extract_words_op()

    def op(
        config: dict,
        note: Note,
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
    ) -> bool:
        nonlocal processed_words_set, all_generated_meanings_dict
        return new_note_all_ops_in_note(
            config,
            note,
            notes_to_add_dict,
            notes_to_update_dict,
            processed_words_set,
            all_generated_meanings_dict,
            extract_words,
        )

    def on_end():
        nonlocal all_generated_meanings_dict
        write_meanings_dict_to_file(all_generated_meanings_dict)

    return await bulk_notes_op(
        message,
        config,
        op,
        col,
        notes,
        edited_nids,
        progress_updater,
        notes_to_add_dict=notes_to_add_dict,
        notes_to_update_dict=notes_to_update_dict,
        on_end=on_end,
    )


def new_note_all_ops_selected_notes(nids: Sequence[NoteId], parent: Browser):
    """Every op a new note needs, then the judge over the words the word array just got. The
    judge is a phase of its own because its requests cannot be planned before the words exist.
    Needs the generator's resources, asked about before any note is touched."""

    def run():
        progress_updater = AsyncTaskProgressUpdater(title="Async AI op: New note all ops")
        done_text = "Ran new note all ops"
        selected_notes_op(
            done_text,
            [
                OpPhase("New note ops", bulk_new_note_all_ops),
                OpPhase("Judging words matchability", make_judge_bulk_op(JUDGE_NEW)),
            ],
            nids,
            parent,
            progress_updater,
        )

    with_generator_resources(parent, run)
