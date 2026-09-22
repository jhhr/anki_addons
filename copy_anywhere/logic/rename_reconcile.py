"""The one pass that follows what Anki renamed, and reports what it will not follow.

A definition stores an id beside the name for every object Anki gives a stable id, and the
id is what it resolves by (`object_refs.py`), so a renamed note type, deck or card type is
still the one the definition meant. Two things that does not do: the *name* it shows the
user goes stale, and a field -- which is stored as the name the user spelled it with,
everywhere -- has nothing to resolve by at all.

This pass closes both, without a rename hook. It keeps a snapshot of the names the ids had
(`config.data["name_snapshot"]`), and a changed name under an unchanged id is a rename with
**both** names in hand -- which is exactly what Anki's one rename hook gives and its three
missing ones do not, and it works for a rename made on another device, or undone with
Ctrl+Z, because the comparison is against the collection as it is now rather than against
an event. See "Following a rename in Anki" in `docs/follow-ups.md`.

What it rewrites is only what is a whole, delimited value: a cached name inside a
reference, a field slot, and a parsed `{{trigger....}}` token in an expression's *text*.
Search terms and code are read by the user and by Anki's own grammar, not by this addon, so
they are reported instead -- `col.replace_in_search_node` swaps every term of a kind and
cannot rename one deck inside a query naming two, and `note['Word']` is a spelling of a
field name that no `{{...}}` rewrite can see.

Nothing here runs while a dialog is still open: the pass reads the collection as Anki saved
it, so a rename the user cancels was never made, and one undone later is just a second
rename this pass follows back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING, Any, Iterator, Optional

from ..shared.interpolate.interpolate_fields import CARD_VALUE_RE, CARD_VALUES_DICT
from .definition_migration import STAGE_EXPRESSION_KEYS, rewrite_references
from .definition_schema import (
    MODE_CODE,
    STAGE_CARD_QUERY,
    STAGE_CONDITION,
    STAGE_EDIT_NOTE,
    STAGE_NOTE_QUERY,
    is_format_2,
    walk_stages,
)
from .flow_analysis import TRIGGER_BINDING
from .object_refs import (
    KIND_CARD_TYPE,
    KIND_DECK,
    KIND_FIELD,
    KIND_NOTE_TYPE,
    card_type_live_name,
    card_type_ref_names_nothing,
    card_type_resolves,
    normalize_card_type_ref,
    normalize_ref,
    resolve_card_type,
    resolve_deck_id,
    resolve_note_type,
)
from .query_terms import stale_search_terms

if TYPE_CHECKING:  # pragma: no cover -- the import would close a cycle at run time
    from ..configuration import Config

logger = logging.getLogger(__name__)

#: The key `referenced` files a definition under for the note types it *triggers* on, as
#: against the ones it merely names in a card action: only a trigger note type's fields and
#: templates are the ones a definition's own field slots and `{{trigger....}}` tokens spell.
_TRIGGER_NOTE_TYPE = "trigger note type"

#: Where the names the ids last had are kept. Not a definition's business -- it is about the
#: collection, and one entry serves every definition that references the object. It says
#: *which* collection, too: this config is the addon's and is shared by every profile on the
#: machine, while every id in it belongs to the one collection that issued it.
SNAPSHOT_KEY = "name_snapshot"


@dataclass(frozen=True)
class StaleName:
    """One name the pass did not follow, and the definition that still spells it."""

    definition_guid: str
    definition_name: str
    kind: str
    name: str


@dataclass
class ReconcileResult:
    """What one pass did and what it wants the user to know.

    The four lists of names are what a picker or an editor marks a definition with; the
    three lists of lines are the story, in the order it happened.
    """

    bound: list[str] = dataclass_field(default_factory=list)
    refreshed: list[str] = dataclass_field(default_factory=list)
    rewritten: list[str] = dataclass_field(default_factory=list)
    #: A reference whose id and name both resolve to nothing and that the snapshot never
    #: knew: the definition names an object this collection has never had, so it does
    #: nothing rather than the wrong thing.
    unresolved: list[StaleName] = dataclass_field(default_factory=list)
    #: A snapshotted id that is no longer in the collection -- the user deleted the object,
    #: and the name is the last one it had. Reported, never rewritten.
    gone: list[StaleName] = dataclass_field(default_factory=list)
    #: A field or template of a trigger note type that carries no id, so a rename of it
    #: cannot be seen at all (note types saved before Anki 23.10 keep null ids).
    unfollowable: list[StaleName] = dataclass_field(default_factory=list)
    #: An old name still inside a code block, where a mechanical rewrite would be a guess
    #: at what the user meant.
    not_rewritten: list[StaleName] = dataclass_field(default_factory=list)
    #: A name a query spells that the collection does not have. Checked every run rather
    #: than only after a rename: a query can go stale on another device, and nothing else
    #: ever looks inside search text (`query_terms.py`).
    stale_terms: list[StaleName] = dataclass_field(default_factory=list)
    #: The one line a pass on a collection other than the snapshot's has to say: nothing
    #: was followed, because nothing in the snapshot was about this collection.
    collection_changed: Optional[str] = None
    changed: bool = False


# What a definition stores a reference in ---------------------------------------------------


def definitions_hold_references(definitions: Any) -> bool:
    """Whether any stored definition names an object this pass could follow.

    The pass costs two `all_names_and_ids`-sized reads and a `models.get` per referenced
    note type, which is cheap but not free, and a config with no format-2 definition in it
    -- or none that names anything -- has nothing for it to do.
    """
    for definition in definitions or []:
        if not is_format_2(definition):
            continue
        triggers = definition.get("triggers") or {}
        if triggers.get("note_types") or triggers.get("deck_names"):
            return True
        if next(_card_type_refs(definition), None) is not None:
            return True
    return False


def _stale(definition: dict, kind: str, name: str) -> StaleName:
    return StaleName(
        definition_guid=definition.get("guid", ""),
        definition_name=definition.get("definition_name", ""),
        kind=kind,
        name=name,
    )


def _card_type_refs(definition: dict) -> Iterator[dict]:
    """Every card action that carries a structured card type reference.

    An action still in the pre-0.5.0 spelling -- one `card_type_name` string -- is left to
    the config step that converts it; an `edit_card` stage's actions name no card type
    because the stage already names the card, and there is nothing to bind there either.
    That one is spelled `card_type: None`, but a config an earlier 0.5.0 step converted
    carries an empty reference in its place, which names nothing just the same.
    """
    for stage in walk_stages(definition.get("stages") or []):
        for card_action in stage.get("card_actions") or []:
            if not isinstance(card_action, dict):
                continue
            reference = card_action.get("card_type")
            if reference is not None and not card_type_ref_names_nothing(reference):
                yield card_action


def unresolved_references(definition: dict, col: Any) -> list[StaleName]:
    """Every structured reference of one definition that this collection cannot resolve.

    The same condition the pass reports as `unresolved`, asked of a single definition
    without touching it, so the editor can refuse a save over a reference that names
    nothing (§N6) in the words the log already uses.
    """
    stale: list[StaleName] = []
    triggers = definition.get("triggers")
    if isinstance(triggers, dict):
        for value in triggers.get("note_types") or []:
            reference = normalize_ref(value)
            if resolve_note_type(reference, col) is None:
                stale.append(_stale(definition, KIND_NOTE_TYPE, reference["name"]))
        for value in triggers.get("deck_names") or []:
            reference = normalize_ref(value)
            if resolve_deck_id(reference, col) is None:
                stale.append(_stale(definition, KIND_DECK, reference["name"]))
    for card_action in _card_type_refs(definition):
        reference = normalize_card_type_ref(card_action["card_type"])
        if not card_type_resolves(reference, col):
            stale.append(_stale(definition, KIND_CARD_TYPE, reference["name"]))
    return stale


def _search_expressions(stage: dict) -> Iterator[dict]:
    """The expressions of one stage whose text is an Anki search rather than a value."""
    stage_type = stage.get("type", "")
    if stage_type in (STAGE_NOTE_QUERY, STAGE_CARD_QUERY):
        if isinstance(stage.get("query"), dict):
            yield stage["query"]
    elif stage_type == STAGE_CONDITION and stage.get("predicate_kind") == "note_query":
        if isinstance(stage.get("predicate"), dict):
            yield stage["predicate"]


def _report_stale_terms(definition: dict, col: Any, result: ReconcileResult) -> None:
    """What every search in this definition names that the collection does not have."""
    for stage in walk_stages(definition.get("stages") or []):
        for expression in _search_expressions(stage):
            if expression.get("mode") == MODE_CODE:
                # A search built by code is not text this scan can read; the code report
                # below is what covers it.
                continue
            for term in stale_search_terms(expression.get("text"), col):
                stale = _stale(definition, term.kind, term.name)
                if stale not in result.stale_terms:
                    result.stale_terms.append(stale)


# Step 1: what the collection no longer has -------------------------------------------------


def _deleted_objects(snapshot: dict, col: Any) -> dict[tuple, str]:
    """The snapshotted note type and deck ids the collection no longer has, by last name.

    Asked before anything re-binds, because a re-bind is what destroys the answer: a
    reference whose id is gone falls back to its name, and once it carries another id
    nothing is left to say whether the user deleted the object it named or whether this
    collection never had it. That is the difference between `gone` and `unresolved`, and
    the snapshot is the only thing that knows it.
    """
    deleted: dict[tuple, str] = {}
    for key, entry in (snapshot.get("note_types") or {}).items():
        note_type_id = _as_int(key)
        if note_type_id is not None and col.models.get(note_type_id) is None:
            deleted[(KIND_NOTE_TYPE, note_type_id)] = entry.get("name", "")
    for key, name in (snapshot.get("decks") or {}).items():
        deck_id = _as_int(key)
        if deck_id is not None and col.decks.name_if_exists(deck_id) is None:
            deleted[(KIND_DECK, deck_id)] = name
    return deleted


# Step 2: bind and refresh ------------------------------------------------------------------


def _bind(
    definition: dict, col: Any, result: ReconcileResult, referenced: dict, deleted: dict
) -> bool:
    """Give every reference the id and the name the collection has for it now."""
    changed = False
    triggers = definition.get("triggers")
    if isinstance(triggers, dict):
        for key, kind in (("note_types", KIND_NOTE_TYPE), ("deck_names", KIND_DECK)):
            stored = triggers.get(key)
            if not isinstance(stored, list):
                continue
            for index, value in enumerate(stored):
                reference = normalize_ref(value)
                if reference != value:
                    # A bare name, or a reference carrying something that is not an id.
                    changed = True
                # Always the normalized dict, because the refreshes below mutate it in
                # place and it is the stored list that has to see them.
                stored[index] = reference
                if kind == KIND_NOTE_TYPE:
                    model = resolve_note_type(reference, col)
                    live = None if model is None else (model["id"], model["name"])
                else:
                    deck_id = resolve_deck_id(reference, col)
                    name = None if deck_id is None else col.decks.name_if_exists(deck_id)
                    live = None if name is None else (deck_id, name)
                if live is None:
                    last_name = deleted.get((kind, reference["id"]))
                    if last_name is None:
                        result.unresolved.append(
                            _stale(definition, kind, reference["name"])
                        )
                    else:
                        # The pass saw this id bound and now sees it gone, so this is the
                        # user's own deletion rather than a name nothing ever answered to.
                        result.gone.append(_stale(definition, kind, last_name))
                    continue
                _remember(referenced, (kind, live[0]), definition)
                if kind == KIND_NOTE_TYPE:
                    _remember(referenced, (_TRIGGER_NOTE_TYPE, live[0]), definition)
                changed |= _refresh(reference, "id", live[0], definition, kind, result)
                changed |= _refresh(reference, "name", live[1], definition, kind, result)

    for card_action in _card_type_refs(definition):
        reference = normalize_card_type_ref(card_action["card_type"])
        if reference != card_action["card_type"]:
            changed = True
        card_action["card_type"] = reference
        model, template = resolve_card_type(reference, col)
        if model is None or template is None:
            result.unresolved.append(_stale(definition, KIND_CARD_TYPE, reference["name"]))
            continue
        _remember(referenced, (KIND_NOTE_TYPE, model["id"]), definition)
        live_name = card_type_live_name(model, template)
        changed |= _refresh(
            reference, "note_type_id", model["id"], definition, KIND_CARD_TYPE, result
        )
        changed |= _refresh(
            reference, "template_id", template.get("id"), definition, KIND_CARD_TYPE, result
        )
        changed |= _refresh(reference, "name", live_name, definition, KIND_CARD_TYPE, result)
    return changed


def _remember(referenced: dict, key: tuple, definition: dict) -> None:
    """Note that this definition names this object, once however many slots name it."""
    holders = referenced.setdefault(key, [])
    if not any(holder is definition for holder in holders):
        holders.append(definition)


def _refresh(
    reference: dict,
    key: str,
    live: Any,
    definition: dict,
    kind: str,
    result: ReconcileResult,
) -> bool:
    """One key of a reference brought up to what the collection says, if it differs."""
    if reference.get(key) == live:
        return False
    was = reference.get(key)
    reference[key] = live
    where = f"'{definition.get('definition_name', '')}': {kind}"
    if key == "name":
        result.refreshed.append(f"{where} '{was}' is now called '{live}'")
    elif was is None:
        result.bound.append(f"{where} '{reference.get('name', '')}' bound to id {live}")
    else:
        result.bound.append(f"{where} '{reference.get('name', '')}' re-bound to id {live}")
    return True


# Step 3: diff the snapshot ---------------------------------------------------------------


@dataclass
class _Renames:
    """What changed name under one note type id, as it was spelled to as it is spelled."""

    fields: dict[str, str] = dataclass_field(default_factory=dict)
    templates: dict[str, str] = dataclass_field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.fields or self.templates)

    def new_field_name(self, name: Any) -> Optional[str]:
        """What this name is called now, matched as the interpolation matches a field name.

        Case-insensitively, that is: `{{trigger.word}}` reads the field `Word`, so a
        definition spelling it that way has to be followed too. A template name is matched
        exactly instead, because the card values dict keys it exactly.
        """
        if not isinstance(name, str) or not name:
            return None
        lowered = name.lower()
        for old_name, new_name in self.fields.items():
            if old_name.lower() == lowered:
                return new_name
        return None


def _live_names_by_id(entries: Any) -> dict[int, str]:
    return {
        entry["id"]: entry["name"]
        for entry in entries or []
        if entry.get("id") is not None
    }


def _diff(
    snapshot: dict, col: Any, referenced: dict, result: ReconcileResult
) -> dict[int, _Renames]:
    """Compare the snapshotted names against the live ones, by id.

    A changed name under an id that is still there is a rename, and the only place both
    names exist. A field or template id that is gone is reported: a deleted field is not a
    renamed one, and guessing which live field replaced it is how a rewrite writes to the
    wrong field. The note types and decks that are gone were reported before the bind
    (`_deleted_objects`); what is left here is what they contain.
    """
    renames: dict[int, _Renames] = {}
    for key, entry in (snapshot.get("note_types") or {}).items():
        note_type_id = _as_int(key)
        definitions = referenced.get((KIND_NOTE_TYPE, note_type_id)) or []
        if note_type_id is None or not definitions:
            # Nothing references it any more; the regenerated snapshot simply drops it.
            continue
        model = col.models.get(note_type_id)
        if model is None:
            # `referenced` holds only ids that bound live a moment ago, and a snapshotted
            # id the collection no longer has was reported gone before that bind, so there
            # is nothing to say here and nothing under it left to compare.
            continue
        # A field or a template only reaches a definition's own slots through the trigger
        # binding, so a definition that names this note type in a card action alone is not
        # told about them: its `{{trigger....}}` tokens read a different note type.
        triggering = referenced.get((_TRIGGER_NOTE_TYPE, note_type_id)) or []
        renamed = _Renames()
        for stored, live, target, kind in (
            (entry.get("fields"), _live_names_by_id(model.get("flds")), renamed.fields, KIND_FIELD),
            (
                entry.get("templates"),
                _live_names_by_id(model.get("tmpls")),
                renamed.templates,
                KIND_CARD_TYPE,
            ),
        ):
            for stored_key, old_name in (stored or {}).items():
                object_id = _as_int(stored_key)
                new_name = live.get(object_id) if object_id is not None else None
                if new_name is None:
                    for definition in triggering:
                        result.gone.append(_stale(definition, kind, old_name))
                elif new_name != old_name:
                    target[old_name] = new_name
                    for definition in triggering:
                        result.refreshed.append(
                            f"'{definition.get('definition_name', '')}': {kind} '{old_name}'"
                            f" is now called '{new_name}'"
                        )
        if renamed:
            renames[note_type_id] = renamed

    return renames


def _report_unfollowable(model: dict, definitions: list, result: ReconcileResult) -> None:
    """Fields and templates with no id: a rename of one of them cannot be seen.

    Anki has given fields and templates ids since 23.10, but a note type saved before that
    with them stripped keeps `None` and the backend does not backfill, so a collection old
    enough can hold one. Nothing follows such a name, so the definitions that could have
    been rewritten are told which names they are on their own with.
    """
    for entries, kind in ((model.get("flds"), KIND_FIELD), (model.get("tmpls"), KIND_CARD_TYPE)):
        for entry in entries or []:
            if entry.get("id") is not None:
                continue
            for definition in definitions:
                result.unfollowable.append(_stale(definition, kind, entry.get("name", "")))


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Step 4: rewrite --------------------------------------------------------------------------


def _rewrite_reference(reference: str, renamed: _Renames) -> str:
    """One `{{...}}` reference, with a renamed field or card type of the trigger followed."""
    head, dot, rest = reference.partition(".")
    if not dot or head != TRIGGER_BINDING:
        return reference
    new_field = renamed.new_field_name(rest)
    if new_field is not None:
        return f"{head}.{new_field}"
    # A card value names its card type as a prefix: `Recognition__Card_Due`. The key is
    # checked against the real ones, as the interpolation checks it, so a field whose own
    # name happens to contain a double underscore is not read as one.
    match = CARD_VALUE_RE.match(rest)
    if match and match.group(2) in CARD_VALUES_DICT:
        new_template = renamed.templates.get(match.group(1))
        if new_template is not None:
            return f"{head}.{new_template}{rest[len(match.group(1)):]}"
    return reference


def _rewrite_names(names: Any, renamed: _Renames) -> tuple[list, int]:
    rewritten = []
    count = 0
    for name in names or []:
        new_name = renamed.new_field_name(name)
        rewritten.append(new_name if new_name is not None else name)
        count += new_name is not None
    return rewritten, count


def _rewrite(definition: dict, renamed: _Renames, result: ReconcileResult) -> bool:
    """Follow one note type's renames into every slot of a definition that spells one."""
    count = 0

    triggers = definition.get("triggers")
    unfocus = (triggers or {}).get("on_unfocus") if isinstance(triggers, dict) else None
    if isinstance(unfocus, dict):
        for key in ("edit_fields", "add_fields"):
            if isinstance(unfocus.get(key), list):
                unfocus[key], renamed_count = _rewrite_names(unfocus[key], renamed)
                count += renamed_count

    for stage in walk_stages(definition.get("stages") or []):
        # The unfocus gate names editor fields, which are the trigger note's whichever note
        # the stage goes on to write; the migrator copies the same keys onto the stages that
        # feed one write, so both carriers are rewritten.
        for carrier in [stage] + [
            write for write in stage.get("fields") or [] if isinstance(write, dict)
        ]:
            if isinstance(carrier.get("unfocus_trigger_fields"), list):
                carrier["unfocus_trigger_fields"], renamed_count = _rewrite_names(
                    carrier["unfocus_trigger_fields"], renamed
                )
                count += renamed_count
            new_name = renamed.new_field_name(carrier.get("write_if_field"))
            if new_name is not None:
                carrier["write_if_field"] = new_name
                count += 1

        if stage.get("type") == STAGE_EDIT_NOTE and _targets_the_trigger(stage):
            for write in stage.get("fields") or []:
                if not isinstance(write, dict):
                    continue
                new_name = renamed.new_field_name(write.get("field"))
                if new_name is not None:
                    write["field"] = new_name
                    count += 1

        for expression in _expressions(stage):
            count += _rewrite_expression(expression, renamed, definition, result)

    if count:
        result.rewritten.append(
            f"'{definition.get('definition_name', '')}': {count} reference(s) followed"
            f" ({_renames_as_text(renamed)})"
        )
    return bool(count)


def _renames_as_text(renamed: _Renames) -> str:
    return ", ".join(
        f"'{old}' -> '{new}'"
        for old, new in list(renamed.fields.items()) + list(renamed.templates.items())
    )


def _targets_the_trigger(stage: dict) -> bool:
    target = stage.get("target")
    return isinstance(target, dict) and target.get("binding") == TRIGGER_BINDING


def _expressions(stage: dict) -> Iterator[dict]:
    """Every value expression one stage holds, wherever the shape keeps it."""
    for key in STAGE_EXPRESSION_KEYS.get(stage.get("type", ""), ()):
        expression = stage.get(key)
        if isinstance(expression, dict):
            yield expression
    if stage.get("type") == STAGE_EDIT_NOTE:
        for write in stage.get("fields") or []:
            if isinstance(write, dict) and isinstance(write.get("value"), dict):
                yield write["value"]


def _rewrite_expression(
    expression: dict, renamed: _Renames, definition: dict, result: ReconcileResult
) -> int:
    """The `{{trigger....}}` tokens of one expression's text. Its code is only reported.

    A reference in text is a delimited token and rewriting one is exact. The same name
    inside code is not: `note['Word']` is the other spelling of the same field and a
    `{{...}}` rewrite cannot see it, while a rewrite that went looking for the name as a
    substring would also find it inside a string literal that meant something else.
    """
    count = 0

    def follow(reference: str) -> str:
        nonlocal count
        rewritten = _rewrite_reference(reference, renamed)
        count += rewritten != reference
        return rewritten

    text = expression.get("text")
    if isinstance(text, str) and text:
        expression["text"] = rewrite_references(text, follow)

    code = expression.get("code")
    if isinstance(code, str) and code:
        for old_name, kind in [(name, KIND_FIELD) for name in renamed.fields] + [
            (name, KIND_CARD_TYPE) for name in renamed.templates
        ]:
            if old_name.lower() in code.lower():
                result.not_rewritten.append(_stale(definition, kind, old_name))
    return count


# The snapshot -------------------------------------------------------------------------------


def referenced_object_ids(definitions: Any) -> tuple[set, set]:
    """The note type ids and deck ids the stored references are bound to."""
    note_type_ids: set = set()
    deck_ids: set = set()
    for definition in definitions or []:
        if not is_format_2(definition):
            continue
        triggers = definition.get("triggers") or {}
        for value in triggers.get("note_types") or []:
            reference = normalize_ref(value)
            if reference["id"] is not None:
                note_type_ids.add(reference["id"])
        for value in triggers.get("deck_names") or []:
            reference = normalize_ref(value)
            if reference["id"] is not None:
                deck_ids.add(reference["id"])
        for card_action in _card_type_refs(definition):
            reference = normalize_card_type_ref(card_action["card_type"])
            if reference["note_type_id"] is not None:
                note_type_ids.add(reference["note_type_id"])
    return note_type_ids, deck_ids


def _collection_path(col: Any) -> str:
    """Which collection a snapshot is of. The config is shared by every profile on the
    machine, and the ids in it are not: they are the issuing collection's own."""
    return str(getattr(col, "path", "") or "")


def _empty_snapshot(col: Any) -> dict:
    return {"collection": _collection_path(col), "note_types": {}, "decks": {}}


def _snapshot_is_of_another_collection(snapshot: Any, col: Any) -> bool:
    """Whether this snapshot was taken of a different collection than the one in hand.

    A snapshot written before the stamp existed carries no path; it is read as this
    collection's once, and the pass that reads it stamps it. Guessing the other way would
    make every upgrade look like a profile switch.
    """
    if not isinstance(snapshot, dict):
        return False
    stored = snapshot.get("collection")
    return bool(stored) and stored != _collection_path(col)


def build_name_snapshot(definitions: Any, col: Any) -> dict:
    """The names every referenced id has right now -- the "old name" Anki never gives us.

    Fields and templates with a null id are left out: nothing can follow a name with no id
    behind it, and an entry keyed by nothing would compare equal to the wrong field the
    next time the note type is saved.
    """
    note_type_ids, deck_ids = referenced_object_ids(definitions)
    note_types: dict = {}
    for note_type_id in note_type_ids:
        model = col.models.get(note_type_id)
        if model is None:
            continue
        note_types[str(note_type_id)] = {
            "name": model["name"],
            "fields": {
                str(entry["id"]): entry["name"]
                for entry in model.get("flds") or []
                if entry.get("id") is not None
            },
            "templates": {
                str(entry["id"]): entry["name"]
                for entry in model.get("tmpls") or []
                if entry.get("id") is not None
            },
        }
    decks: dict = {}
    for deck_id in deck_ids:
        name = col.decks.name_if_exists(deck_id)
        if name is not None:
            decks[str(deck_id)] = name
    return {
        "collection": _collection_path(col),
        "note_types": note_types,
        "decks": decks,
    }


# The pass ------------------------------------------------------------------------------------


def reconcile(config: "Config", col: Any) -> ReconcileResult:
    """Bind, refresh, follow and report, and write the config only if something changed."""
    result = ReconcileResult()
    definitions = [
        definition for definition in config.copy_definitions if is_format_2(definition)
    ]
    changed = False
    referenced: dict = {}
    stored_snapshot = config.data.get(SNAPSHOT_KEY) or {}
    snapshot = stored_snapshot
    if _snapshot_is_of_another_collection(snapshot, col):
        # A note type id, a deck id and a field id are the issuing collection's own, and
        # the config that holds them is shared by every profile. So nothing the snapshot
        # says is evidence about the collection in front of the pass now: no id in it is
        # gone, no name in it changed, and a name that differs under an id both happen to
        # have is not a rename but two collections. The snapshot is left out of this pass
        # entirely -- every reference re-binds by the one rule, the id first and the name
        # after it -- and replaced with this collection's names below. A rename is only
        # ever followed inside the collection it happened in.
        result.collection_changed = (
            f"the collection changed from '{snapshot.get('collection')}'"
            f" to '{_collection_path(col)}'"
        )
        snapshot = {}
    deleted = _deleted_objects(snapshot, col)
    for definition in definitions:
        changed |= _bind(definition, col, result, referenced, deleted)

    for (kind, object_id), holders in referenced.items():
        model = col.models.get(object_id) if kind == _TRIGGER_NOTE_TYPE else None
        if model is not None:
            _report_unfollowable(model, holders, result)

    for definition in definitions:
        _report_stale_terms(definition, col, result)

    renames = _diff(snapshot, col, referenced, result)
    for note_type_id, renamed in renames.items():
        for definition in referenced.get((_TRIGGER_NOTE_TYPE, note_type_id)) or []:
            changed |= _rewrite(definition, renamed, result)

    refreshed_snapshot = build_name_snapshot(definitions, col)
    if refreshed_snapshot != (stored_snapshot or _empty_snapshot(col)):
        # The snapshot going out of date is itself a change worth a write: it is what the
        # next pass compares against, so a first run on a config that has none has to store
        # one or no rename after it could ever be seen. A snapshot that gains nothing but
        # its collection stamp differs too, which is how another collection's is replaced.
        config.data[SNAPSHOT_KEY] = refreshed_snapshot
        changed = True

    result.changed = changed
    if changed:
        config._save_definitions()
    return result


def log_result(result: ReconcileResult) -> None:
    """Put the pass's report where the user looks: this operation's log file.

    A bind, a refreshed name and a followed rename are the pass doing its job, so they are
    written at info; a name that resolves to nothing, a deleted object and a name left
    inside code are things only the user can fix, so they are warnings.
    """
    if result.collection_changed is not None:
        # First, because it is why everything under it re-bound and nothing was followed.
        logger.info(
            "Rename reconcile: %s, so no name was followed across the two",
            result.collection_changed,
        )
    for line in result.bound + result.refreshed + result.rewritten:
        logger.info("Rename reconcile: %s", line)
    for stale in result.unfollowable:
        logger.info(
            "Rename reconcile: '%s' spells %s '%s', which has no id, so a rename of it"
            " cannot be followed",
            stale.definition_name,
            stale.kind,
            stale.name,
        )
    for stale in result.unresolved:
        logger.warning(
            "Rename reconcile: '%s' names %s '%s', which this collection does not have",
            stale.definition_name,
            stale.kind,
            stale.name,
        )
    for stale in result.gone:
        logger.warning(
            "Rename reconcile: %s '%s', which '%s' uses, has been deleted",
            stale.kind,
            stale.name,
            stale.definition_name,
        )
    for stale in result.not_rewritten:
        logger.warning(
            "Rename reconcile: not rewritten: code in '%s' mentions %s '%s'",
            stale.definition_name,
            stale.kind,
            stale.name,
        )
    for stale in result.stale_terms:
        logger.warning(
            "Rename reconcile: not rewritten: a search in '%s' names %s '%s', which this"
            " collection does not have",
            stale.definition_name,
            stale.kind,
            stale.name,
        )


__all__ = [
    "KIND_CARD_TYPE",
    "KIND_DECK",
    "KIND_FIELD",
    "KIND_NOTE_TYPE",
    "SNAPSHOT_KEY",
    "ReconcileResult",
    "StaleName",
    "build_name_snapshot",
    "definitions_hold_references",
    "log_result",
    "reconcile",
    "referenced_object_ids",
    "unresolved_references",
]
