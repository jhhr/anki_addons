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
import logging
from typing import Any, Optional

from anki.cards import Card
from anki.notes import Note

from ...shared.interpolate.interpolate_fields import TARGET_NOTES_COUNT
from ...utils.duplicate_note import duplicate_note
from ...utils.media_files import MediaFileError, normalize_media_filename
from ..copy_primitives import (
    CopyFailedException,
    apply_card_action_to_card,
    apply_card_actions_by_template,
    int_sort_by_field_value,
    sort_by_field_value,
)
from ..definition_schema import expression_is_code
from ..execute_code_wrappers import execute_code_for_files
from ..unsaved_note_search import SearchSyntaxError, UnjudgeableSearch, matches
from .context import Cancelled, SkipBlock, summarize
from .expressions import ExpressionContext, evaluate_text, evaluate_value

logger = logging.getLogger(__name__)


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
    purpose: str = "",
) -> ExpressionContext:
    return ExpressionContext(
        session=frame.session,
        frame=frame,
        environment=env,
        source_note=source_note,
        destination_note=destination_note,
        multiple_note_types=frame.multiple_note_types,
        stage=stage,
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
    )
    return evaluate_value(expression, ctx)


def run_list_variable(stage: dict, env: dict, frame) -> list:
    return []


def run_store(stage: dict, env: dict, frame) -> None:
    target = resolve_binding(env, stage.get("target"), frame, stage, "store target")
    if not isinstance(target, list):
        raise frame.error("store target is not a list", stage)
    ctx = make_context(frame, env, stage, frame.trigger_note, frame.trigger_note)
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


def runs_on_unfocus(carrier: dict, session) -> bool:
    """Whether this migrated write -- or the stage feeding one -- should run this unfocus.

    Only a migrated thing can answer either question. Format 1 asked both per field write --
    which editor fields trigger it, and whether it runs on unfocus at all while editing or
    while adding -- and the migrator records the answers on the write.

    A write the stage editor produced carries neither key and always runs. Format 2 watches
    fields for the definition as a whole (§8), and a stage only runs once that test has been
    passed, so there is nothing left here to decide. Treating a missing key as "no" would
    skip every write in every natively authored definition -- quietly, because the tags and
    card actions in the same stage are not gated and would still apply.

    The same answer has to be available a level up. Migrating Destination-to-sources moves
    each write's per-source read *out* of the write, into a list, a loop and a reduce in
    front of it, and those stages are the expensive half: the code or the process chain runs
    once per source note there, not in the write. Gating only the write would leave them
    running on every unfocus of every watched field, which is why the migrator copies these
    keys onto them and `execute_stage` asks the same question of a stage.
    """
    if "unfocus_trigger_fields" in carrier:
        if session.field_only not in (carrier["unfocus_trigger_fields"] or []):
            return False
    flag = "unfocus_when_add" if session.unfocus_is_add else "unfocus_when_edit"
    # A slow write -- downloading audio, say -- could be left out of the unfocus run and
    # kept for the bulk action. Ignoring the flag ran it on every keystroke that left a
    # watched field, and overwrote a value the user had set by hand.
    return flag not in carrier or bool(carrier[flag])


def feeds_a_filled_field(carrier: dict, frame) -> bool:
    """Whether this migrated stage feeds a `write_if: "empty"` write whose field is filled.

    The other question format 1 asked of a field write before evaluating it, and the same
    answer moved a level up for the same reason as `runs_on_unfocus`: the migrator copies
    the write's `write_if` onto the stages that produce its value, with `write_if_field`
    naming the field, so the per-source read in front of the write is skipped when the write
    itself would be. Only a Destination-to-sources migration produces those stages, and it
    always writes the trigger note, so that is the note asked.

    Read live, as the write reads it: an earlier stage of this run filling the field counts.
    A field the note does not have is not "filled" -- the write reports that as its own
    error, and the stages feeding it are left to run so nothing about that changes.
    """
    if carrier.get("write_if", "always") != "empty" or frame.trigger_note is None:
        return False
    target = frame.session.working_note(frame.trigger_note)
    try:
        return target[carrier.get("write_if_field", "")] != ""
    except KeyError:
        return False


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


def _apply_if_empty(stage: dict, frame, message: str) -> list:
    """The stage's `if_empty` policy, for every way a query can hand back no notes.

    Selecting nothing is the same event whether the query ran and matched nothing or never
    ran at all, and format 1 treated it as one: `get_across_target_notes` returned `[]` for
    a refusal exactly as it did for an empty match, and the caller's one early return
    covered both "so that the target fields aren't wiped" (§11 step 4).
    """
    if_empty = stage.get("if_empty", "continue")
    if if_empty == "error":
        raise frame.error(message, stage)
    if if_empty == "skip_block":
        raise SkipBlock()
    return []


def _empty_result(stage: dict, frame, query: str, kind: str) -> list:
    if stage.get("error_if_empty"):
        logger.error("Error in copy fields: Did not find any %s with query='%s'", kind, query)
    else:
        logger.debug('No %s found with query="%s"', kind, query)
    return _apply_if_empty(stage, frame, f"Query '{query}' matched no {kind}")


def _search_text(resolved: Optional[str]) -> str:
    """What a resolved expression is worth as an Anki search: the text, stripped.

    `find_notes("   ")` matches every note in the collection, so a search that resolved to
    whitespace -- one reference to a field holding a space -- is as empty as one that
    resolved to nothing, and the caller's guard for the empty case has to see it as such.
    What the guard does about it is the caller's: a query stage selects nothing, a
    condition fails.
    """
    return (resolved or "").strip()


def run_query(stage: dict, env: dict, frame, is_card_query: bool) -> list:
    session = frame.session
    kind = "cards" if is_card_query else "notes"
    ctx = make_context(frame, env, stage, frame.trigger_note, frame.trigger_note)
    query = _search_text(evaluate_text(stage.get("query"), ctx))
    if not query:
        # Format 1 logged and selected nothing rather than failing the definition. The
        # complaint is the log line, so `error_if_empty` -- which is about a query that ran
        # -- is not repeated here; what `if_empty` decides is whether the rest of the block
        # still runs over nothing.
        logger.error("Error in copy fields: Could not interpolate copy_from_cards_query")
        return _apply_if_empty(stage, frame, f"Query for {kind} could not be interpolated")

    selection = stage.get("selection") or {}
    selection_error = selection.get("selection_error")
    if selection_error:
        logger.error(selection_error)
        return _apply_if_empty(stage, frame, selection_error)

    if session.check_cancel():
        raise Cancelled()
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
    frame.runtime_values[TARGET_NOTES_COUNT] = len(notes)
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
        if session.field_only is not None and not runs_on_unfocus(field_write, session):
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
            frame, write_env, stage, snapshot, snapshot, purpose=f"field {field}"
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
        _record_note_changes(session, snapshot, target)

    card_actions = stage.get("card_actions") or []
    if card_actions and not target.id:
        # A note being added has no cards until the add creates them, and nothing runs this
        # stage again afterwards, so the actions have nothing to reach. The field writes
        # above have landed on the note object the add saves; only the actions are lost,
        # and the log says so rather than nothing.
        logger.warning(
            "Card actions on '%s' skipped: the note is being added and has no cards yet",
            stage.get("name") or stage.get("type"),
        )
    elif card_actions:
        cards = session.cards_of_note(target)
        try:
            edited = apply_card_actions_by_template(card_actions, target, cards)
        except CopyFailedException as error:
            raise frame.error(str(error), stage) from error
        # What this stage changed, not every card still carrying `edited`: the mark stays on
        # a card until the run is committed, so an earlier stage's change would otherwise be
        # listed again as this one's.
        for card in edited:
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
            edited = apply_card_action_to_card(card_action, card, note)
        except CopyFailedException as error:
            raise frame.error(str(error), stage) from error
        if edited:
            session.mark_card_edited(card)
            if session.recording:
                session.record_mutation(f"card {card.id}: {describe_card(card)}")


# --------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------


def run_read_file(stage: dict, env: dict, frame) -> str:
    ctx = make_context(frame, env, stage, frame.trigger_note, frame.trigger_note)
    filename = evaluate_text(stage.get("filename"), ctx)
    try:
        # The messages name the file actually looked for, with the leading `_` the read
        # adds: a user told "'dictionary.txt' does not exist" while looking at it in the
        # media folder has been told nothing.
        name = normalize_media_filename(filename)
        content = frame.session.read_file(name)
    except MediaFileError as error:
        raise frame.error(str(error), stage) from error
    except UnicodeDecodeError as error:
        raise frame.error(f"File '{name}' is not valid UTF-8: {error}", stage) from error
    frame.session.record_detail("filename", name)
    if content is not None:
        return content
    frame.session.record_detail("missing", True)
    if_missing = stage.get("if_missing", "empty")
    if if_missing == "error":
        raise frame.error(f"File '{name}' does not exist", stage)
    if if_missing == "skip_block":
        raise SkipBlock()
    return ""


def run_write_file(stage: dict, env: dict, frame) -> None:
    session = frame.session
    # A file write names no note of its own -- its references name their bindings -- so the
    # note behind it is the trigger, which is what code mode gets as `note` unless the
    # stage sits in a loop that binds that name itself.
    note = frame.trigger_note

    content_expression = stage.get("content") or {}
    overwrite = bool(stage.get("overwrite", False))
    skip_if_exists = bool(stage.get("skip_if_exists", False))

    if expression_is_code(content_expression):
        # The code path names its own files: it returns (filename, content) pairs, so the
        # stage's filename expression is not used at all.
        ctx = make_context(frame, env, stage, note, note)
        pairs = _code_file_pairs(content_expression, ctx, stage, frame)
        wrote = False
        for filename, content in pairs:
            wrote = _queue_file(frame, stage, filename, content, overwrite, skip_if_exists) or wrote
        if wrote:
            session.update_counts(processed_files_inc=1)
        return

    filename_ctx = make_context(frame, env, stage, note, note)
    filename = evaluate_text(stage.get("filename"), filename_ctx)
    if not filename:
        raise frame.error("Error in copy fields: No file name provided", stage)
    if skip_if_exists:
        # Asked before the content is evaluated, so a write that is going to be skipped does
        # not pay for the expression that feeds it. `queue_file_write` asks the same question
        # again for the writes that get that far, including every pair the code path names.
        try:
            if session.file_is_already_there(filename):
                return
        except MediaFileError as error:
            raise frame.error(str(error), stage) from error
    content_ctx = make_context(frame, env, stage, note, note, purpose=f"file {filename}")
    content = evaluate_text(content_expression, content_ctx)
    if _queue_file(frame, stage, filename, content, overwrite, skip_if_exists):
        session.update_counts(processed_files_inc=1)


def _code_file_pairs(expression: dict, ctx: ExpressionContext, stage: dict, frame) -> list:
    from ..definition_schema import expression_source
    from .expressions import code_globals, resolve_references

    interpolated = resolve_references(expression_source(expression), ctx)
    # The stage's bindings, the same names every other code expression runs with: a file
    # write inside a loop has to be able to say `note` and mean the note being looped over,
    # which is what a migrated Destination-to-sources file write always meant by it.
    pairs, code_error = execute_code_for_files(
        interpolated or "", ctx.source_note, extra_globals=code_globals(ctx)
    )
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


def _unsaved_note_matches(
    stage: dict, frame, target: Note, raw_query: str, interpolated: str
) -> bool:
    """A search condition asked of a note that is being added, so has no row to search.

    `nid:0` finds nothing, so the search is judged in Python against the note as it stands
    and the deck it is being added to, which is what Anki would answer once it is saved. A
    term that answer depends on something this note does not have yet -- its cards, its
    review history, its id -- fails the definition naming the term, rather than guessing
    and silently running or skipping it.
    """
    session = frame.session
    # The same parentheses as the collection's search, so an unbalanced `)` or a newline in
    # the resolved text reads the same way on both paths.
    search = f"({interpolated})"
    session.record_detail("query", search)
    session.record_detail("judged", "without the collection: the note is not added yet")
    try:
        matched = matches(search, target, session.deck_id)
    except UnjudgeableSearch as error:
        raise frame.error(
            f"Error in copy fields: Condition query '{raw_query}': the search term"
            f" '{error.term}' cannot be judged for a note that is not added yet"
            f" ({error.reason})",
            stage,
        ) from error
    except SearchSyntaxError as error:
        # On a saved note the collection's search raises for the same text; this names the
        # condition as well.
        raise frame.error(
            f"Error in copy fields: Condition query '{raw_query}' cannot be judged for a note"
            f" that is not added yet, because Anki would refuse the search: {error}",
            stage,
        ) from error
    session.record_detail("found", 1 if matched else 0)
    if not matched:
        logger.debug(
            "copy_for_single_trigger_note: Condition query '%s' did not match the note being"
            " added",
            interpolated,
        )
    return matched


def evaluate_predicate(stage: dict, env: dict, frame) -> bool:
    """Whether the condition's `then` branch runs.

    A condition matched as an Anki search is scoped to one note, which is how format 1 ran
    the copy condition a migration produced (a note not added yet is judged without the
    collection); any other is boolean code or a scalar the branch takes the truthiness of.
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
        if expression_is_code(expression):
            # There is no code form of a search, which is why the editor hides the toggle
            # once this kind is chosen; only hand-edited JSON reaches here. Saying so beats
            # running the code and searching on whatever it returned.
            raise frame.error(
                "a condition matched as an Anki search cannot be code", stage
            )
        ctx = make_context(frame, env, stage, target, target)
        raw_query = expression.get("text", "") or ""
        # What `run_query` does with a query stage's search, and what every other expression
        # in the executor gets: the resolution the expression's own references ask for.
        interpolated = _search_text(evaluate_text(expression, ctx))
        if not interpolated:
            # Unparenthesised, an empty query reached `find_notes(f" nid:{id}")`, which
            # matches the trigger whatever the condition says, so every one of them read
            # true; parenthesised, Anki refuses the empty group without naming the condition.
            raise frame.error(
                f"Error in copy fields: Condition query '{raw_query}' resolved to"
                f" nothing for note id {target.id}",
                stage,
            )
        if not target.id:
            # A note being added: the add hook's, or the Add dialog's on unfocus.
            return _unsaved_note_matches(stage, frame, target, raw_query, interpolated)
        # Through the session, like a query stage's search: the same predicate asked of the
        # same note twice costs one trip to the collection, and the preview pane -- the one
        # a user opens to see why a branch did not run -- gets the search it actually made.
        # The parentheses keep the note scope on the whole predicate: Anki binds `OR` looser
        # than the implicit AND, so `a OR b nid:X` is `a OR (b nid:X)` and would match any
        # note `a` finds.
        search = f"({interpolated}) nid:{target.id}"
        note_ids = session.find_notes(search)
        session.record_detail("query", search)
        session.record_detail("found", len(note_ids))
        if not note_ids:
            logger.debug(
                "copy_for_single_trigger_note: Condition query '%s' did not match for note id %s",
                interpolated,
                target.id,
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
