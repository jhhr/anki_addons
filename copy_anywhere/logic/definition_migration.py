"""Pure migration of format-1 copy definitions into the format-2 staged program.

The one entry point is `migrate_definition_v1_to_v2()`. It is a pure function: it does not
load or save config, never touches `mw`, does not mutate its input, and returns the same
output every time it is handed the same definition. Every guid it has to invent is derived
from the definition's own guid plus the role the new stage plays, so a definition migrated
twice -- on two devices, or in a test and then in production -- gets the same stage guids.

Running it on something that is already format 2 returns a copy unchanged, so callers may
apply it to a whole config without tracking which definitions have been through it.

The shape of the output is dictated by what the format-1 executor did, which is not always
what a hand-written format-2 definition would look like:

* Format 1 evaluated every variable against the trigger note alone, with no access to the
  variables declared before it. Migrated variable stages are marked
  `legacy_isolated_variables` while this module still has to know it, and promotion -- which
  names the note every reference reads -- makes the mark redundant and drops it.
* Format 1 expressions name note fields unqualified (`{{Word}}`) and the destination note's
  fields with a `__Dest__` prefix. Migration records which note each stage read in
  `legacy_source` / `legacy_destination`, and promotion rewrites the references against
  them; both the keys and the references' old spelling are gone by the time a definition
  leaves here.
* Destination-to-sources joined one value out of many source notes with
  `select_card_separator`. Format 2 has no implicit list-to-text conversion, so the join is
  synthesized here: a list, a loop that stores one value per source note, and a reduce.

Intentional behaviour changes are listed in §11 of the design note: across-note selection is
by note rather than by card (so a note with two matching cards is no longer selected twice),
`Least_reps` becomes `random`, and file writes no longer translate newlines.

`promote_definition()` is the second pass, at the bottom of this module: it rewrites those
format-1 references into format-2 syntax, so that a migrated definition names the note every
reference reads and nothing downstream has to know that format 1 ever existed. It is a pass
of its own because what migration decides -- which stages, in what order -- and what
promotion rewrites are two different questions.
"""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
from typing import Any, Callable, Iterable, Optional

from ..shared.interpolate.interpolate_fields import (
    DESTINATION_PREFIX,
    FROM_TEXT_FIELD_REGEX,
    QUERY_NOTE_INDEX,
    TARGET_NOTES_COUNT,
    extract_cloze_patterns,
    intr_format,
)
from .definition_schema import (
    CARD_TYPE_SEPARATOR,
    FORMAT_VERSION,
    MODE_CODE,
    MODE_TEXT,
    STAGE_CARD_QUERY,
    STAGE_CONDITION,
    STAGE_EDIT_NOTE,
    STAGE_FOR_EACH_NOTE,
    STAGE_LIST_VARIABLE,
    STAGE_NOTE_QUERY,
    STAGE_READ_FILE,
    STAGE_REDUCE,
    STAGE_STORE,
    STAGE_VARIABLE,
    STAGE_WRITE_FILE,
    TEXT,
    CopyDefinitionV2,
    Stage,
    ValueExpression,
    is_format_2,
    stage_body_blocks,
    stage_result_names,
    value_expression,
    walk_stages,
)

COPY_MODE_WITHIN_NOTE = "Within note"
COPY_MODE_ACROSS_NOTES = "Across notes"
DIRECTION_DESTINATION_TO_SOURCES = "Destination to sources"
DIRECTION_SOURCE_TO_DESTINATIONS = "Source to destinations"

#: The binding a migrated definition uses for the notes its query found.
LEGACY_QUERY_RESULT = "legacy_query_notes"
#: The loop binding standing in for format 1's "the note currently being read or written".
LEGACY_ITEM_BINDING = "note"

DEFAULT_SELECT_CARD_SEPARATOR = ", "

#: The only `select_card_by` values format 1 would act on. Spelled out here rather than
#: imported from `configuration`, which the migrator stays clear of so it can be tested
#: without an Anki to read a config from.
LEGACY_SELECT_CARD_BY_VALUES = ("None", "Random", "Least_reps")

#: Format 1's name for the one process that could read more than one note at a time. Spelled
#: out here for the same reason as the values above.
LEGACY_REGEX_PROCESS = "Regex replace"

#: Format-1 expressions accepted unqualified field names (`{{Word}}` meaning "a field of the
#: note this expression is evaluated against"). Migration marks the expressions it writes
#: with this while `promote_definition` still has to find them, and strips the marker again
#: before it returns, so nothing outside this module ever sees one. It is also what
#: recognises an expression a 0.3.0 start stored before promotion existed: read there, never
#: written.
SYNTAX_VERSION_LEGACY = 1


def expression_is_legacy_syntax(expression: Any) -> bool:
    """Whether this expression still speaks format-1 syntax and has to be promoted."""
    return isinstance(expression, dict) and expression.get("syntax_version") == (
        SYNTAX_VERSION_LEGACY
    )


def _split_quoted_list(value: Optional[str]) -> list[str]:
    """Format 1 stored name lists quoted and comma-joined; format 2 stores JSON arrays."""
    if not value:
        return []
    return [name for name in value.strip('""').split('", "') if name]


def _child_guid(definition_guid: str, role: str) -> str:
    """A stage guid derived from the definition and the role the stage plays.

    Deterministic on purpose: migrating the same definition twice has to produce the same
    guids or the editor's focus, the trace correlation and every stored reference would move
    underneath the user on the second run.
    """
    return f"{definition_guid}::{role}"


def _legacy_expression(
    text: str = "",
    code: str = "",
    use_code: bool = False,
    process_chain: Any = None,
    **extra: Any,
) -> dict:
    return value_expression(
        text=text or "",
        code=code or "",
        mode=MODE_CODE if use_code else MODE_TEXT,
        process_chain=list(process_chain or []),
        syntax_version=SYNTAX_VERSION_LEGACY,
        **extra,
    )


def _unfocus_trigger_fields(field_def: dict, modifies_other_notes: bool) -> list[str]:
    """The editor fields that trigger this field write, resolved at migration time.

    Format 1 worked this out on every unfocus from `copy_on_unfocus_trigger_field` plus the
    mode; format 2 keeps the resolved list on the write so the executor does not have to know
    about modes any more.
    """
    trigger_value = field_def.get("copy_on_unfocus_trigger_field", "") or ""
    trigger_fields = _split_quoted_list(trigger_value)
    if modifies_other_notes:
        return trigger_fields
    return trigger_fields or [field_def.get("copy_into_note_field", "")]


def _modifies_other_notes(definition: dict) -> bool:
    """Format 1's `definition_modifies_other_notes`, inlined so this module stays pure."""
    targets_other_notes = (
        definition.get("copy_mode") == COPY_MODE_ACROSS_NOTES
        and definition.get("across_mode_direction") == DIRECTION_SOURCE_TO_DESTINATIONS
    )
    has_field_defs = len(definition.get("field_to_field_defs") or []) > 0
    has_tag_edits = bool(
        (definition.get("add_tags") or "").strip() or (definition.get("remove_tags") or "").strip()
    )
    return targets_other_notes and (has_field_defs or has_tag_edits)


def _field_writes(definition: dict, modifies_other_notes: bool) -> list[dict]:
    writes = []
    for field_def in definition.get("field_to_field_defs") or []:
        writes.append({
            "guid": field_def.get("guid", ""),
            "field": field_def.get("copy_into_note_field", ""),
            "value": _legacy_expression(
                text=field_def.get("copy_from_text", ""),
                code=field_def.get("copy_as_code", ""),
                use_code=field_def.get("use_code", False),
                process_chain=field_def.get("process_chain"),
            ),
            "write_if": "empty" if field_def.get("copy_if_empty", False) else "always",
            "unfocus_trigger_fields": _unfocus_trigger_fields(field_def, modifies_other_notes),
            "unfocus_when_edit": bool(field_def.get("copy_on_unfocus_when_edit", False)),
            "unfocus_when_add": bool(field_def.get("copy_on_unfocus_when_add", False)),
        })
    return writes


#: The keys that say which unfocus a migrated field write answers to, copied onto the
#: stages that only exist to feed one write.
UNFOCUS_GATE_KEYS = ("unfocus_trigger_fields", "unfocus_when_edit", "unfocus_when_add")


def _write_gate(field_write: dict) -> dict:
    """The keys a stage that only feeds `field_write` needs to decline when the write would.

    The unfocus keys are copied as they are. `write_if: "empty"` is copied along with the
    field it asks about, because the write knows its field and the stages in front of it do
    not; a write that always writes leaves nothing to copy.
    """
    gate = {key: field_write[key] for key in UNFOCUS_GATE_KEYS if key in field_write}
    if field_write.get("write_if", "always") == "empty":
        gate["write_if"] = "empty"
        gate["write_if_field"] = field_write.get("field", "")
    return gate


def _tag_writes(definition: dict) -> dict:
    return {
        "add": _split_quoted_list(definition.get("add_tags")),
        "remove": _split_quoted_list(definition.get("remove_tags")),
    }


def _card_actions(definition: dict) -> list[dict]:
    # Card actions keep their card type selector: a note-level action still says which
    # template it applies to (§11 step 6). `edit_card` is the stage without one.
    return [deepcopy(action) for action in definition.get("card_actions") or []]


def _selection(definition: dict, warnings: list[str]) -> dict:
    """Map `select_card_by`/`select_card_count`/`sort_by_field` onto a format-2 selection."""
    select_card_by = definition.get("select_card_by")
    strategy = "first"
    strategy_error: Optional[str] = None
    if select_card_by is None or select_card_by not in LEGACY_SELECT_CARD_BY_VALUES:
        # Format 1 refused to select anything at all here, which is the behaviour to keep: a
        # definition whose stored value is missing or unreadable did nothing and said so, and
        # quietly treating it as "take the first note" would start writing notes for a
        # definition that has never written one. The count and sort field are still read
        # below: they are what the user gets back once they fix the strategy in the editor.
        strategy_error = (
            "Error in copy fields: 'select_card_by' was missing"
            if select_card_by is None
            else f"Error in copy fields: incorrect 'select_card_by' value '{select_card_by}'"
        )
    elif select_card_by == "Random":
        strategy = "random"
    elif select_card_by == "Least_reps":
        # Never used in practice and dropped from format 2; random is the closest surviving
        # strategy, and naming the definition gives the user something to go on.
        strategy = "random"
        warnings.append(
            f"Definition '{definition.get('definition_name', '')}': select_card_by"
            " 'Least_reps' is not supported in format 2 and was migrated to 'random'"
        )

    selection: dict = {
        "strategy": strategy,
        "count": None,
        "sort_field": None,
        "sort_order": "descending",
    }

    raw_count = definition.get("select_card_count")
    if raw_count is None or raw_count == "":
        selection["count"] = 1
    else:
        try:
            count = int(raw_count)
            if count < 0:
                raise ValueError
        except (TypeError, ValueError):
            # Format 1 logged and selected nothing at all; the stage carries the complaint so
            # the executor can log the same thing with the stage attached.
            selection["selection_error"] = (
                f"Error in copy fields: Incorrect 'select_card_count' value '{raw_count}'."
                " Value must be a positive integer or 0"
            )
            selection["strategy"] = "all"
            count = 0
        if count == 0:
            # Zero meant "every match" in format 1.
            selection["strategy"] = "all"
            selection["count"] = None
        else:
            selection["count"] = count

    sort_by_field = definition.get("sort_by_field")
    if sort_by_field and sort_by_field != "-":
        selection["sort_field"] = sort_by_field
        # Format 1 sorted on int(value), falling back to 0, descending.
        selection["sort_numeric"] = True

    if strategy_error:
        # Format 1 checked `select_card_by` before the count, so its complaint is the one
        # that stands. The count stays on the selection: `selection_error` is what makes the
        # stage select nothing, not a missing count.
        selection["strategy"] = "all"
        selection["selection_error"] = strategy_error

    return selection


def _writes_reading_all_notes(definition: dict) -> list[str]:
    """The field and file writes whose regex process interpolated across every source note.

    Only those two kinds are asked. A variable's chain was evaluated against one note in
    every mode -- format 1 passed `notes=[note]` -- so `use_all_notes` on a variable did
    nothing then either, and reporting it would send the user looking for a change that
    never happened.
    """
    named: list[str] = []
    writes = [
        (field_def, field_def.get("copy_into_note_field", ""))
        for field_def in definition.get("field_to_field_defs") or []
    ] + [
        (file_def, file_def.get("copy_into_filename", ""))
        for file_def in definition.get("field_to_file_defs") or []
    ]
    for write, target in writes:
        for process in write.get("process_chain") or []:
            if process.get("name") == LEGACY_REGEX_PROCESS and process.get("use_all_notes"):
                name = target or "an unnamed write"
                if name not in named:
                    named.append(name)
                break
    return named


def _warn_about_reading_all_notes(
    definition: dict, selection: dict, warnings: list[str]
) -> None:
    """Report a `use_all_notes` that the migrated definition cannot honour.

    Format 1 handed the whole source list to the process chain, and a regex process with
    this flag built both its pattern and its replacement out of all of them, joined by
    `regex_separator` and `replacement_separator`. A format-2 stage reads one note, so the
    migrated definition interpolates the trigger note alone: a shorter result, or an empty
    one, with nothing said at runtime.

    Nothing is repaired here, only reported. A config already migrated on a user's machine
    is never migrated again, so a warning is the only thing that can reach those definitions
    at all -- which is why this is worth having whether or not the migrator ever learns to
    build the join itself.

    Only Destination-to-sources asks: it is the one mode whose source list could hold more
    than one note. Within note read a copy of the trigger note, and Source-to-destinations
    read the trigger note, so `len(notes) > 1` was false in both and the flag was already
    dead there.
    """
    if selection.get("selection_error"):
        # Format 1 selected no source notes at all here and said why. There was never a
        # second note for the flag to read, and the refusal is the thing to fix first.
        return
    if selection.get("strategy") != "all" and (selection.get("count") or 0) <= 1:
        # One source note is not "more than one": format 1 took the `[dest_note]` branch.
        return
    targets = _writes_reading_all_notes(definition)
    if not targets:
        return
    warnings.append(
        f"Definition '{definition.get('definition_name', '')}': a regex process with 'use"
        " all notes' built its pattern and replacement out of every source note, which a"
        " format-2 stage cannot do -- it reads one note. Affected:"
        f" {', '.join(targets)}. These now interpolate the trigger note alone; rebuild them"
        " as a loop collecting one value per note and a join."
    )


def _note_query_stage(definition: dict, definition_guid: str, warnings: list[str]) -> Stage:
    run_also_if_no_sources_found = bool(definition.get("run_also_if_no_sources_found", False))
    is_destination_to_sources = (
        definition.get("across_mode_direction") == DIRECTION_DESTINATION_TO_SOURCES
    )
    stage: Stage = {
        "guid": _child_guid(definition_guid, "query"),
        "type": STAGE_NOTE_QUERY,
        "name": "Query notes",
        "enabled": True,
        "result": LEGACY_QUERY_RESULT,
        "query": _legacy_expression(text=definition.get("copy_from_cards_query") or ""),
        "selection": _selection(definition, warnings),
        # Only Destination-to-sources had a source list that could be empty; there, format 1
        # stopped before writing unless `run_also_if_no_sources_found` said otherwise.
        "if_empty": (
            "continue"
            if (run_also_if_no_sources_found or not is_destination_to_sources)
            else "skip_block"
        ),
        "error_if_empty": bool(definition.get("show_error_if_none_found", False)),
        # Format 1's progress label counted the query result as "sources" only when the
        # trigger note was the destination.
        "counts_as_sources": is_destination_to_sources,
    }
    return stage


def _write_file_stages(
    definition: dict,
    source_binding: Optional[str],
    destination_binding: Optional[str] = None,
) -> list[Stage]:
    """Format 1's `field_to_file_defs`, as one `write_file` stage each.

    Both roles have to be written out. `copy_into_single_note` interpolated a filename over
    `notes=[destination_note]` with `dest_note=destination_note`, and ran file code with
    `source_note=` each source note and `dest_note=destination_note` -- so which note stands
    in for each role is a property of the copy mode, not of the file definition, and format 2
    has nowhere to record a mode. `run_write_file` falls back to the source note when there
    is no `legacy_destination`, which is right only when the two really are the same note.
    """
    stages: list[Stage] = []
    for file_def in definition.get("field_to_file_defs") or []:
        use_code = bool(file_def.get("use_code", False))
        stage: Stage = {
            "guid": file_def.get("guid", ""),
            "type": STAGE_WRITE_FILE,
            "name": file_def.get("copy_into_filename", "") or "Write file",
            "enabled": True,
            "filename": _legacy_expression(text=file_def.get("copy_into_filename", "")),
            "content": _legacy_expression(
                text=file_def.get("copy_from_text", ""),
                code=file_def.get("copy_as_code", ""),
                use_code=use_code,
                process_chain=file_def.get("process_chain"),
            ),
            # §11 step 6: migrated file writes overwrite. Format 1's `copy_if_empty` skipped
            # an existing file rather than failing on it, which `overwrite: false` does not
            # mean, so it is carried as its own flag.
            "overwrite": True,
            "skip_if_exists": bool(file_def.get("copy_if_empty", False)),
        }
        if source_binding:
            stage["legacy_source"] = {"binding": source_binding}
        if destination_binding:
            stage["legacy_destination"] = {"binding": destination_binding}
        stages.append(stage)
    return stages


def _edit_note_stage(
    definition: dict,
    guid: str,
    target_binding: str,
    source_binding: Optional[str],
    field_writes: list[dict],
) -> Stage:
    stage: Stage = {
        "guid": guid,
        "type": STAGE_EDIT_NOTE,
        "name": "Edit note",
        "enabled": True,
        "target": {"binding": target_binding},
        "fields": field_writes,
        "tags": _tag_writes(definition),
        "card_actions": _card_actions(definition),
        "read_semantics": "stage_snapshot",
    }
    if source_binding:
        # Format 1 read the right-hand sides from a different note than the one it wrote.
        stage["legacy_source"] = {"binding": source_binding}
    return stage


def _join_stages(
    definition_guid: str,
    index: int,
    expression: dict,
    separator: str,
    gate: Optional[dict] = None,
) -> tuple[list[Stage], str]:
    """The list/loop/store/reduce that stands in for format 1's implicit many-notes join.

    Returns the stages and the name of the result holding the joined text.

    `gate` carries the keys of the write these stages feed that say when it declines to
    write: which unfocus it answers to, and whether it only fills an empty field. Format 1
    read the sources once and then asked those questions per field write, before it
    evaluated anything, so a write that was not going to be applied cost nothing and could
    not fail the run. Here the per-source read has moved in front of the write, so without
    the gate every write's right-hand side is evaluated once per source note whether or not
    the write follows -- and a raising one fails the definition, discarding the writes that
    would have been applied.
    """
    list_name = f"legacy_join_{index}"
    joined_name = f"legacy_joined_{index}"
    gate = dict(gate or {})
    stages: list[Stage] = [
        {
            "guid": _child_guid(definition_guid, f"join-list-{index}"),
            "type": STAGE_LIST_VARIABLE,
            "name": f"Values for join {index}",
            "enabled": True,
            "result": list_name,
            "item_type": TEXT,
            **gate,
        },
        {
            "guid": _child_guid(definition_guid, f"join-loop-{index}"),
            "type": STAGE_FOR_EACH_NOTE,
            "name": "For each found note",
            "enabled": True,
            "input": {"binding": LEGACY_QUERY_RESULT},
            "item_binding": LEGACY_ITEM_BINDING,
            **gate,
            "body": [
                {
                    "guid": _child_guid(definition_guid, f"join-store-{index}"),
                    "type": STAGE_STORE,
                    "name": "Collect value",
                    "enabled": True,
                    "target": {"kind": "list", "binding": list_name},
                    "value": expression,
                    # The value is read from the loop note and the trigger note stands in as
                    # the destination, exactly as format 1's `__Dest__` prefix did.
                    "legacy_source": {"binding": LEGACY_ITEM_BINDING},
                    "legacy_destination": {"binding": "trigger"},
                }
            ],
        },
        {
            "guid": _child_guid(definition_guid, f"join-reduce-{index}"),
            "type": STAGE_REDUCE,
            "name": "Join values",
            "enabled": True,
            "input": {"binding": list_name},
            "result": joined_name,
            "operation": "join",
            "separator": separator,
            "initial": value_expression(text=""),
            "item_binding": "item",
            "accumulator_binding": "accumulator",
            "value": value_expression(text=""),
            **gate,
        },
    ]
    return stages, joined_name


def _within_note_stages(definition: dict, definition_guid: str) -> list[Stage]:
    stages: list[Stage] = []
    field_writes = _field_writes(definition, modifies_other_notes=False)
    stages.append(
        _edit_note_stage(
            definition,
            guid=_child_guid(definition_guid, "edit-trigger"),
            target_binding="trigger",
            # Within note reads and writes the same note, so the stage's own entry snapshot
            # is the source: that is what made swapping two fields work in format 1.
            source_binding=None,
            field_writes=field_writes,
        )
    )
    stages.extend(_write_file_stages(definition, source_binding=None))
    return stages


def _source_to_destinations_stages(
    definition: dict, definition_guid: str, warnings: list[str]
) -> list[Stage]:
    body: list[Stage] = [
        _edit_note_stage(
            definition,
            guid=_child_guid(definition_guid, "edit-each"),
            target_binding=LEGACY_ITEM_BINDING,
            source_binding="trigger",
            field_writes=_field_writes(definition, modifies_other_notes=True),
        )
    ]
    # The loop note is the destination here, so it names the file and answers `__Dest__`;
    # the trigger is the source the content is read from.
    body.extend(
        _write_file_stages(
            definition,
            source_binding="trigger",
            destination_binding=LEGACY_ITEM_BINDING,
        )
    )
    return [
        _note_query_stage(definition, definition_guid, warnings),
        {
            "guid": _child_guid(definition_guid, "loop"),
            "type": STAGE_FOR_EACH_NOTE,
            "name": "For each found note",
            "enabled": True,
            "input": {"binding": LEGACY_QUERY_RESULT},
            "item_binding": LEGACY_ITEM_BINDING,
            "body": body,
        },
    ]


def _destination_to_sources_stages(
    definition: dict, definition_guid: str, warnings: list[str]
) -> list[Stage]:
    separator = definition.get("select_card_separator")
    if separator is None:
        separator = DEFAULT_SELECT_CARD_SEPARATOR
    stages: list[Stage] = [_note_query_stage(definition, definition_guid, warnings)]
    _warn_about_reading_all_notes(definition, stages[0].get("selection") or {}, warnings)

    join_index = 0
    field_writes = _field_writes(definition, modifies_other_notes=False)
    for write in field_writes:
        join_index += 1
        # The process chain ran once, on the joined text, so it stays on the write rather
        # than moving into the loop body that produces one value per source note.
        process_chain = list(write["value"].get("process_chain") or [])
        per_note_value = dict(write["value"], process_chain=[])
        join_stages, joined_name = _join_stages(
            definition_guid,
            join_index,
            per_note_value,
            separator,
            gate=_write_gate(write),
        )
        stages.extend(join_stages)
        write["value"] = value_expression(
            text="{{" + joined_name + "}}", process_chain=process_chain
        )

    stages.append(
        _edit_note_stage(
            definition,
            guid=_child_guid(definition_guid, "edit-trigger"),
            target_binding="trigger",
            source_binding=None,
            field_writes=field_writes,
        )
    )

    for file_stage in _write_file_stages(definition, source_binding=None):
        content = file_stage["content"]
        if content.get("mode") == MODE_CODE:
            # The code path ran once per source note and wrote every (filename, content)
            # pair it returned, so it becomes a write inside a loop over those notes.
            stages.append({
                "guid": _child_guid(definition_guid, f"file-loop-{file_stage['guid']}"),
                "type": STAGE_FOR_EACH_NOTE,
                "name": "For each found note",
                "enabled": True,
                "input": {"binding": LEGACY_QUERY_RESULT},
                "item_binding": LEGACY_ITEM_BINDING,
                "body": [
                    dict(
                        file_stage,
                        # The same pair the join's `store` stage above carries, for the same
                        # reason: the value is read from the loop note and the trigger note
                        # stands in as the destination, as format 1's `__Dest__` prefix did.
                        legacy_source={"binding": LEGACY_ITEM_BINDING},
                        legacy_destination={"binding": "trigger"},
                    )
                ],
            })
            continue
        join_index += 1
        process_chain = list(content.get("process_chain") or [])
        join_stages, joined_name = _join_stages(
            definition_guid, join_index, dict(content, process_chain=[]), separator
        )
        stages.extend(join_stages)
        file_stage["content"] = value_expression(
            text="{{" + joined_name + "}}", process_chain=process_chain
        )
        stages.append(file_stage)

    return stages


def _triggers(definition: dict) -> dict:
    edit_fields: list[str] = []
    add_fields: list[str] = []
    modifies_other_notes = _modifies_other_notes(definition)
    for field_def in definition.get("field_to_field_defs") or []:
        fields = _unfocus_trigger_fields(field_def, modifies_other_notes)
        if field_def.get("copy_on_unfocus_when_edit", False):
            edit_fields.extend(name for name in fields if name and name not in edit_fields)
        if field_def.get("copy_on_unfocus_when_add", False):
            add_fields.extend(name for name in fields if name and name not in add_fields)
    return {
        "note_types": _split_quoted_list(definition.get("copy_into_note_types")),
        "deck_names": (
            []
            if definition.get("only_copy_into_decks") in (None, "", "-")
            else _split_quoted_list(definition.get("only_copy_into_decks"))
        ),
        "include_subdecks": bool(definition.get("include_subdecks", False)),
        "on_sync": bool(definition.get("copy_on_sync", False)),
        "on_add": bool(definition.get("copy_on_add", False)),
        "on_review": bool(definition.get("copy_on_review", False)),
        "on_unfocus": {"edit_fields": edit_fields, "add_fields": add_fields},
    }


class MigrationError(ValueError):
    """A format-1 definition that cannot be expressed as stages at all.

    The two cases are a missing copy mode and a missing or unrecognised across-note
    direction: format 1 refused to run those too, with these same messages.
    """


def migrate_definition_v1_to_v2(
    definition: dict,
    new_guid: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> CopyDefinitionV2:
    """Convert one format-1 definition into its format-2 equivalent.

    :param definition: the stored format-1 definition. It is not mutated.
    :param new_guid: injected so tests get deterministic guids. Only used when the input
        definition has no guid of its own; every synthesized stage guid is derived from the
        definition guid instead.
    :raises MigrationError: when the definition names no copy mode, or names an across-note
        direction that format 1 would also have refused.

    What comes back is in the current syntax throughout: the stages are promoted on the way
    out, so no expression the executor is ever handed carries a `syntax_version`.
    """
    if is_format_2(definition):
        # Promotion, not a plain copy: a definition a 0.3.0 start already staged is format 2
        # and still speaks format 1 inside its expressions, and every caller here wants a
        # definition the executor can run.
        return promote_definition(definition)  # type: ignore[arg-type]

    definition = deepcopy(definition)
    definition_guid = definition.get("guid") or new_guid()
    copy_mode = definition.get("copy_mode")
    across_mode_direction = definition.get("across_mode_direction")
    warnings: list[str] = []

    if copy_mode == COPY_MODE_WITHIN_NOTE:
        body = _within_note_stages(definition, definition_guid)
    elif copy_mode == COPY_MODE_ACROSS_NOTES:
        if across_mode_direction == DIRECTION_SOURCE_TO_DESTINATIONS:
            body = _source_to_destinations_stages(definition, definition_guid, warnings)
        elif across_mode_direction == DIRECTION_DESTINATION_TO_SOURCES:
            body = _destination_to_sources_stages(definition, definition_guid, warnings)
        else:
            raise MigrationError("Error in copy fields: missing across mode direction value")
    else:
        raise MigrationError("Error in copy fields: missing copy mode value")

    # Variables come first and stay in their stored order: format 1 computed them all before
    # anything else, including before the condition.
    stages: list[Stage] = []
    for variable_def in definition.get("field_to_variable_defs") or []:
        stages.append({
            "guid": variable_def.get("guid", ""),
            "type": STAGE_VARIABLE,
            "name": variable_def.get("copy_into_variable", ""),
            "enabled": True,
            "result": variable_def.get("copy_into_variable", ""),
            "value": _legacy_expression(
                text=variable_def.get("copy_from_text", ""),
                code=variable_def.get("copy_as_code", ""),
                use_code=variable_def.get("use_code", False),
                process_chain=variable_def.get("process_chain"),
                # Format 1 evaluated every variable against the trigger note alone; none of
                # them could see the ones declared before it.
                legacy_isolated_variables=True,
            ),
        })

    condition_query = definition.get("copy_condition_query")
    if condition_query:
        stages.append({
            "guid": _child_guid(definition_guid, "condition"),
            "type": STAGE_CONDITION,
            "name": "Copy condition",
            "enabled": True,
            "predicate": _legacy_expression(text=condition_query),
            # The predicate is an Anki search scoped to the trigger note, which is how
            # format 1 ran it: `f"{query} nid:{trigger.id}"`.
            "predicate_kind": "note_query",
            "predicate_target": {"binding": "trigger"},
            # Format 1 skipped the whole trigger note when the condition did not match,
            # rather than carrying on with the stages after it. This stage wraps the entire
            # definition, so there is nothing before it to discard.
            "unmatched_skips_trigger": True,
            "only_on_sync": bool(definition.get("condition_only_on_sync", False)),
            "then": body,
            "else": [],
        })
    else:
        stages.extend(body)

    migrated: CopyDefinitionV2 = {
        "guid": definition_guid,
        "format_version": FORMAT_VERSION,
        "migrated_from_format": 1,
        "definition_name": definition.get("definition_name", ""),
        "triggers": _triggers(definition),
        "stages": stages,
        "exports": [],
        # Derived, never authored. `copy_fields` fills this in from the flow analyser; the
        # migrator leaves it out rather than writing a guess that would be trusted.
        "legacy": {
            # Format 1 counted the trigger note itself as the one source in every mode but
            # Destination-to-sources, where the query result was the source list.
            "trigger_is_source": across_mode_direction != DIRECTION_DESTINATION_TO_SOURCES,
            "select_card_separator": definition.get("select_card_separator"),
            "query_note_index_default": (
                1 if copy_mode == COPY_MODE_WITHIN_NOTE else None
            ),
        },
    }  # type: ignore[typeddict-unknown-key]
    if warnings:
        migrated["migration_warnings"] = warnings  # type: ignore[typeddict-unknown-key]
    # Format-1 syntax does not outlive the migration. The stages above record which note
    # each bare reference meant, in `legacy_source` / `legacy_destination`; promotion spends
    # that record by writing the binding into the reference itself, so what comes out has
    # one syntax and the executor one way to resolve it (§11).
    return promote_definition(migrated)


def migrate_definitions(
    definitions: list[dict],
    new_guid: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> tuple[list[CopyDefinitionV2], list[str]]:
    """Migrate a whole config's worth of definitions, collecting every complaint.

    A definition that cannot be migrated is reported and left out, so one broken definition
    does not stop the rest of the config from being usable.
    """
    migrated: list[CopyDefinitionV2] = []
    problems: list[str] = []
    for index, definition in enumerate(definitions or []):
        if not isinstance(definition, dict):
            problems.append(
                f"copy definition {index + 1} is a {type(definition).__name__},"
                " not a definition, and was left out"
            )
            continue
        try:
            result = migrate_definition_v1_to_v2(definition, new_guid=new_guid)
        except MigrationError as error:
            problems.append(f"'{definition.get('definition_name', '')}': {error}")
            continue
        except Exception as error:  # noqa: BLE001 -- see below
            # `MigrationError` is what the migrator raises on purpose, for the two things
            # format 1 also refused. Everything else reaching here means the stored entry is
            # not shaped like a definition at all -- a list-shaped key holding a dict, a
            # field write that is a bare string -- which `.get` and `deepcopy` report as
            # `AttributeError` or `TypeError`.
            #
            # Letting those through would not be a tidier traceback but a dead addon:
            # `migrate_config()` runs at import time from `__init__.py`, so the exception
            # escapes into Anki's addon loader and nothing loads -- no browser action, no
            # hooks, and no editor left to repair the entry with. The config is
            # hand-editable JSON and is written by older versions of this addon, so a
            # malformed entry is reachable without anything else going wrong, and every
            # later start hits it again. Reporting it keeps the promise this function is
            # here to make: one broken definition does not stop the rest of the config.
            problems.append(
                f"'{definition.get('definition_name', '')}' could not be read"
                f" ({type(error).__name__}: {error}) and was left out"
            )
            continue
        problems.extend(result.get("migration_warnings", []))  # type: ignore[arg-type]
        migrated.append(result)
    return migrated, problems


# --------------------------------------------------------------------------------------
# Promotion: format-1 syntax rewritten as format-2 syntax
# --------------------------------------------------------------------------------------
#
# Migration makes the stages explicit; promotion makes the *references* explicit, so that
# nothing carries format-1 syntax into the executor. `{{Word}}` becomes `{{trigger.Word}}`
# and `{{__Dest__Word}}` becomes `{{note.Word}}`, naming whichever binding stood in for each
# of format 1's two roles where that reference was read -- which `_note_roles` answers per
# stage key, because the executor does. What a promoted expression computes is what the
# format-1 interpolation computed, with one deliberate difference: a reference that resolves
# to nothing is a stage error rather than a silent empty string.

#: A cloze marker is not a reference, so `{{c1::...}}` is left as it is and its content is
#: promoted on its own. Spelled the way the flow analyser spells it.
_CLOZE_REFERENCE_RE = re.compile(r"^c\d+::")

#: The runtime values a bare `{{__Name}}` still resolves to after promotion. They are not
#: syntax -- the executor supplies them and the interpolation menu keeps offering them --
#: so they are left unqualified.
RUNTIME_VALUE_NAMES = (TARGET_NOTES_COUNT, QUERY_NOTE_INDEX)

_RUNTIME_VALUES_BY_LOWER = {name.lower(): name for name in RUNTIME_VALUE_NAMES}

#: The expression-valued keys of each stage type, read off the stage shapes in
#: `definition_schema`. `edit_note` is absent because its expressions live one level down,
#: on each field write; its tags are plain strings and its card actions carry Python code
#: that is executed rather than interpolated, so neither holds a reference to promote.
STAGE_EXPRESSION_KEYS: dict[str, tuple[str, ...]] = {
    STAGE_VARIABLE: ("value",),
    STAGE_NOTE_QUERY: ("query",),
    STAGE_CARD_QUERY: ("query",),
    STAGE_READ_FILE: ("filename",),
    STAGE_WRITE_FILE: ("filename", "content"),
    STAGE_STORE: ("value",),
    STAGE_REDUCE: ("value", "initial"),
    STAGE_CONDITION: ("predicate",),
}


def _promote_reference(
    reference: str,
    source: str,
    destination: str,
    known: dict[str, str],
    binding_heads: frozenset,
) -> str:
    """One `{{...}}` reference, rewritten. `reference` is what stood between the braces."""
    if _CLOZE_REFERENCE_RE.match(reference):
        return intr_format(reference)
    binding = known.get(reference.lower())
    if binding is not None:
        # A binding the migrator named. Format 1 matched names case-insensitively and
        # format 2 resolves a binding exactly, so the promoted reference carries the
        # binding's own spelling rather than the user's.
        return intr_format(binding)
    head, dot, _rest = reference.partition(".")
    if dot and head in binding_heads:
        # Already qualified. Nothing in format 1 could write this, but promoting it again
        # would read the binding as a field name of itself.
        return intr_format(reference)
    if reference.startswith(DESTINATION_PREFIX):
        # `interpolate_from_text` matched this prefix exactly, so a differently-cased one
        # was a field name there and stays one here.
        return intr_format(f"{destination}.{reference[len(DESTINATION_PREFIX):]}")
    runtime_value = _RUNTIME_VALUES_BY_LOWER.get(reference.lower())
    if runtime_value is not None:
        return intr_format(runtime_value)
    # Anything else names something on the source note: a field, a note value
    # (`__Note_Tags`) or a card value (`Recognition__Card_Due`). Once qualified, all three
    # are resolved by the same interpolation, against the note the binding holds. The
    # user's spelling is kept: field matching is case-insensitive on that side too.
    return intr_format(f"{source}.{reference}")


def _promote_text(
    text: str,
    source: str,
    destination: str,
    known: dict[str, str],
    binding_heads: frozenset,
) -> str:
    if not text:
        return text or ""
    # Cloze markers are not references. Their content is, so it is promoted on its own and
    # the marker put back around the result -- the shape `resolve_references` uses.
    placeholders: dict[str, str] = {}
    for index, (start, end, cloze_num, content) in enumerate(
        reversed(extract_cloze_patterns(text))
    ):
        placeholder = f"\x00CLOZE{index}\x00"
        promoted = _promote_text(content, source, destination, known, binding_heads)
        placeholders[placeholder] = f"{{{{c{cloze_num}::{promoted}}}}}"
        text = text[:start] + placeholder + text[end:]

    text = FROM_TEXT_FIELD_REGEX.sub(
        lambda match: _promote_reference(
            match.group(1), source, destination, known, binding_heads
        ),
        text,
    )
    for placeholder, cloze in placeholders.items():
        text = text.replace(placeholder, cloze)
    return text


def promote_expression(
    expression: ValueExpression,
    source: str = "trigger",
    destination: Optional[str] = None,
    known_names: Optional[Iterable[str]] = None,
) -> ValueExpression:
    """One format-1 expression rewritten into format-2 syntax.

    :param expression: the expression. It is not mutated; an expression that is already in
        the current syntax is returned as a copy, unchanged, so this is idempotent.
    :param source: the binding whose fields an unqualified name means
    :param destination: the binding a `__Dest__` name means. `None` says format 1 had no
        destination note for this stage, and the prefix collapses onto `source` -- which is
        the note `interpolate_from_text` read with no destination in hand, minus the silent
        empty it gave for the prefix it then could not strip.
    :param known_names: the bare names that are bindings rather than fields of the source
        note (see `promote_definition`), matched case-insensitively as format 1 matched
        every name
    """
    if not isinstance(expression, dict):
        return expression
    if not expression_is_legacy_syntax(expression):
        return deepcopy(expression)

    promoted = deepcopy(expression)
    promoted.pop("syntax_version", None)
    # Isolation was about which variables a format-1 expression could see through the
    # legacy interpolation; a promoted expression names its bindings, so there is nothing
    # left for the flag to hide.
    promoted.pop("legacy_isolated_variables", None)

    known = {name.lower(): name for name in known_names or () if name}
    binding_heads = frozenset(
        [name for name in known.values()] + [source, destination or source, "trigger"]
    )
    for key in ("text", "code"):
        if promoted.get(key):
            promoted[key] = _promote_text(  # type: ignore[typeddict-item]
                promoted[key], source, destination or source, known, binding_heads
            )
    return promoted


def _binding_of(ref: Any) -> Optional[str]:
    if isinstance(ref, dict) and isinstance(ref.get("binding"), str) and ref["binding"]:
        return ref["binding"]
    return None


def _note_roles(stage: Stage, key: str) -> tuple[str, str]:
    """Which binding an unqualified name, and a `__Dest__` name, read for one stage key.

    This mirrors the contexts the executor builds -- every `make_context` call site in
    `actions.py` and `evaluator.py` -- because that is where format 1's two roles ended up.
    The roles are per *key*, not per stage: `copy_into_single_note` interpolated a file's
    name over the destination note and its content over each source note, and an Edit Note
    read `__Dest__` off the note it was writing however far away the values came from. A
    single pair for the whole stage would quietly move one of them onto the other's note.
    """
    stage_type = stage.get("type", "")
    source = _binding_of(stage.get("legacy_source"))
    destination = _binding_of(stage.get("legacy_destination"))
    if stage_type == STAGE_EDIT_NOTE:
        # Every right-hand side reads the target's entry snapshot, which the stage binds
        # under the target's own name, and `__Dest__` meant the note being written.
        target = _binding_of(stage.get("target")) or "trigger"
        return source or target, target
    if stage_type == STAGE_WRITE_FILE and key == "filename":
        # The name was interpolated over the destination note in both roles.
        name = destination or source or "trigger"
        return name, name
    if stage_type == STAGE_WRITE_FILE:
        # The content's destination falls back to its source: one note in both roles is
        # what a definition with no destination of its own had.
        return source or "trigger", destination or source or "trigger"
    if stage_type == STAGE_STORE:
        return source or "trigger", destination or "trigger"
    if stage_type == STAGE_CONDITION and stage.get("predicate_kind") == "note_query":
        # A migrated copy condition is a search run against one note, which is the note it
        # reads too. Format 1 passed no destination note here at all, so `__Dest__` was an
        # invalid field; it collapses onto the same note rather than staying empty.
        target = _binding_of(stage.get("predicate_target")) or "trigger"
        return target, target
    # A variable, a query, a read, a reduce and a newly authored predicate all read the
    # trigger note: format 1 computed them before it had any other note in hand.
    return "trigger", "trigger"


def promote_stage(stage: Stage, known_names: Optional[Iterable[str]] = None) -> Stage:
    """One stage with every expression it holds promoted, and its legacy markers dropped.

    The stage's `legacy_source` / `legacy_destination` are what say which note format 1
    read an unqualified name and a `__Dest__` name from; once the references name those
    bindings themselves, the two keys have nothing left to say and are removed.
    """
    if not isinstance(stage, dict):
        return stage
    promoted = deepcopy(stage)

    for key in STAGE_EXPRESSION_KEYS.get(promoted.get("type", ""), ()):
        if key in promoted:
            source, destination = _note_roles(promoted, key)
            promoted[key] = promote_expression(  # type: ignore[literal-required]
                promoted[key], source, destination, known_names  # type: ignore[literal-required]
            )
    if promoted.get("type") == STAGE_EDIT_NOTE:
        source, destination = _note_roles(promoted, "value")
        for field_write in promoted.get("fields") or []:
            if isinstance(field_write, dict) and "value" in field_write:
                field_write["value"] = promote_expression(
                    field_write["value"], source, destination, known_names
                )
    for key, block in stage_body_blocks(promoted):
        if key in promoted:
            promoted[key] = [  # type: ignore[literal-required]
                promote_stage(child, known_names) for child in block
            ]
    promoted.pop("legacy_source", None)
    promoted.pop("legacy_destination", None)
    return promoted


def _known_names(definition: Any) -> list[str]:
    """The bare names a promoted expression keeps as they are: the bindings with names.

    Every stage result -- a variable's, a query's, a synthesized join's -- plus the export
    names. The loop and reduce bindings (`note`, `item`, `accumulator`) are deliberately
    left out although they are bindings too: format 1 had no way to name them, so a bare
    `{{Note}}` in a migrated expression is the field `Note`, which is what it has always
    meant, and reading it as the loop's note would turn a common field name into a stage
    error in every migrated across-notes definition.

    A name that is both -- a variable called `Word` on a note type that also has a field
    `Word` -- resolves to the *binding* afterwards, which reverses format 1: it asked the
    note first and fell back to its variables, so the field won. The reversal is deliberate
    and not checked for. Format 2 has no shadowing at all, so there is no promoted spelling
    that could mean the field while the variable is in scope, and a definition with such a
    name would have to be rewritten by hand either way (§11).
    """
    names: list[str] = []
    for stage in walk_stages(definition.get("stages") or []):
        names.extend(stage_result_names(stage))
    for export in definition.get("exports") or []:
        if isinstance(export, dict) and isinstance(export.get("name"), str):
            names.append(export["name"])
    return [name for name in names if name]


def promote_definition(definition: CopyDefinitionV2) -> CopyDefinitionV2:
    """A whole definition with no format-1 syntax left in it.

    Idempotent: a definition whose expressions are already in the current syntax comes back
    as a copy of itself.
    """
    if not isinstance(definition, dict):
        return definition
    promoted = deepcopy(definition)
    stages = promoted.get("stages")
    if isinstance(stages, list):
        known_names = _known_names(promoted)
        promoted["stages"] = [promote_stage(stage, known_names) for stage in stages]
    return promoted


__all__ = [
    "CARD_TYPE_SEPARATOR",
    "DEFAULT_SELECT_CARD_SEPARATOR",
    "LEGACY_ITEM_BINDING",
    "LEGACY_QUERY_RESULT",
    "RUNTIME_VALUE_NAMES",
    "STAGE_EXPRESSION_KEYS",
    "MigrationError",
    "migrate_definition_v1_to_v2",
    "migrate_definitions",
    "promote_definition",
    "promote_expression",
    "promote_stage",
]
