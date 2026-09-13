"""Scope, type, effect and call-graph analysis over a format-2 definition.

One analyser serves both the editor and the runtime, so what the interpolation menu offers
at a stage is by construction what the evaluator will have in scope there. Walking a block
in order, each stage is validated against the bindings visible before it, and the result it
produces -- if any -- is added to the scope the next stage sees.

The analysis is pure: no collection, no `mw`, no config. It answers four questions.

* **Scope**: which bindings a stage can read. `scopes[stage_guid]` is exactly that, recorded
  before the stage runs, which is what an editor widget needs to build its menu.
* **Types**: what a result holds, so an action can reject a value it cannot use -- writing a
  note list into a field, looping over a number, interpolating a list.
* **Effects**: what the definition does to the collection, transitively through calls. These
  are what the add-note and unfocus hooks check, in place of format 1's mode inspection.
* **Call graph**: which definitions call which, so a cycle is refused at save rather than
  found at a recursion limit.

`problems` being empty is the condition for a definition to be saved and to run.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .definition_schema import (
    LIST,
    STAGE_CALL_DEFINITION,
    STAGE_CARD_QUERY,
    STAGE_CONDITION,
    STAGE_EDIT_CARD,
    STAGE_EDIT_NOTE,
    STAGE_FOR_EACH_CARD,
    STAGE_FOR_EACH_NOTE,
    STAGE_LIST_VARIABLE,
    STAGE_NOTE_QUERY,
    STAGE_READ_FILE,
    STAGE_REDUCE,
    STAGE_STORE,
    STAGE_VARIABLE,
    STAGE_WRITE_FILE,
    T_CARD,
    T_CARD_LIST,
    T_NOTE,
    T_NOTE_LIST,
    T_NUMBER,
    T_TEXT,
    T_UNKNOWN,
    CopyDefinitionV2,
    Effects,
    SchemaProblem,
    Stage,
    ValueExpression,
    ValueType,
    expression_is_code,
    expression_is_legacy_syntax,
    expression_source,
    is_format_2,
    list_of,
    result_name_problem,
    stage_result_name,
    validate_definition_structure,
    value_type_from_name,
)

#: Refuse a call chain deeper than this even when no guid repeats, so hand-edited JSON
#: cannot drive the evaluator into a recursion limit (§5.9).
MAX_CALL_DEPTH = 32

#: `{{...}}` references in a text expression. Cloze markers are skipped by the caller.
INTERPOLATION_RE = re.compile(r"\{\{(.+?)\}\}")
CLOZE_REF_RE = re.compile(r"^c\d+::")

TRIGGER_BINDING = "trigger"


class Binding:
    """One name visible to a stage, and what it holds."""

    __slots__ = ("name", "type", "stage_guid", "is_loop_binding")

    def __init__(
        self,
        name: str,
        value_type: ValueType,
        stage_guid: Optional[str] = None,
        is_loop_binding: bool = False,
    ) -> None:
        self.name = name
        self.type = value_type
        self.stage_guid = stage_guid
        self.is_loop_binding = is_loop_binding

    def __repr__(self) -> str:
        return f"Binding({self.name}: {self.type.name})"


Scope = "dict[str, Binding]"


class AnalysisResult:
    """Everything the editor and the runtime need to know about one definition."""

    def __init__(self) -> None:
        self.problems: list[SchemaProblem] = []
        self.warnings: list[SchemaProblem] = []
        #: stage guid -> the bindings visible *before* that stage runs.
        self.scopes: dict[str, dict[str, Binding]] = {}
        #: stage guid -> the type of the result it produces, when it produces one.
        self.result_types: dict[str, ValueType] = {}
        #: export name -> type, for the callers of this definition.
        self.export_types: dict[str, ValueType] = {}
        self.effects: Effects = {
            "edits_trigger": False,
            "edits_other_notes": False,
            "edits_cards": False,
            "reads_files": False,
            "writes_files": False,
            "queries_collection": False,
            "calls_definitions": False,
            "add_note_compatible": True,
        }
        #: guids of the definitions this one calls, directly.
        self.called_definition_guids: list[str] = []

    @property
    def is_valid(self) -> bool:
        return not self.problems

    def problem_messages(self) -> list[str]:
        return [str(problem) for problem in self.problems]


class _Analyzer:
    def __init__(
        self,
        definition: CopyDefinitionV2,
        lookup: Optional[Callable[[str], Optional[CopyDefinitionV2]]] = None,
        analyzed_callees: Optional[dict[str, AnalysisResult]] = None,
        call_stack: Sequence[str] = (),
    ) -> None:
        self.definition = definition
        self.lookup = lookup
        self.result = AnalysisResult()
        # Callee analyses are cached: a definition called from three places is analysed once,
        # and a diamond in the call graph does not become exponential work.
        self.analyzed_callees = analyzed_callees if analyzed_callees is not None else {}
        self.call_stack = list(call_stack)
        # Migrated definitions keep format-1 variable names, which were free text; the
        # identifier rule applies to newly authored ones only.
        self.relaxed_names = definition.get("migrated_from_format") == 1
        #: Root-block results that may be unset because an earlier stage can skip the block.
        self.root_results_may_be_unset = False
        self.root_result_stage_guids: dict[str, str] = {}

    # -- helpers ----------------------------------------------------------------------

    def problem(self, message: str, stage: Optional[Stage] = None) -> None:
        self.result.problems.append(
            SchemaProblem(
                message,
                stage.get("guid") if stage else None,
                stage.get("type") if stage else None,
            )
        )

    def warn(self, message: str, stage: Optional[Stage] = None) -> None:
        self.result.warnings.append(
            SchemaProblem(
                message,
                stage.get("guid") if stage else None,
                stage.get("type") if stage else None,
            )
        )

    def declare(self, scope: dict, stage: Stage, name: Any, value_type: ValueType) -> None:
        problem = result_name_problem(name)
        if problem and (not self.relaxed_names or "reserved" in problem or "missing" in problem):
            self.problem(problem, stage)
            return
        if not isinstance(name, str) or not name:
            return
        if name in scope:
            # No shadowing in format 2: a menu that offered two `M`s could not say which one
            # a trace event meant (§4.2).
            self.problem(
                f"result '{name}' shadows a binding already in scope"
                f" (from stage {scope[name].stage_guid})",
                stage,
            )
            return
        scope[name] = Binding(name, value_type, stage.get("guid"))
        if stage.get("guid"):
            self.result.result_types[stage["guid"]] = value_type

    def resolve(
        self, scope: Mapping[str, Binding], ref: Any, stage: Stage, what: str
    ) -> Optional[Binding]:
        name = ref.get("binding") if isinstance(ref, dict) else None
        if not name:
            return None
        binding = scope.get(name)
        if binding is None:
            self.problem(f"{what} names unknown binding '{name}'", stage)
            return None
        return binding

    def expect(
        self, binding: Optional[Binding], wanted: ValueType, stage: Stage, what: str
    ) -> bool:
        if binding is None:
            return False
        if binding.type.kind == T_UNKNOWN.kind:
            # A code-mode result's type is only known at runtime; the action validates it
            # when the value arrives.
            return True
        if binding.type != wanted:
            self.problem(
                f"{what} must be {wanted.name}, but '{binding.name}' is {binding.type.name}",
                stage,
            )
            return False
        return True

    # -- expressions ------------------------------------------------------------------

    def check_expression(
        self,
        expression: Optional[ValueExpression],
        scope: Mapping[str, Binding],
        stage: Stage,
        what: str,
    ) -> ValueType:
        """Validate one value expression against a scope and report the type it produces."""
        if not isinstance(expression, dict):
            return T_UNKNOWN
        if expression_is_code(expression):
            # Restricted code may return anything the consuming action accepts, and it can
            # search the collection through find_notes/find_cards.
            self.result.effects["queries_collection"] = True
            self.check_references(expression, scope, stage, what)
            return T_UNKNOWN
        self.check_references(expression, scope, stage, what)
        return T_TEXT

    def check_references(
        self,
        expression: ValueExpression,
        scope: Mapping[str, Binding],
        stage: Stage,
        what: str,
    ) -> None:
        if expression_is_legacy_syntax(expression):
            # Format-1 syntax names note fields unqualified, so a reference that is not a
            # binding is a field name rather than a mistake. Those are resolved -- and
            # reported -- at run time, exactly as they were before.
            return
        for match in INTERPOLATION_RE.finditer(expression_source(expression)):
            reference = match.group(1)
            if CLOZE_REF_RE.match(reference):
                continue
            head = reference.split(".", 1)[0]
            binding = scope.get(head)
            if binding is None:
                # Not a binding: a bare note-value or card-value key resolved at run time.
                continue
            if "." in reference:
                if binding.type not in (T_NOTE, T_CARD) and binding.type != T_UNKNOWN:
                    self.problem(
                        f"{what} reads '{reference}', but '{head}' is"
                        f" {binding.type.name}, not a note or card",
                        stage,
                    )
                continue
            if binding.type.is_listy or binding.type in (T_NOTE, T_CARD):
                # There is no implicit list-to-text conversion: how values are combined is
                # the definition's decision, so it has to say so (§4.1).
                self.problem(
                    f"{what} interpolates '{head}', which is {binding.type.name};"
                    " reduce it to a scalar first",
                    stage,
                )

    # -- stages -----------------------------------------------------------------------

    def analyze_block(
        self,
        stages: Sequence[Stage],
        incoming: Mapping[str, Binding],
        at_root: bool = False,
    ) -> dict[str, Binding]:
        """Walk one block in order, returning the scope left behind after it."""
        scope: dict[str, Binding] = dict(incoming)
        for stage in stages or []:
            if not isinstance(stage, dict):
                continue
            guid = stage.get("guid")
            if guid:
                self.result.scopes[guid] = dict(scope)
            if not stage.get("enabled", True):
                # A disabled stage neither runs nor exports; it stays visible in the editor
                # and its own scope is still recorded so it can be re-enabled in place.
                continue
            self.analyze_stage(stage, scope, at_root=at_root)
        return scope

    def analyze_stage(self, stage: Stage, scope: dict[str, Binding], at_root: bool) -> None:
        stage_type = stage.get("type")
        effects = self.result.effects

        if stage_type == STAGE_VARIABLE:
            value_type = self.check_expression(stage.get("value"), scope, stage, "value")
            self.declare(scope, stage, stage_result_name(stage), value_type)

        elif stage_type in (STAGE_NOTE_QUERY, STAGE_CARD_QUERY):
            effects["queries_collection"] = True
            self.check_expression(stage.get("query"), scope, stage, "query")
            self.declare(
                scope,
                stage,
                stage_result_name(stage),
                T_NOTE_LIST if stage_type == STAGE_NOTE_QUERY else T_CARD_LIST,
            )
            if at_root and stage.get("if_empty") == "skip_block":
                # Everything declared after this in the root block may be unset, which is
                # what makes exporting it invalid (§5.9).
                self.root_results_may_be_unset = True

        elif stage_type == STAGE_EDIT_NOTE:
            target = self.resolve(scope, stage.get("target"), stage, "target")
            self.expect(target, T_NOTE, stage, "edit_note target")
            if target is not None:
                if target.name == TRIGGER_BINDING:
                    effects["edits_trigger"] = True
                else:
                    effects["edits_other_notes"] = True
            for field_write in stage.get("fields", []) or []:
                if isinstance(field_write, dict):
                    self.check_expression(
                        field_write.get("value"),
                        scope,
                        stage,
                        f"field '{field_write.get('field', '')}'",
                    )
            if stage.get("card_actions"):
                effects["edits_cards"] = True

        elif stage_type == STAGE_EDIT_CARD:
            target = self.resolve(scope, stage.get("target"), stage, "target")
            self.expect(target, T_CARD, stage, "edit_card target")
            effects["edits_cards"] = True

        elif stage_type == STAGE_READ_FILE:
            effects["reads_files"] = True
            self.check_expression(stage.get("filename"), scope, stage, "filename")
            self.declare(scope, stage, stage_result_name(stage), T_TEXT)
            if at_root and stage.get("if_missing") == "skip_block":
                self.root_results_may_be_unset = True

        elif stage_type == STAGE_WRITE_FILE:
            effects["writes_files"] = True
            self.check_expression(stage.get("filename"), scope, stage, "filename")
            self.check_expression(stage.get("content"), scope, stage, "content")

        elif stage_type == STAGE_LIST_VARIABLE:
            item_type = value_type_from_name(stage.get("item_type", "Text"))
            self.declare(scope, stage, stage_result_name(stage), list_of(item_type))

        elif stage_type == STAGE_STORE:
            target = stage.get("target")
            binding = self.resolve(scope, target, stage, "store target")
            if binding is not None and binding.type.kind != LIST:
                self.problem(
                    f"store target '{binding.name}' is {binding.type.name}, not a list", stage
                )
            self.check_expression(stage.get("value"), scope, stage, "value")

        elif stage_type in (STAGE_FOR_EACH_NOTE, STAGE_FOR_EACH_CARD):
            is_note_loop = stage_type == STAGE_FOR_EACH_NOTE
            binding = self.resolve(scope, stage.get("input"), stage, "loop input")
            self.expect(
                binding,
                T_NOTE_LIST if is_note_loop else T_CARD_LIST,
                stage,
                "loop input",
            )
            body_scope = dict(scope)
            self.bind_loop_name(body_scope, stage, stage.get("item_binding"),
                                T_NOTE if is_note_loop else T_CARD)
            if not is_note_loop:
                self.bind_loop_name(body_scope, stage, stage.get("note_binding"), T_NOTE)
            body_scope["index"] = Binding("index", T_NUMBER, stage.get("guid"), True)
            body_scope["count"] = Binding("count", T_NUMBER, stage.get("guid"), True)
            # Body-local results are discarded after each iteration, so the block's outgoing
            # scope is thrown away rather than merged back.
            self.analyze_block(stage.get("body", []) or [], body_scope)

        elif stage_type == STAGE_REDUCE:
            binding = self.resolve(scope, stage.get("input"), stage, "reduce input")
            if binding is not None and not binding.type.is_listy:
                self.problem(
                    f"reduce input '{binding.name}' is {binding.type.name}, not a list", stage
                )
            initial_type = self.check_expression(stage.get("initial"), scope, stage, "initial")
            reducer_scope = dict(scope)
            self.bind_loop_name(
                reducer_scope,
                stage,
                stage.get("item_binding"),
                binding.type.item if binding is not None and binding.type.item else T_UNKNOWN,
            )
            self.bind_loop_name(
                reducer_scope, stage, stage.get("accumulator_binding"), initial_type
            )
            reducer_scope["index"] = Binding("index", T_NUMBER, stage.get("guid"), True)
            reducer_scope["count"] = Binding("count", T_NUMBER, stage.get("guid"), True)
            if stage.get("operation") == "join":
                result_type = T_TEXT
            else:
                result_type = self.check_expression(
                    stage.get("value"), reducer_scope, stage, "reducer"
                )
            self.declare(scope, stage, stage_result_name(stage), result_type)

        elif stage_type == STAGE_CONDITION:
            self.check_expression(stage.get("predicate"), scope, stage, "predicate")
            if stage.get("predicate_kind") == "note_query":
                effects["queries_collection"] = True
                self.resolve(scope, stage.get("predicate_target"), stage, "predicate target")
            # Branch-local results do not escape, so both branches analyse against the same
            # incoming scope and neither one's outgoing scope is kept (§5.8).
            self.analyze_block(stage.get("then", []) or [], scope)
            self.analyze_block(stage.get("else", []) or [], scope)

        elif stage_type == STAGE_CALL_DEFINITION:
            self.analyze_call(stage, scope)

        if at_root:
            name = stage_result_name(stage)
            if isinstance(name, str) and name and stage.get("guid"):
                self.root_result_stage_guids[name] = stage["guid"]

    def bind_loop_name(
        self, scope: dict[str, Binding], stage: Stage, name: Any, value_type: ValueType
    ) -> None:
        if not isinstance(name, str) or not name:
            return
        # Loop bindings may take the reserved loop names -- that is what they are for -- but
        # they must not quietly hide an outer result.
        if name in scope and not scope[name].is_loop_binding:
            self.problem(f"loop binding '{name}' shadows a result already in scope", stage)
        scope[name] = Binding(name, value_type, stage.get("guid"), True)

    def analyze_call(self, stage: Stage, scope: dict[str, Binding]) -> None:
        self.result.effects["calls_definitions"] = True
        trigger = self.resolve(scope, stage.get("trigger"), stage, "call trigger")
        self.expect(trigger, T_NOTE, stage, "call trigger")
        callee_guid = stage.get("definition_guid")
        if not isinstance(callee_guid, str) or not callee_guid:
            return
        self.result.called_definition_guids.append(callee_guid)

        if self.lookup is None:
            # Without a lookup the callee's interface is unknown, so its outputs are typed
            # Unknown and its effects are assumed to be everything.
            for output in stage.get("outputs", []) or []:
                if isinstance(output, dict):
                    self.declare(scope, stage, output.get("result"), T_UNKNOWN)
            self.result.effects.update({
                "edits_other_notes": True,
                "edits_cards": True,
                "add_note_compatible": False,
            })
            return

        if callee_guid in self.call_stack:
            path = " -> ".join(self.call_stack + [callee_guid])
            self.problem(f"call cycle: {path}", stage)
            return
        if len(self.call_stack) >= MAX_CALL_DEPTH:
            self.problem(
                f"call chain deeper than {MAX_CALL_DEPTH} definitions: "
                + " -> ".join(self.call_stack),
                stage,
            )
            return

        callee = self.lookup(callee_guid)
        if callee is None:
            self.problem(f"calls unknown definition '{callee_guid}'", stage)
            return

        callee_result = self.analyzed_callees.get(callee_guid)
        if callee_result is None:
            callee_result = _Analyzer(
                callee,
                lookup=self.lookup,
                analyzed_callees=self.analyzed_callees,
                call_stack=self.call_stack + [self.definition.get("guid", "")],
            ).run()
            self.analyzed_callees[callee_guid] = callee_result
        if callee_result.problems:
            # Repeat the callee's first complaint: "that definition is not valid" on its own
            # sends the user looking, and a cycle in particular is only named over there.
            self.problem(
                f"calls definition '{callee.get('definition_name', callee_guid)}',"
                f" which is not valid: {callee_result.problems[0].message}",
                stage,
            )

        # Effects are transitive: a definition that calls one editing other notes edits
        # other notes (§6).
        for key in (
            "edits_other_notes",
            "edits_cards",
            "reads_files",
            "writes_files",
            "queries_collection",
            "calls_definitions",
        ):
            if callee_result.effects.get(key):
                self.result.effects[key] = True  # type: ignore[literal-required]
        if callee_result.effects.get("edits_trigger"):
            # The callee's trigger is a note this definition chose, so from here it is
            # another note unless this definition passed its own trigger.
            if trigger is not None and trigger.name == TRIGGER_BINDING:
                self.result.effects["edits_trigger"] = True
            else:
                self.result.effects["edits_other_notes"] = True

        for output in stage.get("outputs", []) or []:
            if not isinstance(output, dict):
                continue
            export_name = output.get("export")
            if export_name not in callee_result.export_types:
                self.problem(
                    f"definition '{callee.get('definition_name', callee_guid)}'"
                    f" exports no '{export_name}'",
                    stage,
                )
                self.declare(scope, stage, output.get("result"), T_UNKNOWN)
                continue
            self.declare(scope, stage, output.get("result"), callee_result.export_types[export_name])

    # -- exports ----------------------------------------------------------------------

    def analyze_exports(self, root_scope: Mapping[str, Binding]) -> None:
        for export in self.definition.get("exports", []) or []:
            if not isinstance(export, dict):
                continue
            name = export.get("name")
            stage_guid = export.get("stage_guid")
            producer = None
            for stage in self.definition.get("stages", []) or []:
                if isinstance(stage, dict) and stage.get("guid") == stage_guid:
                    producer = stage
                    break
            if producer is None:
                self.problem(
                    f"export '{name}' names stage '{stage_guid}', which is not a root stage"
                )
                continue
            if not producer.get("enabled", True):
                self.problem(f"export '{name}' names a disabled stage")
                continue
            result_name = stage_result_name(producer)
            if not result_name:
                self.problem(f"export '{name}' names a stage that produces no result")
                continue
            if self.root_results_may_be_unset:
                # An earlier `skip_block` makes later root results only maybe defined, and an
                # export that might not exist is worse than no export at all (§5.9).
                self.problem(
                    f"export '{name}' cannot be guaranteed: an earlier root stage can skip"
                    " the rest of the block"
                )
                continue
            self.result.export_types[name] = self.result.result_types.get(
                stage_guid, T_UNKNOWN
            )

    # -- entry ------------------------------------------------------------------------

    def run(self) -> AnalysisResult:
        self.result.problems.extend(validate_definition_structure(self.definition))
        root_scope: dict[str, Binding] = {
            TRIGGER_BINDING: Binding(TRIGGER_BINDING, T_NOTE, None)
        }
        outgoing = self.analyze_block(
            self.definition.get("stages", []) or [], root_scope, at_root=True
        )
        self.analyze_exports(outgoing)
        effects = self.result.effects
        # A note being added has no persisted cards and no id, so anything touching another
        # note or any card cannot run in the add hook (§6).
        effects["add_note_compatible"] = not (
            effects["edits_other_notes"] or effects["edits_cards"]
        )
        return self.result


def analyze_definition(
    definition: CopyDefinitionV2,
    lookup: Optional[Callable[[str], Optional[CopyDefinitionV2]]] = None,
) -> AnalysisResult:
    """Analyse one definition, following `call_definition` stages through `lookup`.

    :param lookup: maps a definition guid to the definition. Without it, call stages are
        analysed pessimistically: unknown output types and every effect assumed.
    """
    return _Analyzer(definition, lookup=lookup).run()


def analyze_block(
    stages: Sequence[Stage],
    incoming_scope: Mapping[str, Binding],
    definition: Optional[CopyDefinitionV2] = None,
    lookup: Optional[Callable[[str], Optional[CopyDefinitionV2]]] = None,
) -> AnalysisResult:
    """Analyse a bare block against a scope, for editors working on part of a definition."""
    analyzer = _Analyzer(definition or {"guid": "", "stages": list(stages)}, lookup=lookup)
    analyzer.analyze_block(stages, incoming_scope, at_root=definition is None)
    return analyzer.result


def make_lookup(
    definitions: Iterable[CopyDefinitionV2],
) -> Callable[[str], Optional[CopyDefinitionV2]]:
    by_guid = {
        definition.get("guid", ""): definition
        for definition in definitions
        if isinstance(definition, dict)
    }
    return by_guid.get


def compute_effects(
    definition: CopyDefinitionV2,
    lookup: Optional[Callable[[str], Optional[CopyDefinitionV2]]] = None,
) -> Effects:
    """The `effects` object to persist on this definition (§4)."""
    return analyze_definition(definition, lookup=lookup).effects


def find_call_cycles(definitions: Sequence[CopyDefinitionV2]) -> list[list[str]]:
    """Every call cycle among `definitions`, each as the path of guids that closes it.

    Reported separately from `analyze_definition` so a save can list all of them at once
    rather than the first one the analyser happened to reach.
    """
    edges: dict[str, list[str]] = {}
    for definition in definitions:
        guid = definition.get("guid", "")
        targets: list[str] = []
        for stage in _walk(definition.get("stages", []) or []):
            if stage.get("type") == STAGE_CALL_DEFINITION and stage.get("definition_guid"):
                targets.append(stage["definition_guid"])
        edges[guid] = targets

    cycles: list[list[str]] = []
    seen_cycles: set[tuple[str, ...]] = set()
    visiting: list[str] = []
    done: set[str] = set()

    def visit(guid: str) -> None:
        if guid in visiting:
            cycle = visiting[visiting.index(guid):] + [guid]
            key = tuple(cycle)
            if key not in seen_cycles:
                seen_cycles.add(key)
                cycles.append(cycle)
            return
        if guid in done:
            return
        visiting.append(guid)
        for target in edges.get(guid, []):
            visit(target)
        visiting.pop()
        done.add(guid)

    for guid in edges:
        visit(guid)
    return cycles


def _walk(stages: Sequence[Stage]) -> Iterable[Stage]:
    from .definition_schema import walk_stages

    return walk_stages(stages)


def callers_of(
    definition_guid: str, definitions: Sequence[CopyDefinitionV2]
) -> list[CopyDefinitionV2]:
    """The definitions with a `call_definition` stage pointing at `definition_guid`.

    Saving a callee has to rewrite these definitions' `effects` and re-validate their
    `outputs`, so the caller needs to find them.
    """
    callers = []
    for definition in definitions:
        for stage in _walk(definition.get("stages", []) or []):
            if (
                stage.get("type") == STAGE_CALL_DEFINITION
                and stage.get("definition_guid") == definition_guid
            ):
                callers.append(definition)
                break
    return callers


def refresh_effects(definitions: Sequence[dict]) -> list[str]:
    """Recompute `effects` on every staged definition in `definitions`, in place.

    A definition's effects are transitive through `call_definition` (§4, §6), so they are
    only as current as the bodies of everything it can reach. Saving one definition can
    therefore change another's -- and a chain A -> B -> C means editing C changes both of
    the others, which `callers_of` sees only one link of. So the whole list is recomputed
    rather than walked backwards: a config holds a handful of definitions, and this runs on
    a save rather than per note.

    Returns the guids whose effects actually changed, for a caller that wants to say so.
    """
    staged = [
        definition
        for definition in definitions
        if isinstance(definition, dict) and is_format_2(definition)
    ]
    lookup = make_lookup(staged)
    changed = []
    for definition in staged:
        effects = analyze_definition(definition, lookup=lookup).effects
        if definition.get("effects") != effects:
            definition["effects"] = effects
            changed.append(definition.get("guid", ""))
    return changed


def stage_definitions(
    definitions: Sequence[dict],
    new_guid: Optional[Callable[[], str]] = None,
) -> tuple[list[CopyDefinitionV2], list[str]]:
    """Migrate a config's definitions to format 2 and fill in their `effects`.

    `effects` is derived, never authored, and it is transitive through calls, so it can only
    be computed once every definition is available to look up. This is what a save, a
    startup migration and an import all need, which is why it lives next to the analyser
    rather than in any one of them.
    """
    from .definition_migration import migrate_definitions

    staged, problems = (
        migrate_definitions(list(definitions), new_guid=new_guid)
        if new_guid is not None
        else migrate_definitions(list(definitions))
    )
    lookup = make_lookup(staged)
    for definition in staged:
        result = analyze_definition(definition, lookup=lookup)
        definition["effects"] = result.effects
        problems.extend(
            f"'{definition.get('definition_name', '')}': {message}"
            for message in result.problem_messages()
        )
    return staged, problems
