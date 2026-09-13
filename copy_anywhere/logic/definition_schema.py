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
from typing import Any, Iterable, Literal, Optional, Sequence, TypedDict

from typing_extensions import TypeGuard

# The per-definition format version. The global config version stays for config-wide
# migrations; this one says how to read a single definition.
FORMAT_VERSION = 2

# Format-1 expressions accept unqualified field names ({{Word}} meaning "a field of the
# note this expression is evaluated against"). Migrated expressions carry this marker so the
# runtime keeps resolving them that way; new expressions must qualify their references.
SYNTAX_VERSION_LEGACY = 1
SYNTAX_VERSION_CURRENT = 2

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


def result_name_problem(name: Any, what: str = "Result name") -> Optional[str]:
    """The reason `name` cannot be a result name, or None when it can."""
    if not isinstance(name, str) or not name:
        return f"{what} is missing"
    if name.startswith("__"):
        return f"{what} '{name}' may not begin with '__'"
    if not RESULT_NAME_RE.match(name):
        return (
            f"{what} '{name}' is not an identifier"
            " (letters, digits and underscore, not starting with a digit)"
        )
    if name in RESERVED_BINDING_NAMES:
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
    # Only present on migrated expressions; see SYNTAX_VERSION_LEGACY.
    syntax_version: int
    # Migrated variable expressions that must not see earlier variables (§11 step 2).
    legacy_isolated_variables: bool


def value_expression(
    text: str = "",
    code: str = "",
    mode: Optional[str] = None,
    process_chain: Optional[Sequence[dict]] = None,
    syntax_version: Optional[int] = None,
    **extra: Any,
) -> ValueExpression:
    expression: ValueExpression = {
        "mode": MODE_CODE if (mode == MODE_CODE or (mode is None and code)) else MODE_TEXT,
        "text": text or "",
        "code": code or "",
        "process_chain": list(process_chain or []),
    }
    if syntax_version is not None:
        expression["syntax_version"] = syntax_version
    expression.update(extra)  # type: ignore[typeddict-item]
    return expression


def expression_is_code(expression: Optional[ValueExpression]) -> bool:
    return bool(expression) and expression.get("mode") == MODE_CODE


def expression_source(expression: ValueExpression) -> str:
    """The text this expression interpolates: its code in code mode, its text otherwise."""
    if expression_is_code(expression):
        return expression.get("code", "") or ""
    return expression.get("text", "") or ""


def expression_is_legacy_syntax(expression: Optional[ValueExpression]) -> bool:
    return bool(expression) and expression.get("syntax_version", SYNTAX_VERSION_CURRENT) == (
        SYNTAX_VERSION_LEGACY
    )


# --------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------

STAGE_VARIABLE = "variable"
STAGE_NOTE_QUERY = "note_query"
STAGE_CARD_QUERY = "card_query"
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


class Stage(TypedDict, total=False):
    guid: str
    type: str
    name: str
    enabled: bool


class FieldWrite(TypedDict, total=False):
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


class Export(TypedDict, total=False):
    name: str
    stage_guid: str


class Effects(TypedDict, total=False):
    edits_trigger: bool
    edits_other_notes: bool
    edits_cards: bool
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


class CopyDefinitionV2(TypedDict, total=False):
    guid: str
    format_version: int
    definition_name: str
    triggers: Triggers
    stages: list[Stage]
    exports: list[Export]
    effects: Effects


EMPTY_EFFECTS: Effects = {
    "edits_trigger": False,
    "edits_other_notes": False,
    "edits_cards": False,
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
        block = stage.get(key)  # type: ignore[misc]
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
    STAGE_LIST_VARIABLE: "result",
    STAGE_REDUCE: "result",
}


def stage_result_name(stage: Stage) -> Optional[str]:
    """The single result this stage names, if it names one. A `call_definition` binds any
    number of results through `outputs`, so it is deliberately not in this map."""
    key = RESULT_PRODUCING_STAGES.get(stage.get("type", ""))
    if key is None:
        return None
    name = stage.get(key)  # type: ignore[misc]
    return name if isinstance(name, str) else None


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
    expression = stage.get(key)  # type: ignore[misc]
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
    ref = stage.get(key)  # type: ignore[misc]
    guid, stage_type = stage.get("guid"), stage.get("type")
    if not isinstance(ref, dict) or not isinstance(ref.get("binding"), str) or not ref["binding"]:
        problems.append(SchemaProblem(f"'{key}.binding' is missing", guid, stage_type))


def _require_result_name(stage: Stage, problems: list[SchemaProblem]) -> None:
    guid, stage_type = stage.get("guid"), stage.get("type")
    problem = result_name_problem(stage_result_name(stage))
    if problem:
        problems.append(SchemaProblem(problem, guid, stage_type))


def _validate_selection(stage: Stage, problems: list[SchemaProblem]) -> None:
    guid, stage_type = stage.get("guid"), stage.get("type")
    selection = stage.get("selection", {})  # type: ignore[misc]
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
    value = stage.get(key, default)  # type: ignore[misc]
    if value not in choices:
        problems.append(
            SchemaProblem(
                f"'{key}' must be one of {', '.join(choices)}, got {value!r}",
                stage.get("guid"),
                stage.get("type"),
            )
        )


def validate_stage_structure(stage: Any, problems: list[SchemaProblem]) -> None:
    """Check one stage's own shape. Bindings and types are the analyser's job."""
    if not isinstance(stage, dict):
        problems.append(SchemaProblem(f"stage is not an object: {stage!r}"))
        return
    guid = stage.get("guid")
    stage_type = stage.get("type")
    if not isinstance(guid, str) or not guid:
        problems.append(SchemaProblem("stage has no guid", None, stage_type))
    if stage_type not in ALL_STAGE_TYPES:
        # Unknown types make the definition invalid; they are never silently ignored (§4).
        problems.append(SchemaProblem(f"unknown stage type {stage_type!r}", guid, None))
        return

    if stage_type == STAGE_VARIABLE:
        _require_result_name(stage, problems)
        _require_expression(stage, "value", problems)
    elif stage_type in (STAGE_NOTE_QUERY, STAGE_CARD_QUERY):
        _require_result_name(stage, problems)
        _require_expression(stage, "query", problems)
        _validate_selection(stage, problems)
        _validate_choice(stage, "if_empty", IF_EMPTY_POLICIES, "continue", problems)
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
        _require_result_name(stage, problems)
        _require_expression(stage, "filename", problems)
        _validate_choice(stage, "if_missing", IF_MISSING_POLICIES, "empty", problems)
    elif stage_type == STAGE_WRITE_FILE:
        _require_expression(stage, "content", problems)
        # A code-mode content expression returns its own (filename, content) pairs.
        _require_expression(
            stage, "filename", problems, required=not expression_is_code(stage.get("content"))
        )
    elif stage_type == STAGE_LIST_VARIABLE:
        _require_result_name(stage, problems)
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
            problem = result_name_problem(stage.get(key), key)  # type: ignore[misc]
            # Loop bindings are allowed to use the reserved loop names -- that is what they
            # are for -- but must still be identifiers.
            if problem and "reserved" not in problem:
                problems.append(SchemaProblem(problem, guid, stage_type))
        if not isinstance(stage.get("body", []), list):
            problems.append(SchemaProblem("'body' is not a list", guid, stage_type))
    elif stage_type == STAGE_REDUCE:
        _require_binding(stage, "input", problems)
        _require_result_name(stage, problems)
        _require_expression(stage, "value", problems)
        _require_expression(stage, "initial", problems, required=False)
        for key in ("item_binding", "accumulator_binding"):
            problem = result_name_problem(stage.get(key), key)  # type: ignore[misc]
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
            validate_stage_structure(child, problems)


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
    for stage in stages:
        validate_stage_structure(stage, problems)

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


