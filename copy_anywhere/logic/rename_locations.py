"""The names of the places inside a definition that a rename warning is filed under.

A warning is about one text or slot, and the reconcile pass that files it and the editor
part that shows it have to spell its key the same way, or the warning silently shows
nowhere. So neither builds a key by hand: both call the one function here for the kind of
object that holds the text.

A key is anchored on a guid rather than a position -- `<guid>.<path within that object>` --
so that moving, inserting or deleting a stage leaves every other stage's warnings where
they were. The triggers have no guid, and there is only one set of them, so they use the
fixed anchor `triggers`. Stage, field write and card action guids are uuid4s or derived as
`<definition guid>::<role>`, neither of which holds a dot, which is what lets `split_key`
cut at the first one while the path itself holds more (`value.text`).
"""

from __future__ import annotations

from typing import Final

#: The anchor of every key about the definition's triggers.
TRIGGERS_ANCHOR: Final = "triggers"


def stage_key(stage_guid: str, path: str) -> str:
    """A location in a stage, e.g. `query.text`, `value.code`, `selection.sort_field`."""
    return f"{stage_guid}.{path}"


def field_write_key(write_guid: str, path: str) -> str:
    """A location in one field write of an Edit Note stage: `field`, `value.text` ..."""
    return f"{write_guid}.{path}"


def card_action_key(action_guid: str, path: str) -> str:
    """A location in one card action, e.g. `action_code`."""
    return f"{action_guid}.{path}"


def trigger_key(path: str) -> str:
    """A location in the triggers, e.g. `on_unfocus.edit_fields`."""
    return f"{TRIGGERS_ANCHOR}.{path}"


def split_key(key: str) -> tuple[str, str]:
    """`(anchor, path)`: the guid (or `triggers`) a key is anchored on, and the rest.

    A key with no path comes back with an empty one.
    """
    anchor, _dot, path = key.partition(".")
    return anchor, path


def reanchor_key(key: str, anchor: str) -> str:
    """The same location in another object: the key with its anchor replaced, as a stage
    duplicated with new guids holds the texts its original did."""
    return f"{anchor}.{split_key(key)[1]}"


__all__ = [
    "TRIGGERS_ANCHOR",
    "card_action_key",
    "field_write_key",
    "reanchor_key",
    "split_key",
    "stage_key",
    "trigger_key",
]
