"""Turning one `ValueExpression` into a value, in whichever syntax it is written.

Two syntaxes live side by side and will until every definition has been re-authored.

*Format-1 syntax* (`syntax_version: 1`, written only by the migrator) names note fields
unqualified and the destination note's fields with a `__Dest__` prefix, and is resolved by
the same `get_field_values_from_notes()` the old executor used -- the same interpolation, the
same code execution, the same messages. A migrated definition therefore computes what it
computed before; the staged executor only changes which note is the source and which the
destination, which is exactly what migration made explicit.

*Format-2 syntax* qualifies every reference: `{{trigger.Word}}`, `{{note.Meaning}}`,
`{{card.deck_name}}`, `{{M}}` for a result. A reference is resolved against the binding it
names, so nothing depends on which note happens to be "current", and a reference to a list
or a note is an error rather than a silent `str()`.

Both then run the expression's process chain, which is unchanged from format 1.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from anki.cards import Card
from anki.notes import Note

from ...shared.interpolate.execute_code import execute_code_core
from ...shared.interpolate.interpolate_fields import (
    QUERY_NOTE_INDEX,
    TARGET_NOTES_COUNT,
    extract_cloze_patterns,
    interpolate_from_text,
)
from ...shared.utils.logger import Logger
from ..copy_primitives import (
    CopyFailedException,
    apply_process_chain,
    get_field_values_from_notes,
)
from ..definition_schema import (
    ValueExpression,
    expression_is_code,
    expression_is_legacy_syntax,
    expression_source,
)
from .context import ExecutionSession, StageError
from .facades import (
    CardFacade,
    CardListFacade,
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

    `source_note` is the note unqualified format-1 references read from and the note code
    mode gets as `note`; `destination_note` is what `__Dest__` reads from. A format-2
    expression uses neither except as the fallback for an unqualified reference.
    """

    __slots__ = (
        "session",
        "frame",
        "environment",
        "source_note",
        "destination_note",
        "separator",
        "multiple_note_types",
        "stage",
        "isolated_variables",
    )

    def __init__(
        self,
        session: ExecutionSession,
        frame: Any,
        environment: dict,
        source_note: Note,
        destination_note: Optional[Note] = None,
        separator: Optional[str] = None,
        multiple_note_types: bool = False,
        stage: Optional[dict] = None,
        isolated_variables: bool = False,
    ) -> None:
        self.session = session
        self.frame = frame
        self.environment = environment
        self.source_note = source_note
        self.destination_note = destination_note
        self.separator = separator
        self.multiple_note_types = multiple_note_types
        self.stage = stage
        self.isolated_variables = isolated_variables

    @property
    def logger(self) -> Logger:
        return self.session.logger

    def variables(self) -> Optional[dict]:
        """The bindings a format-1 expression can reach as `{{Name}}`.

        Only scalars: there is no implicit list-to-text conversion, so a note, a card or a
        list is simply not among the variables and reads as an invalid field, which is what
        it is. `legacy_isolated_variables` reproduces format 1's rule that a variable's own
        expression saw no variables at all.
        """
        if self.isolated_variables:
            return None
        variables: dict[str, Any] = {}
        for name, value in self.environment.items():
            if isinstance(value, SCALAR_TYPES):
                variables[name] = value
        variables.update(self.frame.legacy_values)
        return variables

    def error(self, message: str) -> StageError:
        return self.frame.error(message, self.stage)


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


def _card_reference(card: Card, rest: str, ctx: ExpressionContext) -> str:
    facade = CardFacade(card, ctx.session)
    if rest in CARD_PROPERTY_NAMES:
        return str(getattr(facade, rest))
    if rest.startswith("__"):
        # A format-1 card-value key, which is keyed by card type name on the note.
        note = ctx.session.note_by_id(card.nid)
        return _note_reference(note, f"{facade.template_name}{rest}", ctx)
    raise ctx.error(f"'{rest}' is not a card property")


def resolve_references(text: str, ctx: ExpressionContext) -> str:
    """Substitute every `{{...}}` in a format-2 expression with the value it names."""
    if not text:
        return text or ""

    # Cloze markers are not references. Their content is, so it is resolved on its own and
    # the marker put back around the result.
    cloze_patterns = extract_cloze_patterns(text)
    placeholders: dict[str, str] = {}
    for index, (start, end, cloze_num, content) in enumerate(reversed(cloze_patterns)):
        placeholder = f"\x00CLOZE{index}\x00"
        placeholders[placeholder] = f"{{{{c{cloze_num}::{resolve_references(content, ctx)}}}}}"
        text = text[:start] + placeholder + text[end:]

    def replace(match: "re.Match[str]") -> str:
        reference = match.group(1)
        head, separator, rest = reference.partition(".")
        value = ctx.environment.get(head)
        if separator:
            if isinstance(value, Note):
                return _note_reference(value, rest, ctx)
            if isinstance(value, Card):
                return _card_reference(value, rest, ctx)
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
        # Not a binding: a note value, a card value or one of the runtime variables, all of
        # which the format-1 interpolation already knows how to find.
        resolved, invalid = interpolate_from_text(
            match.group(0),
            source_note=ctx.source_note,
            destination_note=ctx.destination_note,
            variable_values_dict=ctx.variables(),
            multiple_note_types=ctx.multiple_note_types,
        )
        if invalid:
            ctx.logger.error(
                f"Error in copy fields: Invalid fields in copy_from_text: {', '.join(invalid)}"
            )
        return resolved or ""

    text = INTERPOLATION_RE.sub(replace, text)
    for placeholder, cloze_value in placeholders.items():
        text = text.replace(placeholder, cloze_value)
    return text


def code_globals(ctx: ExpressionContext) -> dict:
    """The names a format-2 code expression runs with: every binding, as a facade."""
    globals_dict: dict[str, Any] = dict(code_helpers(ctx.session))
    for name, value in ctx.environment.items():
        globals_dict[name] = to_facade(value, ctx.session)
    globals_dict["note"] = NoteFacade(ctx.source_note, ctx.session)
    globals_dict["cards"] = CardListFacade(
        ctx.session.cards_of_note(ctx.source_note), ctx.session
    )
    if ctx.destination_note is not None:
        globals_dict.setdefault(
            "destination", NoteFacade(ctx.destination_note, ctx.session)
        )
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

    if expression_is_legacy_syntax(expression):
        try:
            return get_field_values_from_notes(
                copy_from_text=source,
                notes=[ctx.source_note],
                dest_note=ctx.destination_note,
                multiple_note_types=ctx.multiple_note_types,
                variable_values_dict=ctx.variables(),
                select_card_separator=ctx.separator,
                use_code=is_code,
                logger=ctx.logger,
                progress_updater=ctx.session.progress_updater,
            )
        except CopyFailedException as error:
            raise ctx.error(str(error)) from error

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
        variable_values_dict=ctx.variables(),
        progress_updater=ctx.session.progress_updater,
        logger=ctx.logger,
        file_cache=ctx.session.file_cache,
    )
    if processed is None:
        # `apply_process_chain` returns None only for a FatalProcessError, which it has
        # already logged; the stage fails so the rest of the definition does not run.
        raise ctx.error("Process chain failed")
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


def legacy_runtime_values(
    query_note_index: Optional[int] = None, target_notes_count: Optional[int] = None
) -> dict:
    """The two runtime variables format-1 expressions expect the executor to provide."""
    values: dict[str, Any] = {}
    if query_note_index is not None:
        values[QUERY_NOTE_INDEX] = query_note_index
    if target_notes_count is not None:
        values[TARGET_NOTES_COUNT] = target_notes_count
    return values


__all__ = [
    "ExpressionContext",
    "code_globals",
    "evaluate_raw",
    "evaluate_text",
    "evaluate_value",
    "legacy_runtime_values",
    "resolve_references",
    "run_process_chain",
]
