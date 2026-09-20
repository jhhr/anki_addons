import functools
import logging
from typing import Optional, Union, Tuple
from anki.hooks import (
    wrap,
    note_will_be_added,
    # note_will_flush,
)
from anki.cards import Card, CardId
from anki.scheduler.v3 import CardAnswer, Scheduler as V3Scheduler
from anki.notes import Note, NoteId
from aqt.editor import Editor, EditorMode
from aqt import mw
from aqt.gui_hooks import (
    reviewer_did_answer_card,
    editor_did_unfocus_field,
    editor_did_load_note,
)

from ..shared.anki.write_custom_data import write_custom_data
from ..logging_setup import operation_logging
from ..utils.merge_cards import merge_cards
from ..configuration import (
    Config,
    CopyDefinition,
    get_triggered_field_to_field_defs_for_field,
    definition_is_add_note_compatible,
    definition_modifies_other_notes,
    definition_note_type_names,
    definition_runs_on_add,
    definition_runs_on_review,
    definition_runs_on_sync,
    definition_unfocus_fields,
)
from ..logic.definition_schema import is_format_2
from ..logic.copy_fields import (
    copy_for_single_trigger_note,
    copy_fields,
    make_copy_fields_undo_text,
)

logger = logging.getLogger(__name__)


def get_copy_definitions_for_add_note(note: Note) -> list[CopyDefinition]:
    """The definitions that run when `note` is added: `copy_on_add`, and this note's type.

    Note-type membership only; the caller still has to split the result on
    `definition_is_add_note_compatible`, because a definition that writes to other notes
    or cards has to be written by the hook itself, under its own undo entry.
    """
    config = Config()
    config.load()
    note_type = note.note_type()
    if not note_type:
        # Error situation, note_type should exist when adding note
        return []
    note_type_name = note_type["name"]

    copy_definitions: list[CopyDefinition] = []

    for copy_definition in config.copy_definitions:
        if not definition_runs_on_add(copy_definition):
            continue
        if note_type_name not in definition_note_type_names(copy_definition):
            continue

        copy_definitions.append(copy_definition)

    return copy_definitions


def _edited_cards_for_update(copied_into_cards_dict: dict[int, Card]) -> list[Card]:
    edited_cards = [
        card
        for card in copied_into_cards_dict.values()
        if hasattr(card, "edited") and card.edited
    ]
    for card in edited_cards:
        del card.edited
    return edited_cards


def run_copy_fields_on_add(note: Note, deck_id: int):
    """
    Copy fields when a note is about to be added. This applies to notes being added
    by AnkiConnect or the Add cards dialog. The note is not yet in the database: its id
    is 0, no search can find it, and it has no cards, so a card action on it has nothing
    to reach and is skipped by the stage (with a log line). Its field writes need no
    write here -- the add saves the mutated note object -- but writes to other notes and
    cards do, so those definitions run second, under an undo entry of their own.
    """
    config = Config()
    config.load()
    with operation_logging("note_added", config.log_level):
        # Copy definitions that affect other notes need an undo entry as we want to be able to undo
        editing_other_notes_definitions: list[CopyDefinition] = []

        for copy_definition in get_copy_definitions_for_add_note(note):
            # A definition that writes to any other note or card needs the hook to write and
            # undo those changes itself, so it runs below, under its own undo entry. The flag
            # is the analyser's answer for a format-2 definition and the mode inspection for
            # a format-1 one; either way the hook only reads it and never inspects stages (§8).
            if not definition_is_add_note_compatible(copy_definition):
                editing_other_notes_definitions.append(copy_definition)
                continue
            copy_for_single_trigger_note(
                copy_definition=copy_definition,
                trigger_note=note,
                deck_id=deck_id,
                # Backstop against hand-edited JSON claiming compatibility it does not have: a
                # queued mutation to anything but this note fails the definition (§8).
                add_note_compatible_only=True,
            )

        if not editing_other_notes_definitions:
            return

        # Run the definitions that affect other notes with an undo entry created
        # Thus the changes on other notes can be undone while the changes on the new note
        # will remain, as that seems more user-friendly.
        undo_entry: Optional[int] = None
        for copy_definition in editing_other_notes_definitions:
            copied_into_notes: list[Note] = []
            copied_into_cards_dict: dict[int, Card] = {}
            # Can't use copy_fields here as it'd lead to a
            # "bug: run_in_background not called from main thread" exception
            # TODO: non CollectionOp version of copy_fields
            copy_for_single_trigger_note(
                copy_definition=copy_definition,
                trigger_note=note,
                copied_into_notes=copied_into_notes,
                copied_into_cards_dict=copied_into_cards_dict,
                deck_id=deck_id,
            )
            # Only source to destinations definitions get here and their destinations come from a
            # query, which can't find the unsaved note. Still, an id 0 note would make
            # mw.col.update_notes fail, so keep it out regardless
            copied_into_notes = [note for note in copied_into_notes if note.id != 0]
            edited_cards = _edited_cards_for_update(copied_into_cards_dict)
            if not copied_into_notes and not edited_cards:
                # Nothing was written into other notes (the query matched nothing or the deck
                # whitelist rejected the note), and no card action changed a card either, so
                # there's nothing to undo and an empty entry would only clutter the undo stack.
                continue

            if undo_entry is None:
                undo_text = make_copy_fields_undo_text(
                    copy_definitions=editing_other_notes_definitions,
                    note_count=1,
                    suffix="triggered by adding note",
                )
                # Unfortunately, note_will_be_added is called *before* the note is actually added so
                # after this undo entry will come the "Add Note" undo entry. This is not ideal, but
                # it's the most reliable thing do while a note_was_added hook doesn't exist.
                #
                # Other altenatives would be to add a flag to new notes and run these copy
                # definitions on syncing but that seems less user-friendly.
                undo_entry = mw.col.add_custom_undo_entry(undo_text)
            # Write after every definition, as the next one fetches its destinations from the
            # database: writing once at the end would let a later definition's copy of a note,
            # fetched without an earlier one's edit, overwrite that edit
            if copied_into_notes:
                mw.col.update_notes(copied_into_notes)
            if edited_cards:
                mw.col.update_cards(edited_cards)
            # Merge after every write, or the entry's step falls behind and can't be found
            mw.col.merge_undo_entries(undo_entry)


# The card id and undo step of the latest answer, recorded by the wrapped
# Scheduler.answer_card for run_copy_fields_on_review to merge into
last_answer_undo_step: Optional[Tuple[CardId, int]] = None


def remember_answer_undo_step(answer_card):
    """
    Wrap Scheduler.answer_card to record the undo step the answer creates. By the time
    reviewer_did_answer_card fires, a listener registered before ours may have added undo
    entries of its own, so the newest step is no longer necessarily the Answer card one.
    """

    @functools.wraps(answer_card)
    def wrapper(self, answer: CardAnswer):
        changes = answer_card(self, answer)
        global last_answer_undo_step
        last_answer_undo_step = (CardId(answer.card_id), self.col.undo_status().last_step)
        return changes

    wrapper.copy_anywhere_wrapped = True
    return wrapper


def track_answer_undo_steps():
    # Scheduler.answer_card is a class attribute, so wrap it only once
    if not getattr(V3Scheduler.answer_card, "copy_anywhere_wrapped", False):
        V3Scheduler.answer_card = remember_answer_undo_step(V3Scheduler.answer_card)


def get_answer_card_undo_step(card: Card) -> int:
    """
    The undo step of the answer to `card`, consuming the recorded one so it can't go stale.
    Falls back to the newest step when no answer to this card was recorded.
    """
    global last_answer_undo_step
    recorded, last_answer_undo_step = last_answer_undo_step, None
    if recorded and recorded[0] == card.id:
        return recorded[1]
    return mw.col.undo_status().last_step


def run_copy_fields_on_review(card: Card):
    """
    Copy fields when a card is reviewed. Check whether the card's
    note type is in the list of copy_into_note_types for the copy_definition
    and run those.
    """
    answer_card_undo_entry = get_answer_card_undo_step(card)
    config = Config()
    config.load()
    with operation_logging("card_answered", config.log_level):
        note = card.note()
        note_type = note.note_type()
        if not note_type:
            # Error situation, note_type should exist when reviewing card
            return
        note_type_name = note_type["name"]

        copy_definitions_to_run: list[CopyDefinition] = []
        has_definitions_to_process_on_sync = False

        for copy_definition in config.copy_definitions:
            if not definition_runs_on_review(copy_definition):
                if definition_runs_on_sync(copy_definition):
                    has_definitions_to_process_on_sync = True
                continue
            stored_note_types = copy_definition.get("copy_into_note_types")
            if stored_note_types is not None and not isinstance(stored_note_types, str):
                # The answer is already committed, so raising would only throw the error at the
                # reviewer from inside Anki's hook dispatch and stop every later definition too
                logger.error(
                    "Copy definition '%s' has copy_into_note_types that is not a string: %r",
                    copy_definition.get("definition_name"),
                    stored_note_types,
                )
                continue
            if note_type_name not in definition_note_type_names(copy_definition):
                continue

            copy_definitions_to_run.append(copy_definition)

        if not copy_definitions_to_run:
            return

        copied_into_notes: list[Note] = []
        copied_into_cards_dict: dict[int, Card] = {}
        for copy_definition in copy_definitions_to_run:
            copy_for_single_trigger_note(
                copy_definition=copy_definition,
                trigger_note=note,
                copied_into_notes=copied_into_notes,
                copied_into_cards_dict=copied_into_cards_dict,
            )
            # After each copy_definition, update notes and cards so that subsequent copy_definitions
            # operate on the latest data
            # update_note adds a new undo entry Update note
            mw.col.update_notes(copied_into_notes)
            edited_cards = [
                card
                for card in copied_into_cards_dict.values()
                if hasattr(card, "edited") and card.edited
            ]
            for c in edited_cards:
                if c.id == card.id:
                    # Merge all changes to the reviewed card so that the final update_card call
                    # doesn't overwrite the changes done here
                    # Note, edited attribute is not merged as it's not a real card attribute, we don't
                    # need it after this, as edit the card regardless
                    merge_cards(card, c)
                del c.edited
            # update_card adds a new undo entry Update cards
            mw.col.update_cards(edited_cards)
            # merge all undo entries into the original Answer card undo entry. This also folds in
            # any entry another reviewer_did_answer_card listener added after it, as Anki merges
            # every step newer than the target
            mw.col.merge_undo_entries(answer_card_undo_entry)
        # In order to not have on_sync definitions run twice, we'll set a different fc value
        fc_value = -1 if has_definitions_to_process_on_sync else 1
        try:
            write_custom_data(card, key="fc", value=fc_value)
        except ValueError as e:
            # The copies are already written and merged, so raising here would only throw the
            # error at the reviewer from inside Anki's hook dispatch. Without the flag the note
            # stays queued for the sync sweep, which is the safe side to fail on.
            logger.error("Could not set the fc flag on card %s: %s", card.id, e)
        # Still write the card, as merge_cards may have put copied changes on it
        mw.col.update_card(card)
        # All updates are now merged into the Answer card undo entry
        mw.col.merge_undo_entries(answer_card_undo_entry)


editor_for_note_id: dict[EditorMode, Union[Tuple[Editor, Optional[NoteId]], None]] = {
    EditorMode.ADD_CARDS: None,
    EditorMode.BROWSER: None,
    EditorMode.EDIT_CURRENT: None,
}


def on_editor_did_load_note(editor: Editor):
    """
    Store the editor and note_id in a global dict for later use in other hooks.
    This is a hack to get around the fact that the editor is not passed to the
    unfocus_field hook.
    """
    # None rather than NoteId(0) for no note, as 0 is what a new note's id is and the
    # editor would then match whatever note is being typed in the Add cards dialog
    editor_for_note_id[editor.editorMode] = editor, editor.note.id if editor.note else None


def on_editor_will_cleanup(editor: Editor):
    """
    Forget the editor when its window closes. Otherwise the dict would keep it alive and
    run_copy_fields_on_unfocus_field would call loadNote() on it after its webview is gone,
    whenever its last note is edited in another editor.
    """
    for editor_mode, maybe_editor_tuple in editor_for_note_id.items():
        # By identity, as the slot may already hold a newer editor of the same mode
        if maybe_editor_tuple and maybe_editor_tuple[0] is editor:
            editor_for_note_id[editor_mode] = None


def get_add_cards_deck_id() -> Optional[int]:
    """
    The deck the Add cards dialog is currently set to add into, or None if there's no Add
    cards editor. A new note has no cards yet, so this is the only way to check it against
    the deck whitelist, as run_copy_fields_on_add does with the deck_id it's given.
    """
    maybe_editor_tuple = editor_for_note_id[EditorMode.ADD_CARDS]
    if not maybe_editor_tuple:
        return None
    editor, _ = maybe_editor_tuple
    # The AddCards window is the editor's parentWindow and owns the deck chooser
    deck_chooser = getattr(getattr(editor, "parentWindow", None), "deck_chooser", None)
    if deck_chooser is None:
        return None
    return deck_chooser.selected_deck_id


def run_copy_fields_on_unfocus_field(changed: bool, note: Note, field_idx: int) -> bool:
    is_new_note = note.id == 0
    # Existing notes are checked against the whitelist by their cards' decks instead
    deck_id = get_add_cards_deck_id() if is_new_note else None

    editors_matching_note_id = [
        # There can be three editors open at the same time:
        # ADD_CARDS = the new note adder
        # BROWSER = the note/card browser
        # EDIT CURRENT = the note editor in the reviewer
        #
        # The note_id in the ADD_CARDS editor is always 0, and there can only be one of them.
        # So, if the current note.id == 0, the only editor in the list will be the ADD_CARDS editor.
        #
        # However, the BROWSER and EDIT_CURRENT editors could potentially be both open to the
        # same note. Only one of them has actually triggered the unfocus_field event, but
        # we can't know which one. As a workaround, we'll just trigger the loadNote() on both
        # in such a case
    ]
    for maybe_editor_tuple in editor_for_note_id.values():
        if not maybe_editor_tuple:
            continue
        editor, note_id = maybe_editor_tuple
        if note.id == note_id and editor:
            editors_matching_note_id.append(editor)

    config = Config()
    config.load()
    with operation_logging("field_unfocused", config.log_level):
        note_type = note.note_type()
        if not note_type:
            # Error situation, note_type should exist when unfocusing field
            return changed
        note_type_name = note_type["name"]
        field_name = note.keys()[field_idx]
        # Make a copy because values() returns a reference
        initial_field_values = note.values().copy()
        # Definitions can tag the note too, and the tag bar needs the same reload to show it
        initial_tags = note.tags.copy()

        # Copy definitions that affect other notes need an undo entry as we want to be able to undo
        editing_other_notes_definitions: list[CopyDefinition] = []

        for copy_definition in config.copy_definitions:
            if note_type_name not in definition_note_type_names(copy_definition):
                continue

            modifies_other_notes = definition_modifies_other_notes(copy_definition)

            if is_new_note and not definition_is_add_note_compatible(copy_definition):
                # Do not run ops that edit other notes or cards while editing a new note: the
                # add hook runs them, if `on_add` is on. Same flag it checks (§8).
                continue

            if is_format_2(copy_definition):
                # A staged definition watches fields for the definition as a whole and runs all
                # of it, because which stages a field feeds is not generally decidable (§8).
                if field_name not in definition_unfocus_fields(copy_definition, is_new_note):
                    continue
                if modifies_other_notes:
                    editing_other_notes_definitions.append(copy_definition)
                else:
                    copied_into_cards_dict: dict[int, Card] = {}
                    copy_for_single_trigger_note(
                        copy_definition=copy_definition,
                        trigger_note=note,
                        copied_into_notes=[],
                        copied_into_cards_dict=copied_into_cards_dict,
                        # A migrated write still says which editor fields trigger it and
                        # whether it runs on this kind of unfocus at all; passing the field and
                        # the mode is what lets the stage honour that. A natively authored
                        # write says neither and is not narrowed by either.
                        field_only=field_name,
                        unfocus_is_add=is_new_note,
                        deck_id=deck_id,
                        # The Add dialog is the one place the add can still be cancelled, so
                        # a queued change to anything but the note being typed -- another
                        # note, a card, a file -- fails the definition rather than outliving
                        # an Escape. The gate above reads what the definition claims; this
                        # is what a hand-edited claim runs into (§8).
                        add_note_compatible_only=is_new_note,
                    )
                    edited_cards = _edited_cards_for_update(copied_into_cards_dict)
                    if edited_cards:
                        mw.col.update_cards(edited_cards)
                continue

            # Check field-to-field defs for a match on this field
            field_to_field_defs = copy_definition.get("field_to_field_defs")
            if not field_to_field_defs:
                continue

            # Each def this field triggers is gated by its own add/edit flag, so that one def
            # with the flag off neither runs nor stops the others on the same field from running
            unfocus_flag = "copy_on_unfocus_when_add" if is_new_note else "copy_on_unfocus_when_edit"
            gated_field_to_field_defs = [
                field_def
                for field_def in get_triggered_field_to_field_defs_for_field(
                    field_to_field_defs, field_name, modifies_other_notes
                )
                if field_def.get(unfocus_flag)
            ]
            if not gated_field_to_field_defs:
                continue

            # field_only alone would pick every def this field triggers again, flags or not, so
            # the definition is run with only the defs that passed the gate
            gated_definition = copy_definition.copy()
            gated_definition["field_to_field_defs"] = gated_field_to_field_defs

            if modifies_other_notes:
                # Run these separate with an undo entry
                editing_other_notes_definitions.append(gated_definition)
            else:
                copied_into_cards_dict: dict[int, Card] = {}
                # Either within note or destination to sources, we can run these right away
                # without an undo entry needed
                copy_for_single_trigger_note(
                    copy_definition=gated_definition,
                    trigger_note=note,
                    copied_into_notes=[],
                    copied_into_cards_dict=copied_into_cards_dict,
                    field_only=field_name,
                    # The defs above are already gated by this flag, and the executor checks the
                    # migrated copy of it as well; telling it which flag to look at is what
                    # keeps the two answers the same.
                    unfocus_is_add=is_new_note,
                    deck_id=deck_id,
                    # Same backstop as the format-2 branch above: on a new note, nothing but
                    # that note may be committed.
                    add_note_compatible_only=is_new_note,
                )
                edited_cards = _edited_cards_for_update(copied_into_cards_dict)
                if edited_cards:
                    mw.col.update_cards(edited_cards)

        if editing_other_notes_definitions:
            # Use the CollectionOp version so we get the full report and progress dialog
            copy_fields(
                copy_definitions=editing_other_notes_definitions,
                note_ids=[note.id],
                # The editor's save runs in the background with no ordering against this, so the
                # database may not have the value just typed yet. This note always does.
                trigger_notes=[note],
                field_only=field_name,
                unfocus_is_add=is_new_note,
                undo_text_suffix=f"triggered by unfocus field '{field_name}'",
            )

        # Copy definitions may not just edit this field but any field or the tags in the current
        # note. Check if any of them changed and reload then
        current_field_values = note.values()
        we_changed = initial_field_values != current_field_values or initial_tags != note.tags
        if we_changed:
            for editor in editors_matching_note_id:
                # Keep the caret in the field the user is on, as aqt's own reload for a True does
                editor.loadNoteKeepingFocus()
        # This is a filter hook: keep an earlier handler's True, or aqt won't reload for it
        return changed or we_changed


def init_note_hooks():
    editor_did_load_note.append(on_editor_did_load_note)
    # There's no gui hook for an editor closing, but the browser, the add cards dialog and the
    # reviewer's edit window all call Editor.cleanup() when they close. Wrap it only once, so
    # that calling this again doesn't stack wrappers.
    if not getattr(Editor.cleanup, "copy_anywhere_wrapped", False):
        Editor.cleanup = wrap(Editor.cleanup, on_editor_will_cleanup, "before")
        Editor.cleanup.copy_anywhere_wrapped = True
    track_answer_undo_steps()
    note_will_be_added.append(lambda _col, note, deck_id: run_copy_fields_on_add(note, deck_id))
    reviewer_did_answer_card.append(lambda reviewer, card, ease: run_copy_fields_on_review(card))
    editor_did_unfocus_field.append(run_copy_fields_on_unfocus_field)
