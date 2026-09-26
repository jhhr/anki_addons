"""Format-2 copy definitions: the types, the discriminators, and structural validation.

A format-2 definition is trigger metadata plus an ordered list of *stages*. A stage is one
executable node; a structural stage (loop, reduce, condition) carries child blocks. Stages
that produce a value name it, and later stages in scope may consume it. That replaces the
format-1 model, where one definition-wide mode and direction decided what the fixed
variable/query/field/tag/file/card slots meant.

This module is deliberately pure: it imports nothing from Anki, `aqt`, or `configuration`,
so the schema, the migrator and the flow analyser can all be tested without a collection.
Process-chain entries and card-action dictionaries pass through as opaque dicts; they keep
the format-1 shapes that `logic/copy_fields.py` already knows how to run.

What lives here is *shape*: is this a stage type we know, are its required keys present,
is that a legal result name. What does not live here is *meaning*: whether a referenced
binding exists, whether its type fits the action, which effects a definition has. That is
`flow_analysis.py`, which needs the whole block to answer.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal, Optional, Sequence, TypedDict, cast

from typing_extensions import TypeGuard

# The per-definition format version. The global config version stays for config-wide
# migrations; this one says how to read a single definition.
FORMAT_VERSION = 2

CARD_TYPE_SEPARATOR = "<::>"


# --------------------------------------------------------------------------------------
# Runtime value types
# --------------------------------------------------------------------------------------

TEXT = "Text"
NUMBER = "Number"
BOOLEAN = "Boolean"
NOTE_REF = "NoteRef"
NOTE_LIST = "NoteList"
CARD_REF = "CardRef"
CARD_LIST = "CardList"
LIST = "List"
NULL = "Null"
UNKNOWN = "Unknown"

#: The types whose values interpolation is allowed to stringify (§4.1).
SCALAR_TYPE_NAMES = (TEXT, NUMBER, BOOLEAN)

#: `{{card.<name>}}` properties, resolved on the card facade rather than through the
#: `<template>__Card_*` keys format 1 used. The same names code reads off a card, which are
#: the ones format 1's code saw, so converted code keeps working.
CARD_PROPERTY_NAMES = frozenset({
    "id",
    "nid",
    "did",
    "odid",
    "deck_id",
    "deck_name",
    "original_deck_name",
    "ord",
    "template_name",
    "type",
    "queue",
    "due",
    "odue",
    "ivl",
    "factor",
    "ease",
    "reps",
    "lapses",
    "left",
    "flag",
    "custom_data",
    "desired_retention",
    "stability",
    "difficulty",
    "mod",
    "suspended",
    "buried",
    "created",
    "first_review_time",
    "latest_review_time",
    "average_review_time",
    "total_review_time",
})


class ValueType:
    """A runtime value type. `List` carries its item type; every other kind is atomic."""

    __slots__ = ("kind", "item")

    def __init__(self, kind: str, item: Optional["ValueType"] = None) -> None:
        self.kind = kind
        self.item = item

    @property
    def name(self) -> str:
        if self.kind == LIST:
            return f"List[{self.item.name if self.item else UNKNOWN}]"
        return self.kind

    @property
    def is_scalar(self) -> bool:
        """True when interpolation may stringify a value of this type."""
        return self.kind in SCALAR_TYPE_NAMES

    @property
    def is_listy(self) -> bool:
        return self.kind in (LIST, NOTE_LIST, CARD_LIST)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ValueType):
            return NotImplemented
        return self.kind == other.kind and self.item == other.item

    def __hash__(self) -> int:
        return hash((self.kind, self.item))

    def __repr__(self) -> str:
        return f"ValueType({self.name})"


T_TEXT = ValueType(TEXT)
T_NUMBER = ValueType(NUMBER)
T_BOOLEAN = ValueType(BOOLEAN)
T_NOTE = ValueType(NOTE_REF)
T_NOTE_LIST = ValueType(NOTE_LIST)
T_CARD = ValueType(CARD_REF)
T_CARD_LIST = ValueType(CARD_LIST)
T_NULL = ValueType(NULL)
T_UNKNOWN = ValueType(UNKNOWN)

#: Item types a `list_variable` stage may declare.
LIST_ITEM_TYPE_NAMES = (TEXT, NUMBER, BOOLEAN, NOTE_REF, CARD_REF)


def list_of(item: ValueType) -> ValueType:
    return ValueType(LIST, item)


def value_type_from_name(name: str) -> ValueType:
    """Parse a persisted type name. Unknown names become `Unknown` rather than raising:
    the stage validator reports them with the stage guid attached, which a bare raise
    could not."""
    if name in (TEXT, NUMBER, BOOLEAN, NOTE_REF, NOTE_LIST, CARD_REF, CARD_LIST, NULL):
        return ValueType(name)
    match = re.fullmatch(r"List\[(.+)\]", name or "")
    if match:
        return list_of(value_type_from_name(match.group(1)))
    return T_UNKNOWN


# --------------------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------------------

RESULT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Names the runtime binds itself, so a result may not take them (§4.2).
RESERVED_BINDING_NAMES = frozenset({
    "trigger",
    "note",
    "card",
    "item",
    "index",
    "count",
    "accumulator",
    "find_notes",
    "find_cards",
    "get_note",
    "get_card",
})


def is_valid_result_name(name: Any) -> bool:
    return isinstance(name, str) and bool(RESULT_NAME_RE.match(name)) and not name.startswith("__")


def names_are_relaxed(definition: Any) -> bool:
    """Whether `definition` keeps format-1 variable names, which were free text.

    The identifier rule applies to newly authored definitions only. The structural validator
    and the analyser both run on a save, so they have to agree on this or the exemption
    holds in neither and a migrated definition cannot even be opened to rename the variable.
    """
    return isinstance(definition, dict) and definition.get("migrated_from_format") == 1


#: How an unclosed cloze starts once the match has taken off its `{{`.
_UNCLOSED_CLOZE_RE = re.compile(r"^c(\d+)::")


def unclosed_reference_problem(reference: str) -> Optional[str]:
    """What is wrong with a `{{...}}` reference that holds another `{{`, or None.

    A reference runs up to the first `}}`, so a `{{` that is never closed swallows the
    reference after it: `{{c1::abc {{trigger.Word}}` is matched as one reference named
    `c1::abc {{trigger.Word`, and splitting that on its dot names no binding anyone wrote.
    A closed cloze never gets here; it is taken apart before references are read. The
    phrase follows what the caller says it is checking: "field Note has ...".
    """
    if "{{" not in reference:
        return None
    cloze = _UNCLOSED_CLOZE_RE.match(reference)
    if cloze:
        return f"has a cloze '{{{{c{cloze.group(1)}::' that is never closed"
    before = reference[: reference.index("{{")].strip()
    return f"has a '{{{{' that is never closed, before '{before}'"


def result_name_problem(
    name: Any,
    what: str = "Result name",
    relaxed: bool = False,
    allowed_reserved: Iterable[str] = (),
) -> Optional[str]:
    """The reason `name` cannot be a result name, or None when it can.

    `relaxed` waives the identifier rule (see `names_are_relaxed`); a missing name, the
    reserved names and the `__` prefix the note and card values live under are refused
    regardless, because the runtime owns those.
    """
    if not isinstance(name, str) or not name:
        return f"{what} is missing"
    if name.startswith("__"):
        return f"{what} '{name}' may not begin with '__'"
    if not relaxed and not RESULT_NAME_RE.match(name):
        return (
            f"{what} '{name}' is not an identifier"
            " (letters, digits and underscore, not starting with a digit)"
        )
    if name in RESERVED_BINDING_NAMES and name not in allowed_reserved:
        return f"{what} '{name}' is a reserved binding name"
    return None


# --------------------------------------------------------------------------------------
# Value expressions
# --------------------------------------------------------------------------------------

MODE_TEXT: Literal["text"] = "text"
MODE_CODE: Literal["code"] = "code"


class ValueExpression(TypedDict, total=False):
    mode: Literal["text", "code"]
    text: str
    code: str
    process_chain: Sequence[dict]
    # Migration bookkeeping that `promote_expression` strips before a definition leaves the
    # migrator, and still reads on an expression a 0.3.0 start stored unpromoted.
    syntax_version: int
    legacy_isolated_variables: bool


def value_expression(
    text: str = "",
    code: str = "",
    mode: Optional[str] = None,
    process_chain: Optional[Sequence[dict]] = None,
    **extra: Any,
) -> ValueExpression:
    expression: ValueExpression = {
        "mode": MODE_CODE if (mode == MODE_CODE or (mode is None and code)) else MODE_TEXT,
        "text": text or "",
        "code": code or "",
        "process_chain": list(process_chain or []),
    }
    expression.update(extra)  # type: ignore[typeddict-item]
    return expression


def expression_is_code(expression: Optional[ValueExpression]) -> bool:
    return expression is not None and expression.get("mode") == MODE_CODE


def expression_source(expression: ValueExpression) -> str:
    """The text this expression interpolates: its code in code mode, its text otherwise."""
    if expression_is_code(expression):
        return expression.get("code", "") or ""
    return expression.get("text", "") or ""


# --------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------

STAGE_VARIABLE = "variable"
STAGE_NOTE_QUERY = "note_query"
STAGE_CARD_QUERY = "card_query"
STAGE_SELECT_NOTE = "select_note"
STAGE_SELECT_CARD = "select_card"
STAGE_EDIT_NOTE = "edit_note"
STAGE_EDIT_CARD = "edit_card"
STAGE_READ_FILE = "read_file"
STAGE_WRITE_FILE = "write_file"
STAGE_LIST_VARIABLE = "list_variable"
STAGE_STORE = "store"
STAGE_FOR_EACH_NOTE = "for_each_note"
STAGE_FOR_EACH_CARD = "for_each_card"
STAGE_REDUCE = "reduce"
STAGE_CONDITION = "condition"
STAGE_CALL_DEFINITION = "call_definition"

ALL_STAGE_TYPES = (
    STAGE_VARIABLE,
    STAGE_NOTE_QUERY,
    STAGE_CARD_QUERY,
    STAGE_SELECT_NOTE,
    STAGE_SELECT_CARD,
    STAGE_EDIT_NOTE,
    STAGE_EDIT_CARD,
    STAGE_READ_FILE,
    STAGE_WRITE_FILE,
    STAGE_LIST_VARIABLE,
    STAGE_STORE,
    STAGE_FOR_EACH_NOTE,
    STAGE_FOR_EACH_CARD,
    STAGE_REDUCE,
    STAGE_CONDITION,
    STAGE_CALL_DEFINITION,
)

#: The stages that pick one item of a list by index: a note of a NoteList, a card of a
#: CardList.
SELECT_STAGE_TYPES = (STAGE_SELECT_NOTE, STAGE_SELECT_CARD)

#: The reserved name each select stage may still call its result. What it binds is one
#: item, the way a loop's item is, and a loop may call its item `note` or `card`; a migrated
#: Destination to sources definition that reads one source note depends on `note` meaning
#: that note.
SELECT_RESERVED_NAMES = {
    STAGE_SELECT_NOTE: frozenset({"note"}),
    STAGE_SELECT_CARD: frozenset({"card"}),
}

#: The name a select stage's index code gets its input list under, besides the list's own
#: name, so that code can be written without knowing what the query was called. For cards
#: it is `cards`, which in code otherwise means the note's cards: here it is the list
#: being picked from, as any binding of that name would be.
SELECT_LIST_NAMES = {STAGE_SELECT_NOTE: "notes", STAGE_SELECT_CARD: "cards"}

#: Stages that contain child blocks, mapped to the keys those blocks live under.
STRUCTURAL_STAGE_BODY_KEYS: dict[str, tuple[str, ...]] = {
    STAGE_FOR_EACH_NOTE: ("body",),
    STAGE_FOR_EACH_CARD: ("body",),
    STAGE_CONDITION: ("then", "else"),
}

SELECTION_STRATEGIES = ("all", "first", "random")
IF_EMPTY_POLICIES = ("continue", "skip_block", "error")
IF_MISSING_POLICIES = ("empty", "skip_block", "error")
WRITE_IF_POLICIES = ("always", "empty")
READ_SEMANTICS = ("stage_snapshot",)
SORT_ORDERS = ("ascending", "descending")


class Selection(TypedDict, total=False):
    strategy: Literal["all", "first", "random"]
    count: Optional[int]
    sort_field: Optional[str]
    sort_order: Literal["ascending", "descending"]
    # Migration compatibility: format 1 sorted on int(field value), falling back to 0.
    sort_numeric: bool
    # Migration compatibility: a `select_card_count` format 1 refused to parse. The stage
    # reports it and selects nothing, which is what format 1 did.
    selection_error: str


class BindingRef(TypedDict, total=False):
    binding: str
    # Only a `store` target has one: "list", naming the list the value is appended to.
    kind: str


class FieldWrite(TypedDict, total=False):
    # Only a migrated write has one: the format-1 field definition's own guid, kept.
    guid: str
    field: str
    value: ValueExpression
    write_if: Literal["always", "empty"]
    # Migrated unfocus metadata: which editor fields may trigger this write (§11 step 7).
    unfocus_trigger_fields: list[str]
    unfocus_when_edit: bool
    unfocus_when_add: bool


class TagWrites(TypedDict, total=False):
    add: list[str]
    remove: list[str]


class CallOutput(TypedDict, total=False):
    export: str
    result: str


# Every key any stage type can carry, each optional: which of them a stage has depends on its
# `type`, and `validate_stage_structure` is what checks that the right ones are there. Declared
# as one shape rather than one per type so that code reading a stage of a known type can use
# its keys without a cast at every access. Functional syntax because a condition's branch is
# called `else`.
Stage = TypedDict(
    "Stage",
    {
        # Every stage.
        "guid": str,
        "type": str,
        "name": str,
        "enabled": bool,
        # The single result a variable, query, read_file, list_variable or reduce binds.
        "result": str,
        # variable, store and reduce.
        "value": ValueExpression,
        # note_query and card_query.
        "query": ValueExpression,
        "selection": Selection,
        "if_empty": str,
        "error_if_empty": bool,
        "counts_as_sources": bool,
        # edit_note, edit_card and store.
        "target": BindingRef,
        "fields": list[FieldWrite],
        "tags": TagWrites,
        "read_semantics": str,
        "card_actions": list[dict],
        # read_file and write_file.
        "filename": ValueExpression,
        "content": ValueExpression,
        "if_missing": str,
        "overwrite": bool,
        "skip_if_exists": bool,
        # list_variable.
        "item_type": str,
        # select_note and select_card: which item of the input list, as a number or code
        # returning one.
        "index": ValueExpression,
        # select_note, select_card, the loops and reduce.
        "input": BindingRef,
        "item_binding": str,
        "note_binding": str,
        "body": "list[Stage]",
        "accumulator_binding": str,
        "initial": ValueExpression,
        "operation": str,
        "separator": str,
        # condition.
        "predicate": ValueExpression,
        "predicate_kind": str,
        "predicate_target": BindingRef,
        "only_on_sync": bool,
        "unmatched_skips_trigger": bool,
        "then": "list[Stage]",
        "else": "list[Stage]",
        # call_definition.
        "definition_guid": str,
        "trigger": BindingRef,
        "outputs": list[CallOutput],
        # Migration bookkeeping: which binding format 1 read a value from and wrote it to.
        "legacy_source": BindingRef,
        "legacy_destination": BindingRef,
        # A migrated field write's gate, copied onto the stages that only exist to feed it so
        # they decline when it would (`runs_on_unfocus`, `feeds_a_filled_field`).
        "unfocus_trigger_fields": list[str],
        "unfocus_when_edit": bool,
        "unfocus_when_add": bool,
        "write_if": Literal["always", "empty"],
        "write_if_field": str,
    },
    total=False,
)


class Export(TypedDict, total=False):
    name: str
    stage_guid: str
    # Which of the producing stage's results to export. Only a `call_definition` can bind
    # more than one, so everything else leaves this out and there is nothing to choose.
    result: str


class Effects(TypedDict, total=False):
    edits_trigger: bool
    edits_other_notes: bool
    #: Either of the two below: what a reader that does not care whose cards asks for.
    edits_cards: bool
    edits_trigger_cards: bool
    edits_other_cards: bool
    reads_files: bool
    writes_files: bool
    queries_collection: bool
    calls_definitions: bool
    add_note_compatible: bool


class UnfocusTriggers(TypedDict, total=False):
    edit_fields: list[str]
    add_fields: list[str]


class Triggers(TypedDict, total=False):
    note_types: list[str]
    deck_names: list[str]
    include_subdecks: bool
    on_sync: bool
    on_add: bool
    on_review: bool
    on_unfocus: UnfocusTriggers


class LegacyBehaviour(TypedDict, total=False):
    """What a migrated definition keeps of format 1's behaviour that no stage can express."""

    trigger_is_source: bool
    select_card_separator: Optional[str]
    query_note_index_default: Optional[int]


class CopyDefinitionV2(TypedDict, total=False):
    guid: str
    format_version: int
    definition_name: str
    triggers: Triggers
    stages: list[Stage]
    exports: list[Export]
    effects: Effects
    # Only on a definition the migrator produced.
    migrated_from_format: int
    legacy: LegacyBehaviour
    migration_warnings: list[str]


EMPTY_EFFECTS: Effects = {
    "edits_trigger": False,
    "edits_other_notes": False,
    "edits_cards": False,
    "edits_trigger_cards": False,
    "edits_other_cards": False,
    "reads_files": False,
    "writes_files": False,
    "queries_collection": False,
    "calls_definitions": False,
    "add_note_compatible": True,
}

#: What a definition is assumed to do when its `effects` object is missing or unreadable.
#: Refusing add-note is the safe direction: the hook has no persisted trigger note to
#: mutate, so running an unanalysed definition there could write to the wrong note.
UNKNOWN_EFFECTS: Effects = {
    "edits_trigger": True,
    "edits_other_notes": True,
    "edits_cards": True,
    "edits_trigger_cards": True,
    "edits_other_cards": True,
    "reads_files": True,
    "writes_files": True,
    "queries_collection": True,
    "calls_definitions": True,
    "add_note_compatible": False,
}


def is_format_2(definition: Any) -> TypeGuard[CopyDefinitionV2]:
    return isinstance(definition, dict) and definition.get("format_version") == FORMAT_VERSION


def stage_body_blocks(stage: Stage) -> list[tuple[str, list[Stage]]]:
    """The child blocks of a structural stage, as (key, stages) pairs. Empty for leaves."""
    keys = STRUCTURAL_STAGE_BODY_KEYS.get(stage.get("type", ""), ())
    blocks = []
    for key in keys:
        block = stage.get(key)
        blocks.append((key, block if isinstance(block, list) else []))
    return blocks


def walk_stages(stages: Iterable[Stage], include_disabled: bool = True) -> Iterable[Stage]:
    """Every stage in a block and its descendants, in execution order."""
    for stage in stages or []:
        if not isinstance(stage, dict):
            continue
        if not include_disabled and not stage.get("enabled", True):
            continue
        yield stage
        for _key, block in stage_body_blocks(stage):
            yield from walk_stages(block, include_disabled)


def find_stage(stages: Iterable[Stage], guid: str) -> Optional[Stage]:
    for stage in walk_stages(stages):
        if stage.get("guid") == guid:
            return stage
    return None


def root_stage_guids(definition: CopyDefinitionV2) -> set[str]:
    return {
        stage.get("guid", "")
        for stage in definition.get("stages", [])
        if isinstance(stage, dict)
    }


#: Stage types that name a result, and the key holding that name.
RESULT_PRODUCING_STAGES = {
    STAGE_VARIABLE: "result",
    STAGE_NOTE_QUERY: "result",
    STAGE_CARD_QUERY: "result",
    STAGE_READ_FILE: "result",
    STAGE_SELECT_NOTE: "result",
    STAGE_SELECT_CARD: "result",
    STAGE_LIST_VARIABLE: "result",
    STAGE_REDUCE: "result",
}


def stage_result_name(stage: Stage) -> Optional[str]:
    """The single result this stage names, if it names one. A `call_definition` binds any
    number of results through `outputs`, so it is deliberately not in this map."""
    key = RESULT_PRODUCING_STAGES.get(stage.get("type", ""))
    if key is None:
        return None
    name = stage.get(key)
    return name if isinstance(name, str) else None


def result_reserved_names_allowed(stage: Stage) -> frozenset:
    """The reserved names this stage may nonetheless give its result."""
    return SELECT_RESERVED_NAMES.get(stage.get("type", ""), frozenset())


def stage_result_names(stage: Stage) -> list[str]:
    """Every result name this stage binds, in the order it binds them.

    One for most stages; one per declared output for a `call_definition`, which is the only
    stage that can bind several and the reason this exists alongside `stage_result_name`.
    """
    if stage.get("type") == STAGE_CALL_DEFINITION:
        names = []
        for output in stage.get("outputs", []) or []:
            result = output.get("result") if isinstance(output, dict) else None
            if isinstance(result, str) and result:
                names.append(result)
        return names
    name = stage_result_name(stage)
    return [name] if name else []


def export_result_name(export: Export, producer: Stage) -> Optional[str]:
    """The result of `producer` this export takes its value from, if it names a real one."""
    names = stage_result_names(producer)
    wanted = export.get("result")
    if isinstance(wanted, str) and wanted:
        return wanted if wanted in names else None
    # An export written before a stage could bind more than one names no result of its own:
    # there was only ever the one to take.
    return names[0] if len(names) == 1 else None


# --------------------------------------------------------------------------------------
# Structural validation
# --------------------------------------------------------------------------------------


class SchemaProblem:
    """One structural complaint, carrying enough to point the editor at the stage."""

    __slots__ = ("message", "stage_guid", "stage_type")

    def __init__(
        self, message: str, stage_guid: Optional[str] = None, stage_type: Optional[str] = None
    ) -> None:
        self.message = message
        self.stage_guid = stage_guid
        self.stage_type = stage_type

    def __str__(self) -> str:
        if self.stage_type and self.stage_guid:
            return f"{self.stage_type} stage {self.stage_guid}: {self.message}"
        if self.stage_guid:
            return f"stage {self.stage_guid}: {self.message}"
        return self.message

    def __repr__(self) -> str:
        return f"SchemaProblem({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SchemaProblem):
            return NotImplemented
        return (self.message, self.stage_guid, self.stage_type) == (
            other.message,
            other.stage_guid,
            other.stage_type,
        )


def _require_expression(
    stage: Stage, key: str, problems: list[SchemaProblem], required: bool = True
) -> None:
    expression = stage.get(key)
    guid, stage_type = stage.get("guid"), stage.get("type")
    if expression is None:
        if required:
            problems.append(SchemaProblem(f"'{key}' is missing", guid, stage_type))
        return
    if not isinstance(expression, dict):
        problems.append(SchemaProblem(f"'{key}' is not a value expression", guid, stage_type))
        return
    mode = expression.get("mode", MODE_TEXT)
    if mode not in (MODE_TEXT, MODE_CODE):
        problems.append(
            SchemaProblem(f"'{key}' has unknown mode '{mode}'", guid, stage_type)
        )
    process_chain = expression.get("process_chain", [])
    if process_chain is not None and not isinstance(process_chain, (list, tuple)):
        problems.append(
            SchemaProblem(f"'{key}' has a non-list process_chain", guid, stage_type)
        )


def _require_binding(stage: Stage, key: str, problems: list[SchemaProblem]) -> None:
    ref = stage.get(key)
    guid, stage_type = stage.get("guid"), stage.get("type")
    if not isinstance(ref, dict) or not isinstance(ref.get("binding"), str) or not ref["binding"]:
        problems.append(SchemaProblem(f"'{key}.binding' is missing", guid, stage_type))


def _require_result_name(
    stage: Stage, problems: list[SchemaProblem], relaxed: bool = False
) -> None:
    guid, stage_type = stage.get("guid"), stage.get("type")
    problem = result_name_problem(
        stage_result_name(stage),
        relaxed=relaxed,
        allowed_reserved=result_reserved_names_allowed(stage),
    )
    if problem:
        problems.append(SchemaProblem(problem, guid, stage_type))


def _validate_selection(stage: Stage, problems: list[SchemaProblem]) -> None:
    guid, stage_type = stage.get("guid"), stage.get("type")
    selection = stage.get("selection", {})
    if not isinstance(selection, dict):
        problems.append(SchemaProblem("'selection' is not an object", guid, stage_type))
        return
    strategy = selection.get("strategy", "all")
    if strategy not in SELECTION_STRATEGIES:
        problems.append(
            SchemaProblem(f"unknown selection strategy '{strategy}'", guid, stage_type)
        )
    count = selection.get("count")
    if count is not None:
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            problems.append(
                SchemaProblem(
                    f"selection count must be a positive integer or null, got {count!r}",
                    guid,
                    stage_type,
                )
            )
        elif strategy == "all":
            problems.append(
                SchemaProblem("selection strategy 'all' takes no count", guid, stage_type)
            )
    sort_order = selection.get("sort_order", "descending")
    if sort_order not in SORT_ORDERS:
        problems.append(SchemaProblem(f"unknown sort order '{sort_order}'", guid, stage_type))


def _validate_choice(
    stage: Stage, key: str, choices: Sequence[str], default: str, problems: list[SchemaProblem]
) -> None:
    value = stage.get(key, default)
    if value not in choices:
        problems.append(
            SchemaProblem(
                f"'{key}' must be one of {', '.join(choices)}, got {value!r}",
                stage.get("guid"),
                stage.get("type"),
            )
        )


def validate_stage_structure(
    raw_stage: Any, problems: list[SchemaProblem], relaxed_names: bool = False
) -> None:
    """Check one stage's own shape. Bindings and types are the analyser's job.

    `relaxed_names` is the definition's `names_are_relaxed` answer, passed down because a
    stage does not know which definition it belongs to.
    """
    if not isinstance(raw_stage, dict):
        problems.append(SchemaProblem(f"stage is not an object: {raw_stage!r}"))
        return
    # Read as a stage from here on: every key it may have is optional, and each is checked
    # below before it is trusted.
    stage = cast(Stage, raw_stage)
    guid = stage.get("guid")
    stage_type = stage.get("type")
    if not isinstance(guid, str) or not guid:
        problems.append(SchemaProblem("stage has no guid", None, stage_type))
    if stage_type not in ALL_STAGE_TYPES:
        # Unknown types make the definition invalid; they are never silently ignored (§4).
        problems.append(SchemaProblem(f"unknown stage type {stage_type!r}", guid, None))
        return

    if stage_type == STAGE_VARIABLE:
        _require_result_name(stage, problems, relaxed_names)
        _require_expression(stage, "value", problems)
    elif stage_type in (STAGE_NOTE_QUERY, STAGE_CARD_QUERY):
        _require_result_name(stage, problems, relaxed_names)
        _require_expression(stage, "query", problems)
        _validate_selection(stage, problems)
        _validate_choice(stage, "if_empty", IF_EMPTY_POLICIES, "continue", problems)
    elif stage_type in SELECT_STAGE_TYPES:
        _require_binding(stage, "input", problems)
        _require_result_name(stage, problems, relaxed_names)
        _require_expression(stage, "index", problems)
        _validate_choice(stage, "if_missing", IF_MISSING_POLICIES, "empty", problems)
    elif stage_type == STAGE_EDIT_NOTE:
        _require_binding(stage, "target", problems)
        fields = stage.get("fields", [])
        if not isinstance(fields, list):
            problems.append(SchemaProblem("'fields' is not a list", guid, stage_type))
        else:
            for field_write in fields:
                if not isinstance(field_write, dict) or not field_write.get("field"):
                    problems.append(
                        SchemaProblem("a field write names no field", guid, stage_type)
                    )
                    continue
                _require_expression(field_write, "value", problems)  # type: ignore[arg-type]
                if field_write.get("write_if", "always") not in WRITE_IF_POLICIES:
                    problems.append(
                        SchemaProblem(
                            f"field '{field_write['field']}' has unknown write_if"
                            f" {field_write.get('write_if')!r}",
                            guid,
                            stage_type,
                        )
                    )
        tags = stage.get("tags", {})
        if tags is not None and not isinstance(tags, dict):
            problems.append(SchemaProblem("'tags' is not an object", guid, stage_type))
        _validate_choice(stage, "read_semantics", READ_SEMANTICS, "stage_snapshot", problems)
    elif stage_type == STAGE_EDIT_CARD:
        _require_binding(stage, "target", problems)
        if not isinstance(stage.get("card_actions", []), list):
            problems.append(SchemaProblem("'card_actions' is not a list", guid, stage_type))
    elif stage_type == STAGE_READ_FILE:
        _require_result_name(stage, problems, relaxed_names)
        _require_expression(stage, "filename", problems)
        _validate_choice(stage, "if_missing", IF_MISSING_POLICIES, "empty", problems)
    elif stage_type == STAGE_WRITE_FILE:
        _require_expression(stage, "content", problems)
        # A code-mode content expression returns its own (filename, content) pairs.
        _require_expression(
            stage, "filename", problems, required=not expression_is_code(stage.get("content"))
        )
    elif stage_type == STAGE_LIST_VARIABLE:
        _require_result_name(stage, problems, relaxed_names)
        item_type = stage.get("item_type", TEXT)
        if item_type not in LIST_ITEM_TYPE_NAMES:
            problems.append(
                SchemaProblem(f"unknown list item type {item_type!r}", guid, stage_type)
            )
    elif stage_type == STAGE_STORE:
        target = stage.get("target")
        if not isinstance(target, dict) or target.get("kind") != "list" or not target.get(
            "binding"
        ):
            problems.append(
                SchemaProblem("'target' must name a declared list binding", guid, stage_type)
            )
        _require_expression(stage, "value", problems)
    elif stage_type in (STAGE_FOR_EACH_NOTE, STAGE_FOR_EACH_CARD):
        _require_binding(stage, "input", problems)
        for key in ("item_binding",) + (
            ("note_binding",) if stage_type == STAGE_FOR_EACH_CARD else ()
        ):
            problem = result_name_problem(stage.get(key), key)
            # Loop bindings are allowed to use the reserved loop names -- that is what they
            # are for -- but must still be identifiers.
            if problem and "reserved" not in problem:
                problems.append(SchemaProblem(problem, guid, stage_type))
        if not isinstance(stage.get("body", []), list):
            problems.append(SchemaProblem("'body' is not a list", guid, stage_type))
    elif stage_type == STAGE_REDUCE:
        _require_binding(stage, "input", problems)
        _require_result_name(stage, problems, relaxed_names)
        _require_expression(stage, "value", problems)
        _require_expression(stage, "initial", problems, required=False)
        for key in ("item_binding", "accumulator_binding"):
            problem = result_name_problem(stage.get(key), key)
            if problem and "reserved" not in problem:
                problems.append(SchemaProblem(problem, guid, stage_type))
    elif stage_type == STAGE_CONDITION:
        _require_expression(stage, "predicate", problems)
        for key in ("then", "else"):
            if not isinstance(stage.get(key, []), list):
                problems.append(SchemaProblem(f"'{key}' is not a list", guid, stage_type))
    elif stage_type == STAGE_CALL_DEFINITION:
        if not isinstance(stage.get("definition_guid"), str) or not stage["definition_guid"]:
            problems.append(SchemaProblem("'definition_guid' is missing", guid, stage_type))
        _require_binding(stage, "trigger", problems)
        outputs = stage.get("outputs", [])
        if not isinstance(outputs, list):
            problems.append(SchemaProblem("'outputs' is not a list", guid, stage_type))
        else:
            for output in outputs:
                if not isinstance(output, dict) or not output.get("export"):
                    problems.append(
                        SchemaProblem("an output binds no export name", guid, stage_type)
                    )
                    continue
                problem = result_name_problem(output.get("result"), "Output result name")
                if problem:
                    problems.append(SchemaProblem(problem, guid, stage_type))

    for _key, block in stage_body_blocks(stage):
        for child in block:
            validate_stage_structure(child, problems, relaxed_names)


def validate_definition_structure(definition: Any) -> list[SchemaProblem]:
    """Every structural complaint about `definition`, in reading order.

    An empty list means the definition parses; it does not yet mean it runs. Scope, types,
    exports and call cycles are checked by `flow_analysis.analyze_definition`.
    """
    problems: list[SchemaProblem] = []
    if not isinstance(definition, dict):
        return [SchemaProblem("definition is not an object")]
    if definition.get("format_version") != FORMAT_VERSION:
        problems.append(
            SchemaProblem(
                f"format_version is {definition.get('format_version')!r},"
                f" expected {FORMAT_VERSION}"
            )
        )
    if not isinstance(definition.get("guid"), str) or not definition["guid"]:
        problems.append(SchemaProblem("definition has no guid"))
    stages = definition.get("stages")
    if not isinstance(stages, list):
        problems.append(SchemaProblem("'stages' is not a list"))
        return problems
    relaxed_names = names_are_relaxed(definition)
    for stage in stages:
        validate_stage_structure(stage, problems, relaxed_names)

    exports = definition.get("exports", [])
    if not isinstance(exports, list):
        problems.append(SchemaProblem("'exports' is not a list"))
    else:
        seen: set[str] = set()
        for export in exports:
            if not isinstance(export, dict):
                problems.append(SchemaProblem("an export is not an object"))
                continue
            problem = result_name_problem(export.get("name"), "Export name")
            if problem:
                problems.append(SchemaProblem(problem))
                continue
            if export["name"] in seen:
                problems.append(SchemaProblem(f"duplicate export name '{export['name']}'"))
            seen.add(export["name"])
            if not isinstance(export.get("stage_guid"), str) or not export["stage_guid"]:
                problems.append(
                    SchemaProblem(f"export '{export['name']}' names no producing stage")
                )
    return problems


def read_effects(definition: Any) -> Effects:
    """The stored effect flags, or the pessimistic ones when they are missing or broken."""
    if not isinstance(definition, dict):
        return dict(UNKNOWN_EFFECTS)  # type: ignore[return-value]
    effects = definition.get("effects")
    if not isinstance(effects, dict):
        return dict(UNKNOWN_EFFECTS)  # type: ignore[return-value]
    merged = dict(UNKNOWN_EFFECTS)
    for key in EMPTY_EFFECTS:
        value = effects.get(key)
        if isinstance(value, bool):
            merged[key] = value
    return merged  # type: ignore[return-value]


def new_definition(
    guid: str,
    definition_name: str = "",
    stages: Optional[list[Stage]] = None,
    triggers: Optional[Triggers] = None,
    exports: Optional[list[Export]] = None,
    effects: Optional[Effects] = None,
) -> CopyDefinitionV2:
    return {
        "guid": guid,
        "format_version": FORMAT_VERSION,
        "definition_name": definition_name,
        "triggers": triggers
        or {
            "note_types": [],
            "deck_names": [],
            "include_subdecks": False,
            "on_sync": False,
            "on_add": False,
            "on_review": False,
            "on_unfocus": {"edit_fields": [], "add_fields": []},
        },
        "stages": stages if stages is not None else [],
        "exports": exports if exports is not None else [],
        "effects": effects if effects is not None else dict(EMPTY_EFFECTS),  # type: ignore
    }


