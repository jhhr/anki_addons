"""The format-2 editor, driven headlessly.

These are the cases §12.2 asks a UI suite for: reordering a stage, the scope menus keeping
up with the stage list, a reference that broke staying visible, and save blocking. They run
on Qt's offscreen platform plugin -- no display, no pixels read -- because everything here
is ordinary logic that happens to live in widgets.
"""

import pytest

from copy_anywhere.configuration import (
    definition_deck_names,
    definition_note_type_names,
    definition_runs_on_add,
    definition_unfocus_fields,
)
from copy_anywhere.logic.definition_schema import (
    STAGE_CARD_QUERY,
    STAGE_EDIT_CARD,
    STAGE_EDIT_NOTE,
    STAGE_FOR_EACH_CARD,
    STAGE_FOR_EACH_NOTE,
    STAGE_NOTE_QUERY,
    STAGE_VARIABLE,
    new_definition,
    value_expression,
)
from copy_anywhere.ui.stage_document import StageDocument, default_stage
from copy_anywhere.ui.stage_editor_context import build_contexts, make_note_types_for
from copy_anywhere.ui.stage_editors import (
    StageEditorEnvironment,
    make_stage_editor,
    tags_to_list,
    tags_to_text,
)
from copy_anywhere.ui.stage_list import StageTreeWidget
from copy_anywhere.ui.stage_triggers_editor import selected_names

from conftest import KANJI, VOCAB


@pytest.fixture
def dialog(col, qapp):
    """A fresh staged-definition dialog, with the suite's note type already chosen."""
    from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

    definition = new_definition("d", "A definition")
    definition["triggers"]["note_types"] = [VOCAB]
    built = EditStagedDefinitionDialog(None, definition)
    yield built
    built.deleteLater()


def tree_for(col, *stages, note_types=(VOCAB,), definitions=()):
    definition = new_definition("d", "A definition", stages=list(stages))
    definition["triggers"]["note_types"] = list(note_types)
    document = StageDocument(definition)
    environment = StageEditorEnvironment(
        make_note_types_for(document.definition), [definition, *definitions], "d"
    )
    return StageTreeWidget(None, document, environment)


def variable(guid, name, text="x"):
    stage = default_stage(STAGE_VARIABLE, guid)
    stage["result"] = name
    stage["value"] = value_expression(text=text)
    return stage


def note_query(guid, name):
    stage = default_stage(STAGE_NOTE_QUERY, guid)
    stage["result"] = name
    stage["query"] = value_expression(text="deck:Default")
    return stage


# -- building -------------------------------------------------------------------------


def test_every_stage_type_has_an_editor_that_builds(col, qapp, widget_parent):
    from copy_anywhere.logic.definition_schema import ALL_STAGE_TYPES

    callee = new_definition("callee", "Callee")
    callee["exports"] = [{"name": "H1", "stage_guid": "x"}]
    for stage_type in ALL_STAGE_TYPES:
        stage = default_stage(stage_type, "s")
        definition = new_definition("d", "n", stages=[stage])
        definition["triggers"]["note_types"] = [VOCAB]
        document = StageDocument(definition)
        environment = StageEditorEnvironment(
            make_note_types_for(document.definition), [callee, definition], "d"
        )
        editor = make_stage_editor(
            widget_parent, document.stage("s"), build_contexts(document)["s"], environment
        )
        assert editor is not None, stage_type
        editor.apply()


def test_an_unknown_stage_type_renders_a_row_instead_of_crashing(col, qapp):
    tree = tree_for(col, {"guid": "s", "type": "teleport", "enabled": True})
    assert tree.rows["s"].editor is None
    assert tree.document.problems_for("s")


# -- reorder --------------------------------------------------------------------------


def test_moving_a_stage_reorders_the_definition_and_the_rows(col, qapp):
    tree = tree_for(col, variable("a", "A"), variable("b", "B"))
    tree.move_stage("b", -1)
    assert [stage["guid"] for stage in tree.document.root_block()] == ["b", "a"]
    assert [row.guid for row in tree.root_block.rows] == ["b", "a"]


def test_a_stage_moved_into_a_loop_renders_inside_it(col, qapp):
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "A1"}
    tree = tree_for(col, note_query("q", "A1"), loop, variable("v", "M"))
    tree.move_stage_into("v", "loop", "body")
    assert tree.document.location("v") == ("loop", "body", 0)
    assert [row.guid for row in tree.root_block.rows] == ["q", "loop"]
    assert [row.guid for row in tree.rows["loop"].child_blocks[0].rows] == ["v"]


def test_deleting_a_stage_removes_its_row(col, qapp):
    tree = tree_for(col, variable("a", "A"), variable("b", "B"))
    tree.remove_stage("a")
    assert "a" not in tree.rows
    assert [row.guid for row in tree.root_block.rows] == ["b"]


def test_a_new_stage_is_added_expanded(col, qapp):
    tree = tree_for(col)
    tree.add_stage(STAGE_VARIABLE, None, None)
    guid = tree.document.root_block()[0]["guid"]
    # `isVisible` is False for everything under a window that was never shown, so the
    # question an offscreen test can ask is whether the row hid its own body.
    assert not tree.rows[guid].body.isHidden()
    tree.rows[guid]._toggle()
    assert tree.rows[guid].body.isHidden()


def test_turning_a_stage_off_keeps_its_row(col, qapp):
    tree = tree_for(col, variable("a", "A"))
    tree.set_enabled("a", False)
    assert "a" in tree.rows
    assert tree.document.stage("a")["enabled"] is False


def test_an_edit_survives_a_reorder(col, qapp):
    tree = tree_for(col, variable("a", "A"), variable("b", "B"))
    tree.rows["a"].editor.value.text_layout.set_text("kept")
    tree.move_stage("a", 1)
    assert tree.document.stage("a")["value"]["text"] == "kept"


# -- scope menus ----------------------------------------------------------------------


def test_a_later_stages_menu_gains_an_earlier_stages_result(col, qapp):
    tree = tree_for(col, variable("a", "A"), variable("b", "B"))
    options = tree.rows["b"].editor.value.text_layout.options_dict
    assert options["Variables"] == {"A": "{{A}}"}


def test_renaming_a_result_updates_the_menu_of_the_stage_below(col, qapp):
    tree = tree_for(col, variable("a", "A"), variable("b", "B"))
    tree.rows["a"].editor.result.setText("Renamed")
    tree.contents_changed()
    options = tree.rows["b"].editor.value.text_layout.options_dict
    assert options["Variables"] == {"Renamed": "{{Renamed}}"}


def test_a_loop_body_menu_offers_the_loop_note(col, qapp):
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "A1"}
    loop["body"] = [variable("inner", "Inner")]
    tree = tree_for(col, note_query("q", "A1"), loop)
    options = tree.rows["inner"].editor.value.text_layout.options_dict
    assert "note" in options
    assert "note" not in tree.rows["q"].editor.query.text_layout.options_dict


def test_changing_the_trigger_note_type_changes_the_field_options(dialog):
    dialog.stage_tree.add_stage(STAGE_EDIT_NOTE, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    editor = dialog.stage_tree.rows[guid].editor
    editor._on_add_field()
    assert editor.field_rows[0].field.findText("Word") >= 0

    dialog.triggers_editor.note_types_box.setCurrentText(f'"{KANJI}"')
    dialog.refresh_status()
    dialog.stage_tree.rebuild()
    editor = dialog.stage_tree.rows[guid].editor
    editor._on_add_field()
    assert editor.field_rows[0].field.findText("Kanji") >= 0
    assert editor.field_rows[0].field.findText("Word") < 0


def test_edit_card_is_only_offered_where_a_card_is_in_scope(col, qapp):
    query = default_stage(STAGE_CARD_QUERY, "cq")
    query["result"] = "C1"
    query["query"] = value_expression(text="deck:Default")
    loop = default_stage(STAGE_FOR_EACH_CARD, "loop")
    loop["input"] = {"binding": "C1"}
    loop["body"] = [variable("inner", "Inner")]
    tree = tree_for(col, query, loop, variable("after", "After"))
    contexts = tree.contexts
    assert STAGE_EDIT_CARD in contexts["inner"].available_stage_types()
    assert STAGE_EDIT_CARD not in contexts["after"].available_stage_types()


# -- references that broke ------------------------------------------------------------


def test_a_binding_that_is_no_longer_in_scope_stays_selected_and_is_marked(col, qapp):
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "A1"}
    tree = tree_for(col, loop)  # the query that made A1 is not there
    assert tree.rows["loop"].editor.input.currentText() == "A1"
    assert "A1" in tree.rows["loop"].problem_label.text()


def test_deleting_a_producer_marks_the_stage_that_used_it(col, qapp):
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "A1"}
    tree = tree_for(col, note_query("q", "A1"), loop)
    assert tree.rows["loop"].problem_label.text() == ""
    tree.remove_stage("q")
    assert "A1" in tree.rows["loop"].problem_label.text()
    assert tree.document.stage("loop")["input"] == {"binding": "A1"}


def test_a_stage_summary_says_what_it_does(col, qapp):
    tree = tree_for(col, variable("a", "A", "{{trigger.Word}}"))
    assert tree.rows["a"].summary.text() == "A = {{trigger.Word}}"


# -- saving ---------------------------------------------------------------------------


def test_save_is_off_until_the_definition_is_complete(dialog):
    dialog.stage_tree.add_stage(STAGE_VARIABLE, None, None)
    dialog.refresh_status()
    assert not dialog.ok_button.isEnabled()
    guid = dialog.document.root_block()[0]["guid"]
    dialog.stage_tree.rows[guid].editor.result.setText("M")
    dialog.refresh_status()
    assert dialog.ok_button.isEnabled()


def test_save_is_off_without_a_name(dialog):
    dialog.triggers_editor.name_edit.setText("")
    dialog.refresh_status()
    assert not dialog.ok_button.isEnabled()
    assert "needs a name" in dialog.status_label.text()


def test_the_status_says_why_a_definition_cannot_be_saved(dialog):
    dialog.stage_tree.add_stage(STAGE_FOR_EACH_NOTE, None, None)
    dialog.refresh_status()
    assert "Cannot be saved yet" in dialog.status_label.text()
    assert "Loop Over Notes" in dialog.status_label.text()


def test_an_add_note_trigger_blocks_a_save_on_a_card_editing_definition(dialog):
    stage = default_stage(STAGE_EDIT_CARD, "e")
    stage["target"] = {"binding": "card"}
    dialog.document.root_block().append(stage)
    dialog.triggers_editor.on_add.setChecked(True)
    dialog.stage_tree.rebuild()
    dialog.refresh_status()
    assert not dialog.ok_button.isEnabled()
    assert "note is being added: <b>no</b>" in dialog.status_label.text()


def test_the_status_reports_add_note_compatibility(dialog):
    dialog.refresh_status()
    assert "note is being added: <b>yes</b>" in dialog.status_label.text()


def test_a_call_cycle_blocks_a_save(col, qapp):
    from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

    call_back = default_stage("call_definition", "cb")
    call_back["definition_guid"] = "d"
    other = new_definition("other", "The other one", stages=[call_back])
    call = default_stage("call_definition", "c")
    call["definition_guid"] = "other"
    mine = new_definition("d", "Mine", stages=[call])
    mine["triggers"]["note_types"] = [VOCAB]
    built = EditStagedDefinitionDialog(None, mine, [other])
    built.refresh_status()
    assert not built.ok_button.isEnabled()
    assert "call each other in a circle" in built.status_label.text()
    built.deleteLater()


# -- what gets saved ------------------------------------------------------------------


def test_the_dialog_round_trips_an_edit_note_stage(dialog):
    dialog.stage_tree.add_stage(STAGE_EDIT_NOTE, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    editor = dialog.stage_tree.rows[guid].editor
    editor._on_add_field()
    editor.field_rows[0].field.setCurrentText("Meaning")
    editor.field_rows[0].value.text_layout.set_text("{{trigger.Word}}")
    saved = dialog.get_copy_definition()
    stage = saved["stages"][0]
    assert stage["target"] == {"binding": "trigger"}
    assert stage["fields"][0]["field"] == "Meaning"
    assert stage["fields"][0]["value"]["text"] == "{{trigger.Word}}"
    assert saved["effects"]["edits_trigger"] is True
    assert saved["effects"]["add_note_compatible"] is True


def test_the_dialog_writes_the_trigger_settings_as_arrays(dialog):
    dialog.triggers_editor.on_review.setChecked(True)
    dialog.triggers_editor.include_subdecks.setChecked(True)
    saved = dialog.get_copy_definition()
    assert saved["triggers"]["note_types"] == [VOCAB]
    assert saved["triggers"]["on_review"] is True
    assert saved["triggers"]["include_subdecks"] is True
    assert saved["triggers"]["deck_names"] == []


def test_a_query_stage_round_trips_its_selection(dialog):
    dialog.stage_tree.add_stage(STAGE_NOTE_QUERY, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    editor = dialog.stage_tree.rows[guid].editor
    editor.result.setText("A1")
    editor.query.text_layout.set_text("deck:Default")
    editor.strategy.setCurrentIndex(editor.strategy.findData("first"))
    editor.count.setValue(3)
    editor.if_empty.setCurrentIndex(editor.if_empty.findData("error"))
    saved = dialog.get_copy_definition()["stages"][0]
    assert saved["selection"]["strategy"] == "first"
    assert saved["selection"]["count"] == 3
    assert saved["if_empty"] == "error"


def test_all_selection_drops_the_count(dialog):
    dialog.stage_tree.add_stage(STAGE_NOTE_QUERY, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    editor = dialog.stage_tree.rows[guid].editor
    editor.count.setValue(5)
    editor.strategy.setCurrentIndex(editor.strategy.findData("all"))
    assert editor.count.isHidden()
    assert dialog.get_copy_definition()["stages"][0]["selection"]["count"] is None


def test_a_code_mode_expression_is_saved_as_code(dialog):
    dialog.stage_tree.add_stage(STAGE_VARIABLE, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    editor = dialog.stage_tree.rows[guid].editor
    editor.result.setText("M")
    editor.value.use_code_toggle.setChecked(True)
    editor.value.code_layout.set_text("return note['Word']")
    saved = dialog.get_copy_definition()["stages"][0]
    assert saved["value"]["mode"] == "code"
    assert saved["value"]["code"] == "return note['Word']"


def test_switching_to_code_seeds_it_from_the_text(col, qapp):
    tree = tree_for(col, variable("a", "A", "hello"))
    editor = tree.rows["a"].editor.value
    editor.use_code_toggle.setChecked(True)
    assert editor.apply()["code"] == "value = 'hello'\nreturn value"


def test_an_edit_card_stage_saves_actions_without_a_card_type(col, qapp):
    query = default_stage(STAGE_CARD_QUERY, "cq")
    query["result"] = "C1"
    query["query"] = value_expression(text="deck:Default")
    loop = default_stage(STAGE_FOR_EACH_CARD, "loop")
    loop["input"] = {"binding": "C1"}
    edit = default_stage(STAGE_EDIT_CARD, "e")
    edit["target"] = {"binding": "card"}
    loop["body"] = [edit]
    tree = tree_for(col, query, loop)
    actions = tree.rows["e"].editor.card_actions
    actions.add_new_action()
    key = next(iter(actions.action_ui_components))
    actions.action_ui_components[key]["deck_combo"].setCurrentText("Default")
    tree.apply_editors()
    saved = tree.document.stage("e")["card_actions"]
    assert len(saved) == 1
    assert saved[0]["card_type_name"] == ""
    assert saved[0]["change_deck"] == "Default"


# -- exports --------------------------------------------------------------------------


def test_the_exports_panel_lists_only_root_results(dialog):
    dialog.stage_tree.add_stage(STAGE_VARIABLE, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    dialog.stage_tree.rows[guid].editor.result.setText("M")
    dialog.stage_tree.add_stage(STAGE_FOR_EACH_NOTE, None, None)
    loop_guid = dialog.document.root_block()[1]["guid"]
    dialog.stage_tree.add_stage(STAGE_VARIABLE, loop_guid, "body")
    inner_guid = dialog.document.root_block()[1]["body"][0]["guid"]
    dialog.stage_tree.rows[inner_guid].editor.result.setText("Inner")
    dialog.refresh_status()
    assert [keep.text() for _guid, keep, _name in dialog.exports_editor.rows] == ["M"]


def test_an_export_defaults_to_the_results_own_name(dialog):
    dialog.stage_tree.add_stage(STAGE_VARIABLE, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    dialog.stage_tree.rows[guid].editor.result.setText("M")
    dialog.refresh_status()
    dialog.exports_editor.rows[0][1].setChecked(True)
    saved = dialog.get_copy_definition()
    assert saved["exports"] == [{"name": "M", "stage_guid": guid}]


def test_a_call_stage_offers_the_callees_exports(col, qapp):
    callee = new_definition("callee", "The callee")
    callee["exports"] = [{"name": "H1", "stage_guid": "x"}]
    call = default_stage("call_definition", "c")
    call["definition_guid"] = "callee"
    tree = tree_for(col, call, definitions=[callee])
    editor = tree.rows["c"].editor
    assert [name for name, _keep, _local in editor.output_rows] == ["H1"]
    editor.output_rows[0][1].setChecked(True)
    editor.output_rows[0][2].setText("H1_here")
    tree.apply_editors()
    assert tree.document.stage("c")["outputs"] == [{"export": "H1", "result": "H1_here"}]


# -- tags -----------------------------------------------------------------------------


def test_an_edit_note_stages_tags_survive_a_load_and_save(col, qapp):
    stage = default_stage(STAGE_EDIT_NOTE, "e")
    stage["tags"] = {"add": ["one", "two"], "remove": ["three"]}
    tree = tree_for(col, stage)
    tree.apply_editors()
    assert tree.document.stage("e")["tags"] == {"add": ["one", "two"], "remove": ["three"]}


def test_the_tag_editor_now_loads_more_than_one_tag(col, qapp, widget_parent):
    """It used to load none.

    `MultiComboBox.setCurrentText` splits the stored string on ", " and looks for an item
    whose text matches each piece exactly. The tag boxes were filled with bare names while
    the stored string is quoted, so a two-tag selection matched nothing and came back empty
    -- silently dropping both tags on the next save.
    """
    from copy_anywhere.ui.stage_edit_state import StageEditState
    from copy_anywhere.ui.stage_editor_context import StageEditorContext
    from copy_anywhere.ui.tag_editor import TagEditor

    state = StageEditState(StageEditorContext("", {}))
    editor = TagEditor(
        widget_parent,
        state,
        {"add_tags": '"one", "two"', "remove_tags": '"three"'},
        state.copy_mode,
    )
    editor.initialize_ui_state()
    assert editor.get_add_tags() == '"one", "two"'
    assert editor.get_remove_tags() == '"three"'


def test_tags_round_trip_through_the_format_1_tag_editors_string():
    assert tags_to_text(["one", "two"]) == '"one", "two"'
    assert tags_to_list('"one", "two"') == ["one", "two"]
    assert tags_to_text([]) == ""
    assert tags_to_list("") == []
    # Tolerant of the unquoted shape an older config may hold.
    assert tags_to_list("one, two") == ["one", "two"]


# -- the trigger accessors -------------------------------------------------------------


def test_the_trigger_accessors_read_both_formats(col):
    staged = new_definition("g", "Staged")
    staged["triggers"] = {
        "note_types": [VOCAB],
        "deck_names": ["Default"],
        "on_add": True,
        "on_unfocus": {"edit_fields": ["Word"], "add_fields": []},
    }
    legacy = {
        "guid": "l",
        "copy_into_note_types": f'"{VOCAB}"',
        "only_copy_into_decks": '"Default"',
        "copy_on_add": True,
    }
    for definition in (staged, legacy):
        assert definition_note_type_names(definition) == [VOCAB]
        assert definition_deck_names(definition) == ["Default"]
        assert definition_runs_on_add(definition) is True
    assert definition_unfocus_fields(staged, is_new_note=False) == ["Word"]
    assert definition_unfocus_fields(staged, is_new_note=True) == []
    # Format 1 watches fields per field write, so the definition-level answer is empty.
    assert definition_unfocus_fields(legacy, is_new_note=False) == []


def test_a_dash_note_type_list_means_no_note_types(col):
    assert definition_note_type_names({"copy_into_note_types": "-"}) == []
    assert definition_deck_names({"only_copy_into_decks": "-"}) == []


# -- the query stage's selection -------------------------------------------------------


def query_stage_editor(widget_parent, **selection):
    """A note-query editor over a stage whose selection holds exactly these keys."""
    stage = default_stage(STAGE_NOTE_QUERY, "s")
    stage["result"] = "found"
    stage["query"] = value_expression(text="deck:Default")
    stage["selection"] = dict(selection)
    definition = new_definition("d", "A definition", stages=[stage])
    definition["triggers"]["note_types"] = [VOCAB]
    document = StageDocument(definition)
    environment = StageEditorEnvironment(make_note_types_for(document.definition), [definition], "d")
    editor = make_stage_editor(
        widget_parent, document.stage("s"), build_contexts(document)["s"], environment
    )
    return editor, document.stage("s")


class TestTheSelectionSurvivesASave:
    """Keys the migrator writes and this editor does not own (§11).

    A save used to rebuild `selection` from the four controls, which silently dropped both
    of them: the sort stopped being numeric, and a definition that had deliberately been
    selecting nothing started selecting everything its query matched.
    """

    def test_a_numeric_sort_is_shown_and_kept(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(
            widget_parent, strategy="first", count=1, sort_field="Word", sort_numeric=True
        )
        assert editor.sort_numeric.isChecked() is True
        editor.apply()
        assert stage["selection"]["sort_numeric"] is True

    def test_the_checkbox_turns_a_numeric_sort_off(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(
            widget_parent, strategy="first", count=1, sort_field="Word", sort_numeric=True
        )
        editor.sort_numeric.setChecked(False)
        editor.apply()
        assert stage["selection"]["sort_numeric"] is False

    def test_a_lexical_sort_stays_lexical(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(widget_parent, strategy="all", sort_field="Word")
        assert editor.sort_numeric.isChecked() is False
        editor.apply()
        assert stage["selection"]["sort_numeric"] is False

    def test_a_refusal_survives_a_save_that_changes_nothing(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(
            widget_parent, strategy="all", selection_error="Error in copy fields: no such thing"
        )
        editor.apply()
        assert stage["selection"]["selection_error"] == "Error in copy fields: no such thing"

    def test_the_user_can_say_to_select_after_all(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(
            widget_parent, strategy="all", selection_error="Error in copy fields: no such thing"
        )
        editor.keep_refusing.setChecked(True)
        editor.apply()
        assert "selection_error" not in stage["selection"]

    def test_a_stage_that_never_refused_grows_no_error(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(widget_parent, strategy="first", count=2)
        editor.apply()
        assert "selection_error" not in stage["selection"]
        assert stage["selection"]["count"] == 2


# -- the trigger editor's dependent boxes ----------------------------------------------


def triggers_editor(col, widget_parent, **triggers):
    from anki_shared.testing import real_anki
    from copy_anywhere.ui.stage_triggers_editor import TriggersEditor

    # The deck box only offers decks that actually hold a card of a selected note type, so
    # there has to be one of each before "Default" is on offer at all.
    real_anki.add_note(col, VOCAB, {"Word": "neko"})
    real_anki.add_note(col, KANJI, {"Kanji": "猫"})
    definition = new_definition("d", "A definition")
    definition["triggers"].update(triggers)
    return TriggersEditor(widget_parent, definition), definition


def choose(box, *names):
    box.setCurrentText(", ".join(f'"{name}"' for name in names))


class TestClearingATriggerSelection:
    """The deck and unfocus boxes are rebuilt whenever the note types change.

    They used to treat an empty selection as "not filled in yet" and refill it from the
    stored definition, so a list the user had deliberately emptied came back -- and if they
    did not notice, saved.
    """

    def test_an_emptied_unfocus_list_stays_empty(self, col, qapp, widget_parent):
        editor, definition = triggers_editor(
            col,
            widget_parent, note_types=[VOCAB], on_unfocus={"edit_fields": ["Word"],
                                                           "add_fields": []}
        )
        choose(editor.unfocus_edit)
        choose(editor.note_types_box, VOCAB, KANJI)
        editor.apply()
        assert definition["triggers"]["on_unfocus"]["edit_fields"] == []

    def test_an_emptied_deck_list_stays_empty(self, col, qapp, widget_parent):
        editor, definition = triggers_editor(
            col,
            widget_parent, note_types=[VOCAB], deck_names=["Default"]
        )
        assert selected_names(editor.decks_box) == ["Default"]
        choose(editor.decks_box)
        choose(editor.note_types_box, VOCAB, KANJI)
        editor.apply()
        assert definition["triggers"]["deck_names"] == []

    def test_a_selection_the_user_has_not_touched_survives(self, col, qapp, widget_parent):
        editor, definition = triggers_editor(
            col,
            widget_parent, note_types=[VOCAB], deck_names=["Default"],
            on_unfocus={"edit_fields": ["Word"], "add_fields": []},
        )
        choose(editor.note_types_box, VOCAB, KANJI)
        editor.apply()
        assert definition["triggers"]["deck_names"] == ["Default"]
        assert definition["triggers"]["on_unfocus"]["edit_fields"] == ["Word"]

    def test_a_field_only_the_dropped_note_type_had_comes_back_with_it(
        self, col, qapp, widget_parent
    ):
        # Chosen names outlive the box: it can only offer the fields of the note types that
        # are selected right now, so reading it back as the whole answer would turn "not on
        # offer" into "not wanted".
        editor, definition = triggers_editor(
            col,
            widget_parent, note_types=[VOCAB, KANJI],
            on_unfocus={"edit_fields": ["Word", "Kanji"], "add_fields": []},
        )
        choose(editor.note_types_box, VOCAB)
        assert selected_names(editor.unfocus_edit) == ["Word"]
        choose(editor.note_types_box, VOCAB, KANJI)
        editor.apply()
        # Order follows the field list the boxes offer, which follows the note type order
        # Anki hands back, so it is not the order they were stored in.
        assert sorted(definition["triggers"]["on_unfocus"]["edit_fields"]) == ["Kanji", "Word"]
