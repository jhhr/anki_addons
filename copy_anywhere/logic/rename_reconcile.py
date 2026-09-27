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
an event. Why Anki's hooks cannot do this is in `docs/follow-ups.md`, and what the pass does
for the user is in `docs/staged-definitions.md`, both under "Following a rename in Anki".

What it rewrites is only what is a whole, delimited value: a cached name inside a
reference, a field slot, and a parsed `{{trigger....}}` token in an expression's *text*.
Search terms and code are read by the user and by Anki's own grammar, not by this addon, so
they are reported instead -- `col.replace_in_search_node` swaps every term of a kind and
cannot rename one deck inside a query naming two, and `note['Word']` is a spelling of a
field name that no `{{...}}` rewrite can see.

It rewrites only a definition with one trigger note type, where following a rename is
mechanical. A definition on several spells a field or a card type once for all of them, so
a rename in one leaves it wrong whichever name it spells, and every rule that tried to
decide for it (follow once all of them agree, withhold when another has both names) found
a way to decide wrongly and quietly. So it is not rewritten at all: it is marked
(`BROKEN_KEY`), as is a one-trigger definition whose code still mentions the old name and
any definition spelling a field or card type that was deleted. A marked definition is not
run until the user has updated it and dismissed the mark in the definition editor. The
pass never re-derives a mark; the only one it takes back is a rename undone.

Nothing here runs while a dialog is still open: the pass reads the collection as Anki saved
it, so a rename the user cancels was never made, and one undone later is just a second
rename this pass follows back.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING, Any, Final, Iterator, Optional, Union, cast

from ..shared.interpolate.interpolate_fields import CARD_VALUE_RE, CARD_VALUES_DICT
from .definition_migration import STAGE_EXPRESSION_KEYS, rewrite_references
from .definition_schema import (
    MODE_CODE,
    STAGE_CARD_QUERY,
    STAGE_CONDITION,
    STAGE_EDIT_NOTE,
    STAGE_NOTE_QUERY,
    CopyDefinitionV2,
    FieldWrite,
    Stage,
    ValueExpression,
    is_format_2,
    walk_stages,
)
from .flow_analysis import TRIGGER_BINDING, names_a_note_or_card_value
from .object_refs import (
    KIND_CARD_TYPE,
    KIND_DECK,
    KIND_FIELD,
    KIND_NOTE_TYPE,
    CardTypeRef,
    ObjectRef,
    card_type_live_name,
    card_type_ref_names_nothing,
    card_type_resolves,
    normalize_card_type_ref,
    normalize_ref,
    resolve_card_type,
    resolve_deck_id,
    resolve_note_type,
)
from .query_terms import CollectionNames, stale_search_terms

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
#: machine, while every id in it belongs to the one collection that issued it. The stamp is
#: the collection's path; `_collection_path` says why, and what that costs.
SNAPSHOT_KEY = "name_snapshot"

#: Where a definition keeps the renames and deletions this pass could not follow into it,
#: one entry per field or template: `{"kind", "note_type_id", "id", "old", "new",
#: "message"}`, `new` None for a deletion. Written once, with the names as they were then,
#: and never re-derived: a rename in a definition with several trigger note types, a name
#: its code still mentions, a field or card type it spells that was deleted. The user
#: updates the definition and dismisses the mark in the editor; the pass itself only drops
#: an entry whose object is called `old` again (the rename undone) and updates one renamed
#: a second time. Entries a version before this one stored (`{"field"}` or `{"card_type"}`
#: with a message, and no `"id"`) are read for their message and otherwise left alone. See
#: "Following a rename in Anki" in `docs/staged-definitions.md`.
BROKEN_KEY: Final = "broken_by_rename"


@dataclass(frozen=True)
class StaleName:
    """One name the pass did not follow, and the definition that still spells it."""

    definition_guid: str
    definition_name: str
    kind: str
    name: str
    #: Why, for the entries that have more to say than their kind and name.
    message: str = ""


@dataclass
class ReconcileResult:
    """What one pass did and what it wants the user to know.

    The lists of names are what the log reports, and what the warning after a note type
    edit lists (`newly_marked`); the three lists of lines are the story, in the order it
    happened. Nothing keeps a result past the pass: the picker and the editor ask the
    collection and the stored marks instead, which a later pass or a restart cannot lose.
    """

    bound: list[str] = dataclass_field(default_factory=list)
    refreshed: list[str] = dataclass_field(default_factory=list)
    rewritten: list[str] = dataclass_field(default_factory=list)
    #: A reference whose id and name both resolve to nothing and that the snapshot never
    #: knew: the definition names an object this collection has never had, so it does
    #: nothing rather than the wrong thing.
    unresolved: list[StaleName] = dataclass_field(default_factory=list)
    #: A snapshotted note type, deck or card action's card type that is no longer in the
    #: collection -- the user deleted the object, and the name is the last one it had.
    #: Reported, never rewritten. A deleted field or template marks the definitions that
    #: spell it instead (`broken`), since a report is gone again by the next pass.
    gone: list[StaleName] = dataclass_field(default_factory=list)
    #: A field or template of a trigger note type that carries no id, so a rename of it
    #: cannot be seen at all (note types saved before Anki 23.10 keep null ids).
    unfollowable: list[StaleName] = dataclass_field(default_factory=list)
    #: A name a query spells that the collection does not have. Checked every run rather
    #: than only after a rename: a query can go stale on another device, and nothing else
    #: ever looks inside search text (`query_terms.py`).
    stale_terms: list[StaleName] = dataclass_field(default_factory=list)
    #: Every mark a definition carries after this pass (`BROKEN_KEY`), named by the name the
    #: definition spells. Listed for as long as the definition stays marked, not only on the
    #: pass that marked it.
    broken: list[StaleName] = dataclass_field(default_factory=list)
    #: The marks this pass added, which is what the user has not been told about yet. An
    #: entry it only updated (the object renamed again) is not new.
    newly_marked: list[StaleName] = dataclass_field(default_factory=list)
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


def _stale(definition: CopyDefinitionV2, kind: str, name: str) -> StaleName:
    return StaleName(
        definition_guid=definition.get("guid", ""),
        definition_name=definition.get("definition_name", ""),
        kind=kind,
        name=name,
    )


def _card_type_refs(definition: CopyDefinitionV2) -> Iterator[dict]:
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


def unresolved_references(definition: CopyDefinitionV2, col: Any) -> list[StaleName]:
    """Every structured reference of one definition that this collection cannot resolve.

    The same condition the pass reports as `unresolved` (and, for an object it saw
    deleted, as `gone`), asked of a single definition without touching it, so the editor
    can warn about a reference that names nothing and the picker can mark its row (§N6).
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
        card_type = normalize_card_type_ref(card_action["card_type"])
        if not card_type_resolves(card_type, col):
            stale.append(_stale(definition, KIND_CARD_TYPE, card_type["name"]))
    return stale


def _code_mentions(definition: CopyDefinitionV2, name: str) -> bool:
    """Whether any expression's code holds this name, as the pass looks for one there."""
    lowered = name.lower()
    return bool(lowered) and any(
        isinstance(expression.get("code"), str) and lowered in expression["code"].lower()
        for stage in walk_stages(definition.get("stages") or [])
        for expression in _expressions(stage)
    )


def _search_expressions(stage: Stage) -> Iterator[ValueExpression]:
    """The expressions of one stage whose text is an Anki search rather than a value."""
    stage_type = stage.get("type", "")
    if stage_type in (STAGE_NOTE_QUERY, STAGE_CARD_QUERY):
        if isinstance(stage.get("query"), dict):
            yield stage["query"]
    elif stage_type == STAGE_CONDITION and stage.get("predicate_kind") == "note_query":
        if isinstance(stage.get("predicate"), dict):
            yield stage["predicate"]


def stale_terms_in_searches(
    definition: CopyDefinitionV2, col: Any, names: Optional[CollectionNames] = None
) -> list[StaleName]:
    """What every search in this definition names that the collection does not have.

    Asked of the collection as it is now, one entry per name however many searches spell
    it, so the definition list can show it as the list is drawn as well as the pass
    reporting it. `names` is a name list the caller shares across definitions (the pass,
    the picker); without one, this definition's searches share their own.
    """
    if names is None:
        names = CollectionNames(col)
    found: list[StaleName] = []
    for stage in walk_stages(definition.get("stages") or []):
        for expression in _search_expressions(stage):
            if expression.get("mode") == MODE_CODE:
                # A search built by code is not text this scan can read; the code report
                # below is what covers it.
                continue
            for term in stale_search_terms(expression.get("text"), col, names):
                stale = _stale(definition, term.kind, term.name)
                if stale not in found:
                    found.append(stale)
    return found


def _report_stale_terms(
    definition: CopyDefinitionV2, col: Any, names: CollectionNames, result: ReconcileResult
) -> None:
    for stale in stale_terms_in_searches(definition, col, names):
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

    A template removed from a note type that is still here is a deletion too, and the only
    one a card action can suffer without its note type going. It is keyed by both ids,
    `(KIND_CARD_TYPE, note_type_id, template_id)`, because a template id is only unique
    within its note type. A deleted note type's templates are not listed: the note type
    is what was deleted, and that is reported once.
    """
    deleted: dict[tuple, str] = {}
    for key, entry in (snapshot.get("note_types") or {}).items():
        note_type_id = _as_int(key)
        if note_type_id is None:
            continue
        model = col.models.get(note_type_id)
        if model is None:
            deleted[(KIND_NOTE_TYPE, note_type_id)] = entry.get("name", "")
            continue
        live_templates = _live_names_by_id(model.get("tmpls"))
        for template_key, name in (entry.get("templates") or {}).items():
            template_id = _as_int(template_key)
            if template_id is not None and template_id not in live_templates:
                deleted[(KIND_CARD_TYPE, note_type_id, template_id)] = name
    for key, name in (snapshot.get("decks") or {}).items():
        deck_id = _as_int(key)
        if deck_id is not None and col.decks.name_if_exists(deck_id) is None:
            deleted[(KIND_DECK, deck_id)] = name
    return deleted


# Step 2: bind and refresh ------------------------------------------------------------------


def _bind(
    definition: CopyDefinitionV2,
    col: Any,
    result: ReconcileResult,
    referenced: dict,
    deleted: dict,
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
        card_type = normalize_card_type_ref(card_action["card_type"])
        if card_type != card_action["card_type"]:
            changed = True
        card_action["card_type"] = card_type
        model, template = resolve_card_type(card_type, col)
        if model is None or template is None:
            # Named by the reference's own cached name, not the snapshot's: the pass
            # refreshed it while the card type was live, and it is what a live check of the
            # same reference (`unresolved_references`) spells, so the two read as one.
            stale = _stale(definition, KIND_CARD_TYPE, card_type["name"])
            if (KIND_NOTE_TYPE, card_type["note_type_id"]) in deleted or (
                KIND_CARD_TYPE,
                card_type["note_type_id"],
                card_type["template_id"],
            ) in deleted:
                result.gone.append(stale)
            else:
                result.unresolved.append(stale)
            continue
        _remember(referenced, (KIND_NOTE_TYPE, model["id"]), definition)
        live_name = card_type_live_name(model, template)
        changed |= _refresh(
            card_type, "note_type_id", model["id"], definition, KIND_CARD_TYPE, result
        )
        changed |= _refresh(
            card_type, "template_id", template.get("id"), definition, KIND_CARD_TYPE, result
        )
        changed |= _refresh(card_type, "name", live_name, definition, KIND_CARD_TYPE, result)
    return changed


def _remember(referenced: dict, key: tuple, definition: CopyDefinitionV2) -> None:
    """Note that this definition names this object, once however many slots name it."""
    holders = referenced.setdefault(key, [])
    if not any(holder is definition for holder in holders):
        holders.append(definition)


def _refresh(
    reference: Union[ObjectRef, CardTypeRef],
    key: str,
    live: Any,
    definition: CopyDefinitionV2,
    kind: str,
    result: ReconcileResult,
) -> bool:
    """One key of a reference brought up to what the collection says, if it differs."""
    if reference.get(key) == live:
        return False
    was = reference.get(key)
    # The key is one of the reference's own, named by the caller; a TypedDict only takes a
    # literal one.
    reference[key] = live  # type: ignore[literal-required]
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
    #: The id of each renamed field and template, by kind and old name: what a mark the
    #: rename leaves remembers it by, since a name can pass to another field in the same
    #: save (a swap).
    ids: dict[tuple[str, str], int] = dataclass_field(default_factory=dict)
    #: The fields and templates deleted from the note type, as `(kind, id, last name)`.
    #: Not renames, so `__bool__` and the rewrite ignore them; only a mark says them.
    deleted: list[tuple[str, int, str]] = dataclass_field(default_factory=list)

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

    def new_template_name(self, name: str) -> Optional[str]:
        """What this card type is called now, matched exactly (see `new_field_name`)."""
        return self.templates.get(name)


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
    names exist. A field or template id that is gone is a deletion, kept apart: a deleted
    field is not a renamed one, and guessing which live field replaced it is how a rewrite
    writes to the wrong field. The note types and decks that are gone were reported before
    the bind (`_deleted_objects`); what is left here is what they contain.
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
                if object_id is None:
                    continue
                new_name = live.get(object_id)
                if new_name is None:
                    renamed.deleted.append((kind, object_id, old_name))
                elif new_name != old_name:
                    target[old_name] = new_name
                    renamed.ids[(kind, old_name)] = object_id
                    for definition in triggering:
                        result.refreshed.append(
                            f"'{definition.get('definition_name', '')}': {kind} '{old_name}'"
                            f" is now called '{new_name}'"
                        )
        if renamed or renamed.deleted:
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
        new_template = renamed.new_template_name(match.group(1))
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


def _rewrite(definition: CopyDefinitionV2, renamed: _Renames, result: ReconcileResult) -> bool:
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
        # feed one write, so both carriers are rewritten. `write_if_field` is only ever on
        # such a stage (`definition_migration._write_gate`): the write itself names its
        # field in `field`.
        count += _rewrite_unfocus_gate(stage, renamed)
        new_name = renamed.new_field_name(stage.get("write_if_field"))
        if new_name is not None:
            stage["write_if_field"] = new_name
            count += 1
        for write in stage.get("fields") or []:
            if isinstance(write, dict):
                count += _rewrite_unfocus_gate(write, renamed)

        if stage.get("type") == STAGE_EDIT_NOTE and _targets_the_trigger(stage):
            for write in stage.get("fields") or []:
                if not isinstance(write, dict):
                    continue
                new_name = renamed.new_field_name(write.get("field"))
                if new_name is not None:
                    write["field"] = new_name
                    count += 1

        for expression in _expressions(stage):
            count += _rewrite_expression(expression, renamed)

    if count:
        result.rewritten.append(
            f"'{definition.get('definition_name', '')}': {count} reference(s) followed"
            f" ({_renames_as_text(renamed)})"
        )
    return bool(count)


def _rewrite_unfocus_gate(carrier: Union[Stage, FieldWrite], renamed: _Renames) -> int:
    if not isinstance(carrier.get("unfocus_trigger_fields"), list):
        return 0
    carrier["unfocus_trigger_fields"], renamed_count = _rewrite_names(
        carrier["unfocus_trigger_fields"], renamed
    )
    return renamed_count


def _renames_as_text(renamed: _Renames) -> str:
    return ", ".join(
        f"'{old}' -> '{new}'"
        for old, new in list(renamed.fields.items()) + list(renamed.templates.items())
    )


def _targets_the_trigger(stage: Stage) -> bool:
    target = stage.get("target")
    return isinstance(target, dict) and target.get("binding") == TRIGGER_BINDING


def _expressions(stage: Stage) -> Iterator[ValueExpression]:
    """Every value expression one stage holds, wherever the shape keeps it."""
    for key in STAGE_EXPRESSION_KEYS.get(stage.get("type", ""), ()):
        expression = stage.get(key)
        if isinstance(expression, dict):
            # Read by a key held in a variable, so typed a plain dict; every key
            # STAGE_EXPRESSION_KEYS lists is a stage's ValueExpression slot.
            yield cast(ValueExpression, expression)
    if stage.get("type") == STAGE_EDIT_NOTE:
        for write in stage.get("fields") or []:
            if isinstance(write, dict) and isinstance(write.get("value"), dict):
                yield write["value"]


def _rewrite_expression(expression: ValueExpression, renamed: _Renames) -> int:
    """The `{{trigger....}}` tokens of one expression's text. Its code is left alone.

    A reference in text is a delimited token and rewriting one is exact. The same name
    inside code is not: `note['Word']` is the other spelling of the same field and a
    `{{...}}` rewrite cannot see it, while a rewrite that went looking for the name as a
    substring would also find it inside a string literal that meant something else. A
    mention there marks the definition instead (`_marks_for`).
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
    return count


# Step 5: what a definition spells --------------------------------------------------------


class _NameRecorder(_Renames):
    """A rename that renames nothing and remembers every name it was asked about.

    Run through `_rewrite` it visits exactly the slots a rename would be followed into, so
    what "the fields this definition spells for its trigger" means cannot drift from what
    the rewrite touches. Field names are kept folded to lower case, as they are matched, and
    each with the spelling it was first met under, for a message to quote; card type names
    as spelled.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fields_seen: set[str] = set()
        self.field_spellings: dict[str, str] = {}
        self.templates_seen: set[str] = set()

    def new_field_name(self, name: Any) -> Optional[str]:
        if isinstance(name, str) and name:
            self.fields_seen.add(name.lower())
            self.field_spellings.setdefault(name.lower(), name)
        return None

    def new_template_name(self, name: str) -> Optional[str]:
        if name:
            self.templates_seen.add(name)
        return None


def _recorded_names(definition: CopyDefinitionV2) -> _NameRecorder:
    recorder = _NameRecorder()
    # On a copy: the walk writes each slot back, and a reference it re-serialises is not
    # guaranteed to come back byte for byte.
    _rewrite(deepcopy(definition), recorder, ReconcileResult())
    return recorder


def _trigger_models(definition: CopyDefinitionV2, col: Any) -> list[dict]:
    models: list[dict] = []
    triggers = definition.get("triggers")
    for value in (triggers.get("note_types") or []) if isinstance(triggers, dict) else []:
        model = resolve_note_type(value, col)
        if model is not None and all(model["id"] != other["id"] for other in models):
            models.append(model)
    return models


def _has_field(model: dict, name: str) -> bool:
    """As the interpolation reads a field name: without regard to case."""
    lowered = name.lower()
    return any(entry.get("name", "").lower() == lowered for entry in model.get("flds") or [])


def _has_template(model: dict, name: str) -> bool:
    """As the card values dict keys a card type: exactly (see `_Renames.new_field_name`)."""
    return any(entry.get("name") == name for entry in model.get("tmpls") or [])


def trigger_names_not_on_every_note_type(definition: CopyDefinitionV2, col: Any) -> list[str]:
    """Each trigger field or card type a definition on several note types spells that some
    of them lack, as a sentence that says what to do about it.

    A definition spells a trigger field once for every note type it triggers on, so a field
    one of them lacks makes it fail on that note type's notes and write into the others'
    as if nothing were wrong. That is what a rename in only some of the note types leaves
    behind (the pass marks it, `BROKEN_KEY`), and what an edit that trades one name for
    another can do just as well: `{{trigger.Word}}` rewritten to `{{trigger.Term}}` while
    the other note type still says `Word`. So the editor refuses it, at every slot the
    rewrite walks -- field writes on the trigger, the unfocus lists, `write_if_field`, and
    `{{trigger....}}` tokens in text. The card type a `{{trigger.<Card type>__<Key>}}`
    token names is held to the same rule; a card action's card type is not, since it names
    one note type's template by id.

    Only a name *some* trigger note types have: a field none of them has is the analyser's
    to report (`flow_analysis.check_note_field`), and a note or card value key is not a
    field. One trigger note type cannot disagree with itself, so a definition with one is
    fine.
    """
    models = _trigger_models(definition, col)
    if len(models) < 2:
        return []
    recorded = _recorded_names(definition)
    problems: list[str] = []
    for kind, name, lacking in [
        (KIND_FIELD, name, [model for model in models if not _has_field(model, name)])
        for name in recorded.field_spellings.values()
        if not names_a_note_or_card_value(name)
    ] + [
        (KIND_CARD_TYPE, name, [model for model in models if not _has_template(model, name)])
        # Sorted: a set's order changes from run to run, and the editor lists these.
        for name in sorted(recorded.templates_seen)
    ]:
        if not lacking or len(lacking) == len(models):
            continue
        noun = "note type" if len(lacking) == 1 else "note types"
        problems.append(
            f'{kind.capitalize()} "{name}" is not on {noun}'
            f" {_quoted_list([str(model.get('name', '')) for model in lacking])},"
            f" which this definition also triggers on; use a {kind} all of them have, or"
            " rename it in the others too."
        )
    return problems


def _quoted_list(names: list[str]) -> str:
    quoted = [f'"{name}"' for name in names]
    if len(quoted) < 2:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " & " + quoted[-1]


# Step 6: mark what was not followed -------------------------------------------------------


def _stored_trigger_count(definition: CopyDefinitionV2) -> int:
    """How many trigger note types a definition stores, resolved or not.

    Counted as stored rather than as resolved: a definition whose second note type is
    missing today still spells its names for both, and deciding by what happens to resolve
    would follow a rename into it that the other note type never had.
    """
    triggers = definition.get("triggers")
    stored = triggers.get("note_types") if isinstance(triggers, dict) else None
    return len(stored) if isinstance(stored, list) else 0


def _spells(recorded: _NameRecorder, kind: str, name: str) -> bool:
    if kind == KIND_FIELD:
        return name.lower() in recorded.fields_seen
    return name in recorded.templates_seen


def _same_name(kind: str, one: str, other: str) -> bool:
    """As each kind is matched: a field without regard to case, a card type exactly."""
    return one.lower() == other.lower() if kind == KIND_FIELD else one == other


def _mark_message(
    kind: str,
    note_type_name: str,
    old: str,
    new: Optional[str],
    followed: bool,
    in_code: bool,
) -> str:
    """The sentence a mark carries, with the names as they are when it is written."""
    what = f'{kind.capitalize()} "{old}" of note type "{note_type_name}"'
    what += " was deleted" if new is None else f' was renamed to "{new}"'
    if not in_code:
        return what
    if followed:
        # The text was rewritten, so the definition is half followed; saying only "renamed"
        # would send the user looking for `{{...}}` references that are already right.
        return (
            f"{what}; its {{{{...}}}} references were followed, but code in this definition"
            f' still mentions "{old}"'
        )
    return f'{what}, and code in this definition still mentions "{old}"'


def _mark_key(entry: Any) -> Optional[tuple[int, str, int]]:
    """The object an entry is about, `(note_type_id, kind, id)`, if it names one.

    The entries an earlier version stored name no object this way (`BROKEN_KEY`), so they
    have no key: nothing here updates or removes them.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("old"), str):
        return None
    kind = entry.get("kind")
    note_type_id = _as_int(entry.get("note_type_id"))
    object_id = _as_int(entry.get("id"))
    if kind not in (KIND_FIELD, KIND_CARD_TYPE) or note_type_id is None or object_id is None:
        return None
    return note_type_id, kind, object_id


def _mark_keys(definition: CopyDefinitionV2) -> set[tuple[int, str, int]]:
    stored = definition.get(BROKEN_KEY)
    keys = {_mark_key(entry) for entry in (stored if isinstance(stored, list) else [])}
    return {key for key in keys if key is not None}


def _refresh_marks(definition: CopyDefinitionV2, col: Any) -> bool:
    """Take back the entries whose rename was undone, and follow one renamed again.

    Asked of every stored entry by its ids on every pass, not only when the snapshot shows a
    rename: undoing a rename is itself a rename, but the object called `old` again is the
    simpler test, and it holds after a restart too. That is the only way a mark goes by
    itself; one the user has made moot by editing the definition stays until dismissed,
    since the pass cannot tell a fix from a mistake. An entry whose note type is gone is
    left as it is.
    """
    stored = definition.get(BROKEN_KEY)
    if not isinstance(stored, list):
        return False
    changed = False
    kept: list = []
    for entry in stored:
        key = _mark_key(entry)
        model = None if key is None else col.models.get(key[0])
        if key is None or model is None:
            kept.append(entry)
            continue
        _note_type_id, kind, object_id = key
        live = _live_names_by_id(model.get("flds" if kind == KIND_FIELD else "tmpls")).get(
            object_id
        )
        old = entry["old"]
        if live is not None and _same_name(kind, live, old):
            changed = True
            continue
        if live != entry.get("new"):
            entry["new"] = live
            entry["message"] = _mark_message(
                kind,
                str(model.get("name", "")),
                old,
                live,
                followed=live is not None and _stored_trigger_count(definition) == 1,
                in_code=_code_mentions(definition, old),
            )
            changed = True
        kept.append(entry)
    if not changed:
        return False
    if kept:
        definition[BROKEN_KEY] = kept
    else:
        del definition[BROKEN_KEY]
    return True


def _marks_for(
    definition: CopyDefinitionV2,
    recorded: _NameRecorder,
    model: dict,
    changes: _Renames,
    followed: bool,
) -> list[dict]:
    """The entries one note type's renames and deletions leave on one definition.

    `followed` says its renames were rewritten into the definition (one trigger note
    type), so a rename is marked only where its code still mentions the old name; otherwise
    wherever the definition spells the old name or its code mentions it. A deletion is
    marked wherever it is spelled or mentioned, followed or not. `recorded` is what the
    definition spelled before any rewrite, since a rename in the same save can hand a
    deleted field's name to another one.
    """
    note_type_name = str(model.get("name", ""))
    found: list[tuple[str, int, str, Optional[str]]] = [
        (kind, changes.ids[(kind, old)], old, new)
        for kind, renames in ((KIND_FIELD, changes.fields), (KIND_CARD_TYPE, changes.templates))
        for old, new in renames.items()
    ] + [(kind, object_id, old, None) for kind, object_id, old in changes.deleted]
    entries: list[dict] = []
    for kind, object_id, old, new in found:
        in_code = _code_mentions(definition, old)
        rewritten = followed and new is not None
        if not in_code and (rewritten or not _spells(recorded, kind, old)):
            continue
        entries.append(
            {
                "kind": kind,
                "note_type_id": model["id"],
                "id": object_id,
                "old": old,
                "new": new,
                "message": _mark_message(kind, note_type_name, old, new, rewritten, in_code),
            }
        )
    return entries


def _add_marks(
    definition: CopyDefinitionV2,
    entries: list[dict],
    known: set[tuple[int, str, int]],
    result: ReconcileResult,
) -> bool:
    """Store the entries about objects the definition was not already marked for.

    `known` is what its marks were about when the pass began. An object among them has had
    its entry updated or taken back already (`_refresh_marks`), and a rename the snapshot
    shows for it is that same change seen from the other side: undoing a swap is a swap,
    and marking it again would put back the entry the undo just removed.
    """
    stored = definition.get(BROKEN_KEY)
    marks = stored if isinstance(stored, list) else []
    added = False
    for entry in entries:
        key = _mark_key(entry)
        if key in known or any(_mark_key(existing) == key for existing in marks):
            continue
        marks.append(entry)
        result.newly_marked.append(_mark_as_stale(definition, entry))
        added = True
    if added:
        definition[BROKEN_KEY] = marks
    return added


def _mark_as_stale(definition: CopyDefinitionV2, entry: dict) -> StaleName:
    """One mark entry as the report lists it, whichever version stored it."""
    if isinstance(entry.get("old"), str):
        kind = KIND_CARD_TYPE if entry.get("kind") == KIND_CARD_TYPE else KIND_FIELD
        name = entry["old"]
    else:
        kind = KIND_CARD_TYPE if "field" not in entry and "card_type" in entry else KIND_FIELD
        name = str(entry.get("card_type" if kind == KIND_CARD_TYPE else "field", ""))
    return StaleName(
        definition_guid=definition.get("guid", ""),
        definition_name=definition.get("definition_name", ""),
        kind=kind,
        name=name,
        message=str(entry.get("message", "")),
    )


def _report_broken(definition: CopyDefinitionV2, result: ReconcileResult) -> None:
    stored = definition.get(BROKEN_KEY)
    for entry in stored if isinstance(stored, list) else []:
        if isinstance(entry, dict):
            result.broken.append(_mark_as_stale(definition, entry))


#: What the user can do about a definition marked `BROKEN_KEY`, said after the stored
#: message wherever a run refuses it. Undoing the rename takes the mark back too
#: (`_refresh_marks`), but the advice is for keeping the rename, which is the usual case.
BROKEN_ADVICE = "Update the definition, then dismiss the mark in the definition editor."


def broken_by_rename_messages(definition: Any) -> list[str]:
    """The stored messages of a definition a rename left marked, or none if it is whole.

    A marked definition is not run: a name it spells, or its code mentions, no longer means
    what it did in some note type it triggers on, so it would fail on that note type's notes
    or read another field, and go on writing as if nothing had happened, while the user
    still has to decide what it should say. The mark is read as stored, never re-derived: it
    is the user's to dismiss. Any entry with a message counts, including the shapes earlier
    versions stored. A mark that has been mangled by hand -- not a list, entries that are
    not dicts, no message -- counts only for its well-formed entries: one with nothing to
    say does not stop the run.
    """
    return [message for _entry, message in broken_by_rename_entries(definition)]


def broken_by_rename_entries(definition: Any) -> list[tuple[dict, str]]:
    """Each stored mark entry that has something to say, with its message, in stored order.

    The entries themselves rather than their messages, for the editor, which dismisses one
    entry at a time: two entries can carry the same message (a stored list edited by hand,
    or shapes from two versions), and dismissing one must not take the other with it.
    """
    if not isinstance(definition, dict):
        return []
    stored = definition.get(BROKEN_KEY)
    if not isinstance(stored, list):
        return []
    found: list[tuple[dict, str]] = []
    for entry in stored:
        message = entry.get("message") if isinstance(entry, dict) else None
        if isinstance(message, str) and message.strip():
            found.append((entry, message))
    return found


def broken_by_rename_tooltip(messages: list[str]) -> str:
    """The marked lines as a list reads them where a definition is offered to be run.

    The definition list and the browser's menu both refuse a marked definition and say why
    in the same words, so a user who meets it in one recognises it in the other.
    """
    return "\n".join(["This definition is not run while it is marked:"] + messages + [BROKEN_ADVICE])


def broken_by_rename_explanation(messages: list[str]) -> str:
    """The stored messages as one explanation: each unchanged, then what to do about it."""
    text = "; ".join(messages)
    end = "" if text.rstrip().endswith((".", "!", "?")) else "."
    return f"{text}{end} {BROKEN_ADVICE}"


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
            card_type = normalize_card_type_ref(card_action["card_type"])
            if card_type["note_type_id"] is not None:
                note_type_ids.add(card_type["note_type_id"])
    return note_type_ids, deck_ids


def _collection_path(col: Any) -> str:
    """Which collection a snapshot is of. The config is shared by every profile on the
    machine, and the ids in it are not: they are the issuing collection's own.

    The path, not the creation time (`crt`) that syncs with the collection: that was tried,
    and Anki rounds `crt` to the start of the day, so two profiles made the same day read
    as one collection. The known cost: a config synced to another desktop (`addon_config_sync`)
    reads as another collection's there, so a rename made on one desktop is not followed on
    the other. Its definitions keep resolving by id; only the field and card type names
    they spell are left as they were.
    """
    return str(getattr(col, "path", "") or "")


def _empty_snapshot(col: Any) -> dict:
    return {"collection": _collection_path(col), "note_types": {}, "decks": {}}


def _another_collection(snapshot: Any, col: Any) -> Optional[str]:
    """What tells this snapshot's collection from the one in hand, or None if they are one.

    A snapshot with no path was written before the stamp existed, or stamped with
    `collection_crt` by a version that identified collections by creation time; it is read
    as this collection's once, and the pass that reads it stamps it with the path. Guessing
    the other way would make every upgrade look like a profile switch.
    """
    if not isinstance(snapshot, dict):
        return None
    stored = snapshot.get("collection")
    if not stored or stored == _collection_path(col):
        return None
    return (
        f"the collection changed from the one at '{stored}'"
        f" to the one at '{_collection_path(col)}'"
    )


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
    collection_changed = _another_collection(snapshot, col)
    if collection_changed is not None:
        # A note type id, a deck id and a field id are the issuing collection's own, and
        # the config that holds them is shared by every profile. So nothing the snapshot
        # says is evidence about the collection in front of the pass now: no id in it is
        # gone, no name in it changed, and a name that differs under an id both happen to
        # have is not a rename but two collections. The snapshot is left out of this pass
        # entirely -- every reference re-binds by the one rule, the id first and the name
        # after it -- and replaced with this collection's names below. A rename is only
        # ever followed inside the collection it happened in.
        result.collection_changed = collection_changed
        snapshot = {}
    deleted = _deleted_objects(snapshot, col)
    for definition in definitions:
        changed |= _bind(definition, col, result, referenced, deleted)

    for (kind, object_id), holders in referenced.items():
        model = col.models.get(object_id) if kind == _TRIGGER_NOTE_TYPE else None
        if model is not None:
            _report_unfollowable(model, holders, result)

    # One name list for every search of every definition: nothing in the pass changes a
    # name the collection has, only names the definitions spell.
    collection_names = CollectionNames(col)
    for definition in definitions:
        _report_stale_terms(definition, col, collection_names, result)

    renames = _diff(snapshot, col, referenced, result)
    # Before anything is followed or marked: what each definition's marks were about, and
    # those marks brought up to date. Not on another collection's snapshot, whose ids say
    # nothing about the objects in front of the pass.
    known: dict[int, set[tuple[int, str, int]]] = {}
    if collection_changed is None:
        for definition in definitions:
            known[id(definition)] = _mark_keys(definition)
            changed |= _refresh_marks(definition, col)
    for note_type_id, changes in renames.items():
        model = col.models.get(note_type_id)
        if model is None:
            continue
        for definition in referenced.get((_TRIGGER_NOTE_TYPE, note_type_id)) or []:
            # A definition storing several trigger note types spells each name for all of
            # them, so a rename in one is never followed into it: the user decides, and the
            # mark is how they are asked. Stored, not resolved: see `_stored_trigger_count`.
            followed = _stored_trigger_count(definition) == 1
            recorded = _recorded_names(definition)
            if followed:
                changed |= _rewrite(definition, changes, result)
            changed |= _add_marks(
                definition,
                _marks_for(definition, recorded, model, changes, followed),
                known.get(id(definition), set()),
                result,
            )

    for definition in definitions:
        _report_broken(definition, result)

    refreshed_snapshot = build_name_snapshot(definitions, col)
    if refreshed_snapshot != (stored_snapshot or _empty_snapshot(col)):
        # The snapshot going out of date is itself a change worth a write: it is what the
        # next pass compares against, so a first run on a config that has none has to store
        # one or no rename after it could ever be seen. A snapshot that gains nothing but
        # its collection stamp differs too, which is how another collection's is replaced
        # and one stamped with `collection_crt` instead of the path restamped.
        config.data[SNAPSHOT_KEY] = refreshed_snapshot
        changed = True

    result.changed = changed
    if changed:
        # Not `_save_definitions`, which would take the snapshot stored just above a second
        # time. Nor does `effects` need recomputing: it follows stage types, the bindings
        # stages target, expression modes, card actions and calls (`flow_analysis`), and
        # the pass changes none of them -- it binds ids, rewrites names and adds or drops
        # marks.
        config.save()
    return result


def log_result(result: ReconcileResult) -> None:
    """Put the pass's report where the user looks: this operation's log file.

    A bind, a refreshed name and a followed rename are the pass doing its job, so they are
    written at info; a name that resolves to nothing, a deleted object and a mark are
    things only the user can fix, so they are warnings.
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
    for stale in result.broken:
        logger.warning(
            "Rename reconcile: '%s' is marked, and not run until the mark is dismissed: %s",
            stale.definition_name,
            stale.message,
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
    "BROKEN_ADVICE",
    "BROKEN_KEY",
    "KIND_CARD_TYPE",
    "KIND_DECK",
    "KIND_FIELD",
    "KIND_NOTE_TYPE",
    "SNAPSHOT_KEY",
    "ReconcileResult",
    "StaleName",
    "broken_by_rename_entries",
    "broken_by_rename_explanation",
    "broken_by_rename_tooltip",
    "broken_by_rename_messages",
    "build_name_snapshot",
    "definitions_hold_references",
    "log_result",
    "reconcile",
    "referenced_object_ids",
    "stale_terms_in_searches",
    "trigger_names_not_on_every_note_type",
    "unresolved_references",
]
