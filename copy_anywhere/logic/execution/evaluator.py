"""Block dispatch and the structural stages: loops, reduce, condition, call.

A block is an ordered list of stages sharing one lexical scope. Running it means running
each enabled stage against the scope in front of it and adding whatever it produced, which
is precisely what `flow_analysis.analyze_block()` predicts at edit time -- one walk, two
consumers, so the menu a user sees and the bindings the evaluator has cannot drift apart.

Structural stages create child scopes and throw them away again. A loop body's variables do
not survive the iteration; a branch's results do not escape the branch. What does survive is
what the enclosing scope already held -- including a list, which is why appending to one is
the way a loop reports anything back.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from ...shared.interpolate.interpolate_fields import QUERY_NOTE_INDEX
from ..definition_schema import (
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
    is_format_2,
    stage_result_name,
)
from . import actions
from .context import (
    DefinitionFrame,
    SkipBlock,
    StageError,
    TraceEvent,
    TriggerSkipped,
    summarize,
)
from .expressions import evaluate_value

#: The same hard limit the analyser uses, repeated at run time because the JSON may have
#: been hand-edited since it was last analysed (§5.9).
MAX_CALL_DEPTH = 32


class Cancelled(Exception):
    """The user asked to stop. Nothing half-evaluated is committed (§7.1)."""


def execute_block(
    stages: Sequence[dict],
    env: dict,
    frame: DefinitionFrame,
    parent_event: Optional[TraceEvent] = None,
) -> None:
    """Run every enabled stage in `stages`, updating `env` as results appear."""
    for stage in stages or []:
        if not isinstance(stage, dict) or not stage.get("enabled", True):
            continue
        execute_stage(stage, env, frame, parent_event)


def execute_stage(
    stage: dict,
    env: dict,
    frame: DefinitionFrame,
    parent_event: Optional[TraceEvent] = None,
) -> None:
    session = frame.session
    event = session.start_event(stage, frame.loop_path, parent_event, env)
    stage_type = stage.get("type")
    # Actions record what they planned against whichever stage is running, so the trace can
    # say which stage caused a change without every handler taking a trace parameter.
    outer_event = session.current_event
    session.current_event = event
    try:
        result = _dispatch(stage, env, frame, event)
    except SkipBlock:
        session.finish_event(event, "skipped")
        raise
    except TriggerSkipped:
        session.finish_event(event, "skipped")
        raise
    except Cancelled:
        session.finish_event(event, "cancelled")
        raise
    except StageError as error:
        error.with_context(
            definition_guid=frame.guid,
            definition_name=frame.name,
            stage_guid=stage.get("guid"),
            stage_type=stage_type,
            note_id=frame.trigger_note.id if frame.trigger_note is not None else None,
            loop_path=tuple(frame.loop_path),
        )
        session.finish_event(event, "failed", error=error.message)
        raise
    except Exception as error:  # noqa: BLE001 -- recorded, then left to the caller
        # Anything the evaluator did not raise itself -- an Anki search error, a bug in a
        # process -- keeps its own type and traceback. Turning those into a logged
        # `StageError` would hide the stack that says where they actually came from.
        session.finish_event(event, "failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        session.current_event = outer_event

    name = stage_result_name(stage)
    if name and stage_type != STAGE_CALL_DEFINITION:
        env[name] = result
    session.finish_event(event, "ok", result=result)


def _dispatch(stage: dict, env: dict, frame: DefinitionFrame, event: Optional[TraceEvent]) -> Any:
    stage_type = stage.get("type")

    if stage_type == STAGE_VARIABLE:
        return actions.run_variable(stage, env, frame)
    if stage_type == STAGE_LIST_VARIABLE:
        return actions.run_list_variable(stage, env, frame)
    if stage_type == STAGE_STORE:
        return actions.run_store(stage, env, frame)
    if stage_type == STAGE_NOTE_QUERY:
        return actions.run_query(stage, env, frame, is_card_query=False)
    if stage_type == STAGE_CARD_QUERY:
        return actions.run_query(stage, env, frame, is_card_query=True)
    if stage_type == STAGE_EDIT_NOTE:
        return actions.run_edit_note(stage, env, frame)
    if stage_type == STAGE_EDIT_CARD:
        return actions.run_edit_card(stage, env, frame)
    if stage_type == STAGE_READ_FILE:
        return actions.run_read_file(stage, env, frame)
    if stage_type == STAGE_WRITE_FILE:
        return actions.run_write_file(stage, env, frame)
    if stage_type == STAGE_FOR_EACH_NOTE:
        return _run_loop(stage, env, frame, event, is_card_loop=False)
    if stage_type == STAGE_FOR_EACH_CARD:
        return _run_loop(stage, env, frame, event, is_card_loop=True)
    if stage_type == STAGE_REDUCE:
        return _run_reduce(stage, env, frame, event)
    if stage_type == STAGE_CONDITION:
        return _run_condition(stage, env, frame, event)
    if stage_type == STAGE_CALL_DEFINITION:
        return _run_call(stage, env, frame, event)
    raise frame.error(f"unknown stage type '{stage_type}'", stage)


# --------------------------------------------------------------------------------------
# Structural stages
# --------------------------------------------------------------------------------------


def _run_loop(
    stage: dict,
    env: dict,
    frame: DefinitionFrame,
    event: Optional[TraceEvent],
    is_card_loop: bool,
) -> None:
    session = frame.session
    items = actions.resolve_binding(env, stage.get("input"), frame, stage, "loop input")
    if not isinstance(items, list):
        raise frame.error("loop input is not a list", stage)
    item_binding = stage.get("item_binding") or ("card" if is_card_loop else "note")
    note_binding = stage.get("note_binding") or "note"
    count = len(items)
    session.record_detail("iterations", count)

    previous_index = frame.legacy_values.get(QUERY_NOTE_INDEX)
    for index, item in enumerate(items, 1):
        if session.check_cancel():
            raise Cancelled()
        # Body-local results are discarded after each iteration: the body gets a copy of the
        # scope, and only the objects it was already holding (a list, a note) outlive it.
        body_env = dict(env)
        body_env[item_binding] = item
        if is_card_loop:
            body_env[note_binding] = session.note_by_id(item.nid)
        body_env["index"] = index
        body_env["count"] = count
        # Format-1 expressions read the position in the query result as a variable.
        frame.legacy_values[QUERY_NOTE_INDEX] = index
        frame.loop_path.append(index)
        try:
            execute_block(stage.get("body") or [], body_env, frame, event)
        except SkipBlock:
            # `skip_block` inside a loop body ends that iteration, not the loop.
            pass
        finally:
            frame.loop_path.pop()
    if previous_index is None:
        frame.legacy_values.pop(QUERY_NOTE_INDEX, None)
    else:
        frame.legacy_values[QUERY_NOTE_INDEX] = previous_index


def _run_reduce(
    stage: dict, env: dict, frame: DefinitionFrame, event: Optional[TraceEvent]
) -> Any:
    items = actions.resolve_binding(env, stage.get("input"), frame, stage, "reduce input")
    if not isinstance(items, list):
        raise frame.error("reduce input is not a list", stage)

    frame.session.record_detail("items", len(items))
    if stage.get("operation") == "join":
        # The join the migrator synthesizes: format 1's `select_card_separator` between the
        # values it read from each source note, with no separator before the first.
        separator = stage.get("separator")
        if separator is None:
            separator = ", "
        return separator.join("" if item is None else str(item) for item in items)

    ctx = actions.make_context(frame, env, stage, frame.trigger_note, frame.trigger_note)
    accumulator = evaluate_value(stage.get("initial"), ctx)
    item_binding = stage.get("item_binding") or "item"
    accumulator_binding = stage.get("accumulator_binding") or "accumulator"
    count = len(items)
    for index, item in enumerate(items, 1):
        # A version-2 reducer is pure: it computes the next accumulator and cannot contain
        # mutation stages, so a fold never hides a write (§5.7).
        reducer_env = dict(env)
        reducer_env[item_binding] = item
        reducer_env[accumulator_binding] = accumulator
        reducer_env["index"] = index
        reducer_env["count"] = count
        reducer_ctx = actions.make_context(
            frame, reducer_env, stage, frame.trigger_note, frame.trigger_note
        )
        accumulator = evaluate_value(stage.get("value"), reducer_ctx)
    return accumulator


def _run_condition(
    stage: dict, env: dict, frame: DefinitionFrame, event: Optional[TraceEvent]
) -> None:
    matched = actions.evaluate_predicate(stage, env, frame)
    if event is not None:
        event.details["matched"] = matched
    if not matched and stage.get("predicate_kind") == "note_query" and not stage.get("else"):
        # A migrated copy condition that does not match is not a branch that took the other
        # path: format 1 skipped the trigger note entirely and reported it as skipped.
        raise TriggerSkipped()
    branch = stage.get("then") if matched else stage.get("else")
    # Branch-local results do not escape, so the branch runs against a copy of the scope.
    try:
        execute_block(branch or [], dict(env), frame, event)
    except SkipBlock:
        # `skip_block` inside a branch ends that branch and nothing more, the way it ends
        # one iteration of a loop body rather than the loop. The block it names is the one
        # the stage is in, which is what the editor's "stop running the rest of this block"
        # says and what the analyser assumes: only a root-level skip makes the results after
        # it maybe-unset (§5.9), so a branch that could end the whole definition would make
        # an export the analyser accepted sometimes missing at runtime.
        pass


def _run_call(
    stage: dict, env: dict, frame: DefinitionFrame, event: Optional[TraceEvent]
) -> None:
    session = frame.session
    callee_guid = stage.get("definition_guid")
    if not callee_guid:
        raise frame.error("call names no definition", stage)
    if session.definition_lookup is None:
        raise frame.error("no definitions are available to call", stage)
    if callee_guid in session.call_stack:
        path = " -> ".join(session.call_stack + [callee_guid])
        raise frame.error(f"call cycle: {path}", stage)
    if frame.depth + 1 > MAX_CALL_DEPTH:
        raise frame.error(f"call chain deeper than {MAX_CALL_DEPTH} definitions", stage)

    callee = session.definition_lookup(callee_guid)
    if callee is None:
        raise frame.error(f"calls unknown definition '{callee_guid}'", stage)
    if not is_format_2(callee):
        raise frame.error(
            f"definition '{callee_guid}' is not in format 2 and cannot be called", stage
        )

    trigger = actions.resolve_note(env, stage.get("trigger"), frame, stage, "call trigger")
    if session.check_cancel():
        raise Cancelled()

    # A fresh frame: the callee sees its trigger note and nothing else of the caller's --
    # no variables, no lists, no loop bindings, no result names (§5.9).
    callee_frame = DefinitionFrame(callee, session.working_note(trigger), session, frame.depth + 1)
    session.record_detail("calls", callee.get("definition_name") or callee_guid)
    session.call_stack.append(callee_guid)
    try:
        exported = execute_definition(callee_frame, parent_event=event)
    finally:
        session.call_stack.pop()

    for output in stage.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        export_name = output.get("export")
        result_name = output.get("result")
        if export_name not in exported:
            raise frame.error(
                f"definition '{callee.get('definition_name', callee_guid)}'"
                f" exports no '{export_name}'",
                stage,
            )
        value = exported[export_name]
        # Lists are copied on the way out so the child frame stays isolated; note and card
        # references still resolve through the shared overlays.
        env[result_name] = list(value) if isinstance(value, list) else value
        session.record_detail(f"output {result_name}", summarize(value))


# --------------------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------------------


def execute_definition(
    frame: DefinitionFrame, parent_event: Optional[TraceEvent] = None
) -> dict:
    """Run one definition's root block and return the values it exports."""
    env: dict = {"trigger": frame.trigger_note}
    frame.mark_root_environment(env)
    legacy = frame.definition.get("legacy") or {}
    default_index = legacy.get("query_note_index_default")
    if default_index is not None:
        frame.legacy_values[QUERY_NOTE_INDEX] = default_index
    try:
        execute_block(frame.definition.get("stages") or [], env, frame, parent_event)
    except SkipBlock:
        # A root-level `skip_block` ends the definition; what ran before it still counts.
        pass
    except TriggerSkipped:
        # A called definition whose own condition does not match simply does nothing. At the
        # top level this is the caller's signal that the trigger note was skipped, so it is
        # only swallowed for a nested call.
        if frame.depth == 0:
            raise

    exported: dict = {}
    for export in frame.definition.get("exports") or []:
        if not isinstance(export, dict):
            continue
        name = export.get("name")
        stage_guid = export.get("stage_guid")
        for stage in frame.definition.get("stages") or []:
            if isinstance(stage, dict) and stage.get("guid") == stage_guid:
                result_name = stage_result_name(stage)
                if frame.root_env is not None and result_name in frame.root_env:
                    exported[name] = frame.root_env[result_name]
                break
    return exported
