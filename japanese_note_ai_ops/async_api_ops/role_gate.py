"""Leaving out of a run the notes its op cannot work on, with one message per note type.

In the two-type layout each configured note type has one role (note_roles): the sentence ops
(extract, judge, find proper nouns, kanjify, translate, match, find missing ids) read sentence
fields and word arrays, the vocab ops (meanings, clean meaning, the single-word rematch, ...)
word fields. Run over notes of the other type, an op met fields that type's block does not
name: a raise per note, each reported with its traceback, or the whole match run failing as it
planned. A selection, or a chain's search, holding both types is ordinary, so a bulk op leaves
those notes out before it starts and says so once per note type instead.

Free of anki and aqt: a note is anything with `note_type()`, as Anki's Note has.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional, TypeVar

from ..note_roles import role_error
from .run_errors import report_error

logger = logging.getLogger(__name__)

N = TypeVar("N")


def note_type_name(note: Any) -> str:
    """The note's type's name, "" when Anki cannot find its type."""
    note_type = note.note_type()
    return note_type["name"] if note_type else ""


def notes_passing(
    notes: Sequence[N], refusal: Callable[[str], Optional[str]], *, report: bool = True
) -> list[N]:
    """The notes whose type `refusal(type name)` lets through (None), in their order.

    The others are left out and, with `report`, reported as one run error per note type: the
    refusal and how many notes it cost. A note whose type Anki cannot find is let through, to
    fail in the op as it always did.
    """
    refusals: dict[str, Optional[str]] = {}
    kept: list[N] = []
    left_out: dict[str, int] = {}
    for note in notes:
        name = note_type_name(note)
        if name not in refusals:
            refusals[name] = refusal(name) if name else None
        if refusals[name] is None:
            kept.append(note)
        else:
            left_out[name] = left_out.get(name, 0) + 1
    if report:
        for name, count in left_out.items():
            plural = "note" if count == 1 else "notes"
            text = f"{refusals[name]} Left out of this run: {count} {plural} of it."
            logger.warning(text)
            report_error(text, f'Notes of "{name}"')
    return kept


def notes_of_role(
    config: Mapping[str, Any], notes: Sequence[N], role: str, *, report: bool = True
) -> list[N]:
    """The notes an op over `role` notes (note_roles.SENTENCE_ROLE or VOCAB_ROLE) may run on;
    the rest left out as `notes_passing` says. Outside the two-type layout, all of them."""
    return notes_passing(notes, lambda name: role_error(config, name, role), report=report)
