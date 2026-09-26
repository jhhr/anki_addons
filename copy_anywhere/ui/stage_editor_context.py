"""What one stage's editor is allowed to see.

Format 1 had two menu dictionaries on `EditState` -- one for before the query, one for
after -- because a definition had exactly one query and therefore exactly two places a
text edit could sit. A staged definition has as many scopes as it has stages, so the menus
cannot be precomputed globally any more. They come from the flow analyser instead: it
already records the bindings visible before every stage, and this module turns one of those
scopes into the menu, the validation dictionary and the Add Stage entries for that stage.

Each context is immutable and cheap to rebuild; the dialog throws them all away and asks for
new ones whenever the definition changes, which is what keeps the menus honest.
"""

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from anki.models import NotetypeDict

from ..logic.definition_schema import (
    CARD_PROPERTY_NAMES,
    CARD_REF,
    CopyDefinitionV2,
    LIST,
    NOTE_LIST,
    CARD_LIST,
    NOTE_REF,
    STAGE_EDIT_CARD,
    STAGE_FOR_EACH_CARD,
    STAGE_FOR_EACH_NOTE,
    SchemaProblem,
    Stage,
    T_CARD,
    T_NOTE,
    walk_stages,
)
from ..logic.flow_analysis import Binding
from ..shared.interpolate.interpolate_fields import (
    CARD_VALUES,
    NOTE_CARD_COUNT,
    NOTE_HAS_TAG,
    NOTE_ID,
    NOTE_TAGS,
    NOTE_TYPE_ID,
    intr_format,
)
from ..shared.ui.add_intersecting_model_field_options_to_dict import (
    add_intersecting_model_field_options_to_dict,
    get_intersecting_model_fields,
)
from ..shared.ui.add_model_options_to_dict import (
    add_model_options_to_dict,
    get_max_cloze_ords,
)
from ..shared.ui.interpolated_text_edit import make_validate_dict
from .stage_document import STAGE_TYPE_MENU_ORDER, StageDocument

#: The menu heading under which a note binding's own metadata is listed.
NOTE_VALUES_KEY = "Note values"
CARD_PROPERTIES_KEY = "Card properties"
CARD_VALUES_KEY = "Card values"
VARIABLES_MENU_KEY = "Variables"

#: Shown instead of a field list when a binding may hold any note type.
ALL_FIELDS_KEY = "All fields (not every note type has all of these)"

#: Note metadata, as (menu label, key) pairs. The key is what goes after the binding name.
NOTE_VALUE_ENTRIES: tuple[tuple[str, str], ...] = (
    ("Note Type ID (mid:)", NOTE_TYPE_ID),
    ("Note ID (nid:)", NOTE_ID),
    ("All tags", NOTE_TAGS),
    ("Has tag", NOTE_HAS_TAG),
    ("No. of different card types", NOTE_CARD_COUNT),
)

#: A function from a note binding's name to the note types it might hold, or None for
#: "any note type". Injected so the context can be built in a test without guessing what
#: the collection holds.
NoteTypesFor = Callable[[str], Optional[list[NotetypeDict]]]


@dataclass(frozen=True)
class StageEditorContext:
    """The immutable view one stage's editors get of their surroundings."""

    stage_guid: str
    scope: Mapping[str, Binding]
    options_dict: dict = field(default_factory=dict)
    validate_dict: dict = field(default_factory=dict)
    problems: tuple = ()
    warnings: tuple = ()
    #: True when a note binding in scope may hold more than one note type, so the field
    #: list is an intersection and a full list is offered separately.
    mixed_note_types: bool = False

    def bindings_of_kind(self, kind: str) -> list[str]:
        return sorted(name for name, binding in self.scope.items() if binding.type.kind == kind)

    @property
    def note_bindings(self) -> list[str]:
        return self.bindings_of_kind(NOTE_REF)

    @property
    def card_bindings(self) -> list[str]:
        return self.bindings_of_kind(CARD_REF)

    @property
    def list_bindings(self) -> list[str]:
        """Bindings a `store` stage may append to: declared lists, not note or card lists."""
        return sorted(
            name for name, binding in self.scope.items() if binding.type.kind == LIST
        )

    @property
    def note_list_bindings(self) -> list[str]:
        return self.bindings_of_kind(NOTE_LIST)

    @property
    def card_list_bindings(self) -> list[str]:
        return self.bindings_of_kind(CARD_LIST)

    @property
    def scalar_bindings(self) -> list[str]:
        return sorted(name for name, binding in self.scope.items() if binding.type.is_scalar)

    def available_stage_types(self) -> list[str]:
        """The Add Stage entries offered at this point.

        Only Edit Card is scope-dependent (§10): it names one card, and without a card
        binding in scope there is nothing it could name.
        """
        return [
            stage_type
            for stage_type in STAGE_TYPE_MENU_ORDER
            if stage_type != STAGE_EDIT_CARD or self.card_bindings
        ]


def note_values_dict(binding: str) -> dict:
    return {label: intr_format(f"{binding}.{key}") for label, key in NOTE_VALUE_ENTRIES}


def card_menu_dict(binding: str) -> dict:
    """A card binding's own menu: the facade properties, then the format-1 card values.

    Fields of the card's note are deliberately absent. A card loop binds the note too
    (§5.11), so the note's fields are reached through that name rather than through the
    card, and one reference form per value keeps the trace unambiguous.
    """
    return {
        CARD_PROPERTIES_KEY: {
            name: intr_format(f"{binding}.{name}") for name in sorted(CARD_PROPERTY_NAMES)
        },
        CARD_VALUES_KEY: {
            value: intr_format(f"{binding}.{value}") for value in CARD_VALUES
        },
    }


def note_menu_dict(
    binding: str,
    note_types: Optional[Sequence[NotetypeDict]],
    max_cloze_ords: Optional[Mapping[int, int]] = None,
) -> tuple[dict, bool]:
    """A note binding's menu, and whether its fields had to be an intersection.

    `note_types` of None means "this binding may hold any note type", which is the honest
    answer for a query result: nothing in the definition says what the query will match.

    `max_cloze_ords` is `get_max_cloze_ords()`, which a caller building several menus reads
    once for all of them: the cloze entries it sizes cost a pass over the cards table.
    """
    from aqt import mw

    if max_cloze_ords is None:
        max_cloze_ords = get_max_cloze_ords()
    menu: dict = {NOTE_VALUES_KEY: note_values_dict(binding)}
    prefix = f"{binding}."
    if note_types is None:
        assert mw is not None and mw.col is not None
        all_models: dict = {}
        for model in mw.col.models.all_names_and_ids():
            add_model_options_to_dict(
                model.name, model.id, all_models, prefix, max_cloze_ords=max_cloze_ords
            )
        menu[ALL_FIELDS_KEY] = all_models
        return menu, True
    if len(note_types) == 1:
        model = note_types[0]
        add_model_options_to_dict(
            model["name"], model["id"], menu, prefix, max_cloze_ords=max_cloze_ords
        )
        return menu, False
    if len(note_types) > 1:
        add_intersecting_model_field_options_to_dict(
            models=list(note_types),
            target_dict=menu,
            intersecting_fields=get_intersecting_model_fields(list(note_types)),
            prefix=prefix,
        )
        every: dict = {}
        for model in note_types:
            add_model_options_to_dict(
                model["name"], model["id"], every, prefix, max_cloze_ords=max_cloze_ords
            )
        menu[ALL_FIELDS_KEY] = every
        return menu, True
    # No note types selected at all: the definition does not run yet, so offer nothing
    # beyond the metadata rather than every field in the collection.
    return menu, False


def scope_options_dict(
    scope: Mapping[str, Binding],
    note_types_for: NoteTypesFor,
    max_cloze_ords: Optional[Mapping[int, int]] = None,
) -> tuple[dict, bool]:
    """The whole interpolation menu for one scope, and whether any note binding is mixed.

    List results are absent by design (§6): there is no interpolation that could produce a
    string from one, so offering it would only produce an analysis error later.
    """
    if max_cloze_ords is None:
        max_cloze_ords = get_max_cloze_ords()
    options: dict = {}
    mixed = False
    variables: dict = {}
    for name in sorted(scope):
        binding = scope[name]
        if binding.type.kind == NOTE_REF:
            options[name], binding_mixed = note_menu_dict(
                name, note_types_for(name), max_cloze_ords
            )
            mixed = mixed or binding_mixed
        elif binding.type.kind == CARD_REF:
            options[name] = card_menu_dict(name)
        elif binding.type.is_scalar:
            variables[name] = intr_format(name)
    if variables:
        options[VARIABLES_MENU_KEY] = variables
    return options, mixed


def selected_note_types(definition: CopyDefinitionV2) -> list[NotetypeDict]:
    """The note types a definition's trigger can be, from its trigger settings.

    By reference, so a note type the user renamed in Anki is still the one the definition
    means; a reference that resolves to nothing contributes no note type, as a stale name
    did before.
    """
    from aqt import mw

    from ..configuration import definition_note_type_refs
    from ..logic.object_refs import resolve_note_type

    assert mw is not None and mw.col is not None
    models = []
    for ref in definition_note_type_refs(definition):
        model = resolve_note_type(ref, mw.col)
        if model is not None:
            models.append(model)
    return models


def unresolved_reference_problems(definition: CopyDefinitionV2) -> list[str]:
    """What a definition names that this collection does not have, as save blockers.

    A reference resolves by id and then by name (`logic/object_refs.py`), so one that
    answers to neither names nothing at all: the definition triggers on no note, or holds
    a card action that reaches no card. Saving it would leave the user with a definition
    that looks complete and does nothing, so the editor refuses and says which name it is
    -- the same name the reconcile pass already logged, worded the same way.

    A field a definition on several note types spells for its trigger is the same kind of
    name: one of those note types lacking it makes the definition fail on its notes. The
    analyser checks a `{{trigger.X}}` against the fields any trigger note type has, which
    is all it can know from a list of names, so the field some of them lack is refused here
    (`rename_reconcile.trigger_fields_not_on_every_note_type`).
    """
    from aqt import mw

    from ..logic.rename_reconcile import (
        trigger_fields_not_on_every_note_type,
        unresolved_references,
    )

    if mw is None or mw.col is None:
        return []
    return [
        f"{stale.kind.capitalize()} '{stale.name}' no longer exists;"
        " pick another or remove it."
        for stale in unresolved_references(definition, mw.col)
    ] + [
        f"{problem}; use a field all of them have, or rename it in the others too."
        for problem in trigger_fields_not_on_every_note_type(definition, mw.col)
    ]


def known_fields_for(definition: CopyDefinitionV2) -> dict[str, set[str]]:
    """The field names each note binding's note can have, for the analyser.

    Only the trigger has an answer: it is the one binding whose note types the definition
    itself names. Everything else comes from a query, and a query's note types are not
    knowable without running it, so a reference off one of those is checked at run time.
    With no trigger note type chosen yet there is nothing to check against, and the entry
    is left out rather than being an empty list of fields.
    """
    fields = {
        field["name"] for model in selected_note_types(definition) for field in model["flds"]
    }
    return {"trigger": fields} if fields else {}


def make_note_types_for(definition: CopyDefinitionV2) -> NoteTypesFor:
    """Resolve note bindings to note types the way the editor should.

    `trigger` is exactly the note types the definition triggers on. Every other note
    binding comes from a query or a loop over one, and a query's note types are not knowable
    without running it, so those bindings report "any", which is what format 1's across-mode
    menu also offered.

    The trigger's note types are looked up per call rather than captured: the editor holds
    one of these for the life of a dialog, and the user changes the trigger note type from
    inside that dialog.
    """

    def note_types_for(binding: str) -> Optional[list[NotetypeDict]]:
        if binding == "trigger":
            return selected_note_types(definition)
        return None

    return note_types_for


def build_contexts(
    document: StageDocument, note_types_for: Optional[NoteTypesFor] = None
) -> dict[str, StageEditorContext]:
    """One context per stage in the document, keyed by stage guid.

    Stages the analyser never reached -- those after a structural problem, or inside a
    stage whose own shape is broken -- still get a context, with the root scope, so their
    editors render with something rather than nothing.
    """
    if note_types_for is None:
        note_types_for = make_note_types_for(document.definition)
    analysis = document.analysis
    # Read once for every scope's menus rather than per note type per binding per scope.
    max_cloze_ords = get_max_cloze_ords()
    cache: dict[frozenset, tuple[dict, dict, bool]] = {}
    contexts: dict[str, StageEditorContext] = {}

    def options_for(scope: Mapping[str, Binding]) -> tuple[dict, dict, bool]:
        # Built once per distinct scope, because each build walks every note type in the
        # collection. Keyed by what the scope holds rather than by the dict: the analyser
        # records a copy per stage, but a stage that declares nothing passes the very same
        # `Binding` objects on, so sibling stages after it compare equal here.
        key = frozenset(scope.items())
        if key not in cache:
            options, mixed = scope_options_dict(scope, note_types_for, max_cloze_ords)
            cache[key] = (options, make_validate_dict(options), mixed)
        return cache[key]

    root_scope = {"trigger": Binding("trigger", T_NOTE)}
    for stage in walk_stages(document.root_block()):
        guid = stage.get("guid", "")
        scope = analysis.scopes.get(guid, root_scope)
        options, validate, mixed = options_for(scope)
        contexts[guid] = StageEditorContext(
            stage_guid=guid,
            scope=scope,
            options_dict=options,
            validate_dict=validate,
            problems=tuple(
                problem for problem in analysis.problems if problem.stage_guid == guid
            ),
            warnings=tuple(
                problem for problem in analysis.warnings if problem.stage_guid == guid
            ),
            mixed_note_types=mixed,
        )
    return contexts


def root_context(
    document: StageDocument, note_types_for: Optional[NoteTypesFor] = None
) -> StageEditorContext:
    """The context for the definition's own top-level block: only the trigger is bound."""
    if note_types_for is None:
        note_types_for = make_note_types_for(document.definition)
    scope = {"trigger": Binding("trigger", T_NOTE)}
    options, mixed = scope_options_dict(scope, note_types_for)
    return StageEditorContext(
        stage_guid="",
        scope=scope,
        options_dict=options,
        validate_dict=make_validate_dict(options),
        problems=tuple(document.definition_problems()),
        mixed_note_types=mixed,
    )


def context_after(
    document: StageDocument,
    contexts: Mapping[str, StageEditorContext],
    parent_guid: Optional[str],
    body_key: Optional[str],
) -> StageEditorContext:
    """The context an Add Stage menu in this block should offer.

    The scope at the end of a block is the scope of its last stage plus whatever that stage
    declares, and the analyser does not record it separately. The last stage's own context
    is close enough for the one question the menu asks -- is a card binding in scope -- and
    a loop's context does not include its own item binding, which is why a card loop's body
    uses the body's own first stage instead.
    """
    block = document.block(parent_guid, body_key) or []
    for stage in reversed(block):
        context = contexts.get(stage.get("guid", ""))
        if context is not None:
            return context
    if parent_guid is not None:
        # An empty body: the loop's item binding is in scope inside it even though no stage
        # records that yet, so take the binding names from the parent stage itself.
        parent = document.stage(parent_guid)
        context = contexts.get(parent_guid)
        if parent is not None and context is not None:
            return _with_loop_bindings(context, parent)
    return root_context(document)


def _with_loop_bindings(context: StageEditorContext, parent: Stage) -> StageEditorContext:
    scope = dict(context.scope)
    stage_type = parent.get("type")
    if stage_type == STAGE_FOR_EACH_NOTE:
        name = parent.get("item_binding") or "note"
        scope[name] = Binding(name, T_NOTE, parent.get("guid"), True)
    elif stage_type == STAGE_FOR_EACH_CARD:
        name = parent.get("item_binding") or "card"
        scope[name] = Binding(name, T_CARD, parent.get("guid"), True)
        note_name = parent.get("note_binding") or "note"
        scope[note_name] = Binding(note_name, T_NOTE, parent.get("guid"), True)
    return StageEditorContext(
        stage_guid=context.stage_guid,
        scope=scope,
        options_dict=context.options_dict,
        validate_dict=context.validate_dict,
        mixed_note_types=context.mixed_note_types,
    )


def stage_problem_text(problems: Sequence[SchemaProblem]) -> str:
    return "<br/>".join(problem.message for problem in problems)
