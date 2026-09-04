"""Executes user-provided Python code in a restricted namespace.

The code is expected to be the body of a function (using ``return`` to produce
the result).  ``{{field}}`` interpolation markers are resolved *before* this
module is invoked, so the code text already contains the final field values.

Security notes
--------------
Python does not provide a true sandbox.  A sufficiently motivated user can
escape the restricted ``__builtins__`` via introspection (e.g. accessing
``''.__class__.__mro__[1].__subclasses__()``).  Because this addon runs
locally and the user writes their own code, the restriction is a safeguard
against *accidental* misuse, not a cryptographic boundary.

What IS restricted: ``__import__``, ``open``, ``exec``, ``eval``,
``compile``, ``breakpoint``, ``os``, ``sys``, network access.
What IS allowed: the explicit allowlist below (``re``, ``json``, ``html``,
``print``, ``find_cards``, ``find_notes``, ``note``) plus a curated set of built-ins.

Public API
----------
``execute_code_core(code, note)``
    Run the code and return its raw result, for callers that validate the
    return value themselves.

``execute_code_for_field(code, note)``
    Execute code expected to return a single string value.

Wrappers whose expected return value is specific to this addon's features
live in ``execute_code_wrappers.py``.
"""

import html
import json
import re
import textwrap
import traceback
from typing import Any, Optional, Tuple, Union

from anki.consts import MODEL_CLOZE
from anki.cards import Card
from anki.notes import Note
from anki.models import NotetypeDict
from aqt import mw

from .interpolate_fields import (
    get_card_last_reps,
    get_card_type_as_string,
    get_formatted_card_created_time,
    get_card_time_values,
    get_formatted_first_review_time,
    get_formatted_latest_review_time,
    get_formatted_average_time,
    get_formatted_total_time,
)

# ---------------------------------------------------------------------------
# Safe built-ins whitelist
# Notably absent: __import__, open, exec, eval, compile, breakpoint,
#                 globals, locals, vars, __build_class__, input, setattr
# ---------------------------------------------------------------------------
_SAFE_BUILTINS: dict = {
    # Type constructors
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "frozenset": frozenset,
    "tuple": tuple,
    "bytes": bytes,
    "bytearray": bytearray,
    # Singletons
    "None": None,
    "True": True,
    "False": False,
    # Numeric
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": pow,
    "divmod": divmod,
    # Sequence / iteration
    "len": len,
    "range": range,
    "sorted": sorted,
    "reversed": reversed,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "any": any,
    "all": all,
    "next": next,
    "iter": iter,
    # Type inspection (read-intent only)
    "isinstance": isinstance,
    "issubclass": issubclass,
    "type": type,
    "callable": callable,
    "hasattr": hasattr,
    "getattr": getattr,
    # Repr / formatting
    "repr": repr,
    "format": format,
    "chr": chr,
    "ord": ord,
    "hex": hex,
    "oct": oct,
    "bin": bin,
    "hash": hash,
    # Exceptions
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "StopIteration": StopIteration,
    "RuntimeError": RuntimeError,
    "NotImplementedError": NotImplementedError,
    # JSON errors
    "JSONDecodeError": json.JSONDecodeError,
}


class _ReadOnlyNote:
    """Thin read-only proxy around an Anki Note.

    Exposes only ``note[field_name]`` (``__getitem__``) and ``note.keys()`` so
    that user code can read field values but cannot modify them.  This prevents
    accidental (or intentional) mutation of the source note during code
    execution, which would otherwise corrupt the note object shared with the
    rest of the copy pipeline.
    """

    __slots__ = ("_note",)

    def __init__(self, note: Note) -> None:
        object.__setattr__(self, "_note", note)

    def __getitem__(self, key: str) -> str:
        return object.__getattribute__(self, "_note")[key]

    def keys(self):
        return object.__getattribute__(self, "_note").keys()

    def __setitem__(self, key: str, value: str) -> None:
        raise TypeError("note fields are read-only inside code execution")

    def __setattr__(self, name: str, value) -> None:
        raise TypeError("note fields are read-only inside code execution")


class _ReadOnlyCard:
    """Read-only view of an Anki card for use inside code execution.

    Exposes the same fields as a normal card but prevents mutation.

    Use ``card.id`` with the ``get_card_last_reps`` function available in the
    exec namespace to retrieve review history for a card.
    """

    __slots__ = ("_card", "_card_time_values", "_is_cloze")

    def __init__(self, card: Card, note_type: NotetypeDict) -> None:
        object.__setattr__(self, "_card", card)
        object.__setattr__(self, "_card_time_values", None)
        object.__setattr__(
            self, "_is_cloze", bool(note_type and note_type.get("type") == MODEL_CLOZE)
        )

    def __getattr__(self, name: str):
        if name == "ease":
            return getattr(object.__getattribute__(self, "_card"), "factor") / 10 or 0
        elif name == "type":
            card_type = getattr(object.__getattribute__(self, "_card"), "type", None)
            return get_card_type_as_string(card_type)
        elif name == "stability":
            mem_state = getattr(object.__getattribute__(self, "_card"), "memory_state", None)
            return round(mem_state.stability, 1) if mem_state else 0
        elif name == "difficulty":
            mem_state = getattr(object.__getattribute__(self, "_card"), "memory_state", None)
            return round(mem_state.difficulty, 1) if mem_state else 0
        elif name == "created":
            # Expose created time in seconds since epoch for convenience, even though it's stored in ms
            card_id = getattr(object.__getattribute__(self, "_card"), "id", None)
            return get_formatted_card_created_time(card_id) if card_id else "-"
        elif name in (
            "first_review_time",
            "latest_review_time",
            "average_review_time",
            "total_review_time",
        ):
            # init _card_time_values on first access to avoid unnecessary computation for cards that aren't reviewed
            if self._card_time_values is None:
                card_id = getattr(object.__getattribute__(self, "_card"), "id", None)
                if card_id:
                    object.__setattr__(self, "_card_time_values", get_card_time_values(card_id))
                else:
                    # Init to tuple so we don't keep trying to fetch time values again
                    object.__setattr__(self, "_card_time_values", (None, None, None, None))
            first_ms, latest_ms, review_count, total_ms = self._card_time_values
            if name == "first_review_time":
                return get_formatted_first_review_time(first_ms)
            elif name == "latest_review_time":
                return get_formatted_latest_review_time(latest_ms)
            elif name == "average_review_time":
                return get_formatted_average_time(total_ms, review_count)
            elif name == "total_review_time":
                return get_formatted_total_time(total_ms)
        elif name == "template_name":
            template = getattr(object.__getattribute__(self, "_card"), "template", None)
            tmpl_name = template()["name"] if template else "-"
            if self._is_cloze:
                return (
                    f'{tmpl_name} {getattr(object.__getattribute__(self, "_card"), "ord", "?") + 1}'
                )
            return tmpl_name
        # id, nid, due, ivl, reps, lapses, factor, desired_retention returned as-is
        return getattr(object.__getattribute__(self, "_card"), name)

    def __setitem__(self, name: str, value) -> None:
        raise TypeError("card properties are read-only inside code execution")

    def __setattr__(self, name: str, value) -> None:
        raise TypeError("card properties are read-only inside code execution")

    def __repr__(self) -> str:
        card_id = getattr(object.__getattribute__(self, "_card"), "id", "?")
        return f"<_ReadOnlyCard id={card_id}>"


def execute_code_core(code: str, note: Note) -> Tuple[Any, Optional[str]]:
    """Compile and run user-provided code in a restricted namespace.

    The code is wrapped in a function body so that ``return`` statements work
    naturally.  The raw Python return value is returned as-is; callers are
    responsible for type-checking it.

    :param code: The Python code to run (function body, may include
        ``return``).  ``{{field}}`` markers have already been interpolated.
    :param note: The current source note, available as ``note`` inside the
        code.
    :return: ``(raw_result, error_message)`` — *error_message* is ``None``
        on success.  *raw_result* is ``None`` when the code returns nothing or
        when an error occurred.
    """
    if not code.strip():
        return None, None

    indented = textwrap.indent(code, "    ")
    wrapped = f"def _user_func():\n{indented}\n_result = _user_func()\n"

    # Copy _SAFE_BUILTINS per-call so that exec cannot leak mutations between
    # invocations (Python exposes __builtins__ inside exec'd namespaces and
    # user code could mutate the dict if it were shared).
    note_cards = []
    if note.id not in (None, 0):
        try:
            note_cards = note.cards()
        except Exception:
            note_cards = []
    note_type = note.note_type()
    exec_globals: dict = {
        "__builtins__": dict(_SAFE_BUILTINS),
        "re": re,
        "json": json,
        "html": html,
        "print": print,
        "find_cards": mw.col.find_cards,
        "find_notes": mw.col.find_notes,
        "note": _ReadOnlyNote(note),
        "cards": [_ReadOnlyCard(c, note_type) for c in note_cards],
        "get_card_last_reps": get_card_last_reps,
    }

    try:
        compiled = compile(wrapped, "<copy_anywhere_code>", "exec")
    except SyntaxError as e:
        user_lineno = max(1, (e.lineno or 1) - 1)  # subtract the injected def line
        kind = "Indentation error" if isinstance(e, IndentationError) else "Syntax error"
        # e.text is the offending source line; e.offset is the column (1-based)
        pointer = ""
        if e.text:
            col = max(0, (e.offset or 1) - 1)
            pointer = f"\n    {e.text.rstrip()}\n    {' ' * col}^"
        return None, f"{kind} (line {user_lineno}): {e.msg}{pointer}"

    try:
        exec(compiled, exec_globals)  # noqa: S102
    except Exception:  # noqa: BLE001
        # Extract the traceback, strip frames that belong to execute_code itself,
        # and shift line numbers back by 1 to account for the injected `def` line.
        tb_lines = traceback.format_exc().splitlines()
        adjusted: list[str] = []
        for line in tb_lines:
            # Rewrite "File "<copy_anywhere_code>", line N" references
            if "<copy_anywhere_code>" in line:
                line = re.sub(
                    r"line (\d+)",
                    lambda m: f"line {max(1, int(m.group(1)) - 1)}",
                    line,
                )
            adjusted.append(line)
        # Drop the outer frame that points into execute_code.py itself
        # (the first two lines are "Traceback..." + the exec() call site)
        error_msg = "\n".join(adjusted).replace('File "<copy_anywhere_code>"', "Code line")
        return None, error_msg

    return exec_globals.get("_result", None), None


def execute_code_for_field(code: str, note: Note) -> Tuple[Union[str, None], Optional[str]]:
    """Execute user-provided Python code expected to return a single string.

    :param code: The Python code to run (function body, may include
        ``return``).  ``{{field}}`` markers have already been interpolated.
    :param note: The current source note, available as ``note`` inside the
        code.
    :return: ``(result_string|None, error_message)`` — *error_message* is
        ``None`` on success.  ``None`` result indicates no return value or an
        error.
    """
    result, error = execute_code_core(code, note)
    if error:
        return None, error
    if result is None:
        return None, None
    return str(result), None
