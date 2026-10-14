"""The warnings a rename or deletion in Anki left on a definition, and how to read them.

The reconcile pass (`rename_reconcile.py`) files each warning under the location whose text
spells the old name (`rename_locations.py`), as `definition["rename_warnings"]`:

    {"<location key>": [{"kind", "object_id", "note_type_id", "old", "new", "blocks_run",
                         "through_trigger", "message", "inactive"?}, ...], ...}

`new` is None for a deletion; `note_type_id` is the note type a field or card type belongs
to; `through_trigger` says whether the definition's own trigger tokens and slots counted as
spelling the name (`rename_scan.entry_hits`). `blocks_run` says whether the warning would
block where a run reads its location; `inactive` is there while none does (the other side
of an expression, a switched-off stage), and then it does not. A warning that blocks
(`entry_blocks_run`) keeps the definition from running on every path until it
is gone: the editor drops it on save once its location no longer spells the old name
(`rename_reconcile.drop_cleared_warnings`), or the user dismisses it. The store is read
only through the functions here: every run path, the picker, the browser menu and
`call_definition` ask the same question, and a stored value mangled by hand -- not a dict, a
location that is not a list, an entry with nothing to say -- must read the same way to all
of them. An entry with no message counts for nothing, so a mangled one never blocks a run
the user could not see a reason for.
"""

from __future__ import annotations

from typing import Any, Final

WARNINGS_KEY: Final = "rename_warnings"
#: Set on an entry while no run reads its location (`rename_reconcile._Location.active`).
INACTIVE_KEY: Final = "inactive"

#: What the user can do about a definition a blocking warning holds back, said after the
#: stored message wherever a run refuses it. Undoing the rename takes the warning back too
#: (`rename_reconcile._refresh_marks`), but the advice is for keeping the rename, which is
#: the usual case. Dismissing is the other way out, for a hit that is not the renamed name.
BLOCKING_ADVICE = (
    "Replace the old name in the definition editor and save, or dismiss the warning there."
)


def rename_warning_entries(definition: Any) -> list[tuple[str, dict]]:
    """Each stored warning that has something to say, with its location key, in stored order.

    The entries themselves rather than their messages, for the editor, which dismisses one
    entry at a time: two entries can carry the same message, and dismissing one must not
    take the other with it.
    """
    if not isinstance(definition, dict):
        return []
    stored = definition.get(WARNINGS_KEY)
    if not isinstance(stored, dict):
        return []
    found: list[tuple[str, dict]] = []
    for key, entries in stored.items():
        if not isinstance(key, str) or not isinstance(entries, list):
            continue
        for entry in entries:
            message = entry.get("message") if isinstance(entry, dict) else None
            if isinstance(message, str) and message.strip():
                found.append((key, entry))
    return found


def entry_blocks_run(entry: Any) -> bool:
    """Whether this one entry keeps its definition from running: it would block where it
    is, and a run reads where it is."""
    return (
        isinstance(entry, dict)
        and entry.get("blocks_run") is True
        and entry.get(INACTIVE_KEY) is not True
    )


def blocking_messages(definition: Any) -> list[str]:
    """The messages of the warnings that keep this definition from running, or none.

    A blocking warning is one whose name, spelled where the definition spells it, no longer
    means what it did in a note type it runs on: it would fail on that note type's notes or
    read another field, and go on writing as if nothing had happened, while the user still
    has to decide what it should say. Read as stored, never re-derived: it is the user's to
    dismiss.
    """
    return [
        entry["message"]
        for _key, entry in rename_warning_entries(definition)
        if entry_blocks_run(entry)
    ]


def non_blocking_messages(definition: Any) -> list[str]:
    """The messages of the warnings that let this definition run, in stored order.

    A name spelled where it may not mean the renamed object -- a card type or field in a
    search, a sort field, another binding's token -- so the definition runs as it is and
    the user is told rather than stopped. Read for the picker's information icon, which
    shows them beside the stale search terms.
    """
    return [
        entry["message"]
        for _key, entry in rename_warning_entries(definition)
        if not entry_blocks_run(entry)
    ]


def blocks_run(definition: Any) -> bool:
    """Whether any warning keeps this definition from running (`blocking_messages`)."""
    return bool(blocking_messages(definition))


def blocking_tooltip(messages: list[str]) -> str:
    """The blocking lines as a list reads them where a definition is offered to be run.

    The definition list and the browser's menu both refuse such a definition and say why in
    the same words, so a user who meets it in one recognises it in the other.
    """
    header = "This definition is not run while it has these rename warnings:"
    return "\n".join([header, *messages, BLOCKING_ADVICE])


def blocking_explanation(messages: list[str]) -> str:
    """The stored messages as one explanation: each unchanged, then what to do about it."""
    text = "; ".join(messages)
    end = "" if text.rstrip().endswith((".", "!", "?")) else "."
    return f"{text}{end} {BLOCKING_ADVICE}"


def remove_rename_warning(definition: Any, entry: dict) -> bool:
    """Take one stored entry off, found by identity. True if it was there.

    By identity, not by position or message: two entries can say the same thing, and the
    editor's rows hold the entries they were built from. A location left empty goes, and so
    does the whole store once nothing in it has a message -- an entry with none is never
    shown, so it could never be dismissed.
    """
    stored = definition.get(WARNINGS_KEY) if isinstance(definition, dict) else None
    if not isinstance(stored, dict):
        return False
    removed = False
    for key in list(stored):
        entries = stored[key]
        if not isinstance(entries, list) or not any(kept is entry for kept in entries):
            continue
        kept = [other for other in entries if other is not entry]
        removed = True
        if kept:
            stored[key] = kept
        else:
            del stored[key]
    if not rename_warning_entries(definition):
        definition.pop(WARNINGS_KEY, None)
    return removed


__all__ = [
    "BLOCKING_ADVICE",
    "INACTIVE_KEY",
    "WARNINGS_KEY",
    "blocking_explanation",
    "blocking_messages",
    "blocking_tooltip",
    "blocks_run",
    "entry_blocks_run",
    "non_blocking_messages",
    "remove_rename_warning",
    "rename_warning_entries",
]
