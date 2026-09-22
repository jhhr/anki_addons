"""Stored references to the Anki objects that have a stable id.

Anki has exactly one rename hook -- `fields_did_rename_field` -- and it fires before the
Fields dialog is accepted, so a rename can be cancelled out from under it; note type, card
template and deck renames have no hook at all, and a rename made on another device arrives
as "something changed" and nothing more (`docs/follow-ups.md`, "Following a rename in
Anki"). What those three kinds of object do have is an id that survives the rename, so a
definition stores the id beside the name and resolves with **the id winning if it still
exists, and the name being looked up only when it does not**. That one rule covers a rename
on any device, a delete and re-create under the old name, a definition kept for a note type
the user has not made yet, and the shipped examples, which carry null ids.

Fields are deliberately not in here. A field id exists too, but a field is named by the
user in expressions, queries and code, so what a definition stores for one is the name it
is spelled with -- see the same record.

Nothing here writes: resolving a reference never re-binds a null id. Building a reference
from a live object is what does that, and only the editor and the reconcile pass do it.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence, TypedDict

from .definition_schema import CARD_TYPE_SEPARATOR

#: What one of these objects is called when something has to say which kind it is talking
#: about -- a log line, a save blocker, a warning under a query. Fields are in the list
#: because those reports name them too, even though a field has no reference of its own.
KIND_NOTE_TYPE = "note type"
KIND_DECK = "deck"
KIND_CARD_TYPE = "card type"
KIND_FIELD = "field"

#: Appended to a stored name that resolves to neither an id nor a name in this collection,
#: wherever a picker still has to show it. The user chose that name once; hiding it would
#: leave them with a definition that has quietly stopped naming anything.
NOT_FOUND_SUFFIX = " (not found)"


def not_found_label(name: str) -> str:
    return f"{name}{NOT_FOUND_SUFFIX}"


class ObjectRef(TypedDict, total=False):
    """A note type or a deck: its id, which may be null, and the name last seen with it."""

    id: Optional[int]
    name: str


class CardTypeRef(TypedDict, total=False):
    """A card template, which takes both halves: a template id is only unique per note type.

    `name` keeps the display form, `"<NoteType><::><CardType>"`, because that is what the
    card type picker lists and what the user reads back.
    """

    note_type_id: Optional[int]
    template_id: Optional[int]
    name: str


def _as_id(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def normalize_ref(value: Any) -> ObjectRef:
    """Read a stored slot as a reference, accepting the bare name that came before it."""
    if isinstance(value, dict):
        return {"id": _as_id(value.get("id")), "name": str(value.get("name") or "")}
    return {"id": None, "name": str(value or "")}


def normalize_card_type_ref(value: Any) -> CardTypeRef:
    if isinstance(value, dict):
        return {
            "note_type_id": _as_id(value.get("note_type_id")),
            "template_id": _as_id(value.get("template_id")),
            "name": str(value.get("name") or ""),
        }
    return {"note_type_id": None, "template_id": None, "name": str(value or "")}


def normalize_refs(values: Any) -> list[ObjectRef]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        return []
    return [normalize_ref(value) for value in values]


def card_action_card_type(card_action: Any) -> CardTypeRef:
    """The card type a note-level action applies to, in whichever form it is stored.

    `card_type_name` is the pre-0.5.0 spelling: one string holding both halves and no ids.
    A stored definition has one key or the other, never both -- the 0.5.0 step replaces it.
    """
    if not isinstance(card_action, dict):
        return normalize_card_type_ref("")
    if card_action.get("card_type") is not None:
        return normalize_card_type_ref(card_action["card_type"])
    return normalize_card_type_ref(card_action.get("card_type_name"))


def card_type_ref_names_nothing(ref: Any) -> bool:
    """Whether this reference names no card type at all: no ids, and no name either.

    An `edit_card` stage's actions carry one, because the stage has already named the card
    they apply to; before 0.5.0 that was spelled as an empty `card_type_name` string. It is
    not a reference to a card type this collection is missing, and a reader that treats it
    as one reports a card type called ''.
    """
    reference = normalize_card_type_ref(ref)
    return (
        reference["note_type_id"] is None
        and reference["template_id"] is None
        and not reference["name"]
    )


def split_card_type_name(name: str) -> Optional[tuple[str, str]]:
    """The note type and card type halves of a display name, or None if it has neither."""
    if CARD_TYPE_SEPARATOR not in name:
        return None
    note_type_name, card_type_name = name.split(CARD_TYPE_SEPARATOR, 1)
    return note_type_name, card_type_name


# Building a reference from a live object -----------------------------------------------


def note_type_ref(model: Any) -> ObjectRef:
    return {"id": _as_id(model.get("id")), "name": str(model.get("name") or "")}


def deck_ref(deck_id: Any, name: str) -> ObjectRef:
    return {"id": _as_id(deck_id), "name": name}


def card_type_live_name(model: Any, template: Any) -> str:
    """The display form of a resolved card type, spelled the one way a reference does."""
    return f"{model.get('name') or ''}{CARD_TYPE_SEPARATOR}{template.get('name') or ''}"


def card_type_ref(model: Any, template: Any) -> CardTypeRef:
    return {
        "note_type_id": _as_id(model.get("id")),
        # Nullable since before 23.10: a note type saved with template ids stripped keeps
        # None and the backend does not backfill it, so that reference is name-only.
        "template_id": _as_id(template.get("id")),
        "name": card_type_live_name(model, template),
    }


# Resolving ------------------------------------------------------------------------------


def resolve_note_type(ref: Any, col: Any) -> Optional[Any]:
    """The note type this reference names, by id if that still exists, else by name."""
    reference = normalize_ref(ref)
    if reference["id"] is not None:
        model = col.models.get(reference["id"])
        if model is not None:
            return model
    if not reference["name"]:
        return None
    return col.models.by_name(reference["name"])


def resolve_deck_id(ref: Any, col: Any) -> Optional[int]:
    reference = normalize_ref(ref)
    if reference["id"] is not None and col.decks.name_if_exists(reference["id"]) is not None:
        return reference["id"]
    if not reference["name"]:
        return None
    return col.decks.id_for_name(reference["name"])


def ref_matches_note_type(ref: Any, model: Any) -> bool:
    """Whether this reference is to `model`: by id when it has one, else by name."""
    if model is None:
        return False
    reference = normalize_ref(ref)
    if reference["id"] is not None:
        return reference["id"] == model.get("id")
    return bool(reference["name"]) and reference["name"] == model.get("name")


def card_type_ref_matches_note_type(ref: Any, model: Any) -> bool:
    """Whether a card type reference is one of `model`'s.

    A note-level card action carries actions for several note types and each note takes
    only its own, so this is the half that decides whether an action is even addressed to
    this note before its template is looked for.
    """
    reference = normalize_card_type_ref(ref)
    halves = split_card_type_name(reference["name"])
    return ref_matches_note_type(
        {"id": reference["note_type_id"], "name": halves[0] if halves else ""}, model
    )


def resolve_template(ref: Any, model: Any) -> Optional[Any]:
    """The template of `model` this reference names, by id if it has one, else by name."""
    if model is None or not card_type_ref_matches_note_type(ref, model):
        return None
    reference = normalize_card_type_ref(ref)
    templates = model.get("tmpls") or []
    if reference["template_id"] is not None:
        for template in templates:
            if template.get("id") == reference["template_id"]:
                return template
    halves = split_card_type_name(reference["name"])
    if halves is None:
        return None
    for template in templates:
        if template.get("name") == halves[1]:
            return template
    return None


def resolve_card_type(ref: Any, col: Any) -> tuple[Optional[Any], Optional[Any]]:
    """The note type and the template a card type reference names, each or both None.

    The one rule for both halves: the id wins while it exists, the stored half of the
    display name is looked up when it does not. Every reader of a card type reference goes
    through here, because a card type is the one kind whose resolution takes two steps and
    three copies of those two steps drifted apart the moment one of them was fixed.
    """
    reference = normalize_card_type_ref(ref)
    halves = split_card_type_name(reference["name"]) or ("", "")
    model = resolve_note_type({"id": reference["note_type_id"], "name": halves[0]}, col)
    if model is None:
        return None, None
    # The template is looked for in the note type that was *found*, not in the one the
    # reference names: when a stale id fell through to the name, the two differ, and
    # `resolve_template`'s own gate -- which is what tells a note-level action addressed to
    # this note type from one addressed to another (`copy_primitives.py`) -- would answer
    # None for a template whose name is right there.
    return model, resolve_template({**reference, "note_type_id": model["id"]}, model)


def card_type_resolves(ref: Any, col: Any) -> bool:
    """Whether this card type reference still finds a note type and a template of it.

    Both halves, because either can go: the note type may have been deleted, or kept and
    the template removed from it.
    """
    model, template = resolve_card_type(ref, col)
    return model is not None and template is not None


# Display ---------------------------------------------------------------------------------


def note_type_display_name(ref: Any, col: Any) -> str:
    """The live name when the id still resolves, the stored one otherwise.

    This is what every picker shows: after a rename the box offers the new name, and a
    reference that resolves to nothing keeps showing what the user wrote so they can see
    which name went stale.
    """
    model = resolve_note_type(ref, col)
    return str(model["name"]) if model is not None else normalize_ref(ref)["name"]


def deck_display_name(ref: Any, col: Any) -> str:
    reference = normalize_ref(ref)
    deck_id = resolve_deck_id(reference, col)
    if deck_id is None:
        return reference["name"]
    return col.decks.name_if_exists(deck_id) or reference["name"]


def card_type_display_name(ref: Any, col: Any) -> str:
    model, template = resolve_card_type(ref, col)
    if model is None or template is None:
        return normalize_card_type_ref(ref)["name"]
    return card_type_live_name(model, template)
