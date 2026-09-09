"""Builders for the copy definitions the tests drive.

A definition is a plain dict with around two dozen keys, most of which any one test does not
care about. Spelling them out in every case buries the one key under test, so these builders
supply the shape and let each test name only what it is about. The defaults deliberately
match what the addon's own editor writes for a new definition, so a test that names nothing
is exercising the out-of-the-box configuration rather than an invented one.
"""

from typing import Any, Optional

from conftest import VOCAB

CARD_TYPE_SEPARATOR = "<::>"


def quoted_list(names: list[str]) -> str:
    """The stored form of a name list: quoted and comma-joined, as the editors write it."""
    return '", "'.join(names)


def field_to_field(
    copy_into_note_field: str,
    copy_from_text: str = "",
    **extra: Any,
) -> dict:
    return {
        "guid": f"ftf-{copy_into_note_field}-{copy_from_text}",
        "copy_into_note_field": copy_into_note_field,
        "copy_from_text": copy_from_text,
        "copy_as_code": "",
        "use_code": False,
        "copy_if_empty": False,
        "copy_on_unfocus_when_edit": False,
        "copy_on_unfocus_when_add": False,
        "copy_on_unfocus_trigger_field": "",
        "process_chain": None,
        **extra,
    }


def field_to_file(copy_into_filename: str, copy_from_text: str = "", **extra: Any) -> dict:
    return {
        "guid": f"ftfile-{copy_into_filename}",
        "copy_into_filename": copy_into_filename,
        "copy_from_text": copy_from_text,
        "copy_as_code": "",
        "use_code": False,
        "copy_if_empty": False,
        "process_chain": None,
        **extra,
    }


def field_to_variable(copy_into_variable: str, copy_from_text: str = "", **extra: Any) -> dict:
    return {
        "guid": f"ftv-{copy_into_variable}",
        "copy_into_variable": copy_into_variable,
        "copy_from_text": copy_from_text,
        "copy_as_code": "",
        "use_code": False,
        "process_chain": None,
        **extra,
    }


def card_action(note_type_name: str, card_type_name: str, **extra: Any) -> dict:
    return {
        "guid": f"ca-{note_type_name}-{card_type_name}",
        "card_type_name": f"{note_type_name}{CARD_TYPE_SEPARATOR}{card_type_name}",
        "change_deck": None,
        "set_flag": None,
        "suspend": None,
        "bury": None,
        "set_desired_retention": None,
        "action_code": None,
        "use_code": False,
        **extra,
    }


def regex_process(regex: str, replacement: str, **extra: Any) -> dict:
    return {
        "guid": f"rx-{regex}-{replacement}",
        "name": "Regex replace",
        "regex": regex,
        "replacement": replacement,
        "regex_separator": "",
        "replacement_separator": "",
        "flags": "",
        "use_all_notes": False,
        **extra,
    }


def fonts_check_process(fonts_dict_file: str, **extra: Any) -> dict:
    return {
        "guid": f"fc-{fonts_dict_file}",
        "name": "Fonts check",
        "fonts_dict_file": fonts_dict_file,
        "limit_to_fonts": None,
        "character_limit_regex": None,
        **extra,
    }


def _base(
    definition_name: str,
    note_types: list[str],
    field_to_field_defs: Optional[list[dict]],
    extra: dict,
) -> dict:
    definition = {
        "guid": f"def-{definition_name}",
        "definition_name": definition_name,
        "copy_on_sync": False,
        "copy_on_add": False,
        "copy_on_review": False,
        "copy_into_note_types": quoted_list(note_types),
        "field_to_field_defs": field_to_field_defs or [],
        "field_to_file_defs": [],
        "field_to_variable_defs": [],
        "card_actions": [],
        "add_tags": "",
        "remove_tags": "",
        "only_copy_into_decks": None,
        "include_subdecks": False,
        "copy_condition_query": None,
        "condition_only_on_sync": False,
        "copy_from_cards_query": None,
        "sort_by_field": None,
        "select_card_by": "None",
        "select_card_count": "1",
        "select_card_separator": ", ",
        "show_error_if_none_found": False,
        "run_also_if_no_sources_found": False,
    }
    definition.update(extra)
    return definition


def within_note(
    definition_name: str = "within",
    note_types: Optional[list[str]] = None,
    field_to_field_defs: Optional[list[dict]] = None,
    **extra: Any,
) -> dict:
    definition = _base(definition_name, note_types or [VOCAB], field_to_field_defs, extra)
    definition["copy_mode"] = "Within note"
    definition["across_mode_direction"] = None
    return definition


def destination_to_sources(
    definition_name: str = "dest-to-sources",
    note_types: Optional[list[str]] = None,
    field_to_field_defs: Optional[list[dict]] = None,
    copy_from_cards_query: str = "",
    **extra: Any,
) -> dict:
    """Across notes with the trigger note as the destination: the query finds the sources."""
    definition = _base(definition_name, note_types or [VOCAB], field_to_field_defs, extra)
    definition["copy_mode"] = "Across notes"
    definition["across_mode_direction"] = "Destination to sources"
    definition["copy_from_cards_query"] = copy_from_cards_query
    return definition


def source_to_destinations(
    definition_name: str = "source-to-dests",
    note_types: Optional[list[str]] = None,
    field_to_field_defs: Optional[list[dict]] = None,
    copy_from_cards_query: str = "",
    **extra: Any,
) -> dict:
    """Across notes with the trigger note as the source: the query finds the destinations."""
    definition = _base(definition_name, note_types or [VOCAB], field_to_field_defs, extra)
    definition["copy_mode"] = "Across notes"
    definition["across_mode_direction"] = "Source to destinations"
    definition["copy_from_cards_query"] = copy_from_cards_query
    return definition
