import html
import re
import time
from typing import Any, Callable, Optional, Sequence, Union

from anki.cards import Card
from anki.collection import OpChanges
from anki.notes import Note, NoteId
from anki.utils import ids2str
from aqt import mw
from aqt.operations import CollectionOp
from aqt.qt import (
    QDialog,
    QGuiApplication,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import tooltip

from ..configuration import (
    Config,
    CopyDefinition,
)
from ..shared.anki.write_custom_data import write_custom_data
from ..shared.ui.auto_resizing_text_edit import AutoResizingTextEdit
from ..shared.utils.logger import Logger
from .copy_primitives import (
    CopyFailedException,
    ProgressUpdateDef,
    ProgressUpdater,
    apply_card_action_to_card,
    apply_card_actions_by_template,
    apply_process_chain,
    card_actions_by_template_name,
    get_field_values_from_notes,
    get_variable_values_for_note,
    int_sort_by_field_value,
    sort_by_field_value,
)
from .definition_migration import MigrationError
from .definition_schema import is_format_2
from .execution.context import ExecutionSession
from .execution.runner import as_format_2, run_definition_for_trigger_note
from .legacy_executor import copy_into_single_note, get_across_target_notes

# Re-exported so the characterization suite and any caller that has always imported these
# from here keeps working while `legacy_executor` is on its way out.
__all__ = [
    "CacheResults",
    "CopyFailedException",
    "ProgressUpdateDef",
    "ProgressUpdater",
    "ScrollMessageBox",
    "apply_card_action_to_card",
    "apply_card_actions_by_template",
    "apply_process_chain",
    "card_actions_by_template_name",
    "copy_fields",
    "copy_fields_in_background",
    "copy_for_single_trigger_note",
    "copy_into_single_note",
    "get_across_target_notes",
    "get_field_values_from_notes",
    "get_variable_values_for_note",
    "int_sort_by_field_value",
    "make_copy_fields_undo_text",
    "note_passes_deck_whitelist",
    "sort_by_field_value",
]

CONSOLE_COLOR_RE = r"\x1b\[[0-9;]*m"


class ScrollMessageBox(QDialog):
    """
    A simple class to show a scrollable message box to display debug messages

    :param message_list: A list of messages to display
    :param title: The title of the message box
    :param parent: The parent widget
    """

    def __init__(self, message_list, title, parent=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.setWindowTitle(title)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        self.content = QWidget()
        scroll.setWidget(self.content)
        lay = QVBoxLayout(self.content)
        textbox = AutoResizingTextEdit(self, readOnly=True)
        # remove console colors as they can't be displayed in the text box
        message_list = [
            re.sub(CONSOLE_COLOR_RE, "", line) if isinstance(line, str) else str(line)
            for line in message_list
        ]
        textbox.setPlainText("\n".join(message_list))
        lay.addWidget(textbox)
        self.main_layout = QVBoxLayout(self)
        self.main_layout.addWidget(scroll)
        self.setModal(False)
        # resize horizontally to a percentage of screen width or sizeHint, whichever is larger
        # but allow vertical resizing to follow sizeHint
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geometry = screen.availableGeometry()
            self.resize(
                max(self.sizeHint().height(), int(geometry.width() * 0.35)),
                self.sizeHint().height(),
            )
        else:
            self.resize(self.sizeHint())
        self.show()


class CacheResults:
    """
    Helper class used with CollectionOp, as suggested in qt/aqt/operations/__init__.py:
        '''
        `op` should either return OpChanges, or an object with a 'changes'
        property. The changes will be passed to `operation_did_execute` so that
        the UI can decide whether it needs to update itself.
        '''
    The result of merge_undo_entries(undo_entry) should be entered into the 'changes' property.
    Any other properties can be used to store additional information.
    Here, result_text is used to store a text that will be displayed in the UI after
    an op finishes.
    """

    changes: OpChanges

    def __init__(self, result_text: str, changes, count: int = 0):
        self.result_text = result_text
        self.changes = changes
        self.count = count

    def set_result_text(self, result_text):
        self.result_text = result_text

    def add_result_text(self, result_text):
        self.result_text += result_text

    def get_result_text(self):
        return self.result_text

    def incr_count(self, count: int):
        self.count += count

    def get_count(self):
        return self.count






def definition_note_type_names(copy_definition: Union[CopyDefinition, dict]) -> list:
    """The note type names a definition triggers on, in either stored format."""
    if is_format_2(copy_definition):
        return list(copy_definition.get("triggers", {}).get("note_types") or [])
    stored = copy_definition.get("copy_into_note_types") or ""
    # Split by comma and remove the first wrapping " but keeping the last one
    return stored.strip('""').split('", "')


def definition_note_types_label(copy_definition: Union[CopyDefinition, dict]):
    """The note type names as the error messages have always spelled them, or None."""
    if is_format_2(copy_definition):
        names = copy_definition.get("triggers", {}).get("note_types")
        return '", "'.join(names) if names else None
    return copy_definition.get("copy_into_note_types", None)


def definition_trigger_flag(
    copy_definition: Union[CopyDefinition, dict], format_2_key: str, format_1_key: str
) -> bool:
    if is_format_2(copy_definition):
        return bool(copy_definition.get("triggers", {}).get(format_2_key, False))
    return bool(copy_definition.get(format_1_key, False))


def definition_queries_collection(copy_definition: Union[CopyDefinition, dict]) -> bool:
    """Whether the definition searches the collection, which the progress label reports."""
    if is_format_2(copy_definition):
        effects = copy_definition.get("effects") or {}
        return bool(effects.get("queries_collection", True))
    return copy_definition.get("copy_mode") == "Across notes"


def make_copy_fields_undo_text(
    copy_definitions: list[CopyDefinition],
    note_count: Optional[int] = None,
    suffix: Optional[str] = "",
) -> str:
    """
    Create an undo text for a copy fields operation
    :param copy_definitions: The definitions of what to copy, includes process chains
    :param note_count: The number of notes that will be copied into
    :param suffix: A suffix to add to the undo text
    :return: The undo text
    """
    if len(copy_definitions) == 1:
        undo_text = f"Copy fields ({copy_definitions[0]['definition_name']})"
    else:
        undo_text = f"Copy fields with {len(copy_definitions)} definitions"

    if note_count:
        undo_text += f" for {note_count} notes"
    if suffix:
        undo_text += f" {suffix}"
    return undo_text


def copy_fields(
    copy_definitions: list[CopyDefinition],
    note_ids: Optional[Sequence[Union[int, NoteId]]] = None,
    note_ids_per_definition: Optional[list[Sequence[Union[int, NoteId]]]] = None,
    trigger_notes: Optional[Sequence[Note]] = None,
    parent=None,
    field_only: Optional[str] = None,
    undo_entry: Optional[int] = None,
    undo_text_suffix: Optional[str] = "",
    update_sync_result: Optional[Callable[[str, int], None]] = None,
    on_done: Optional[Callable[[], None]] = None,
    progress_title: Optional[str] = None,
):
    """
    Run many copy definitions at once using CollectionOp. Includes fancy progress updates
    :param copy_definitions: The definitions of what to copy
    :param note_ids: The note ids to copy into, if None, all notes of the note type are copied into
    :param note_ids_per_definition: An alternate of note_ids, a list of note ids to copy into for
        each definition used by PickCopyDefinitionsDialog. Must hold one list per definition,
        otherwise nothing is copied and an error is logged
    :param trigger_notes: Notes to use as they are instead of fetching them by their ids. Used
        by the note editor, whose note can be ahead of the database while its save is still
        running in the background
    :param parent: The parent widget
    :param undo_entry: The undo entry to merge the changes into, if None, a custom entry
        is created
    :param field_only: Optional field to limit copying to. Used when copying is applied
      in the note editor
    :param undo_text_suffix: Optional suffix to add to the undo text.
        Useless, if undo_entry is passed
    :param update_sync_result: Provided when this is a sync operation. Used to update the sync
        result text and count
    :param on_done: Optional function to run when the operation is done
    :param progress_title: Optional title for the progress dialog
    """
    start_time = time.time()
    debug_texts = []
    is_sync = update_sync_result is not None
    config = Config()
    config.load()

    def log(message: str):
        nonlocal debug_texts
        debug_texts.append(message)
        print(message)

    logger = Logger(config.log_level, log=log)

    def on_success(copy_results: CacheResults):
        mw.progress.finish()
        result = copy_results.get_result_text()
        # Don't show a blank tooltip with just the time
        if result:
            main_time = (
                f"{time.time() - start_time:.2f}s total time"
                if len(copy_definitions) > 1
                else "Finished in "
            )
            result_text = f"{main_time}{result}"
            count = copy_results.get_count()
            if update_sync_result is not None:
                update_sync_result(result_text, count)
            else:
                tooltip(
                    result_text,
                    parent=parent,
                    period=5000 + len(copy_definitions) * 1000,
                    y_offset=100,
                )
        if not is_sync and len(debug_texts) > 0:
            ScrollMessageBox(debug_texts, title="Copy fields debug Messages", parent=parent)
        if on_done is not None:
            on_done()

    def on_failure(exception):
        mw.progress.finish()
        logger.error(f"Copying failed: {exception}")
        if not is_sync and len(debug_texts) > 0:
            ScrollMessageBox(debug_texts, title="Copy Fields debug Messages", parent=parent)
        if on_done is not None:
            on_done()
        # Need to raise the exception to get the traceback to the cause in the console
        raise exception

    def op(_) -> CacheResults:
        if not copy_definitions:
            logger.error("Error in copy fields: No definitions given")
            return CacheResults(result_text="", changes=OpChanges())

        # Checked up front: definition i runs over list i, so a mismatch found mid-loop would
        # leave the earlier definitions written and merged into the undo entry. Any Sequence
        # passes, as PickCopyDefinitionsDialog hands over find_notes' protobuf containers.
        if note_ids_per_definition is not None:
            if len(note_ids_per_definition) != len(copy_definitions):
                logger.error(
                    "Error in copy fields: Got"
                    f" {len(note_ids_per_definition)} note id lists for"
                    f" {len(copy_definitions)} definitions"
                )
                return CacheResults(result_text="", changes=OpChanges())
            for i, ids in enumerate(note_ids_per_definition):
                if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)):
                    logger.error(
                        f"Error in copy fields: Note ids for definition {i + 1} are not a list"
                    )
                    return CacheResults(result_text="", changes=OpChanges())

        copied_into_cards_dict: dict[int, Card] = {}
        copied_into_notes: list[Note] = []
        # If an undo_entry isn't passed, create one
        nonlocal undo_entry
        if undo_entry is None:
            note_count = None
            if note_ids is not None:
                note_count = len(note_ids)
            elif note_ids_per_definition is not None:
                note_count = sum(len(ids) for ids in note_ids_per_definition)
            undo_text = make_copy_fields_undo_text(
                copy_definitions=copy_definitions,
                note_count=note_count,
                suffix=undo_text_suffix,
            )
            undo_entry = mw.col.add_custom_undo_entry(undo_text)
        results = CacheResults(
            result_text="",
            changes=mw.col.merge_undo_entries(undo_entry),
        )

        for i, copy_definition in enumerate(copy_definitions):
            logger.reset_prefix()
            logger.copy_definition_name = copy_definition.get("definition_name", None)
            results = copy_fields_in_background(
                copy_definition=copy_definition,
                note_ids=(
                    note_ids_per_definition[i] if note_ids_per_definition is not None else note_ids
                ),
                trigger_notes=trigger_notes,
                logger=logger,
                is_sync=is_sync,
                copied_into_cards_dict=copied_into_cards_dict,
                copied_into_notes=copied_into_notes,
                results=results,
                field_only=field_only,
                progress_title=progress_title,
            )
            # Update each modified note after every operation, so that if multiple ops are updating
            # the same note, all changes are saved
            # Because of this, if multiple ops use the same note data as a source, the final result
            # depends on the order of the ops
            mw.col.update_notes(copied_into_notes)
            # Update all edited cards so far, then remove the edited flag
            # This must be done after each operation, so that if subsequent use card data as source,
            # the final result depends on the order of the ops
            edited_cards = [
                card
                for card in copied_into_cards_dict.values()
                if hasattr(card, "edited") and card.edited
            ]
            for card in edited_cards:
                del card.edited
            mw.col.update_cards(edited_cards)
            # undo_entry has to be updated after every undoable op or the last_step will
            # increment causing an "target undo op not found" error!
            results.changes = mw.col.merge_undo_entries(undo_entry)
            if mw.progress.want_cancel():
                break
        if is_sync:
            # Update all card custom-data after all ops are complete
            copied_into_cards = list(copied_into_cards_dict.values())
            for card in copied_into_cards:
                write_custom_data(card, key="fc", value=1)
            mw.col.update_cards(copied_into_cards)
            results.changes = mw.col.merge_undo_entries(undo_entry)
            # Ensure that all fc flags are reset in the DB, if the notes/cards were not
            # modified during the copy operations
            rest_cards = [
                mw.col.get_card(cid) for cid in mw.col.find_cards("prop:cdn:fc=-1 OR prop:cdn:fc=0")
            ]
            for card in rest_cards:
                write_custom_data(card, key="fc", value=1)
            mw.col.update_cards(rest_cards)
            results.changes = mw.col.merge_undo_entries(undo_entry)
        return results

    return (
        CollectionOp(
            parent=parent,
            op=op,
        )
        .success(on_success)
        .failure(on_failure)
        .run_in_background()
    )


def copy_fields_in_background(
    copy_definition: CopyDefinition,
    copied_into_cards_dict: dict[int, Card],
    copied_into_notes: list[Note],
    results: CacheResults,
    is_sync: Optional[bool] = False,
    note_ids: Optional[Sequence[int]] = None,
    trigger_notes: Optional[Sequence[Note]] = None,
    field_only: Optional[str] = None,
    logger: Logger = Logger("error"),
    progress_title: Optional[str] = None,
) -> CacheResults:
    """
    Function run to copy stuff into many notes at once.
    :param copy_definition: The definition of what to copy, includes process chains
    :param copied_into_cards_dict: An initially empty dictionary of cards that will be appended to with the
        cards of the notes that were copied into
    :param copied_into_notes: An initially empty list of notes that will be appended to with the
        notes that were copied into
    :param results: The results object to update with the final result text
    :param note_ids: The note ids to copy into, if None, all notes of the note type are copied into
    :param trigger_notes: Notes to use as they are instead of fetching them by their ids. They
        still have to pass the query, so they are only used where it selects their id
    :param field_only: Optional field to limit copying to. Used when copying is applied
      in the note editor
    :param logger: Logger to use for errors and debug messages
    :param is_sync: Whether this is a sync operation or not
    :param progress_title: Optional title for the progress dialog
    :return: the CacheResults object passed as results
    """
    copy_into_note_types = definition_note_types_label(copy_definition)
    definition_name = copy_definition.get("definition_name", "")

    start_time = time.time()

    note_cnt = 0

    if copy_into_note_types is None:
        logger.error(
            f"""Error in copy fields: Note type for copy_into_note_types '{copy_into_note_types}'
            not found, check your spelling""",
        )
        return results

    note_type_names = definition_note_type_names(copy_definition)
    note_type_ids = list(
        filter(None, [mw.col.models.id_for_name(name) for name in note_type_names])
    )

    copy_on_review = definition_trigger_flag(copy_definition, "on_review", "copy_on_review")
    copy_on_sync = definition_trigger_flag(copy_definition, "on_sync", "copy_on_sync")
    copy_on_sync_after_review = not copy_on_review and copy_on_sync

    assert mw.col.db is not None

    nids_query = f"AND n.id IN {ids2str(note_ids)}" if note_ids is not None else ""
    given_notes = {note.id: note for note in trigger_notes or []}
    notes = [
        given_notes[nid] if nid in given_notes else mw.col.get_note(nid)
        for nid in mw.col.db.list(
            # When syncing, only copy into notes that have been been flagged for a field change
            # in the custom scheduler by setting the field changed flag to 0 or -1 in note_hooks.py
            # and filter by any given note_ids. DISTINCT, as the join yields a row per flagged
            # card and each note must be copied into only once.
            f"""
        SELECT DISTINCT n.id
        FROM notes n, cards c
        WHERE n.mid IN {ids2str(note_type_ids)}
        AND c.nid = n.id
        AND json_extract(json_extract(c.data, '$.cd'), '$.fc') {
                f"IN (0, -1)" if copy_on_sync_after_review else "= 0"
            }
        {nids_query}
        """
            if is_sync
            # Otherwise, copy into all notes and filter by any given note_ids
            else f"""
        SELECT n.id
        FROM notes n
        WHERE n.mid IN {ids2str(note_type_ids)}
        {nids_query}
        """
        )
    ]

    if not is_sync and len(notes) == 0:
        # When syncing, it's normal to get zero results if no cards have been reviewed
        logger.error(
            f"Error in copy fields: Did not find any notes of note type(s) {copy_into_note_types}"
        )
        return results

    total_notes_count = len(notes)
    is_across = definition_queries_collection(copy_definition)

    progress_updater = ProgressUpdater(
        start_time=start_time,
        definition_name=definition_name,
        total_notes_count=total_notes_count,
        is_across=is_across,
        title=progress_title,
    )

    # Cache any opened files, so process chains can use them instead of needing to open them again
    # contents will be cached by file name
    # Key: file name, Value: whatever a process would need from the file
    file_cache: dict[str, Any] = {}

    total_processed_sources = 0
    total_processed_dests = 0
    for note in notes:
        note_cnt += 1

        success = copy_for_single_trigger_note(
            copy_definition=copy_definition,
            trigger_note=note,
            is_sync=is_sync,
            copied_into_notes=copied_into_notes,
            copied_into_cards_dict=copied_into_cards_dict,
            field_only=field_only,
            logger=logger,
            file_cache=file_cache,
            progress_updater=progress_updater,
        )

        progress_updater.maybe_render_update()

        if not success:
            # Something went wrong, stop operation so the issue can be debugged. Checked before
            # the cancel, so a cancel can't turn the failure into a reported partial run.
            return results

        if mw.progress.want_cancel():
            break

    # When syncing, don't show a pointless message that nothing was done
    # Otherwise, when copy fields is run manually, you want to know the result in any case
    (
        processed_note_cnt,
        total_processed_sources,
        total_processed_dests,
        total_processed_files,
        total_processed_cards,
    ) = progress_updater.get_counts()
    should_report_result = processed_note_cnt > 0 if is_sync else True
    if should_report_result:
        #  Get counts from ProgressUpdater and render the final update
        results.add_result_text(f"""<br><span>
            {time.time() - start_time:.2f}s -
            <i>{html.escape(copy_definition["definition_name"])}:</i>
            {f"{total_processed_dests} destinations" if total_processed_dests > 0 else ""}
            {f"{total_processed_files} files" if total_processed_files > 0 else ""}
            {f"{total_processed_cards} cards" if total_processed_cards > 0 else ""}
            {f'''processed with {total_processed_sources} sources''' if is_across else "processed"}
        </span>""")
        results.incr_count(1)
    return results






def note_passes_deck_whitelist(
    deck_names: list,
    include_subdecks: bool,
    trigger_note: Note,
    deck_id: Optional[int] = None,
    logger: Logger = Logger("error"),
) -> bool:
    """Whether the definition's deck whitelist lets this trigger note through.

    Trigger filtering stays outside the stage interpreter (§8): which notes a definition
    considers is decided by its triggers, and only then does the program run.
    """
    if not deck_names:
        return True

    unique_whitelist_dids: set = {
        mw.col.decks.id_for_name(target_deck_name) for target_deck_name in deck_names
    }
    if include_subdecks:
        parent_dids = set()
        for did in unique_whitelist_dids:
            # A name that matched no deck resolved to None, which children() would send
            # to the backend as deck 0 and raise NotFoundError on. It has no subdecks.
            if did is None:
                continue
            child_dids = [d[1] for d in mw.col.decks.children(did)]
            parent_dids.update(child_dids)
        unique_whitelist_dids.update(parent_dids)

    deck_ids_of_cards = []
    if deck_id is not None:
        deck_ids_of_cards.append(deck_id)
    else:
        for card in trigger_note.cards():
            deck_ids_of_cards.append(card.odid or card.did)
    logger.debug(
        f"copy_for_single_trigger_note: deck_ids={deck_ids_of_cards},"
        f" unique_whitelist_dids={unique_whitelist_dids}"
    )
    if deck_ids_of_cards and not any(
        card_deck_id in unique_whitelist_dids for card_deck_id in deck_ids_of_cards
    ):
        logger.debug(
            "copy_for_single_trigger_note: No deck id in whitelist, skipping copy for note"
            f" {trigger_note.id}"
        )
        return False
    return True


def copy_for_single_trigger_note(
    copy_definition: Union[CopyDefinition, dict],
    trigger_note: Note,
    is_sync: Optional[bool] = False,
    copied_into_notes: Optional[list[Note]] = None,
    copied_into_cards_dict: Optional[dict[int, Card]] = None,
    field_only: Optional[str] = None,
    deck_id: Optional[int] = None,
    logger: Logger = Logger("error"),
    file_cache: Optional[dict] = None,
    progress_updater: Optional[ProgressUpdater] = None,
    definitions_for_calls: Optional[Sequence[dict]] = None,
) -> bool:
    """Run one copy definition for one trigger note.

    The definition may be stored in either format: a format-1 one is migrated on the way in,
    so there is one executor over one format no matter what the config holds. Everything
    below this call is stages.

    :param copy_definition: the definition, in format 1 or format 2
    :param trigger_note: the note that triggered this copy or was targeted otherwise
    :param is_sync: whether this is a sync operation, which some conditions only apply to
    :param copied_into_notes: appended with the notes that were written into, for the
        caller's batched `update_notes()`. Omit when nothing needs saving.
    :param copied_into_cards_dict: filled with the cards of every note the definition
        touched, keyed by card id, for the caller's batched `update_cards()`
    :param field_only: limits field writes to those the named editor field triggers
    :param deck_id: the deck a not-yet-added note's cards will go into, since it has none
    :param logger: logger for errors and debug messages
    :param file_cache: a dictionary caching opened files' content for process chains
    :param progress_updater: optional object to update the progress bar
    :param definitions_for_calls: the definitions a `call_definition` stage may reach
    :return: True when the note is done -- written into or benignly skipped -- and False
        when the definition failed and the caller's bulk loop should stop
    """
    logger.nid = trigger_note.id

    try:
        staged_definition = as_format_2(copy_definition)
    except MigrationError as error:
        logger.error(str(error))
        return False

    triggers = staged_definition.get("triggers", {})
    if not note_passes_deck_whitelist(
        deck_names=triggers.get("deck_names") or [],
        include_subdecks=bool(triggers.get("include_subdecks", False)),
        trigger_note=trigger_note,
        deck_id=deck_id,
        logger=logger,
    ):
        # Deck not in whitelist, so skip this note; things are ok, so return True
        if progress_updater is not None:
            progress_updater.update_counts(skipped_note_cnt_inc=1)
        return True

    lookup = None
    if definitions_for_calls:
        lookup = make_definition_lookup(definitions_for_calls)

    session = ExecutionSession(
        logger=logger,
        is_sync=bool(is_sync),
        field_only=field_only,
        deck_id=deck_id,
        progress_updater=progress_updater,
        file_cache=file_cache,
        definition_lookup=lookup,
        want_cancel=mw.progress.want_cancel,
    )
    return run_definition_for_trigger_note(
        definition=staged_definition,
        trigger_note=trigger_note,
        session=session,
        copied_into_notes=copied_into_notes,
        copied_into_cards_dict=copied_into_cards_dict,
    )


def make_definition_lookup(definitions: Sequence[dict]):
    """Map guid -> format-2 definition, for `call_definition` stages to resolve against."""
    by_guid: dict = {}
    for definition in definitions:
        try:
            staged = as_format_2(definition)
        except MigrationError:
            continue
        by_guid[staged.get("guid", "")] = staged
    return by_guid.get












