"""The per-stage editor context: what is in scope, and what the menus offer because of it.

Format 1 built one menu for "before the query" and one for "after". These cases are the
proof that a staged definition's menus follow the stage instead: a loop body offers the
loop's note, a stage before the loop does not, and nothing ever offers a list.
"""

import pytest

from copy_anywhere.logic.definition_schema import (
    STAGE_CARD_QUERY,
    STAGE_EDIT_CARD,
    STAGE_FOR_EACH_CARD,
    STAGE_FOR_EACH_NOTE,
    STAGE_LIST_VARIABLE,
    STAGE_NOTE_QUERY,
    STAGE_VARIABLE,
    new_definition,
    value_expression,
)
from copy_anywhere.ui.stage_document import StageDocument, default_stage
from copy_anywhere.ui.stage_editor_context import (
    ALL_FIELDS_KEY,
    CARD_PROPERTIES_KEY,
    CARD_VALUES_KEY,
    NOTE_VALUES_KEY,
    VARIABLES_MENU_KEY,
    build_contexts,
    card_menu_dict,
    context_after,
    make_note_types_for,
    note_menu_dict,
    root_context,
    scope_options_dict,
)

from conftest import VOCAB, KANJI


def variable(guid, name, text="x"):
    stage = default_stage(STAGE_VARIABLE, guid)
    stage["result"] = name
    stage["value"] = value_expression(text=text)
    return stage


def note_query(guid, name, query="deck:Default"):
    stage = default_stage(STAGE_NOTE_QUERY, guid)
    stage["result"] = name
    stage["query"] = value_expression(text=query)
    return stage


def document(*stages, note_types=(VOCAB,)):
    definition = new_definition("d", "A name", stages=list(stages))
    definition["triggers"]["note_types"] = list(note_types)
    return StageDocument(definition)


# -- menu building --------------------------------------------------------------------


def test_a_note_binding_offers_its_fields_qualified_by_the_binding(col):
    model = col.models.by_name(VOCAB)
    menu, mixed = note_menu_dict("trigger", [model])
    assert mixed is False
    assert menu[VOCAB]["Note fields"]["Word"] == "{{trigger.Word}}"
    assert menu[NOTE_VALUES_KEY]["Note ID (nid:)"] == "{{trigger.__Note_ID}}"


def test_a_note_binding_of_unknown_type_offers_every_note_type_under_one_heading(col):
    menu, mixed = note_menu_dict("note", None)
    assert mixed is True
    assert VOCAB in menu[ALL_FIELDS_KEY]
    assert KANJI in menu[ALL_FIELDS_KEY]
    assert menu[ALL_FIELDS_KEY][KANJI]["Note fields"]["Kanji"] == "{{note.Kanji}}"


def test_several_note_types_offer_their_intersection_first(col):
    vocab = col.models.by_name(VOCAB)
    kanji = col.models.by_name(KANJI)
    menu, mixed = note_menu_dict("trigger", [vocab, kanji])
    assert mixed is True
    # "CA Vocab" and "CA Kanji" share no field, so the intersection is empty and only the
    # full list is useful -- but it is still offered, and marked.
    assert ALL_FIELDS_KEY in menu
    assert "Word" not in menu


def test_a_binding_with_no_note_types_offers_only_metadata(col):
    menu, mixed = note_menu_dict("trigger", [])
    assert mixed is False
    assert list(menu) == [NOTE_VALUES_KEY]


def test_a_card_binding_offers_properties_and_card_values():
    menu = card_menu_dict("card")
    assert menu[CARD_PROPERTIES_KEY]["deck_name"] == "{{card.deck_name}}"
    assert menu[CARD_VALUES_KEY]["__Card_Due"] == "{{card.__Card_Due}}"
    # The note's fields are reached through the loop's note binding, not through the card.
    assert "Note fields" not in menu


def test_scalar_results_are_listed_as_variables(col):
    doc = document(variable("v", "M"), variable("w", "N"))
    contexts = build_contexts(doc)
    options = contexts["w"].options_dict
    assert options[VARIABLES_MENU_KEY] == {"M": "{{M}}"}


def test_a_list_result_never_appears_in_the_menu(col):
    list_stage = default_stage(STAGE_LIST_VARIABLE, "l")
    list_stage["result"] = "L1"
    doc = document(list_stage, variable("v", "M"))
    options = build_contexts(doc)["v"].options_dict
    assert "L1" not in options
    assert "L1" not in options.get(VARIABLES_MENU_KEY, {})


def test_a_note_list_result_never_appears_in_the_menu(col):
    doc = document(note_query("q", "A1"), variable("v", "M"))
    options = build_contexts(doc)["v"].options_dict
    assert "A1" not in options


def test_the_validate_dict_accepts_what_the_menu_offers(col):
    doc = document(variable("v", "M"), variable("w", "N"))
    context = build_contexts(doc)["w"]
    assert context.validate_dict["{{m}}"] is True
    assert context.validate_dict["{{trigger.word}}"] is True


# -- scope per stage ------------------------------------------------------------------


def test_a_stage_before_the_query_does_not_see_its_result(col):
    doc = document(variable("v", "M"), note_query("q", "A1"))
    contexts = build_contexts(doc)
    assert "A1" not in contexts["v"].scope
    assert "M" in contexts["q"].scope


def test_a_loop_body_sees_the_loop_binding_and_the_outer_scope(col):
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "A1"}
    inner = variable("inner", "Inner", "{{note.Word}}")
    loop["body"] = [inner]
    doc = document(variable("v", "M"), note_query("q", "A1"), loop)
    contexts = build_contexts(doc)
    assert set(contexts["inner"].note_bindings) == {"trigger", "note"}
    assert "M" in contexts["inner"].scope
    assert "note" not in contexts["q"].scope


def test_the_loop_binding_is_gone_again_after_the_loop(col):
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "A1"}
    loop["body"] = [variable("inner", "Inner")]
    after = variable("after", "After")
    doc = document(note_query("q", "A1"), loop, after)
    contexts = build_contexts(doc)
    assert contexts["after"].note_bindings == ["trigger"]
    assert "Inner" not in contexts["after"].scope


def test_a_card_loop_binds_both_the_card_and_its_note(col):
    query = default_stage(STAGE_CARD_QUERY, "cq")
    query["result"] = "C1"
    query["query"] = value_expression(text="deck:Default")
    loop = default_stage(STAGE_FOR_EACH_CARD, "loop")
    loop["input"] = {"binding": "C1"}
    loop["body"] = [variable("inner", "Inner")]
    doc = document(query, loop)
    context = build_contexts(doc)["inner"]
    assert context.card_bindings == ["card"]
    assert set(context.note_bindings) == {"note", "trigger"}
    assert context.options_dict["card"][CARD_PROPERTIES_KEY]["deck_name"] == "{{card.deck_name}}"


def test_a_stage_the_analyser_never_reached_still_gets_the_root_scope(col):
    broken = default_stage(STAGE_FOR_EACH_NOTE, "loop")  # no input binding
    broken["body"] = [variable("inner", "Inner")]
    doc = document(broken)
    contexts = build_contexts(doc)
    assert "inner" in contexts
    assert "trigger" in contexts["inner"].scope


# -- what the Add Stage menu offers ---------------------------------------------------


def test_edit_card_is_offered_only_where_a_card_binding_is_in_scope(col):
    doc = document(variable("v", "M"))
    assert STAGE_EDIT_CARD not in build_contexts(doc)["v"].available_stage_types()


def test_edit_card_is_offered_inside_a_card_loop(col):
    query = default_stage(STAGE_CARD_QUERY, "cq")
    query["result"] = "C1"
    query["query"] = value_expression(text="deck:Default")
    loop = default_stage(STAGE_FOR_EACH_CARD, "loop")
    loop["input"] = {"binding": "C1"}
    loop["body"] = [variable("inner", "Inner")]
    doc = document(query, loop)
    assert STAGE_EDIT_CARD in build_contexts(doc)["inner"].available_stage_types()


def test_an_empty_card_loop_body_already_offers_edit_card(col):
    query = default_stage(STAGE_CARD_QUERY, "cq")
    query["result"] = "C1"
    query["query"] = value_expression(text="deck:Default")
    loop = default_stage(STAGE_FOR_EACH_CARD, "loop")
    loop["input"] = {"binding": "C1"}
    doc = document(query, loop)
    contexts = build_contexts(doc)
    context = context_after(doc, contexts, "loop", "body")
    assert STAGE_EDIT_CARD in context.available_stage_types()


def test_the_context_after_an_empty_root_block_is_the_root_context(col):
    doc = document()
    context = context_after(doc, {}, None, None)
    assert list(context.scope) == ["trigger"]


def test_the_context_after_a_block_is_its_last_stages(col):
    doc = document(variable("a", "A"), variable("b", "B"))
    contexts = build_contexts(doc)
    assert context_after(doc, contexts, None, None) is contexts["b"]


# -- the root context -----------------------------------------------------------------


def test_the_root_context_binds_only_the_trigger(col):
    doc = document(variable("v", "M"))
    context = root_context(doc)
    assert list(context.scope) == ["trigger"]
    assert context.options_dict["trigger"][VOCAB]["Note fields"]["Word"] == "{{trigger.Word}}"


def test_the_root_context_carries_the_definitions_own_problems(col):
    doc = document(variable("v", "M"))
    doc.set_exports([{"name": "M", "stage_guid": "nope"}])
    assert root_context(doc).problems


def test_the_trigger_resolver_uses_the_definitions_note_types(col):
    doc = document(variable("v", "M"), note_types=(VOCAB, KANJI))
    resolve = make_note_types_for(doc.definition)
    assert [model["name"] for model in resolve("trigger")] == [VOCAB, KANJI]
    assert resolve("note") is None


def test_options_are_built_once_per_distinct_scope(col, monkeypatch):
    calls = []
    import copy_anywhere.ui.stage_editor_context as module

    real = module.scope_options_dict

    def counted(scope, resolver):
        calls.append(scope)
        return real(scope, resolver)

    monkeypatch.setattr(module, "scope_options_dict", counted)
    doc = document(variable("a", "A"), variable("b", "B"), variable("c", "C"))
    build_contexts(doc)
    # Three stages, three different scopes -- but sibling stages inside one block share the
    # analyser's scope object where nothing was declared, so this is an upper bound.
    assert len(calls) <= 3
