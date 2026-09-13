"""The format-1 executor, kept while format 2 rolls out.

`copy_into_single_note()` and `get_across_target_notes()` are what a format-1 definition's
one mode and one direction meant: assign global source and destination roles, run at most
one card query, then write every field, tag, file and card action into one note. The staged
executor in `execution/` does none of that -- a migrated definition says explicitly which
note each stage reads and writes -- but these stay until phase 6 retires them, as the
reference the migration is checked against.

Nothing in the addon calls them any more; `copy_fields.py` re-exports them so the
characterization suite can keep driving the old behaviour directly.
"""

from typing import Optional, Tuple

from anki.cards import Card
from anki.notes import Note
from anki.utils import ids2str
from aqt import mw

import base64
import random

from ..configuration import (
    SELECT_CARD_BY_VALUES,
    CardAction,
    CopyDefinition,
    CopyFieldToField,
    CopyFieldToFile,
    SelectCardByType,
    get_field_to_field_unfocus_trigger_fields,
    split_tags,
)
from ..shared.utils.logger import Logger
from ..shared.interpolate.interpolate_fields import (
    QUERY_NOTE_INDEX,
    interpolate_from_text,
)
from ..utils.duplicate_note import duplicate_note
from ..utils.file_exists_in_media_folder import file_exists_in_media_folder
from ..utils.write_to_media_folder import write_to_media_folder
from .copy_primitives import (
    CopyFailedException,
    ProgressUpdater,
    apply_card_action_to_card,
    apply_process_chain,
    card_actions_by_template_name,
    get_field_values_from_notes,
    int_sort_by_field_value,
)
from .execute_code_wrappers import execute_code_for_files


def copy_into_single_note(
    field_to_field_defs: list[CopyFieldToField],
    field_to_file_defs: list[CopyFieldToFile],
    card_actions: list[CardAction],
    destination_note: Note,
    source_notes: list[Note],
    add_tags: Optional[str] = "",
    remove_tags: Optional[str] = "",
    variable_values_dict: Optional[dict] = None,
    field_only: Optional[str] = None,
    modifies_other_notes: bool = False,
    multiple_note_types: bool = False,
    select_card_separator: Optional[str] = None,
    file_cache: Optional[dict] = None,
    logger: Logger = Logger("error"),
    progress_updater: Optional[ProgressUpdater] = None,
) -> Tuple[bool, bool, list[Card]]:

    modified_dest_note = False
    wrote_to_file = False

    # Duplicate the destination note so field-to-field defs that use the destination note's fields
    # as source values all use the same initial values, instead of the source values being modified
    # as the field-to-field defs are processed
    destination_note_copy = duplicate_note(destination_note)

    for field_to_field_def in field_to_field_defs:
        copy_into_note_field = field_to_field_def.get("copy_into_note_field", "")
        trigger_fields = get_field_to_field_unfocus_trigger_fields(
            field_to_field_def, modifies_other_notes
        )
        if field_only is not None and field_only not in trigger_fields:
            # If we're only meant to copy a specific def, defined by the field_only parameter
            # Note, depending on the mode, may be that field_only == copy_into_note_field
            continue
        copy_from_text = field_to_field_def.get("copy_from_text", "")
        copy_as_code = field_to_field_def.get("copy_as_code", "")
        use_code = field_to_field_def.get("use_code", False)
        copy_if_empty = field_to_field_def.get("copy_if_empty", False)
        process_chain = field_to_field_def.get("process_chain", None)

        try:
            cur_field_value = destination_note[copy_into_note_field]
        except KeyError:
            logger.error(f"Error in copy fields: Field '{copy_into_note_field}' not found in note")
            # Rest of defs are not processed
            raise CopyFailedException

        if copy_if_empty and cur_field_value != "":
            continue

        result_val = get_field_values_from_notes(
            copy_from_text=copy_as_code if use_code else copy_from_text,
            notes=source_notes,
            dest_note=destination_note_copy,
            multiple_note_types=multiple_note_types,
            select_card_separator=select_card_separator,
            use_code=use_code,
            logger=logger,
            variable_values_dict=variable_values_dict,
            progress_updater=progress_updater,
        )
        if process_chain is not None:
            processed_val = apply_process_chain(
                process_chain=process_chain,
                text=result_val,
                notes=source_notes,
                dest_note=destination_note_copy,
                multiple_note_types=multiple_note_types,
                variable_values_dict=variable_values_dict,
                progress_updater=progress_updater,
                logger=logger,
                file_cache=file_cache,
            )
            # result_val should always be at least "", None indicates an error
            if processed_val is None:
                logger.error(
                    f"Error in copy fields: Process chain failed for field {copy_into_note_field}"
                )
                raise CopyFailedException
            result_val = processed_val

        # Finally, copy the value into the note
        destination_note[copy_into_note_field] = result_val
        modified_dest_note = True

    for tag in split_tags(add_tags):
        if destination_note.has_tag(tag):
            continue
        destination_note.add_tag(tag)
        modified_dest_note = True

    for tag in split_tags(remove_tags):
        if not destination_note.has_tag(tag):
            continue
        destination_note.remove_tag(tag)
        modified_dest_note = True

    for field_to_file_def in field_to_file_defs:
        copy_into_filename = field_to_file_def.get("copy_into_filename", "")
        copy_from_text = field_to_file_def.get("copy_from_text", "")
        copy_as_code = field_to_file_def.get("copy_as_code", "")
        use_code = field_to_file_def.get("use_code", False)
        process_chain = field_to_file_def.get("process_chain", None)
        dont_overwrite = field_to_file_def.get("copy_if_empty", False)

        if use_code:
            # Code path: execute code per source note, each execution returns a list of
            # (filename, content) tuples that are all written as separate files.
            all_file_tuples: list[tuple[str, str]] = []
            multiple_source_notes = len(source_notes) > 1
            for i, note in enumerate(source_notes):
                if multiple_source_notes and variable_values_dict is not None:
                    variable_values_dict[QUERY_NOTE_INDEX] = i + 1
                interpolated_code, invalid_fields = interpolate_from_text(
                    copy_as_code,
                    source_note=note,
                    destination_note=destination_note_copy,
                    variable_values_dict=variable_values_dict,
                    multiple_note_types=multiple_note_types,
                )
                if invalid_fields:
                    logger.error(
                        "Error in copy fields: Invalid fields in copy_as_code:"
                        f" {', '.join(invalid_fields)}"
                    )
                file_tuples, code_error = execute_code_for_files(interpolated_code, note)
                if code_error:
                    raise CopyFailedException(
                        f"Code execution error in file definition:\n{code_error}"
                    )
                if file_tuples:
                    all_file_tuples.extend(file_tuples)
                if progress_updater is not None:
                    progress_updater.maybe_render_update()

            for fname, fcontent in all_file_tuples:
                if dont_overwrite and file_exists_in_media_folder(fname):
                    continue
                try:
                    write_to_media_folder(fname, fcontent)
                    wrote_to_file = True
                except Exception as e:
                    logger.error(f"Error in writing to file: {e}")
                    raise CopyFailedException
        else:
            # Non-code path: single file written to a pre-determined filename.
            if not copy_into_filename:
                logger.error("Error in copy fields: No file name provided")
                raise CopyFailedException

            # Interpolate filename with values from the note (never executed as code)
            copy_into_filename = get_field_values_from_notes(
                copy_from_text=copy_into_filename,
                notes=[destination_note],
                dest_note=destination_note_copy,
                multiple_note_types=multiple_note_types,
                select_card_separator=select_card_separator,
                logger=logger,
                variable_values_dict=variable_values_dict,
                progress_updater=progress_updater,
            )

            if dont_overwrite and file_exists_in_media_folder(copy_into_filename):
                continue

            result_val = get_field_values_from_notes(
                copy_from_text=copy_from_text,
                notes=source_notes,
                dest_note=destination_note_copy,
                multiple_note_types=multiple_note_types,
                select_card_separator=select_card_separator,
                logger=logger,
                variable_values_dict=variable_values_dict,
                progress_updater=progress_updater,
            )
            if process_chain is not None:
                processed_val = apply_process_chain(
                    process_chain=process_chain,
                    text=result_val,
                    notes=source_notes,
                    dest_note=destination_note_copy,
                    multiple_note_types=multiple_note_types,
                    variable_values_dict=variable_values_dict,
                    progress_updater=progress_updater,
                    logger=logger,
                    file_cache=file_cache,
                )
                # result_val should always be at least "", None indicates an error
                if processed_val is None:
                    logger.error(
                        f"Error in copy fields: Process chain failed for file {copy_into_filename}"
                    )
                    raise CopyFailedException
                result_val = processed_val

            # Finally, copy the value into the file
            try:
                write_to_media_folder(copy_into_filename, result_val)
                wrote_to_file = True
            except Exception as e:
                logger.error(f"Error in writing to file: {e}")
                raise CopyFailedException

    actions_by_template = card_actions_by_template_name(
        card_actions, destination_note, logger=logger
    )

    dest_note_cards = destination_note.cards()
    for card in dest_note_cards:
        card_action = actions_by_template.get(card.template()["name"], None)
        if card_action is None:
            continue
        if (
            apply_card_action_to_card(card_action, card, destination_note, logger=logger)
            and progress_updater is not None
        ):
            progress_updater.update_counts(processed_cards_inc=1)
    return (modified_dest_note, wrote_to_file, dest_note_cards)

def get_across_target_notes(
    copy_definition: CopyDefinition,
    copy_from_cards_query: str,
    trigger_note: Note,
    extra_state: dict,
    select_card_by: Optional[SelectCardByType] = "Random",
    sort_by_field: Optional[str] = None,
    deck_id: Optional[int] = None,
    variable_values_dict: Optional[dict] = None,
    include_subdecks: bool = False,
    select_card_count: Optional[str] = "1",
    logger: Logger = Logger("error"),
) -> list[Note]:
    """
    Get the target notes based on the search value and the query. These will either be
    the source notes or the destination notes depending on the across mode direction

    :param copy_from_cards_query: The query to find the cards to copy from.
            Uses {{}} syntax for note fields and special values
    :param trigger_note: The note to copy into, used to interpolate the query
    :param select_card_by: How to select the card to copy from, if we get multiple results using the
            the query
    :param deck_id: Optional deck id of the note to copy into
    :param extra_state: A dictionary to store cached values to re-use in subsequent calls
    :param variable_values_dict: A dictionary of custom variable values to use in interpolating text
    :param only_copy_into_decks: A comma separated whitelist of deck names. Limits the cards to copy
        from to only those in the decks in the whitelist
    :param include_subdecks: Whether to include subdecks of the whitelisted decks
    :param select_card_count: How many cards to select from the query. Default is 1
    :param logger: Logger to use for errors and debug messages.
    :return: A list of notes to copy from
    """
    logger.debug(
        f"get_across_target_notes: copy_from_cards_query='{copy_from_cards_query}',"
        f" select_card_by='{select_card_by}', deck_id={deck_id},"
        f" select_card_count='{select_card_count}', include_subdecks={include_subdecks}"
    )

    if not select_card_by:
        logger.error("Error in copy fields: Required value 'select_card_by' was missing.")
        return []

    if select_card_by not in SELECT_CARD_BY_VALUES:
        logger.error(
            f"""Error in copy fields: incorrect 'select_card_by' value '{select_card_by}'.
            It must be one of {SELECT_CARD_BY_VALUES}""",
        )
        return []

    if select_card_count:
        try:
            select_card_count_int = int(select_card_count)
            if select_card_count_int < 0:
                raise ValueError
        except ValueError:
            # The value as given, not the parsed one: int() raises before the parsed one is
            # bound, so reporting that would fail with UnboundLocalError inside the handler
            # meant to report the problem.
            logger.error(
                "Error in copy fields: Incorrect 'select_card_count' value"
                f" '{select_card_count}'. Value must be a positive integer or 0"
            )
            return []
    else:
        select_card_count_int = 1

    interpolated_cards_query, invalid_fields = interpolate_from_text(
        copy_from_cards_query,
        source_note=trigger_note,
        variable_values_dict=variable_values_dict,
    )
    logger.debug(
        f"get_across_target_notes: interpolated_cards_query='{interpolated_cards_query}',"
        f" invalid_fields={invalid_fields}"
    )
    if not interpolated_cards_query:
        logger.error("Error in copy fields: Could not interpolate copy_from_cards_query")
        return []
    cards_query_id = base64.b64encode(f"cards{interpolated_cards_query}".encode()).decode()
    try:
        # A copy of the cached list: the selection below pops from card_ids, and mutating the
        # cached entry would hand the next call a query result with cards missing from it.
        card_ids = list(extra_state[cards_query_id])
    except KeyError:
        card_ids = mw.col.find_cards(interpolated_cards_query)
        extra_state[cards_query_id] = list(card_ids)

    if len(invalid_fields) > 0:
        logger.error(
            "Error in copy fields: Invalid fields in copy_from_cards_query:"
            f" {', '.join(invalid_fields)}"
        )

    if len(card_ids) == 0:
        if copy_definition.get("show_error_if_none_found", False):
            logger.error(
                "Error in copy fields: Did not find any cards with"
                f" copy_from_cards_query='{interpolated_cards_query}'"
            )
        else:
            logger.debug(f'No cards found with copy_from_cards_query="{interpolated_cards_query}",')
        return []

    has_sort_by_field = sort_by_field and sort_by_field != "-"

    def sort_notes(notes: list[Note]):
        if has_sort_by_field:
            notes.sort(key=lambda n: int_sort_by_field_value(n, sort_by_field), reverse=True)
        return notes

    assert mw.col.db is not None
    db = mw.col.db
    # zero is a special value that means all cards
    if select_card_count_int == 0:
        distinct_note_ids = db.list(
            f"SELECT DISTINCT nid FROM cards c WHERE c.id IN {ids2str(card_ids)}"
        )
        return sort_notes([mw.col.get_note(note_id) for note_id in distinct_note_ids])

    # select a card or cards based on the select_card_by value
    selected_notes = []
    for i in range(select_card_count_int):
        selected_card_id = None
        # just iterate the list
        if select_card_by == "None" and len(card_ids) > 0:
            selected_card_id = card_ids.pop()
            if selected_card_id:
                selected_notes.append(mw.col.get_note(mw.col.get_card(selected_card_id).nid))
            continue
        elif len(card_ids) == 0:
            break
        # We don't make this key entirely unique as we want to cache the selected card for the same
        # deck_id and from_note_type_id combination, so that getting a different field from the same
        # card type will still return the same card

        card_select_key = base64.b64encode(
            f"selected_card{interpolated_cards_query}{select_card_by}{i}".encode()
        ).decode()

        if select_card_by == "Random":
            # We don't want to cache this as it should in fact be different each time
            selected_card_id = random.choice(card_ids)
        elif select_card_by == "Least_reps":
            # Loop through cards and find the one with the least reviews
            # Check cache first
            try:
                selected_card_id = extra_state[card_select_key]
            except KeyError:
                selected_card_id = min(
                    card_ids,
                    key=lambda c: db.scalar(f"SELECT COUNT() FROM revlog WHERE cid = {c}"),
                )
                extra_state[card_select_key] = selected_card_id
        if selected_card_id is None:
            logger.error("Error in copy fields: could not select card")
            break

        # Remove selected card so it can't be picked again
        card_ids = [c for c in card_ids if c != selected_card_id]
        selected_note_id = mw.col.get_card(selected_card_id).nid

        selected_note = mw.col.get_note(selected_note_id)
        selected_notes.append(selected_note)

        # If we've run out of cards, stop and return what we got
        if len(card_ids) == 0:
            break

    return sort_notes(selected_notes)
