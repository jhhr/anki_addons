"""Result validators for code execution that are specific to this addon.

``execute_code.py`` holds the sandbox and the plain string-returning wrapper;
it is intentionally free of imports from ``configuration`` so that it can be
shared with other addons.  The wrappers here validate return values whose
shape only means something to CopyAnywhere's copy definitions.
"""

from typing import Optional, Tuple, Union

from anki.notes import Note

from ..configuration import CardActionDict
from .execute_code import execute_code_core


def execute_code_for_files(
    code: str, note: Note
) -> Tuple[Union[list[tuple[str, str]], None], Optional[str]]:
    """Execute user-provided Python code expected to return a list of file tuples.

    The code must ``return`` a ``list`` of ``(filename, content)`` pairs where
    both elements are strings.  Each pair will be written as a separate file.

    :param code: The Python code to run (function body, may include
        ``return``).  ``{{field}}`` markers have already been interpolated.
    :param note: The current source note, available as ``note`` inside the
        code.
    :return: ``(file_tuples|None, error_message)`` — *error_message* is
        ``None`` on success.  ``None`` result indicates no return value or an
        error.
    """
    result, error = execute_code_core(code, note)
    if error:
        return None, error
    if result is None:
        return None, None

    if not isinstance(result, list):
        return None, f"Expected a list of (filename, content) tuples, got {type(result).__name__}"

    validated: list[tuple[str, str]] = []
    for i, item in enumerate(result):
        if not isinstance(item, tuple) or len(item) != 2:
            return None, f"Item {i} must be a 2-tuple of (filename, content), got: {item!r}"
        fname, fcontent = item
        if not isinstance(fname, str):
            return None, f"Item {i} filename must be a str, got {type(fname).__name__}"
        if not isinstance(fcontent, str):
            return None, f"Item {i} content must be a str, got {type(fcontent).__name__}"
        validated.append((fname, fcontent))

    return validated, None


def execute_code_for_card_action(
    code: str, note: Note
) -> Tuple[Optional[CardActionDict], Optional[str]]:
    """Execute user-provided Python code expected to return a CardActionDict or None.

    The code must ``return`` a ``dict`` with any combination of the optional
    ``CardActionDict`` keys (``change_deck``, ``set_flag``, ``suspend``,
    ``bury``, ``set_desired_retention``), or ``None`` to skip all actions for
    this card type.

    :param code: The Python code to run (function body, may include
        ``return``).  ``{{field}}`` markers have already been interpolated.
    :param note: The destination note, available as ``note`` inside the code.
    :return: ``(CardActionDict|None, error_message)`` — *error_message* is
        ``None`` on success.  ``None`` result means skip all actions.
    """
    result, error = execute_code_core(code, note)
    if error:
        return None, error
    if result is None:
        return None, None
    if not isinstance(result, dict):
        return None, f"Expected a dict or None, got {type(result).__name__}"

    # Validate each known key's type if present.
    change_deck = result.get("change_deck")
    if change_deck is not None and not isinstance(change_deck, (str, int)):
        return None, f"'change_deck' must be str, int, or None; got {type(change_deck).__name__}"
    set_flag = result.get("set_flag")
    if set_flag is not None:
        if isinstance(set_flag, bool) or not isinstance(set_flag, int) or not (0 <= set_flag <= 7):
            return None, f"'set_flag' must be an int 0\u20137 or None; got {set_flag!r}"
    suspend = result.get("suspend")
    if suspend is not None and not isinstance(suspend, bool):
        return None, f"'suspend' must be True, False, or None; got {type(suspend).__name__}"
    bury = result.get("bury")
    if bury is not None and not isinstance(bury, bool):
        return None, f"'bury' must be True, False, or None; got {type(bury).__name__}"
    set_dr = result.get("set_desired_retention")
    if set_dr is not None and not isinstance(set_dr, (float, int, str)):
        return (
            None,
            (
                "'set_desired_retention' must be float, int, str, or None;"
                f" got {type(set_dr).__name__}"
            ),
        )

    return result, None  # type: ignore[return-value]
