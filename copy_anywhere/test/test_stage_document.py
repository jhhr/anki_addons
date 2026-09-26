"""The editable stage tree, without a widget in sight.

`StageDocument` is where every structural edit in the format-2 editor goes, so these cases
are about tree surgery and about the questions the dialog asks before it lets a user save.
None of it needs Qt or a collection.
"""

import pytest

from copy_anywhere.logic.definition_schema import (
    FORMAT_VERSION,
    STAGE_CALL_DEFINITION,
    STAGE_CONDITION,
    STAGE_EDIT_CARD,
    STAGE_EDIT_NOTE,
    STAGE_FOR_EACH_NOTE,
    STAGE_NOTE_QUERY,
    STAGE_VARIABLE,
    STAGE_WRITE_FILE,
    new_definition,
    validate_definition_structure,
    value_expression,
    walk_stages,
)
from copy_anywhere.ui.stage_document import (
    ALL_STAGE_TYPES,
    STAGE_TYPE_LABELS,
    StageDocument,
    default_stage,
    reguid_stage,
    stage_label,
)


def counting_guids(prefix="g"):
    counter = {"n": 0}

    def make():
        counter["n"] += 1
        return f"{prefix}{counter['n']}"

    return make


def document(*stages, **kwargs):
    definition = new_definition("def-guid", "A name", stages=list(stages))
    return StageDocument(definition, make_guid=counting_guids(), **kwargs)


# -- defaults -------------------------------------------------------------------------


@pytest.mark.parametrize("stage_type", ALL_STAGE_TYPES)
def test_every_stage_type_has_a_label_and_a_default(stage_type):
    assert stage_type in STAGE_TYPE_LABELS
    stage = default_stage(stage_type, "x")
    assert stage["type"] == stage_type
    assert stage["enabled"] is True


@pytest.mark.parametrize(
    "stage_type",
    [
        # The types whose blank default is already structurally complete: the rest need a
        # name or a binding from the user, and the analyser is what asks for it.
        STAGE_EDIT_NOTE,
        STAGE_CONDITION,
    ],
)
def test_a_default_stage_of_these_types_is_structurally_valid(stage_type):
    definition = new_definition("d", "n", stages=[default_stage(stage_type, "x")])
    assert validate_definition_structure(definition) == []


def test_a_default_stage_of_an_unknown_type_is_refused():
    with pytest.raises(ValueError):
        default_stage("teleport")


def test_reguid_replaces_every_guid_in_the_subtree():
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["body"] = [default_stage(STAGE_VARIABLE, "inner")]
    copy = reguid_stage(loop, counting_guids("new"))
    guids = {stage["guid"] for stage in walk_stages([copy])}
    assert guids == {"new1", "new2"}
    # and the original is untouched
    assert {stage["guid"] for stage in walk_stages([loop])} == {"loop", "inner"}


# -- navigation -----------------------------------------------------------------------


def test_the_document_copies_the_definition_it_was_given():
    definition = new_definition("d", "n", stages=[default_stage(STAGE_VARIABLE, "v")])
    doc = StageDocument(definition)
    doc.add_stage(STAGE_VARIABLE)
    assert len(definition["stages"]) == 1


def test_a_format_1_definition_is_refused():
    with pytest.raises(ValueError):
        StageDocument({"guid": "d", "copy_mode": "Within note"})


def test_location_finds_a_stage_inside_a_condition_branch():
    condition = default_stage(STAGE_CONDITION, "c")
    condition["else"] = [default_stage(STAGE_VARIABLE, "v")]
    doc = document(condition)
    assert doc.location("v") == ("c", "else", 0)
    assert doc.location("c") == (None, None, 0)
    assert doc.location("nope") is None


def test_path_of_names_every_containing_stage():
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["name"] = "Over the query"
    edit = default_stage(STAGE_EDIT_NOTE, "edit")
    loop["body"] = [edit]
    doc = document(loop)
    assert doc.path_of("edit") == "Over the query > Edit Note"


def test_stage_label_prefers_the_users_own_name():
    assert stage_label({"type": STAGE_VARIABLE}) == "Variable"
    assert stage_label({"type": STAGE_VARIABLE, "name": "  Meaning  "}) == "Meaning"


# -- mutation -------------------------------------------------------------------------


def test_add_stage_appends_to_the_block_it_names():
    condition = default_stage(STAGE_CONDITION, "c")
    doc = document(condition)
    added = doc.add_stage(STAGE_VARIABLE, parent_guid="c", body_key="then")
    assert added is not None
    assert doc.definition["stages"][0]["then"] == [added]


def test_add_stage_into_a_block_that_does_not_exist_does_nothing():
    doc = document(default_stage(STAGE_VARIABLE, "v"))
    assert doc.add_stage(STAGE_VARIABLE, parent_guid="v", body_key="body") is None
    assert len(doc.root_block()) == 1


def test_remove_stage_also_drops_the_exports_that_named_it():
    query = default_stage(STAGE_NOTE_QUERY, "q")
    query["result"] = "A1"
    doc = document(query)
    doc.set_exports([{"name": "A1", "stage_guid": "q"}])
    doc.remove_stage("q")
    assert doc.exports() == []


def test_removing_a_loop_drops_exports_naming_stages_inside_it():
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    inner = default_stage(STAGE_VARIABLE, "inner")
    loop["body"] = [inner]
    doc = document(loop)
    doc.set_exports([{"name": "X", "stage_guid": "inner"}])
    doc.remove_stage("loop")
    assert doc.exports() == []


def test_a_stage_moved_into_a_block_and_back_out_keeps_its_export():
    # Moving goes through `remove_stage`, which strips exports naming what it removed. That
    # is right for a delete -- the stage is gone -- but a move puts the same stage back, so
    # stripping there loses the export name the user chose, silently, for a round trip that
    # ends where it started.
    query = default_stage(STAGE_NOTE_QUERY, "q")
    query["result"] = "A1"
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    doc = document(query, loop)
    doc.set_exports([{"name": "shared", "stage_guid": "q", "result": "A1"}])

    assert doc.move_to("q", "loop", "body") is True
    assert doc.move_to("q", None, None) is True

    assert doc.exports() == [{"name": "shared", "stage_guid": "q", "result": "A1"}]


def test_a_stage_moved_at_the_top_level_keeps_its_export():
    query = default_stage(STAGE_NOTE_QUERY, "q")
    query["result"] = "A1"
    other = default_stage(STAGE_VARIABLE, "v")
    doc = document(query, other)
    doc.set_exports([{"name": "A1", "stage_guid": "q", "result": "A1"}])

    assert doc.move_to("q", None, None, 1) is True

    assert doc.exports() == [{"name": "A1", "stage_guid": "q", "result": "A1"}]


def test_duplicate_lands_directly_below_and_blanks_only_its_own_name():
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    inner = default_stage(STAGE_VARIABLE, "inner")
    inner["result"] = "M"
    loop["body"] = [inner]
    doc = document(default_stage(STAGE_VARIABLE, "before"), loop)
    copy = doc.duplicate_stage("loop")
    assert copy is not None
    assert [stage["guid"] for stage in doc.root_block()] == ["before", "loop", copy["guid"]]
    # the body keeps working: its own scope, so "M" does not collide
    assert copy["body"][0]["result"] == "M"
    assert copy["body"][0]["guid"] != "inner"


def test_duplicate_blanks_a_result_name_that_would_collide():
    variable = default_stage(STAGE_VARIABLE, "v")
    variable["result"] = "M"
    doc = document(variable)
    copy = doc.duplicate_stage("v")
    assert copy["result"] == ""


def test_duplicate_blanks_the_output_names_of_a_call():
    call = default_stage(STAGE_CALL_DEFINITION, "call")
    call["outputs"] = [{"export": "H1", "result": "H1_here"}]
    doc = document(call)
    copy = doc.duplicate_stage("call")
    assert copy["outputs"] == [{"export": "H1", "result": ""}]


def test_move_within_block_stops_at_the_block_edges():
    doc = document(
        default_stage(STAGE_VARIABLE, "a"),
        default_stage(STAGE_VARIABLE, "b"),
    )
    assert doc.move_within_block("a", -1) is False
    assert doc.move_within_block("a", 1) is True
    assert [stage["guid"] for stage in doc.root_block()] == ["b", "a"]
    assert doc.move_within_block("a", 1) is False


def test_a_stage_cannot_be_moved_inside_itself():
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    doc = document(loop)
    assert doc.move_to("loop", "loop", "body") is False
    assert doc.root_block()[0]["guid"] == "loop"


def test_move_targets_offers_every_other_block():
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    condition = default_stage(STAGE_CONDITION, "c")
    variable = default_stage(STAGE_VARIABLE, "v")
    doc = document(loop, condition, variable)
    targets = doc.move_targets("v")
    # already at the top level, so the top level is not offered again
    assert (None, None, "the definition's top level") not in targets
    assert ("loop", "body", "Loop Over Notes → Do") in targets
    assert ("c", "then", "Condition → Then") in targets
    assert ("c", "else", "Condition → Otherwise") in targets


def test_move_targets_of_a_nested_stage_offers_the_top_level_and_not_its_own_block():
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["body"] = [default_stage(STAGE_VARIABLE, "v")]
    doc = document(loop)
    targets = doc.move_targets("v")
    assert (None, None, "the definition's top level") in targets
    assert ("loop", "body", "Loop Over Notes → Do") not in targets


def test_move_to_relocates_the_whole_subtree():
    outer = default_stage(STAGE_FOR_EACH_NOTE, "outer")
    inner = default_stage(STAGE_FOR_EACH_NOTE, "inner")
    inner["body"] = [default_stage(STAGE_VARIABLE, "leaf")]
    doc = document(outer, inner)
    assert doc.move_to("inner", "outer", "body") is True
    assert doc.location("leaf") == ("inner", "body", 0)
    assert doc.location("inner") == ("outer", "body", 0)


def test_set_enabled_marks_the_stage():
    doc = document(default_stage(STAGE_VARIABLE, "v"))
    assert doc.set_enabled("v", False) is True
    assert doc.stage("v")["enabled"] is False
    assert doc.set_enabled("gone", False) is False


# -- analysis and saving --------------------------------------------------------------


def valid_variable(guid, name):
    stage = default_stage(STAGE_VARIABLE, guid)
    stage["result"] = name
    stage["value"] = value_expression(text="x")
    return stage


def test_analysis_is_cached_until_a_mutation():
    doc = document(valid_variable("v", "M"))
    first = doc.analysis
    assert doc.analysis is first
    doc.add_stage(STAGE_VARIABLE)
    assert doc.analysis is not first


def test_problems_are_reported_against_the_stage_that_caused_them():
    doc = document(default_stage(STAGE_VARIABLE, "v"))  # no result name
    assert doc.problems_for("v")
    assert doc.problems_for("other") == []


def test_save_is_blocked_while_a_stage_is_incomplete():
    doc = document(default_stage(STAGE_VARIABLE, "v"))
    blockers = doc.save_blockers()
    assert not doc.can_save()
    assert any("Variable" in blocker for blocker in blockers)


def test_save_is_blocked_without_a_definition_name():
    definition = new_definition("d", "", stages=[valid_variable("v", "M")])
    doc = StageDocument(definition)
    assert "The definition needs a name." in doc.save_blockers()


def test_a_complete_definition_can_be_saved():
    doc = document(valid_variable("v", "M"))
    assert doc.save_blockers() == []


def test_warnings_do_not_block_a_save():
    doc = document(valid_variable("v", "M"))
    doc.definition["triggers"]["note_types"] = ["One", "Two"]
    assert doc.can_save()


def test_a_migrated_definition_keeps_the_free_text_name_format_1_allowed():
    # The save runs the structural validator and the analyser together; both have to let
    # the old name through, or the user cannot open the definition to rename it.
    doc = document(valid_variable("v", "My Word"))
    doc.definition["migrated_from_format"] = 1
    assert doc.save_blockers() == []


def filling_a_field_and_flagging_the_card(guid="e"):
    """The definition format 1 ran happily, and the one the refusal trapped in the dialog.

    Structurally valid and targeting the trigger note, so nothing but the card action stands
    between it and a save -- which is the point: with the refusal in place there was no way
    out of the dialog except deleting the card action or a trigger the user never set.
    """
    edit = default_stage(STAGE_EDIT_NOTE, guid)
    edit["fields"] = [
        {
            "field": "Meaning",
            "value": value_expression(text="{{trigger.Word}}"),
            "write_if": "always",
        }
    ]
    edit["card_actions"] = [{"card_type_name": "CA Vocab: Card 1", "set_flag": 1}]
    return edit


def writing_a_file(guid="f"):
    """The stage that is forbidden while a note is being added.

    The file is on disk whether or not the user goes through with the add, so a definition
    with one in it cannot run as part of adding a note.
    """
    writing = default_stage(STAGE_WRITE_FILE, guid)
    writing["filename"] = value_expression(text="out.txt")
    writing["content"] = value_expression(text="{{trigger.Word}}")
    return writing


def flagging_the_card_and_writing_a_file(guid="e"):
    """Fill-and-flag with a file write beside it: impossible *and* forbidden at add time.

    The card action on the trigger's own cards cannot run -- the note has no cards yet --
    but it leaves nothing behind either. The file does, which is what keeps this definition
    out of the add hook's trigger-only pile.
    """
    return [filling_a_field_and_flagging_the_card(guid), writing_a_file()]


def test_an_add_note_trigger_accepts_a_stage_that_flags_the_triggers_card():
    # Refusing the save protected nothing -- the hooks and the commit read the stored flag,
    # never the editor -- and there is nothing left to refuse: an action on the cards of
    # the note being added cannot run, but it cannot survive a cancelled add either.
    doc = document(filling_a_field_and_flagging_the_card())
    doc.definition["triggers"]["on_add"] = True
    assert doc.add_note_compatible()
    assert doc.can_save()


def test_the_add_note_warning_says_the_card_action_on_the_named_stage_will_not_run():
    # A note being added has no cards, and nothing runs the action later, so the warning
    # names the stage and promises no later run. The definition is still one the add hook
    # may run: a skipped action leaves nothing behind, so there is nothing to forbid.
    doc = document(filling_a_field_and_flagging_the_card())
    doc.definition["triggers"]["on_add"] = True
    doc.stage("e")["name"] = "Fill and flag"
    (warning,) = doc.warnings()
    assert "card action on Fill and flag" in warning
    assert "will not run" in warning
    assert "once the note is saved" not in warning
    assert doc.add_note_compatible()


def test_the_add_note_warning_promises_no_later_run_for_an_unfocus_only_trigger_either():
    doc = document(filling_a_field_and_flagging_the_card())
    doc.definition["triggers"]["on_unfocus"] = {"edit_fields": [], "add_fields": ["Word"]}
    (warning,) = doc.warnings()
    assert "card action on Edit Note" in warning
    assert "will not run" in warning
    assert "once the note is saved" not in warning


def test_a_file_write_is_what_the_forbidden_warning_names():
    # Nothing here is impossible -- there is no card action -- so the only message is the
    # one about work that would outlive a cancelled add, and it names the stage.
    doc = document(writing_a_file())
    doc.definition["triggers"]["on_add"] = True
    (warning,) = doc.warnings()
    assert "Write File — writes a file" in warning
    assert not doc.add_note_compatible()


def test_the_impossible_and_the_forbidden_are_two_messages():
    # A definition can deserve both: the card action cannot run, and the file write is not
    # allowed to. Neither sentence is about the other, so neither is buried in the other.
    doc = document(*flagging_the_card_and_writing_a_file())
    doc.definition["triggers"]["on_add"] = True
    doc.stage("e")["name"] = "Fill and flag"
    impossible, forbidden = doc.warnings()
    assert "card action on Fill and flag" in impossible
    assert "Because of" not in impossible
    assert "Write File — writes a file" in forbidden
    assert "card action" not in forbidden


def test_the_add_note_warning_names_a_stage_editing_a_card():
    edit = default_stage(STAGE_EDIT_CARD, "e")
    edit["target"] = {"binding": "card"}
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    doc = document(loop, edit)
    doc.definition["triggers"]["on_add"] = True
    assert any("Edit Card — edits a card" in warning for warning in doc.warnings())


def test_the_add_note_warning_also_names_a_stage_editing_another_note():
    query = default_stage(STAGE_NOTE_QUERY, "q")
    query["result"] = "A1"
    query["query"] = value_expression(text="deck:x")
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "A1"}
    edit = default_stage(STAGE_EDIT_NOTE, "e")
    edit["target"] = {"binding": "note"}
    loop["body"] = [edit]
    doc = document(query, loop)
    doc.definition["triggers"]["on_unfocus"] = {"edit_fields": [], "add_fields": ["Word"]}
    assert doc.can_save()
    assert any("edits 'note', not the trigger" in warning for warning in doc.warnings())


def test_an_unfocus_only_add_trigger_is_told_what_to_turn_on():
    # Nothing to defer to: without `on_add` the definition is skipped in the Add dialog and
    # never runs on save either, so "it runs once the note is saved" would be a lie.
    doc = document(writing_a_file())
    doc.definition["triggers"]["on_unfocus"] = {"edit_fields": [], "add_fields": ["Word"]}
    (warning,) = doc.warnings()
    assert "skipped there" in warning
    assert "Run when adding a new note" in warning


def test_an_add_note_trigger_warns_about_nothing_when_the_definition_fits():
    edit = default_stage(STAGE_EDIT_NOTE, "e")
    edit["fields"] = [
        {"field": "Meaning", "value": value_expression(text="{{trigger.Word}}"), "write_if": "always"}
    ]
    doc = document(edit)
    doc.definition["triggers"]["on_add"] = True
    assert doc.warnings() == []


def test_a_definition_with_no_add_trigger_is_not_warned_about():
    # The incompatibility only matters where a note is being added; a definition that never
    # runs there is simply an ordinary definition with a card action.
    doc = document(filling_a_field_and_flagging_the_card())
    assert doc.warnings() == []


def searching(search, guid="c", **extra):
    """A search condition on the trigger, the only kind a note being added judges itself."""
    condition = default_stage(STAGE_CONDITION, guid)
    condition["predicate_kind"] = "note_query"
    condition["predicate"] = value_expression(text=search)
    condition["predicate_target"] = {"binding": "trigger"}
    condition.update(extra)
    return condition


def test_a_search_condition_the_note_being_added_cannot_answer_is_named():
    doc = document(searching("Word:neko is:due"))
    doc.definition["triggers"]["on_add"] = True
    (warning,) = doc.warnings()
    assert "search condition on Condition" in warning
    assert "'is:due'" in warning
    assert "not added yet" in warning


def test_the_same_warning_for_an_add_dialog_unfocus_trigger():
    doc = document(searching("prop:due>1"))
    doc.definition["triggers"]["on_unfocus"] = {"edit_fields": [], "add_fields": ["Word"]}
    (warning,) = doc.warnings()
    assert "'prop:due>1'" in warning


@pytest.mark.parametrize(
    "stage",
    [
        # Answerable from the note and the deck it goes into.
        searching('Word:neko tag:jp deck:"JP vocab" -note:x'),
        # What is searched is only known at run time.
        searching("{{trigger.Word}} is:due"),
        # Never checked outside a sync, and adding is not one.
        searching("is:due", only_on_sync=True),
        # A note from a query or loop is a saved one.
        searching("is:due", predicate_target={"binding": "note"}),
        # Refused everywhere, not just while adding; the run says so.
        searching("is:due)"),
    ],
    ids=["answerable", "interpolated", "only-on-sync", "other-note", "syntax-error"],
)
def test_a_search_condition_warns_only_when_as_written_it_cannot_be_answered(stage):
    doc = document(stage)
    doc.definition["triggers"]["on_add"] = True
    assert doc.warnings() == []


def test_a_search_condition_is_not_warned_about_without_an_add_trigger():
    doc = document(searching("is:due"))
    doc.definition["triggers"]["on_unfocus"] = {"edit_fields": ["Word"], "add_fields": []}
    assert doc.warnings() == []


def test_an_unfocus_only_definition_skipped_while_adding_is_not_warned_about_its_search():
    # It reaches beyond the note, so the Add dialog skips it and never judges the search;
    # the only thing to say is the existing "skipped there" warning.
    doc = document(searching("is:due"), writing_a_file())
    doc.definition["triggers"]["on_unfocus"] = {"edit_fields": [], "add_fields": ["Word"]}
    (warning,) = doc.warnings()
    assert "skipped there" in warning


def test_an_add_note_trigger_is_fine_for_a_trigger_only_definition():
    edit = default_stage(STAGE_EDIT_NOTE, "e")
    edit["fields"] = [
        {"field": "Meaning", "value": value_expression(text="{{trigger.Word}}"), "write_if": "always"}
    ]
    doc = document(edit)
    doc.definition["triggers"]["on_add"] = True
    assert doc.can_save()
    assert doc.add_note_compatible()


def test_a_card_action_on_the_triggers_own_cards_is_not_an_add_note_blocker():
    # It cannot run, which the impossible warning says; it also cannot outlive a cancelled
    # add, so it is not one of the reasons the definition stays out of the add hook's pile.
    doc = document(filling_a_field_and_flagging_the_card())
    assert doc.incompatible_stage_paths() == []


def test_a_card_action_on_another_note_is_still_an_add_note_blocker():
    query = default_stage(STAGE_NOTE_QUERY, "q")
    query["result"] = "A1"
    query["query"] = value_expression(text="deck:x")
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "A1"}
    edit = default_stage(STAGE_EDIT_NOTE, "e")
    edit["target"] = {"binding": "note"}
    edit["card_actions"] = [{"card_type_name": "CA Vocab: Card 1", "set_flag": 1}]
    loop["body"] = [edit]
    doc = document(query, loop)
    assert any(path.endswith("— has card actions") for path in doc.incompatible_stage_paths())


def test_a_file_write_is_listed_as_an_add_note_blocker():
    doc = document(writing_a_file())
    assert doc.incompatible_stage_paths() == ["Write File — writes a file"]


def test_a_disabled_stage_is_not_listed_as_an_add_note_blocker():
    edit = default_stage(STAGE_EDIT_CARD, "e")
    edit["enabled"] = False
    doc = document(edit)
    assert doc.incompatible_stage_paths() == []


def calling(effects):
    callee = new_definition("callee", "The callee")
    callee["effects"] = effects
    call = default_stage(STAGE_CALL_DEFINITION, "call")
    call["definition_guid"] = "callee"
    return StageDocument(
        new_definition("d", "n", stages=[call]), lookup={"callee": callee}.get
    )


@pytest.mark.parametrize(
    "effects",
    [
        {"edits_other_notes": True},
        {"edits_other_cards": True},
        {"writes_files": True},
    ],
)
def test_incompatible_paths_follow_a_call_into_the_callee(effects):
    doc = calling(effects)
    assert doc.incompatible_stage_paths() == [
        "Call Definition — calls 'The callee', which reaches beyond the trigger note"
    ]


def test_a_callee_that_only_flags_the_triggers_card_is_not_listed():
    # The callee is handed this definition's own trigger, whose cards do not exist yet:
    # the action is skipped, not refused, so the call is not a reason to hold the
    # definition back.
    doc = calling({"edits_trigger_cards": True, "edits_cards": True})
    assert doc.incompatible_stage_paths() == []


# -- exports --------------------------------------------------------------------------


def test_only_enabled_root_results_can_be_exported():
    root_query = default_stage(STAGE_NOTE_QUERY, "q")
    root_query["result"] = "A1"
    disabled = valid_variable("off", "Off")
    disabled["enabled"] = False
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["body"] = [valid_variable("inner", "Inner")]
    doc = document(root_query, disabled, loop)
    assert doc.exportable_stages() == [("q", "A1")]


def test_a_calls_outputs_are_exportable():
    call = default_stage(STAGE_CALL_DEFINITION, "call")
    call["outputs"] = [{"export": "H1", "result": "H1_here"}]
    doc = document(call)
    assert doc.exportable_stages() == [("call", "H1_here")]


# -- serialization --------------------------------------------------------------------


def test_to_definition_writes_the_derived_effects():
    edit = default_stage(STAGE_EDIT_CARD, "e")
    edit["target"] = {"binding": "card"}
    doc = document(edit)
    saved = doc.to_definition()
    assert saved["format_version"] == FORMAT_VERSION
    assert saved["effects"]["edits_cards"] is True
    assert saved["effects"]["add_note_compatible"] is False


def test_to_definition_keeps_the_visual_order():
    doc = document(
        valid_variable("a", "A"),
        valid_variable("b", "B"),
    )
    doc.move_within_block("b", -1)
    assert [stage["guid"] for stage in doc.to_definition()["stages"]] == ["b", "a"]


def test_to_definition_does_not_hand_out_the_documents_own_stages():
    doc = document(valid_variable("a", "A"))
    saved = doc.to_definition()
    saved["stages"][0]["result"] = "changed"
    assert doc.stage("a")["result"] == "A"
