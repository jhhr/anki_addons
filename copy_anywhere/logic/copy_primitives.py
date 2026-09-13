"""The value-producing primitives both executors share.

Interpolation, process chains, variable evaluation and progress reporting were written for
the format-1 executor and are reused verbatim by the staged one, so that what a migrated
definition computes is what the old code computed -- the same interpolation, the same
process-chain ordering, the same error text. Anything that decides *which* notes a value is
read from or written to belongs to the executor, not here.

Split out of `copy_fields.py` so `execution/` can use these without importing the module
that imports it.
"""

import html
import json
import time
from typing import Any, Optional, Sequence, Tuple, Union

from anki.cards import Card
from anki.notes import Note
from aqt import mw

from ..configuration import (
    CARD_TYPE_SEPARATOR,
    CardAction,
    CopyFieldToVariable,
    FontsCheckProcess,
    KanaHighlightProcess,
    KanjiumToJavdejongProcess,
    RegexProcess,
    is_fonts_check_process,
    is_kana_highlight_process,
    is_kanjium_to_javdejong_process,
    is_regex_process,
    is_word_highlight_process,
)
from ..shared.utils.logger import Logger
from ..shared.interpolate.execute_code import execute_code_for_field
from ..shared.interpolate.interpolate_fields import (
    QUERY_NOTE_INDEX,
    interpolate_from_text,
)
from ..utils.move_card_to_deck import move_card_to_deck
from .execute_code_wrappers import execute_code_for_card_action
from .FatalProcessError import FatalProcessError
from .fonts_check_process import fonts_check_process
from .kana_highlight_process import WithTagsDef, kana_highlight_process
from .kanjium_to_javdejong_process import kanjium_to_javdejong_process
from .regex_process import regex_process
from .word_highlight_process import word_highlight_process


class ProgressUpdateDef:
    """
    Helper class used with CollectionOp.with_backend_progress(progress_update) to
    update the progress bar and its label. In qt/aqt/progress.py the progress-update
    function is used like this:
        '''
        update = ProgressUpdate(user_wants_abort=user_wants_abort)
        progress = self.mw.backend.latest_progress()
        progress_update(progress, update)
        '''
    The update.label, update.value and update.max are used to update the progress bar.
    The progress_update function then needs a way to get information from the within the
    op while it runs. This class is used to store the label, value and max_value in
    a mutable object that the progress_update function can access.
    """

    def __init__(
        self,
        label: Optional[str] = None,
        value: Optional[int] = None,
        max_value: Optional[int] = None,
    ):
        self.label = label
        self.value = value
        self.max_value = max_value

    def has_update(self):
        return self.label is not None or self.value is not None or self.max_value is not None

    def clear(self):
        self.label = None
        self.value = None
        self.max_value = None

class ProgressUpdater:
    """
    Helper class to update the progress bar and its label. This class is used to store
    the start time, definition name, total cards count, progress update definition and
    whether the copy is across notes. It also stores the current card count and the
    total processed sources and destinations. The update_counts method is used to
    increment the counts and the render_update method is used to update the progress
    bar and its label.
    """

    def __init__(
        self,
        start_time: float,
        definition_name: str,
        total_notes_count: int,
        is_across: bool,
        title: Optional[str],
    ):
        self.start_time = start_time
        self.definition_name = definition_name
        self.total_notes_count = total_notes_count
        self.is_across = is_across
        self.note_cnt = 0
        # Notes skipped by the deck whitelist or the condition query. Kept apart from note_cnt,
        # which callers read as "notes actually processed", but the progress bar needs them to
        # reach total_notes_count, the number of notes the SQL query returned.
        self.skipped_note_cnt = 0
        self.total_processed_sources = 0
        self.total_processed_destinations = 0
        self.total_processed_cards = 0
        self.total_processed_files = 0
        self.last_render_update = 0.0
        if title is None:
            title = "Copying fields"
        self.set_title(title)

    def update_counts(
        self,
        note_cnt_inc: Optional[int] = None,
        processed_sources_inc: Optional[int] = None,
        processed_destinations_inc: Optional[int] = None,
        processed_files_inc: Optional[int] = None,
        processed_cards_inc: Optional[int] = None,
        skipped_note_cnt_inc: Optional[int] = None,
    ):
        if note_cnt_inc is not None:
            self.note_cnt += note_cnt_inc
        if skipped_note_cnt_inc is not None:
            self.skipped_note_cnt += skipped_note_cnt_inc
        if processed_sources_inc is not None:
            self.total_processed_sources += processed_sources_inc
        if processed_destinations_inc is not None:
            self.total_processed_destinations += processed_destinations_inc
        if processed_files_inc is not None:
            self.total_processed_files += processed_files_inc
        if processed_cards_inc is not None:
            self.total_processed_cards += processed_cards_inc

    def get_counts(self) -> Tuple[int, int, int, int, int]:
        return (
            self.note_cnt,
            self.total_processed_sources,
            self.total_processed_destinations,
            self.total_processed_files,
            self.total_processed_cards,
        )

    def maybe_render_update(self, force: bool = False):
        elapsed_s = time.time() - self.start_time
        elapsed_since_last_update = elapsed_s - self.last_render_update
        done_cnt = self.note_cnt + self.skipped_note_cnt
        is_last_note = done_cnt == self.total_notes_count
        no_notes = not self.total_notes_count > 0
        if (elapsed_since_last_update < 0.5 and not (force or is_last_note)) or no_notes:
            return
        self.last_render_update = elapsed_s

        elapsed_time = time.strftime("%H:%M:%S", time.gmtime(elapsed_s))
        label = f"""<strong>{html.escape(self.definition_name)}</strong>:
        <br>Copied {self.note_cnt}/{self.total_notes_count} notes{
            f", skipped {self.skipped_note_cnt}" if self.skipped_note_cnt > 0 else ""
        }
        <br><small>Processed{
            f"-  destination notes: {self.total_processed_destinations}"
            if self.total_processed_destinations > 0
            else ""
        }
            {
            f"- files: {self.total_processed_files}"
            if self.total_processed_files > 0
            else ""
        }
            {f", sources: {self.total_processed_sources}" if self.is_across else ""}
            {
            f", cards: {self.total_processed_cards}"
            if self.total_processed_cards > 0
            else ""
        }
        </small><br>Time: {elapsed_time}"""
        if done_cnt / self.total_notes_count > 0.10 or elapsed_s > 1:
            if done_cnt > 0:
                eta_s = (elapsed_s / done_cnt) * (self.total_notes_count - done_cnt)
                eta = time.strftime("%H:%M:%S", time.gmtime(eta_s))
                label += f" - ETA: {eta}"
        value = done_cnt
        max_value = self.total_notes_count

        mw.taskman.run_on_main(
            lambda: mw.progress.update(
                label=label,
                value=value,
                max=max_value,
            )
        )

    def set_title(self, title: str):
        mw.taskman.run_on_main(lambda: mw.progress.set_title(title))

class CopyFailedException(Exception):
    pass

def apply_process_chain(
    process_chain: Sequence[
        Union[
            KanjiumToJavdejongProcess,
            RegexProcess,
            FontsCheckProcess,
            KanaHighlightProcess,
        ]
    ],
    text: str,
    notes: list[Note],
    dest_note: Note = None,
    variable_values_dict: Optional[dict] = None,
    multiple_note_types: bool = False,
    progress_updater: Optional[ProgressUpdater] = None,
    logger: Logger = Logger("error"),
    file_cache: Optional[dict] = None,
) -> Union[str, None]:
    """
    Apply a list of processes to a text
    :param process_chain: The list of processes to apply
    :param text: The text to apply the processes to
    :param dest_note: The note to use for the processes that is the destination of the result value
    :param notes: Other source notes to use for the processes, used for interpolation
    :param variable_values_dict: A dictionary of variable values to use for interpolation
    :param multiple_note_types: Whether the copy is across multiple note types
    :param progress_updater: Optional object to update the progress bar
    :param logger: Logger to use for errors and debug messages
    :param file_cache: A dictionary to cache opened files' content
    :return: The text after the processes have been applied or None if there was an error
    """

    for process in process_chain:
        try:
            if is_kana_highlight_process(process):
                text = kana_highlight_process(
                    text=text,
                    kanji_field=process.get("kanji_field", ""),
                    return_type=process.get("return_type", "kana_only"),
                    with_tags_def=WithTagsDef(
                        process.get("wrap_readings_in_tags", True),
                        process.get("merge_consecutive_tags", True),
                        process.get("onyomi_to_katakana", False),
                        False,  # include_suru_okuri always false
                    ),
                    note=dest_note,
                    logger=logger,
                )
            elif is_word_highlight_process(process):
                text = word_highlight_process(
                    text=text,
                    word_field=process.get("word_field", ""),
                    note=dest_note,
                    logger=logger,
                )
            elif is_regex_process(process):
                use_all_notes = process.get("use_all_notes", False)
                interpolated_regex = get_field_values_from_notes(
                    copy_from_text=process.get("regex", ""),
                    notes=notes if use_all_notes and len(notes) > 1 else [dest_note],
                    dest_note=dest_note if use_all_notes and len(notes) > 1 else None,
                    variable_values_dict=variable_values_dict,
                    select_card_separator=process.get("regex_separator", ""),
                    multiple_note_types=multiple_note_types,
                    logger=logger,
                    progress_updater=progress_updater,
                )

                interpolated_replacement = get_field_values_from_notes(
                    copy_from_text=process.get("replacement", ""),
                    notes=notes if use_all_notes and len(notes) > 1 else [dest_note],
                    dest_note=dest_note if use_all_notes and len(notes) > 1 else None,
                    variable_values_dict=variable_values_dict,
                    select_card_separator=process.get("replacement_separator", ""),
                    multiple_note_types=multiple_note_types,
                    logger=logger,
                    progress_updater=progress_updater,
                )
                text = regex_process(
                    text=text,
                    regex=interpolated_regex,
                    replacement=interpolated_replacement,
                    flags=process.get("flags", None),
                    logger=logger,
                )

            elif is_fonts_check_process(process):
                text = fonts_check_process(
                    text=text,
                    fonts_dict_file=process.get("fonts_dict_file", ""),
                    limit_to_fonts=process.get("limit_to_fonts", None),
                    character_limit_regex=process.get("character_limit_regex", None),
                    logger=logger,
                    file_cache=file_cache,
                )

            elif is_kanjium_to_javdejong_process(process):
                text = kanjium_to_javdejong_process(
                    text=text,
                    delimiter=process.get("delimiter", ""),
                    logger=logger,
                )
        except FatalProcessError as e:
            # If some process fails in a way that will always fail, we stop the whole op
            # so the user can fix the issue without needing to wait for the whole op to finish
            logger.error(f"Error in {process['name']} process: {e}")
            return None
    return text

def get_variable_values_for_note(
    field_to_variable_defs: list[CopyFieldToVariable],
    note: Note,
    file_cache: Optional[dict] = None,
    logger: Logger = Logger("error"),
) -> dict:
    """
    Get the values for the variables from the note
    :param field_to_variable_defs: The definitions of the variables to get
    :param note: The note to get the values from
    :param file_cache: A dictionary to cache opened files' content for process chains
    :param logger: Logger to use for errors and debug messages
    :return: A dictionary of the values for the variables or None if there was an error
    """

    variable_values_dict = {}
    for field_to_variable_def in field_to_variable_defs:
        copy_into_variable = field_to_variable_def["copy_into_variable"]
        copy_from_text = field_to_variable_def["copy_from_text"]
        copy_as_code = field_to_variable_def.get("copy_as_code", "")
        use_code = field_to_variable_def.get("use_code", False)
        active_text = copy_as_code if use_code else copy_from_text
        process_chain = field_to_variable_def.get("process_chain", None)

        # Step 1: Interpolate the text with values from the note
        interpolated_value, invalid_fields = interpolate_from_text(
            active_text,
            source_note=note,
        )
        if len(invalid_fields) > 0:
            logger.error(
                "Error getting variable values: Invalid fields in copy_from_text:"
                f" {', '.join(invalid_fields)}"
            )

        # Step 1b: Execute as code if requested
        if use_code and interpolated_value is not None:
            interpolated_value, code_error = execute_code_for_field(interpolated_value, note)
            if code_error:
                raise CopyFailedException(
                    f"Code execution error in variable '{copy_into_variable}':\n{code_error}"
                )

        # Step 2: If we have further processing steps, run them
        if process_chain is not None and interpolated_value is not None:
            interpolated_value = apply_process_chain(
                process_chain=process_chain,
                text=interpolated_value,
                dest_note=note,
                notes=[note],
                multiple_note_types=False,
                logger=logger,
                file_cache=file_cache,
            )
            if interpolated_value is None:
                return {}

        variable_values_dict[copy_into_variable] = interpolated_value

    return variable_values_dict

def int_sort_by_field_value(note: Note, sort_by_field) -> int:
    # KeyError as well as ValueError: sort_by_field names a field on the *source* note type,
    # which need not be the one the definition copies into, so a query that returns a note of
    # another type reaches here with a field the note does not have.
    try:
        return int(note[sort_by_field])
    except (ValueError, KeyError):
        return 0

def sort_by_field_value(note: Note, sort_by_field) -> Any:
    try:
        return note[sort_by_field]
    except KeyError:
        return ""

def get_field_values_from_notes(
    copy_from_text: str,
    notes: list[Note],
    dest_note: Optional[Note],
    multiple_note_types: bool = False,
    variable_values_dict: Optional[dict] = None,
    select_card_separator: Optional[str] = ", ",
    use_code: bool = False,
    logger: Logger = Logger("error"),
    progress_updater: Optional[ProgressUpdater] = None,
) -> str:
    """
    Get the value from the field in the selected notes gotten with interpolation.
    :param copy_from_text: Text defining the content to copy. Contains text and field names and
            special values enclosed in double curly braces that need to be replaced with the actual
            values from the notes.
    :param notes: The selected notes to get the value from. In the case of COPY_MODE_WITHIN_NOTE,
            this will be a list with only one note
    :param dest_note: The note to copy into, omitted in COPY_MODE_WITHIN_NOTE
    :param multiple_note_types: Whether the copy is into multiple note types
    :param variable_values_dict: A dictionary of custom variable values to use in interpolating text
    :param select_card_separator: The separator to use when joining the values from the notes.
        Irrelevant if there is only one note
    :param use_code: When True, the interpolated text is executed as Python code and the return
        value of that code is used as the result instead of the interpolated text itself.
    :param logger: Logger to use for errors and debug messages, used for storing all messages
        until the end of the whole operation to show them in a GUI element at the end
    :param progress_updater: An object to update the progress bar with
    :return: String with the values from the field in the notes
    """

    if copy_from_text is None:
        logger.error(
            "Error in copy fields: Required value 'copy_from_text' was missing.",
        )
        return ""

    if select_card_separator is None:
        select_card_separator = ", "

    result_val = ""

    multiple_source_notes = len(notes) > 1
    for i, note in enumerate(notes):
        if multiple_source_notes and variable_values_dict is not None:
            # Handles DESTINATION_TO_SOURCES (many sources). Skipped for SOURCE_TO_DESTINATIONS
            # (one source note) to preserve the outer loop's index.
            variable_values_dict[QUERY_NOTE_INDEX] = i + 1
        try:
            # Return the interpolated value using the note
            interpolated_value, invalid_fields = interpolate_from_text(
                copy_from_text,
                source_note=note,
                destination_note=dest_note,
                variable_values_dict=variable_values_dict,
                multiple_note_types=multiple_note_types,
            )
        except ValueError as e:
            logger.error(f"Error in text interpolation: {e}")
            break

        if len(invalid_fields) > 0:
            logger.error(
                "Error in copy fields: Invalid fields in copy_from_text:"
                f" {', '.join(invalid_fields)}"
            )

        if use_code:
            interpolated_value, code_error = execute_code_for_field(interpolated_value, note)
            if code_error:
                raise CopyFailedException(f"Code execution error:\n{code_error}")

        if progress_updater is not None:
            progress_updater.maybe_render_update()
        if interpolated_value is not None:
            result_val += f"{select_card_separator if i > 0 else ''}{interpolated_value}"

    return result_val


def card_actions_by_template_name(
    card_actions: Sequence[CardAction],
    note: Note,
    logger: Logger = Logger("error"),
) -> dict:
    """Index note-level card actions by the template name they apply to.

    A note-level action names both the note type and the card type, so the same definition
    can carry actions for several note types and each note takes only its own.
    """
    by_template_name: dict = {}
    note_type = note.note_type()
    for card_action in card_actions or []:
        note_type_and_card_type = card_action.get("card_type_name", "")
        if CARD_TYPE_SEPARATOR not in note_type_and_card_type:
            logger.error(
                f"Error in copy fields: Invalid card type name '{note_type_and_card_type}'"
            )
            continue
        note_type_name, card_type_name = note_type_and_card_type.split(CARD_TYPE_SEPARATOR, 1)
        if not note_type or note_type_name != note_type["name"]:
            continue
        by_template_name[card_type_name] = card_action
    return by_template_name


def apply_card_action_to_card(
    card_action: CardAction,
    card: Card,
    note: Note,
    logger: Logger = Logger("error"),
) -> bool:
    """Apply one card action to one card, in memory. Returns whether the card changed.

    The card is marked with an `edited` attribute, which is what the caller's batched
    `update_cards()` looks for; the attribute is removed again once the update has run.
    """
    action_code = card_action.get("action_code", None)
    if card_action.get("use_code", False) and action_code and action_code.strip():
        executed_action, code_error = execute_code_for_card_action(action_code, note)
        if code_error:
            raise CopyFailedException(f"Code execution error in card action:\n{code_error}")
        if executed_action is None:
            return False
        card_action = executed_action  # type: ignore[assignment]

    change_deck = card_action.get("change_deck", None)
    suspend_card = card_action.get("suspend", None)
    bury_card = card_action.get("bury", None)
    set_flag = card_action.get("set_flag", None)
    set_dr = card_action.get("set_desired_retention", None)
    edited = False

    if change_deck not in [None, "-", 0]:
        move_card_to_deck(card, change_deck, logger=logger)
        edited = True
    if suspend_card in [True, False]:
        # see pylib/anki/cards.py for queue values
        card.queue = -1 if suspend_card else card.type
        edited = True
    if bury_card in [True, False] and card.queue != -1:
        # Card cannot be buried, if it is suspended. To bury a suspended card, it must first
        # be unsuspended with a suspend action.
        card.queue = -2 if bury_card else card.type
        edited = True
    if isinstance(set_flag, int) and 0 <= set_flag <= 7:
        card.set_user_flag(set_flag)
        edited = True
    if set_dr is not None:
        if isinstance(set_dr, str):
            # Get value from custom data property
            if card.custom_data:
                try:
                    custom_data = json.loads(card.custom_data)
                except json.JSONDecodeError:
                    custom_data = {}
                set_dr = custom_data.get(set_dr, None)
            else:
                set_dr = None
        if isinstance(set_dr, int) and not isinstance(set_dr, bool):
            set_dr = float(set_dr) / 100
        if isinstance(set_dr, float) and 0 < set_dr < 1:
            card.desired_retention = set_dr
            edited = True

    if edited:
        card.edited = True
    return edited


def apply_card_actions_by_template(
    card_actions: Sequence[CardAction],
    note: Note,
    cards: Sequence[Card],
    logger: Logger = Logger("error"),
    progress_updater: Any = None,
) -> list[Card]:
    """Apply note-level card actions to the note's cards, matching by card type name.

    Returns the cards this call changed. Cards with no matching action are left alone, and a
    card type named by an action the note has no card for is simply not reached.
    """
    by_template_name = card_actions_by_template_name(card_actions, note, logger=logger)
    edited: list[Card] = []
    for card in cards:
        template = card.template()
        card_action = by_template_name.get(template["name"] if template else "", None)
        if card_action is None:
            continue
        if apply_card_action_to_card(card_action, card, note, logger=logger):
            edited.append(card)
            if progress_updater is not None:
                progress_updater.update_counts(processed_cards_inc=1)
    return edited
