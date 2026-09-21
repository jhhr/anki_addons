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


def card_action_ref(model: Any, template: Any, **extra: Any) -> dict:
    """A card action that carries the ids of a live note type and template, as the editor
    writes one. `card_action` above is the pre-0.5.0 spelling, which is still read."""
    action = card_action(model["name"], template["name"], **extra)
    del action["card_type_name"]
    action["card_type"] = {
        "note_type_id": model["id"],
        "template_id": template["id"],
        "name": f"{model['name']}{CARD_TYPE_SEPARATOR}{template['name']}",
    }
    return action


def object_ref(name: str, object_id: Optional[int] = None) -> dict:
    """The stored form of a note type or deck reference: the id, which may be null, and
    the name last seen with it."""
    return {"id": object_id, "name": name}


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


# Format-2 builders ------------------------------------------------------------------------
#
# A staged definition is an ordered program, so its builders are per stage rather than per
# definition: a test names the stages it is about and `staged()` supplies the trigger
# metadata and the derived `effects`. Everything here writes format-2 syntax, so a
# `{{...}}` reference has to qualify the binding it reads -- `{{trigger.Word}}`, not
# `{{Word}}`. The legacy builders above are what produces `syntax_version: 1` expressions,
# by going through the migrator.

_stage_counter = {"n": 0}


def _stage_guid(kind: str) -> str:
    _stage_counter["n"] += 1
    return f"{kind}-{_stage_counter['n']}"


def text(value: str = "", process_chain: Optional[list] = None) -> dict:
    return {
        "mode": "text",
        "text": value,
        "code": "",
        "process_chain": process_chain or [],
    }


def code(value: str = "", process_chain: Optional[list] = None) -> dict:
    return {
        "mode": "code",
        "text": "",
        "code": value,
        "process_chain": process_chain or [],
    }


def _stage(stage_type: str, **fields: Any) -> dict:
    stage = {
        "guid": fields.pop("guid", None) or _stage_guid(stage_type),
        "type": stage_type,
        "name": fields.pop("name", stage_type),
        "enabled": fields.pop("enabled", True),
    }
    stage.update(fields)
    return stage


def variable(result: str, value: Optional[dict] = None, **extra: Any) -> dict:
    return _stage("variable", result=result, value=value or text(""), **extra)


def note_query(result: str, query: str, strategy: str = "all", count=None, **extra: Any) -> dict:
    selection = extra.pop("selection", None) or {
        "strategy": strategy,
        "count": count,
        "sort_field": None,
        "sort_order": "descending",
    }
    return _stage(
        "note_query",
        result=result,
        query=text(query),
        selection=selection,
        if_empty=extra.pop("if_empty", "continue"),
        **extra,
    )


def card_query(result: str, query: str, strategy: str = "all", count=None, **extra: Any) -> dict:
    selection = extra.pop("selection", None) or {
        "strategy": strategy,
        "count": count,
        "sort_field": None,
        "sort_order": "descending",
    }
    return _stage(
        "card_query",
        result=result,
        query=text(query),
        selection=selection,
        if_empty=extra.pop("if_empty", "continue"),
        **extra,
    )


def write(field: str, value: dict, write_if: str = "always") -> dict:
    return {"field": field, "value": value, "write_if": write_if}


def edit_note(binding: str, fields: Optional[list] = None, **extra: Any) -> dict:
    return _stage(
        "edit_note",
        target={"binding": binding},
        fields=fields or [],
        tags=extra.pop("tags", None) or {"add": [], "remove": []},
        card_actions=extra.pop("card_actions", None) or [],
        read_semantics="stage_snapshot",
        **extra,
    )


def edit_card(binding: str, card_actions: Optional[list] = None, **extra: Any) -> dict:
    return _stage("edit_card", target={"binding": binding}, card_actions=card_actions or [], **extra)


def list_variable(result: str, item_type: str = "Text", **extra: Any) -> dict:
    return _stage("list_variable", result=result, item_type=item_type, **extra)


def store(binding: str, value: dict, **extra: Any) -> dict:
    return _stage("store", target={"kind": "list", "binding": binding}, value=value, **extra)


def for_each_note(binding: str, body: list, item_binding: str = "note", **extra: Any) -> dict:
    return _stage(
        "for_each_note",
        input={"binding": binding},
        item_binding=item_binding,
        body=body,
        **extra,
    )


def for_each_card(
    binding: str,
    body: list,
    item_binding: str = "card",
    note_binding: str = "note",
    **extra: Any,
) -> dict:
    return _stage(
        "for_each_card",
        input={"binding": binding},
        item_binding=item_binding,
        note_binding=note_binding,
        body=body,
        **extra,
    )


def reduce(binding: str, result: str, value: Optional[dict] = None, **extra: Any) -> dict:
    return _stage(
        "reduce",
        input={"binding": binding},
        result=result,
        initial=extra.pop("initial", None) or text(""),
        item_binding=extra.pop("item_binding", "item"),
        accumulator_binding=extra.pop("accumulator_binding", "accumulator"),
        value=value or text(""),
        **extra,
    )


def join(binding: str, result: str, separator: str = ", ", **extra: Any) -> dict:
    return reduce(binding, result, operation="join", separator=separator, **extra)


def condition(predicate: dict, then: list, otherwise: Optional[list] = None, **extra: Any) -> dict:
    stage = _stage("condition", predicate=predicate, then=then, **extra)
    stage["else"] = otherwise or []
    return stage


def read_file(result: str, filename: str, if_missing: str = "empty", **extra: Any) -> dict:
    return _stage(
        "read_file", result=result, filename=text(filename), if_missing=if_missing, **extra
    )


def write_file(filename: str, content: dict, overwrite: bool = True, **extra: Any) -> dict:
    return _stage(
        "write_file",
        filename=text(filename),
        content=content,
        overwrite=overwrite,
        **extra,
    )


def call_definition(definition_guid: str, trigger: str = "trigger", **extra: Any) -> dict:
    return _stage(
        "call_definition",
        definition_guid=definition_guid,
        trigger={"binding": trigger},
        outputs=extra.pop("outputs", None) or [],
        **extra,
    )


def export(name: str, stage: dict) -> dict:
    return {"name": name, "stage_guid": stage["guid"]}


def staged(
    definition_name: str = "staged",
    stages: Optional[list] = None,
    note_types: Optional[list[str]] = None,
    exports: Optional[list] = None,
    guid: Optional[str] = None,
    **trigger_extra: Any,
) -> dict:
    """A format-2 definition with derived `effects`, as a saved one would carry."""
    from copy_anywhere.logic.flow_analysis import compute_effects

    triggers = {
        "note_types": note_types if note_types is not None else [VOCAB],
        "deck_names": [],
        "include_subdecks": False,
        "on_sync": False,
        "on_add": False,
        "on_review": False,
        "on_unfocus": {"edit_fields": [], "add_fields": []},
    }
    triggers.update(trigger_extra)
    # Names in, references out: a definition stores an id beside the name for every object
    # Anki gives one, and a test naming a note type or a deck should not have to say so.
    for key in ("note_types", "deck_names"):
        triggers[key] = [
            object_ref(value) if isinstance(value, str) else value for value in triggers[key]
        ]
    definition = {
        "guid": guid or f"def-{definition_name}",
        "format_version": 2,
        "definition_name": definition_name,
        "triggers": triggers,
        "stages": stages or [],
        "exports": exports or [],
    }
    definition["effects"] = compute_effects(definition)
    return definition
