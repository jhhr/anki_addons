"""The leaf stage handlers: everything that reads, computes or writes one thing.

Each handler takes the stage, the scope it runs in and the frame it belongs to, and either
produces a result for the scope or adds a mutation to the run's plan. Structural stages --
loops, reduce, condition, call -- live in `evaluator.py`; nothing here recurses.

Where a handler reproduces a format-1 behaviour that is not obvious, the comment says which
one, because "this looks needlessly specific" is exactly the kind of thing a later change
removes and a characterization test then fails on.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from anki.cards import Card
from anki.notes import Note
from aqt import mw

from ...shared.interpolate.interpolate_fields import (
    TARGET_NOTES_COUNT,
    interpolate_from_text,
)
from ...utils.duplicate_note import duplicate_note
from ...utils.media_files import MediaFileError
from ..copy_primitives import (
    CopyFailedException,
    apply_card_action_to_card,
    apply_card_actions_by_template,
    int_sort_by_field_value,
    sort_by_field_value,
)
from ..definition_schema import expression_is_code
from ..execute_code_wrappers import execute_code_for_files
from .context import SkipBlock, summarize
from .expressions import ExpressionContext, evaluate_text, evaluate_value


def describe_card(card: Card) -> str:
    """The card properties a card action can change, for the preview's list of changes."""
    parts = [f"deck {card.odid or card.did}", f"queue {card.queue}"]
    flag = card.user_flag()
    if flag:
        parts.append(f"flag {flag}")
    return ", ".join(parts)


def _record_note_changes(session, snapshot: Note, target: Note) -> None:
    """Say which fields and tags this stage changed, comparing against its own snapshot.

    The snapshot is the note as the stage found it, so this is exactly the stage's own
    contribution even when an earlier stage already edited the same note.

    Comparing every field is not free and a bulk run does it per note per stage, so it is
    skipped entirely when nothing is collecting a trace.
    """
    if not session.recording:
        return
    before = dict(zip(snapshot.keys(), snapshot.values()))
    for field, value in zip(target.keys(), target.values()):
        was = before.get(field)
        if was != value:
            session.record_mutation(
                f"note {target.id} {field}: {summarize(was, 80)} → {summarize(value, 80)}"
            )
    added = [tag for tag in target.tags if tag not in snapshot.tags]
    removed = [tag for tag in snapshot.tags if tag not in target.tags]
    if added:
        session.record_mutation(f"note {target.id} +tags {' '.join(added)}")
    if removed:
        session.record_mutation(f"note {target.id} -tags {' '.join(removed)}")


def binding_name(reference: Any) -> Optional[str]:
    if isinstance(reference, dict):
        name = reference.get("binding")
        if isinstance(name, str) and name:
            return name
    return None


def resolve_binding(env: dict, reference: Any, frame, stage: dict, what: str) -> Any:
    name = binding_name(reference)
    if name is None:
        raise frame.error(f"{what} names no binding", stage)
    if name not in env:
        raise frame.error(f"{what} names unknown binding '{name}'", stage)
    return env[name]


def resolve_note(env: dict, reference: Any, frame, stage: dict, what: str) -> Note:
    value = resolve_binding(env, reference, frame, stage, what)
    if not isinstance(value, Note):
        raise frame.error(f"{what} must be a note, but it holds {type(value).__name__}", stage)
    return value


def resolve_card(env: dict, reference: Any, frame, stage: dict, what: str) -> Card:
    value = resolve_binding(env, reference, frame, stage, what)
    if not isinstance(value, Card):
        raise frame.error(f"{what} must be a card, but it holds {type(value).__name__}", stage)
    return value


def make_context(
    frame,
    env: dict,
    stage: dict,
    source_note: Note,
    destination_note: Optional[Note] = None,
    isolated_variables: bool = False,
    purpose: str = "",
) -> ExpressionContext:
    return ExpressionContext(
        session=frame.session,
        frame=frame,
        environment=env,
        source_note=source_note,
        destination_note=destination_note,
        separator=frame.definition.get("legacy", {}).get("select_card_separator"),
        multiple_note_types=frame.multiple_note_types,
        stage=stage,
        isolated_variables=isolated_variables,
        purpose=purpose,
    )


# --------------------------------------------------------------------------------------
# Variables and lists
# --------------------------------------------------------------------------------------


def run_variable(stage: dict, env: dict, frame) -> Any:
    expression = stage.get("value") or {}
    ctx = make_context(
        frame,
        env,
        stage,
        source_note=frame.trigger_note,
        destination_note=frame.trigger_note,
        # Format 1 computed every variable from the trigger note alone, before anything
        # else, so none of them could read the ones declared earlier.
        isolated_variables=bool(expression.get("legacy_isolated_variables", False)),
    )
    return evaluate_value(expression, ctx)


def run_list_variable(stage: dict, env: dict, frame) -> list:
    return []


def run_store(stage: dict, env: dict, frame) -> None:
    target = resolve_binding(env, stage.get("target"), frame, stage, "store target")
    if not isinstance(target, list):
        raise frame.error("store target is not a list", stage)
    source_note = frame.trigger_note
    destination_note = frame.trigger_note
    legacy_source = binding_name(stage.get("legacy_source"))
    if legacy_source:
        source_note = resolve_note(env, stage["legacy_source"], frame, stage, "store source")
    legacy_destination = binding_name(stage.get("legacy_destination"))
    if legacy_destination:
        destination_note = resolve_note(
            env, stage["legacy_destination"], frame, stage, "store destination"
        )
    ctx = make_context(frame, env, stage, source_note, destination_note)
    target.append(evaluate_value(stage.get("value"), ctx))


# --------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------


def _select(ids: list[int], selection: dict) -> list[int]:
    strategy = selection.get("strategy", "all")
    count = selection.get("count")
    if strategy == "all" or not count:
        return ids
    if strategy == "random":
        # Without replacement: a finite selection never returns the same note twice.
        return random.sample(ids, min(int(count), len(ids)))
    # "first" takes them in the order the search returned, which is the intentional change
    # from format 1's `None` strategy popping card ids off the end (§11).
    return ids[: int(count)]


def _write_runs_on_unfocus(field_write: dict, session) -> bool:
    """Whether this field write is one the unfocus in progress should run.

    Only a migrated write can answer either question. Format 1 asked both per field write --
    which editor fields trigger it, and whether it runs on unfocus at all while editing or
    while adding -- and the migrator records the answers on the write.

    A write the stage editor produced carries neither key and always runs. Format 2 watches
    fields for the definition as a whole (§8), and a stage only runs once that test has been
    passed, so there is nothing left here to decide. Treating a missing key as "no" would
    skip every write in every natively authored definition -- quietly, because the tags and
    card actions in the same stage are not gated and would still apply.
    """
    if "unfocus_trigger_fields" in field_write:
        if session.field_only not in (field_write["unfocus_trigger_fields"] or []):
            return False
    flag = "unfocus_when_add" if session.unfocus_is_add else "unfocus_when_edit"
    # A slow write -- downloading audio, say -- could be left out of the unfocus run and
    # kept for the bulk action. Ignoring the flag ran it on every keystroke that left a
    # watched field, and overwrote a value the user had set by hand.
    return flag not in field_write or bool(field_write[flag])


def _sort_notes(notes: list[Note], selection: dict) -> list[Note]:
    sort_field = selection.get("sort_field")
    if not sort_field:
        return notes
    reverse = selection.get("sort_order", "descending") == "descending"
    if selection.get("sort_numeric"):
        # Format 1 sorted on int(value) with 0 for anything unparsable, never on the text.
        notes.sort(key=lambda note: int_sort_by_field_value(note, sort_field), reverse=reverse)
    else:
        notes.sort(key=lambda note: sort_by_field_value(note, sort_field), reverse=reverse)
    return notes


def _empty_result(stage: dict, frame, query: str, kind: str) -> list:
    if stage.get("error_if_empty"):
        frame.session.logger.error(
            f"Error in copy fields: Did not find any {kind} with query='{query}'"
        )
    else:
        frame.session.logger.debug(f'No {kind} found with query="{query}"')
    if_empty = stage.get("if_empty", "continue")
    if if_empty == "error":
        raise frame.error(f"Query '{query}' matched no {kind}", stage)
    if if_empty == "skip_block":
        raise SkipBlock()
    return []


def run_query(stage: dict, env: dict, frame, is_card_query: bool) -> list:
    session = frame.session
    kind = "cards" if is_card_query else "notes"
    ctx = make_context(frame, env, stage, frame.trigger_note, frame.trigger_note)
    query = evaluate_text(stage.get("query"), ctx)
    if not query:
        # Format 1 logged and selected nothing rather than failing the definition.
        session.logger.error("Error in copy fields: Could not interpolate copy_from_cards_query")
        return []

    selection = stage.get("selection") or {}
    selection_error = selection.get("selection_error")
    if selection_error:
        session.logger.error(selection_error)
        return []

    session.check_cancel()
    ids = session.find_cards(query) if is_card_query else session.find_notes(query)
    session.record_detail("query", query)
    session.record_detail("found", len(ids))
    if not ids:
        session.record_detail("selected", 0)
        return _empty_result(stage, frame, query, kind)

    selected = _select(list(ids), selection)
    session.record_detail("selected", len(selected))
    if is_card_query:
        cards = [session.card_by_id(card_id) for card_id in selected]
        return _sort_cards(cards, selection, session)
    notes = [session.note_by_id(note_id) for note_id in selected]
    notes = _sort_notes(notes, selection)
    if stage.get("counts_as_sources"):
        session.update_counts(processed_sources_inc=len(notes))
    # Format 1 published the size of the query result as a variable for every across-note
    # definition; migrated expressions still read it as `{{__Target_Notes_Count}}`.
    frame.legacy_values[TARGET_NOTES_COUNT] = len(notes)
    return notes


def _sort_cards(cards: list[Card], selection: dict, session) -> list[Card]:
    sort_field = selection.get("sort_field")
    if not sort_field:
        return cards
    reverse = selection.get("sort_order", "descending") == "descending"
    key = int_sort_by_field_value if selection.get("sort_numeric") else sort_by_field_value
    cards.sort(key=lambda card: key(session.note_by_id(card.nid), sort_field), reverse=reverse)
    return cards


# --------------------------------------------------------------------------------------
# Notes and cards
# --------------------------------------------------------------------------------------


def run_edit_note(stage: dict, env: dict, frame) -> None:
    session = frame.session
    target = session.working_note(resolve_note(env, stage.get("target"), frame, stage, "target"))
    # Every right-hand side in this stage reads the note as it was when the stage started,
    # which is what lets one stage swap two fields (§5.3).
    snapshot = duplicate_note(target)
    source_note = snapshot
    if binding_name(stage.get("legacy_source")):
        source_note = resolve_note(env, stage["legacy_source"], frame, stage, "source")
    # Inside this stage the target's own binding names the snapshot, so `{{trigger.Word}}`
    # on the right of a write reads the value the stage started with rather than one an
    # earlier write in the same stage has already replaced.
    write_env = dict(env)
    target_name = binding_name(stage.get("target"))
    if target_name:
        write_env[target_name] = snapshot

    field_writes = []
    for field_write in stage.get("fields") or []:
        if not isinstance(field_write, dict):
            continue
        if session.field_only is not None and not _write_runs_on_unfocus(field_write, session):
            continue
        field = field_write.get("field", "")
        try:
            target[field]
        except KeyError:
            # Checked for every write before any of them is applied, so a definition naming a
            # field the note does not have leaves the note untouched (§5.3).
            raise frame.error(
                f"Error in copy fields: Field '{field}' not found in note", stage
            ) from None
        field_writes.append(field_write)

    modified = False
    for field_write in field_writes:
        field = field_write["field"]
        if field_write.get("write_if", "always") == "empty" and target[field] != "":
            # Read live rather than from the snapshot: an earlier write in this same stage
            # counts as filling the field, which is what format 1 did.
            continue
        ctx = make_context(
            frame, write_env, stage, source_note, snapshot, purpose=f"field {field}"
        )
        target[field] = evaluate_text(field_write.get("value"), ctx)
        modified = True

    tags = stage.get("tags") or {}
    for tag in tags.get("add") or []:
        if not target.has_tag(tag):
            target.add_tag(tag)
            modified = True
    for tag in tags.get("remove") or []:
        if target.has_tag(tag):
            target.remove_tag(tag)
            modified = True

    if modified:
        session.mark_note_modified(target)
        session.update_counts(processed_destinations_inc=1)
        _record_note_changes(session, snapshot, target)

    cards = session.cards_of_note(target)
    # Format 1 handed every card of every destination note to the caller, edited or not: the
    # sync path writes its `fc` flag onto all of them.
    session.touch_cards(cards)
    card_actions = stage.get("card_actions") or []
    if card_actions:
        try:
            apply_card_actions_by_template(
                card_actions, target, cards, session.logger, session.progress_updater
            )
        except CopyFailedException as error:
            raise frame.error(str(error), stage) from error
        for card in cards:
            if getattr(card, "edited", False):
                session.mark_card_edited(card)
                if session.recording:
                    session.record_mutation(f"card {card.id}: {describe_card(card)}")


def run_edit_card(stage: dict, env: dict, frame) -> None:
    session = frame.session
    card = resolve_card(env, stage.get("target"), frame, stage, "target")
    card = session.card_by_id(card.id) if card.id else card
    note = session.note_by_id(card.nid) if card.nid else frame.trigger_note
    for card_action in stage.get("card_actions") or []:
        if not isinstance(card_action, dict):
            continue
        try:
            # No card type selector here: the action applies to exactly this card (§5.12).
            edited = apply_card_action_to_card(card_action, card, note, logger=session.logger)
        except CopyFailedException as error:
            raise frame.error(str(error), stage) from error
        if edited:
            session.mark_card_edited(card)
            session.update_counts(processed_cards_inc=1)
            if session.recording:
                session.record_mutation(f"card {card.id}: {describe_card(card)}")
    session.touch_cards([card])


# --------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------


def run_read_file(stage: dict, env: dict, frame) -> str:
    ctx = make_context(frame, env, stage, frame.trigger_note, frame.trigger_note)
    filename = evaluate_text(stage.get("filename"), ctx)
    try:
        content = frame.session.read_file(filename)
    except MediaFileError as error:
        raise frame.error(str(error), stage) from error
    except UnicodeDecodeError as error:
        raise frame.error(f"File '{filename}' is not valid UTF-8: {error}", stage) from error
    frame.session.record_detail("filename", filename)
    if content is not None:
        return content
    frame.session.record_detail("missing", True)
    if_missing = stage.get("if_missing", "empty")
    if if_missing == "error":
        raise frame.error(f"File '{filename}' does not exist", stage)
    if if_missing == "skip_block":
        raise SkipBlock()
    return ""


def run_write_file(stage: dict, env: dict, frame) -> None:
    session = frame.session
    source_note = frame.trigger_note
    if binding_name(stage.get("legacy_source")):
        source_note = resolve_note(env, stage["legacy_source"], frame, stage, "source")
    destination_note = source_note
    if binding_name(stage.get("legacy_destination")):
        destination_note = resolve_note(
            env, stage["legacy_destination"], frame, stage, "destination"
        )

    content_expression = stage.get("content") or {}
    overwrite = bool(stage.get("overwrite", False))
    skip_if_exists = bool(stage.get("skip_if_exists", False))

    if expression_is_code(content_expression):
        # The code path names its own files: it returns (filename, content) pairs, so the
        # stage's filename expression is not used at all.
        ctx = make_context(frame, env, stage, source_note, destination_note)
        pairs = _code_file_pairs(content_expression, ctx, stage, frame)
        wrote = False
        for filename, content in pairs:
            wrote = _queue_file(frame, stage, filename, content, overwrite, skip_if_exists) or wrote
        if wrote:
            session.update_counts(processed_files_inc=1)
        return

    filename_ctx = make_context(frame, env, stage, destination_note, destination_note)
    filename = evaluate_text(stage.get("filename"), filename_ctx)
    if not filename:
        raise frame.error("Error in copy fields: No file name provided", stage)
    if skip_if_exists and filename not in session.file_overlay:
        try:
            from ...utils.media_files import media_file_exists

            if media_file_exists(filename):
                return
        except MediaFileError as error:
            raise frame.error(str(error), stage) from error
    content_ctx = make_context(
        frame, env, stage, source_note, destination_note, purpose=f"file {filename}"
    )
    content = evaluate_text(content_expression, content_ctx)
    if _queue_file(frame, stage, filename, content, overwrite, skip_if_exists):
        session.update_counts(processed_files_inc=1)


def _code_file_pairs(expression: dict, ctx: ExpressionContext, stage: dict, frame) -> list:
    from ..definition_schema import expression_is_legacy_syntax, expression_source
    from .expressions import resolve_references

    source = expression_source(expression)
    if expression_is_legacy_syntax(expression):
        interpolated, invalid = interpolate_from_text(
            source,
            source_note=ctx.source_note,
            destination_note=ctx.destination_note,
            variable_values_dict=ctx.variables(),
            multiple_note_types=ctx.multiple_note_types,
        )
        if invalid:
            ctx.logger.error(
                "Error in copy fields: Invalid fields in copy_as_code:"
                f" {', '.join(invalid)}"
            )
    else:
        interpolated = resolve_references(source, ctx)
    pairs, code_error = execute_code_for_files(interpolated or "", ctx.source_note)
    if code_error:
        raise frame.error(f"Code execution error in file definition:\n{code_error}", stage)
    frame.session.render_progress()
    return list(pairs or [])


def _queue_file(
    frame, stage: dict, filename: str, content: str, overwrite: bool, skip_if_exists: bool
) -> bool:
    try:
        return frame.session.queue_file_write(
            filename, content, overwrite=overwrite, skip_if_exists=skip_if_exists
        )
    except MediaFileError as error:
        raise frame.error(f"Error in writing to file: {error}", stage) from error


# --------------------------------------------------------------------------------------
# Condition predicate
# --------------------------------------------------------------------------------------


def evaluate_predicate(stage: dict, env: dict, frame) -> bool:
    """Whether the condition's `then` branch runs.

    A migrated condition is an Anki search scoped to one note, which is how format 1 ran the
    copy condition; a newly authored one is boolean code or a scalar the branch takes the
    truthiness of.
    """
    session = frame.session
    if stage.get("only_on_sync") and not session.is_sync:
        # `condition_only_on_sync`: outside a sync the condition is not checked at all, so
        # the definition runs as if there were none.
        return True

    if stage.get("predicate_kind") == "note_query":
        target = frame.trigger_note
        if binding_name(stage.get("predicate_target")):
            target = resolve_note(
                env, stage["predicate_target"], frame, stage, "predicate target"
            )
        expression = stage.get("predicate") or {}
        ctx = make_context(frame, env, stage, target, target)
        raw_query = expression.get("text", "") or ""
        interpolated, invalid = interpolate_from_text(
            raw_query,
            source_note=target,
            variable_values_dict=ctx.variables(),
        )
        if not interpolated:
            raise frame.error(
                f"Error in copy fields: Condition query '{raw_query}' could not be"
                f" interpolated for note id {target.id} due to missing fields:"
                f" {', '.join(invalid)}",
                stage,
            )
        note_ids = mw.col.find_notes(f"{interpolated} nid:{target.id}")
        if not note_ids:
            session.logger.debug(
                "copy_for_single_trigger_note: "
                f"Condition query '{interpolated}' did not match for note "
                f"id {target.id}"
            )
            return False
        return True

    ctx = make_context(frame, env, stage, frame.trigger_note, frame.trigger_note)
    value = evaluate_value(stage.get("predicate"), ctx)
    if isinstance(value, str):
        # A text predicate is true when it produced anything but an empty string, so a
        # `{{Field}}` predicate reads as "this field has a value".
        return bool(value.strip())
    return bool(value)


__all__ = [
    "evaluate_predicate",
    "make_context",
    "resolve_binding",
    "resolve_card",
    "resolve_note",
    "run_edit_card",
    "run_edit_note",
    "run_list_variable",
    "run_query",
    "run_read_file",
    "run_store",
    "run_variable",
    "run_write_file",
]
