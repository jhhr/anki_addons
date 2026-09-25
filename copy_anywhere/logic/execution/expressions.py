"""Turning one `ValueExpression` into a value.

Every reference is qualified: `{{trigger.Word}}`, `{{note.Meaning}}`, `{{card.deck_name}}`,
`{{M}}` for a result. A reference is resolved against the binding it names, so nothing
depends on which note happens to be "current", and a reference to a list or a note is an
error rather than a silent `str()`. A bare name is a result, or one of the two values the
run itself supplies, or a mistake -- and the stage says so rather than writing nothing.

There is one syntax. Format 1's unqualified names and its `__Dest__` prefix are rewritten
into references to named bindings by the migrator, so nothing reaching here speaks it.

The expression's process chain runs after the value is produced, unchanged from format 1.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from anki.cards import Card
from anki.notes import Note

from ...shared.interpolate.execute_code import execute_code_core
from ...shared.interpolate.interpolate_fields import (
    CardValues,
    extract_cloze_patterns,
    get_card_value,
    interpolate_from_text,
)
from ..copy_primitives import apply_process_chain

from ..definition_schema import (
    ValueExpression,
    expression_is_code,
    expression_source,
)
from .context import ExecutionSession, StageError
from .facades import (
    CardFacade,
    NoteCardsFacade,
    NoteFacade,
    code_helpers,
    from_facade,
    to_facade,
)


INTERPOLATION_RE = re.compile(r"\{\{(.+?)\}\}")

#: The scalar Python types interpolation is allowed to stringify (§4.1).
SCALAR_TYPES = (str, int, float, bool)

#: `{{card.<name>}}` properties, resolved on the card facade rather than through the
#: `<template>__Card_*` keys format 1 used.
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
})


class ExpressionContext:
    """Everything an expression is evaluated against.

    `source_note` is the note code mode gets as `note`; `destination_note` is the one a
    process chain treats as the note being written into. References themselves name their
    binding and read neither.
    """

    __slots__ = (
        "session",
        "frame",
        "environment",
        "source_note",
        "destination_note",
        "multiple_note_types",
        "stage",
        "purpose",
    )

    def __init__(
        self,
        session: ExecutionSession,
        frame: Any,
        environment: dict,
        source_note: Note,
        destination_note: Optional[Note] = None,
        multiple_note_types: bool = False,
        stage: Optional[dict] = None,
        purpose: str = "",
    ) -> None:
        self.session = session
        self.frame = frame
        self.environment = environment
        self.source_note = source_note
        self.destination_note = destination_note
        self.multiple_note_types = multiple_note_types
        self.stage = stage
        # What this expression is being evaluated for -- "field Meaning", "file out.txt" --
        # for the messages that are about the thing rather than about the stage. A stage can
        # hold many writes, so naming the stage is not enough to find the one that failed.
        self.purpose = purpose

    def process_chain_variables(self) -> dict:
        """The names a process chain's own text can reach as `{{Name}}`.

        A regex process interpolates its pattern and its replacement itself, through
        format 1's interpolation, which is the one place that syntax survives -- those two
        boxes are not value expressions and have no stage scope behind them. Only scalars
        are offered: there is no implicit list-to-text conversion, so a note, a card or a
        list is simply not among them.
        """
        variables: dict[str, Any] = {}
        for name, value in self.environment.items():
            if isinstance(value, SCALAR_TYPES):
                variables[name] = value
        variables.update(self.frame.runtime_values)
        return variables

    def error(self, message: str) -> StageError:
        return self.frame.error(message, self.stage)

    def for_purpose(self) -> str:
        """` for field Meaning`, or nothing when the expression stands for the whole stage."""
        return f" for {self.purpose}" if self.purpose else ""


# --------------------------------------------------------------------------------------
# Format-2 reference resolution
# --------------------------------------------------------------------------------------


def _note_reference(note: Note, rest: str, ctx: ExpressionContext) -> str:
    value, invalid = interpolate_from_text(
        "{{" + rest + "}}",
        source_note=note,
        multiple_note_types=ctx.multiple_note_types,
    )
    if invalid:
        raise ctx.error(f"'{rest}' is not a field or value of that note")
    return value or ""


def _card_reference(
    card: Card, rest: str, ctx: ExpressionContext, card_values: dict[int, CardValues]
) -> str:
    facade = CardFacade(card, ctx.session)
    if rest in CARD_PROPERTY_NAMES:
        return str(getattr(facade, rest))
    if rest.startswith("__"):
        # A card-value key, read from the card this binding names. Format 1 had no card
        # binding -- it had a note -- so it keyed these by card template name and found them
        # through `get_from_note_fields`. Going back through that path meant discarding the
        # card in hand to re-derive it from its note, which cannot name one cloze card (they
        # share a template) and which a definition spanning several note types refuses
        # outright (the prefix is not allowed there). Neither question arises here.
        #
        # One `CardValues` per card for the whole expression, so its four review-time values
        # share one revlog query however many of them the text names. Keyed by the object,
        # which the entry keeps alive, because the values are read off that object.
        note = ctx.session.note_by_id(card.nid)
        values = card_values.get(id(card))
        if values is None:
            values = card_values[id(card)] = CardValues(card, note)
        try:
            value = get_card_value(card, note, rest, card_values=values)
        except KeyError:
            raise ctx.error(f"'{rest}' is not a card value") from None
        # A getter with nothing to say answers None -- custom data another add-on left
        # unparsable, say -- which format 1 rendered as "". The reference itself is fine.
        return "" if value is None else str(value)
    raise ctx.error(f"'{rest}' is not a card property")


def resolve_references(
    text: str, ctx: ExpressionContext, card_values: Optional[dict[int, CardValues]] = None
) -> str:
    """Substitute every `{{...}}` in a format-2 expression with the value it names.

    `card_values` is passed only by the cloze recursion below, which is still the same
    expression: the `CardValues` built so far, one per card. They live no longer than one
    expression because a `CardValues` is a snapshot of its card, and a later stage may have
    changed the card.
    """
    if not text:
        return text or ""
    if card_values is None:
        card_values = {}

    # Cloze markers are not references. Their content is, so it is resolved on its own and
    # the marker put back around the result.
    cloze_patterns = extract_cloze_patterns(text)
    placeholders: dict[str, str] = {}
    for index, (start, end, cloze_num, content) in enumerate(reversed(cloze_patterns)):
        placeholder = f"\x00CLOZE{index}\x00"
        placeholders[placeholder] = (
            f"{{{{c{cloze_num}::{resolve_references(content, ctx, card_values)}}}}}"
        )
        text = text[:start] + placeholder + text[end:]

    def replace(match: "re.Match[str]") -> str:
        reference = match.group(1)
        head, separator, rest = reference.partition(".")
        value = ctx.environment.get(head)
        if separator:
            if isinstance(value, Note):
                return _note_reference(value, rest, ctx)
            if isinstance(value, Card):
                return _card_reference(value, rest, ctx, card_values)
            if value is None:
                raise ctx.error(f"'{head}' is not a binding in scope")
            raise ctx.error(f"'{head}' is not a note or card, so '{reference}' has no value")
        if head in ctx.environment:
            if isinstance(value, SCALAR_TYPES):
                return str(value)
            raise ctx.error(
                f"'{head}' holds a note, card or list and cannot be interpolated;"
                " reduce it to a scalar first"
            )
        if head in ctx.frame.runtime_values:
            # The run's own values -- the query's size, the current loop index -- which no
            # stage declares and so no binding holds.
            return str(ctx.frame.runtime_values[head])
        # There is nothing else a bare name can be. Reading it as a field of whichever note
        # happened to be in hand is what let a typo run a truncated query or write an empty
        # string, with the definition reporting success either way (§11).
        raise ctx.error(f"'{head}' is not a binding or a runtime value")

    text = INTERPOLATION_RE.sub(replace, text)
    for placeholder, cloze_value in placeholders.items():
        text = text.replace(placeholder, cloze_value)
    return text


def code_globals(ctx: ExpressionContext) -> dict:
    """The names a format-2 code expression runs with: every binding, as a facade."""
    globals_dict: dict[str, Any] = dict(code_helpers(ctx.session))
    # `note` means the expression's own source note, as it always has -- but only where
    # nothing in scope is called that. Inside a loop the loop's own binding is what `note`
    # has to mean, or a predicate would silently read the trigger instead of the note being
    # iterated.
    globals_dict["note"] = NoteFacade(ctx.source_note, ctx.session)
    if ctx.destination_note is not None:
        globals_dict["destination"] = NoteFacade(ctx.destination_note, ctx.session)
    for name, value in ctx.environment.items():
        globals_dict[name] = to_facade(value, ctx.session)
    if "cards" not in ctx.environment:
        # `cards` are the cards of whichever note `note` turned out to be, so inside a loop
        # they are the loop note's rather than the trigger's. A binding called `note` that
        # holds something other than a note says nothing about which cards are meant, so
        # `cards` stays the source note's then. Fetched only if the code reads it.
        in_scope = ctx.environment.get("note")
        cards_note = in_scope if isinstance(in_scope, Note) else ctx.source_note
        globals_dict["cards"] = NoteCardsFacade(cards_note, ctx.session)
    return globals_dict


# --------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------


def evaluate_raw(expression: Optional[ValueExpression], ctx: ExpressionContext) -> Any:
    """The expression's value before its process chain runs.

    Text mode always produces a string. Code mode produces whatever the code returned, so
    the consuming action can accept a list of file tuples, a card action dictionary, or a
    note facade, and reject what it cannot use.
    """
    if not isinstance(expression, dict):
        return ""
    source = expression_source(expression)
    is_code = expression_is_code(expression)

    resolved = resolve_references(source, ctx)
    if not is_code:
        return resolved
    result, code_error = execute_code_core(
        resolved, ctx.source_note, extra_globals=code_globals(ctx)
    )
    if code_error:
        raise ctx.error(f"Code execution error:\n{code_error}")
    return from_facade(result)


def run_process_chain(value: str, expression: ValueExpression, ctx: ExpressionContext) -> str:
    process_chain = expression.get("process_chain") or None
    if not process_chain:
        return value
    processed = apply_process_chain(
        process_chain=process_chain,
        text=value,
        notes=[ctx.source_note],
        dest_note=ctx.destination_note if ctx.destination_note is not None else ctx.source_note,
        multiple_note_types=ctx.multiple_note_types,
        variable_values_dict=ctx.process_chain_variables(),
        progress_updater=ctx.session.progress_updater,
        file_cache=ctx.session.file_cache,
    )
    if processed is None:
        # `apply_process_chain` returns None only for a FatalProcessError, which it has
        # already logged; the stage fails so the rest of the definition does not run.
        raise ctx.error(f"Process chain failed{ctx.for_purpose()}")
    return processed


def evaluate_text(expression: Optional[ValueExpression], ctx: ExpressionContext) -> str:
    """The expression as text, with its process chain applied.

    A code expression that returns a note, card or list is refused here: an action that
    wants text needs the definition to say how those become text.
    """
    raw = evaluate_raw(expression, ctx)
    if raw is None:
        raw = ""
    if isinstance(raw, (Note, Card, list, tuple, NoteFacade, CardFacade)):
        raise ctx.error(
            f"expected text but the expression produced {type(raw).__name__};"
            " reduce it to a scalar first"
        )
    return run_process_chain(str(raw), expression or {}, ctx)


def evaluate_value(expression: Optional[ValueExpression], ctx: ExpressionContext) -> Any:
    """The expression's value with its type kept: text, a number, a note, a list of notes.

    Used where an action accepts more than text -- a variable holding a filtered note list,
    say. The process chain only applies to text, so a non-text value skips it.
    """
    raw = evaluate_raw(expression, ctx)
    if isinstance(raw, (Note, Card)):
        return raw
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if raw is None:
        return ""
    if isinstance(raw, (int, float, bool)) and not isinstance(raw, str):
        return raw
    return run_process_chain(str(raw), expression or {}, ctx)


__all__ = [
    "ExpressionContext",
    "code_globals",
    "evaluate_raw",
    "evaluate_text",
    "evaluate_value",
    "resolve_references",
    "run_process_chain",
]
