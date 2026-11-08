import logging
from collections.abc import Container, Sequence
from typing import Callable, Optional

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning

from ..configuration import GeneratedMeaningsDictType
from ..generator_resources import with_generator_resources
from ..note_roles import ROLES, SENTENCE_ROLE, VOCAB_ROLE, role_error, roles_of
from ..word_array.match_flags import JUDGE_NEW
from .chain_types import ChainStep
from .base_ops import AsyncTaskProgressUpdater, OpPhase, bulk_notes_op, selected_notes_op
from .clean_meaning import clean_meaning_in_note
from .extract_words import extract_words_op
from .kanjify_sentence import kanjify_sentence_in_note
from .make_all_meanings import (
    load_meanings_dict_from_file,
    make_meanings_in_note,
    write_meanings_dict_to_file,
)
from .role_gate import note_type_name, notes_of_role, notes_passing
from .word_matching_judge import make_bulk_op as make_judge_bulk_op

logger = logging.getLogger(__name__)


def new_note_all_ops_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    processed_words_set: set[str],
    all_generated_meanings_dict: GeneratedMeaningsDictType,
    extract_words: Callable[..., bool],
    roles: Container[str] = ROLES,
) -> bool:
    """The steps of `roles` (note_roles.roles_of the note's type): meanings and their
    cleaning for a vocab note, kanjify and extract for a sentence note. Outside the two-type
    layout a type has both roles, and its notes get all four, as before roles existed."""
    changed = False

    if VOCAB_ROLE in roles:
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
        ).changed

    if SENTENCE_ROLE in roles:
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
    # The new notes of both types of the two-type layout are one selection, so each note gets
    # its role's steps rather than every note one role's. Read once per type, on this thread
    roles_by_type: dict[str, tuple[str, ...]] = {}

    def type_roles(name: str) -> tuple[str, ...]:
        if name not in roles_by_type:
            roles_by_type[name] = roles_of(config, name)
        return roles_by_type[name]

    # A type with no role is half of a broken two-type layout: its notes are left out, and
    # its layout error reported once
    notes = notes_passing(
        notes,
        lambda name: role_error(config, name, SENTENCE_ROLE) if not type_roles(name) else None,
    )
    roles_by_note = {note.id: type_roles(note_type_name(note)) for note in notes}

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
            roles_by_note.get(note.id, ROLES),
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


def judge_sentence_notes_op():
    """The judge over the sentence notes of the run's notes only. Unlike the judge's own run, it
    leaves the vocab notes out without a word: in this op's selection they are expected, and
    the notes of no role were reported by the first phase."""
    judge = make_judge_bulk_op(JUDGE_NEW)

    async def bulk_judge_sentence_notes_op(col: Collection, notes: Sequence[Note], **kwargs):
        config = mw.addonManager.getConfig(__name__) or {}
        sentence_notes = notes_of_role(config, notes, SENTENCE_ROLE, report=False)
        return await judge(col, notes=sentence_notes, **kwargs)

    return bulk_judge_sentence_notes_op


def new_note_all_ops_selected_notes(
    nids: Sequence[NoteId], parent: Browser, chain: Optional[ChainStep] = None
):
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
                OpPhase("Judging words matchability", judge_sentence_notes_op()),
            ],
            nids,
            parent,
            progress_updater,
            chain=chain,
        )

    with_generator_resources(parent, run, chain=chain)
