"""The format-2 editor, driven headlessly.

These are the cases §12.2 asks a UI suite for: reordering a stage, the scope menus keeping
up with the stage list, a reference that broke staying visible, and save blocking. They run
on Qt's offscreen platform plugin -- no display, no pixels read -- because everything here
is ordinary logic that happens to live in widgets.
"""

import copy

import pytest

from copy_anywhere.configuration import (
    definition_deck_names,
    definition_note_type_names,
    definition_runs_on_add,
    definition_unfocus_fields,
)
import definitions as d
from anki_shared.testing import real_anki
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note
from copy_anywhere.logic.definition_migration import migrate_definition_v1_to_v2
from copy_anywhere.logic.definition_schema import (
    STAGE_CARD_QUERY,
    STAGE_CONDITION,
    STAGE_EDIT_CARD,
    STAGE_EDIT_NOTE,
    STAGE_FOR_EACH_CARD,
    STAGE_FOR_EACH_NOTE,
    STAGE_LIST_VARIABLE,
    STAGE_NOTE_QUERY,
    STAGE_READ_FILE,
    STAGE_REDUCE,
    STAGE_VARIABLE,
    STAGE_WRITE_FILE,
    new_definition,
    value_expression,
)
from copy_anywhere.ui import edit_extra_processing_dialog as processing
from copy_anywhere.ui.stage_document import StageDocument, default_stage
from copy_anywhere.ui.stage_editor_context import build_contexts, make_note_types_for
from copy_anywhere.ui.stage_editors import (
    StageEditorEnvironment,
    combo_value,
    make_stage_editor,
    tags_to_list,
    tags_to_text,
)
from copy_anywhere.ui.stage_list import StageTreeWidget
from copy_anywhere.ui.stage_triggers_editor import selected_names

from copy_anywhere.configuration import CARD_TYPE_SEPARATOR

from conftest import KANJI, VOCAB


@pytest.fixture
def dialog(col, qapp):
    """A fresh staged-definition dialog, with the suite's note type already chosen."""
    from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

    definition = new_definition("d", "A definition")
    definition["triggers"]["note_types"] = [VOCAB]
    built = EditStagedDefinitionDialog(None, definition)
    yield built
    # The re-analysis is deferred through a timer, so a case that scheduled one would
    # otherwise have it fire against a collection this case has already finished with.
    built._refresh_timer.stop()
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


def test_an_add_note_trigger_still_saves_a_card_flagging_definition(dialog):
    # A definition that fills a field and flags the card: what format 1 ran in the Add
    # dialog, and what the editor used to refuse with no way to clear the refusal. The
    # action on the new note's own cards cannot run, but it cannot outlive a cancelled add
    # either, so the definition is one the add hook may run and the Save button stays live.
    stage = default_stage(STAGE_EDIT_NOTE, "e")
    stage["fields"] = [
        {
            "field": "Meaning",
            "value": value_expression(text="{{trigger.Word}}"),
            "write_if": "always",
        }
    ]
    stage["card_actions"] = [{"card_type_name": "CA Vocab: Card 1", "set_flag": 1}]
    dialog.document.root_block().append(stage)
    dialog.triggers_editor.on_add.setChecked(True)
    dialog.stage_tree.rebuild()
    dialog.refresh_status()
    status = dialog.status_label.text()
    assert dialog.ok_button.isEnabled()
    assert "note is being added: <b>yes</b>" in status
    assert "Cannot be saved yet" not in status
    # The amber note is still there, saying the one thing that does not happen.
    assert "Worth knowing" in status
    assert "card action on Edit Note" in status
    assert "will not run" in status
    assert "once the note is saved" not in status


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
    assert [keep.text() for _guid, _result, keep, _name in dialog.exports_editor.rows] == ["M"]


def test_an_export_defaults_to_the_results_own_name(dialog):
    dialog.stage_tree.add_stage(STAGE_VARIABLE, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    dialog.stage_tree.rows[guid].editor.result.setText("M")
    dialog.refresh_status()
    dialog.exports_editor.rows[0][2].setChecked(True)
    saved = dialog.get_copy_definition()
    assert saved["exports"] == [{"name": "M", "stage_guid": guid, "result": "M"}]


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


def test_a_call_stages_outputs_can_be_exported(col, qapp):
    # The panel offered these all along; ticking one used to block the save with "names a
    # stage that produces no result", which pointed at the stage rather than at the gap.
    callee = new_definition("callee", "The callee", stages=[variable("x", "H1")])
    callee["exports"] = [{"name": "H1", "stage_guid": "x"}]
    definition = new_definition("d", "A definition")
    definition["triggers"]["note_types"] = [VOCAB]
    call = default_stage("call_definition", "c")
    call["definition_guid"] = "callee"
    call["outputs"] = [{"export": "H1", "result": "M2"}]
    definition["stages"] = [call]
    definition["exports"] = [{"name": "M2", "stage_guid": "c", "result": "M2"}]
    document = StageDocument(definition, lookup={"callee": callee}.get)
    assert document.exportable_stages() == [("c", "M2")]
    assert [problem.message for problem in document.analysis.problems] == []


def test_an_export_stored_without_a_result_still_names_its_stage(col, qapp):
    # Every export written before a call stage could be exported from names only its stage,
    # which could bind one result and no more.
    definition = new_definition("d", "A definition", stages=[variable("v", "M")])
    definition["triggers"]["note_types"] = [VOCAB]
    definition["exports"] = [{"name": "M", "stage_guid": "v"}]
    document = StageDocument(definition)
    assert [problem.message for problem in document.analysis.problems] == []


def exports_panel(widget_parent, definition):
    from copy_anywhere.ui.stage_exports_editor import ExportsEditor

    definition["triggers"]["note_types"] = [VOCAB]
    document = StageDocument(definition)
    return ExportsEditor(widget_parent, document), document


def test_an_export_whose_stage_left_the_top_level_keeps_its_row(col, qapp, widget_parent):
    # The panel rebuilds `exports` from its rows, so an export with no row is dropped the
    # next time anything in the dialog changes. A stage moved into a loop cannot be
    # exported, and saying so is §6's rule for a reference that broke: show it, mark it, and
    # let the user decide -- rather than quietly unpicking what they chose.
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["body"] = [variable("v", "M")]
    definition = new_definition("d", "A definition", stages=[loop])
    definition["exports"] = [{"name": "M", "stage_guid": "v", "result": "M"}]

    panel, document = exports_panel(widget_parent, definition)

    assert [name.text() for _guid, _result, _keep, name in panel.rows] == ["M"]
    panel.apply()
    assert document.exports() == [{"name": "M", "stage_guid": "v", "result": "M"}]


def test_a_stray_export_can_be_unticked(col, qapp, widget_parent):
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["body"] = [variable("v", "M")]
    definition = new_definition("d", "A definition", stages=[loop])
    definition["exports"] = [{"name": "M", "stage_guid": "v", "result": "M"}]

    panel, document = exports_panel(widget_parent, definition)
    panel.rows[0][2].setChecked(False)
    panel.apply()

    assert document.exports() == []


def test_a_stray_export_stored_without_a_result_keeps_the_stages_own_name(
    col, qapp, widget_parent
):
    # Keyed by what the stage produces, not by the export's name, so the row is the same one
    # it will have when the stage is back at the top level rather than a second one beside it.
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["body"] = [variable("v", "M")]
    definition = new_definition("d", "A definition", stages=[loop])
    definition["exports"] = [{"name": "exported_as", "stage_guid": "v"}]

    panel, document = exports_panel(widget_parent, definition)

    assert [keep.text() for _guid, _result, keep, _name in panel.rows] == ["M"]
    panel.apply()
    assert document.exports() == [
        {"name": "exported_as", "stage_guid": "v", "result": "M"}
    ]


def test_renaming_an_exported_result_carries_the_export_with_it(col, qapp, widget_parent):
    # The row is keyed by the stage, and a stage at the top level whose result was renamed
    # has not gone anywhere. Keyed by the old name alone, the export dropped out of the
    # offered set and came back as a stray row telling the user to move a stage back out
    # that never moved -- while the export they set up quietly stopped naming anything.
    definition = new_definition("d", "A definition", stages=[variable("v", "H1")])
    definition["exports"] = [{"name": "H1", "stage_guid": "v", "result": "H1"}]
    panel, document = exports_panel(widget_parent, definition)

    document.stage("v")["result"] = "H2"
    panel.rebuild()

    assert [(result, keep.isChecked()) for _guid, result, keep, _name in panel.rows] == [
        ("H2", True)
    ]
    panel.apply()
    assert document.exports() == [{"name": "H2", "stage_guid": "v", "result": "H2"}]
    assert [problem.message for problem in document.analysis.problems] == []


def test_an_export_given_its_own_name_keeps_it_through_a_rename(col, qapp, widget_parent):
    # Only an export named after the result follows the rename; one the user named
    # themselves is theirs to keep.
    definition = new_definition("d", "A definition", stages=[variable("v", "H1")])
    definition["exports"] = [{"name": "handed_back", "stage_guid": "v", "result": "H1"}]
    panel, document = exports_panel(widget_parent, definition)

    document.stage("v")["result"] = "H2"
    panel.rebuild()

    assert [name.text() for _guid, _result, _keep, name in panel.rows] == ["handed_back"]
    panel.apply()
    assert document.exports() == [{"name": "handed_back", "stage_guid": "v", "result": "H2"}]


def test_renaming_a_result_in_the_dialog_keeps_its_export_and_the_save(dialog):
    dialog.stage_tree.add_stage(STAGE_VARIABLE, None, None)
    guid = dialog.document.root_block()[0]["guid"]
    dialog.stage_tree.rows[guid].editor.result.setText("H1")
    dialog.refresh_status()
    dialog.exports_editor.rows[0][2].setChecked(True)

    dialog.stage_tree.rows[guid].editor.result.setText("H2")
    dialog.refresh_status()

    assert dialog.document.exports() == [{"name": "H2", "stage_guid": guid, "result": "H2"}]
    assert dialog.ok_button.isEnabled(), dialog.status_label.text()


def test_an_export_of_a_deleted_stage_is_not_resurrected_as_a_row(col, qapp, widget_parent):
    # `remove_stage` drops a deleted stage's export, so the panel only ever meets one that
    # names a missing stage in a stored definition -- hand-edited, or saved before that
    # drop existed. Such an export is refused by the analyser and belongs to nothing the
    # user can see; a row for it would write it back on every apply.
    definition = new_definition("d", "A definition", stages=[variable("v", "M")])
    document = StageDocument(definition)
    document.set_exports([
        {"name": "M", "stage_guid": "v", "result": "M"},
        {"name": "Gone", "stage_guid": "deleted", "result": "Gone"},
    ])

    from copy_anywhere.ui.stage_exports_editor import ExportsEditor

    panel = ExportsEditor(widget_parent, document)

    assert [(guid, result) for guid, result, _keep, _name in panel.rows] == [("v", "M")]
    panel.apply()
    assert document.exports() == [{"name": "M", "stage_guid": "v", "result": "M"}]


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

    def test_a_refusal_keeps_the_count_it_migrated_with(self, col, qapp, widget_parent):
        # A refused selection migrates as "all" with its count and sort field still on it,
        # for the user to get back once they fix the strategy. The count box is hidden
        # under "all", and a save that wrote None for it threw the count away before the
        # user ever saw it.
        editor, stage = query_stage_editor(
            widget_parent,
            strategy="all",
            count=3,
            sort_field="Word",
            selection_error="Error in copy fields: no such thing",
        )
        editor.apply()
        assert stage["selection"]["count"] == 3
        assert stage["selection"]["sort_field"] == "Word"

    def test_saying_to_select_after_all_keeps_the_count_too(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(
            widget_parent, strategy="all", count=3, selection_error="Error in copy fields: no such thing"
        )
        editor.keep_refusing.setChecked(True)
        editor.apply()
        assert "selection_error" not in stage["selection"]
        assert stage["selection"]["count"] == 3

    def test_a_strategy_that_takes_a_count_offers_the_migrated_one(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(
            widget_parent, strategy="all", count=3, selection_error="Error in copy fields: no such thing"
        )
        editor.strategy.setCurrentIndex(editor.strategy.findData("first"))
        assert editor.count.isVisibleTo(editor) is True
        editor.apply()
        assert stage["selection"]["strategy"] == "first"
        assert stage["selection"]["count"] == 3

    def test_a_stage_that_never_refused_grows_no_error(self, col, qapp, widget_parent):
        editor, stage = query_stage_editor(widget_parent, strategy="first", count=2)
        editor.apply()
        assert "selection_error" not in stage["selection"]
        assert stage["selection"]["count"] == 2

    def test_a_stage_that_never_refused_does_not_show_the_override(self, col, qapp, widget_parent):
        # The checkbox is built either way and only added to the form when there is a
        # refusal to override. A child widget no layout ever places still paints itself,
        # where it was born -- on top of the first row of every other query stage.
        editor, _stage = query_stage_editor(widget_parent, strategy="first", count=2)
        assert editor.keep_refusing.isVisibleTo(editor) is False

    def test_a_refusal_shows_the_override(self, col, qapp, widget_parent):
        editor, _stage = query_stage_editor(
            widget_parent, strategy="all", selection_error="Error in copy fields: no such thing"
        )
        assert editor.keep_refusing.isVisibleTo(editor) is True


# -- the condition stage's predicate ----------------------------------------------------


def condition_stage_editor(widget_parent, **stage_keys):
    """A condition editor over a stage holding exactly these keys besides its predicate."""
    stage = default_stage(STAGE_CONDITION, "s")
    stage.setdefault("then", [])
    stage.update(stage_keys)
    definition = new_definition("d", "A definition", stages=[stage])
    definition["triggers"]["note_types"] = [VOCAB]
    document = StageDocument(definition)
    environment = StageEditorEnvironment(make_note_types_for(document.definition), [definition], "d")
    editor = make_stage_editor(
        widget_parent, document.stage("s"), build_contexts(document)["s"], environment
    )
    return editor, document.stage("s")


def migrated_condition(widget_parent, **stage_keys):
    """What the migrator writes for `copy_condition_query` (§11): a search, not a value."""
    return condition_stage_editor(
        widget_parent,
        predicate=value_expression(text="tag:done"),
        predicate_kind="note_query",
        predicate_target={"binding": "trigger"},
        **{"only_on_sync": False, **stage_keys},
    )


class TestTheConditionEditorOwnsHowThePredicateIsRun:
    """`predicate_kind`, `predicate_target` and `only_on_sync` decide what the executor runs.

    A migrated copy condition is an Anki search scoped to one note; a newly authored one is
    boolean code or a value. The editor showed the same generic expression box for both and
    wrote none of the three keys back, so the only way to tell which was stored was to read
    the JSON -- and the code toggle it offered on a migrated condition wrote code the
    executor would never reach.
    """

    def test_a_migrated_condition_says_it_is_a_search(self, col, qapp, widget_parent):
        editor, _stage = migrated_condition(widget_parent)

        assert editor.match_as_search.isChecked() is True
        assert editor.target.currentText() == "trigger"

    def test_a_search_condition_round_trips(self, col, qapp, widget_parent):
        editor, stage = migrated_condition(widget_parent, only_on_sync=True)

        assert editor.only_on_sync.isChecked() is True
        editor.apply()
        assert stage["predicate_kind"] == "note_query"
        assert stage["predicate_target"] == {"binding": "trigger"}
        assert stage["only_on_sync"] is True
        assert stage["predicate"]["text"] == "tag:done"

    def test_a_search_condition_offers_no_code(self, col, qapp, widget_parent):
        # The executor reads `text` and runs it as a search whatever the mode says, so a
        # code toggle here can only produce code that is silently never run.
        editor, _stage = migrated_condition(widget_parent)

        assert editor.predicate.code_is_allowed is False

    def test_turning_the_search_off_leaves_an_ordinary_expression(
        self, col, qapp, widget_parent
    ):
        editor, stage = migrated_condition(widget_parent, only_on_sync=True)

        editor.match_as_search.setChecked(False)
        editor.apply()

        assert "predicate_kind" not in stage
        assert "predicate_target" not in stage
        # `only_on_sync` is checked before the kind is, so it belongs to both of them.
        assert stage["only_on_sync"] is True

    def test_only_on_sync_can_be_turned_off(self, col, qapp, widget_parent):
        editor, stage = migrated_condition(widget_parent, only_on_sync=True)

        editor.only_on_sync.setChecked(False)
        editor.apply()

        assert stage["only_on_sync"] is False

    def test_an_authored_condition_grows_no_migrated_keys(self, col, qapp, widget_parent):
        editor, stage = condition_stage_editor(
            widget_parent, predicate=value_expression(mode="code", code="return True")
        )

        assert editor.match_as_search.isChecked() is False
        editor.apply()

        assert "predicate_kind" not in stage
        assert "predicate_target" not in stage
        assert stage["only_on_sync"] is False

    def test_an_authored_condition_can_be_made_a_search(self, col, qapp, widget_parent):
        editor, stage = condition_stage_editor(
            widget_parent, predicate=value_expression(text="tag:done")
        )

        editor.match_as_search.setChecked(True)
        editor.apply()

        assert stage["predicate_kind"] == "note_query"
        assert stage["predicate_target"] == {"binding": "trigger"}

    def test_the_search_form_ticked_and_unticked_keeps_a_code_predicate(
        self, col, qapp, widget_parent
    ):
        # Ticking the search form hides the code toggle, since a search has no code form.
        # Hiding it unchecked it too, so unticking the form again came back to an empty text
        # predicate: the code was still stored, nothing read it, and the branch never ran.
        editor, stage = condition_stage_editor(
            widget_parent, predicate=value_expression(mode="code", code="return True")
        )

        editor.match_as_search.setChecked(True)
        editor.match_as_search.setChecked(False)
        editor.apply()

        assert stage["predicate"]["mode"] == "code"
        assert stage["predicate"]["code"] == "return True"

    def test_saving_as_a_search_keeps_the_code_but_stores_the_text_form(
        self, col, qapp, widget_parent
    ):
        # A search is text run through find_notes, so the text box is what a search-form
        # save stores. The code stays behind the hidden toggle for the day the form is
        # turned off again.
        editor, stage = condition_stage_editor(
            widget_parent, predicate=value_expression(mode="code", code="return True")
        )

        editor.match_as_search.setChecked(True)
        editor.predicate.text_layout.set_text("tag:done")
        editor.apply()

        assert stage["predicate"]["mode"] == "text"
        assert stage["predicate"]["text"] == "tag:done"
        assert stage["predicate"]["code"] == "return True"


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


def tick_first(box):
    """Check a `MultiComboBox`'s first item the way clicking its row does.

    `setCurrentText` is the programmatic path and blocks the model's signals on purpose, so
    it is not what a test about noticing a user's edit should drive.
    """
    from aqt.qt import Qt

    box.model().item(0).setCheckState(Qt.CheckState.Checked)


def edit_note_editor_in_a_loop(col, target):
    """An Edit Note stage inside a loop over a query, so `trigger` and `note` are both in scope."""
    query = default_stage(STAGE_NOTE_QUERY, "q")
    query["result"] = "found"
    query["query"] = value_expression(text="deck:Default")
    loop = default_stage(STAGE_FOR_EACH_NOTE, "loop")
    loop["input"] = {"binding": "found"}
    edit = default_stage(STAGE_EDIT_NOTE, "e")
    edit["target"] = {"binding": target}
    loop["body"] = [edit]
    tree = tree_for(col, query, loop)
    return tree, tree.rows["e"].editor


def offered_card_note_types(editor):
    """The note types whose card types an Edit Note editor's selector currently offers."""
    box = editor.card_actions.card_type_selector
    return {
        box.itemText(index).split(CARD_TYPE_SEPARATOR)[0].strip()
        for index in range(box.count())
        if CARD_TYPE_SEPARATOR in box.itemText(index)
    }


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


class TestSavingWhatTheBoxesCannotOffer:
    """`apply()` has to read the chosen names, not the boxes.

    A box only ever holds what the currently selected note types put in it, so reading it
    back as the whole answer drops every stored name that is out of its reach -- and a deck
    whitelist that drops to empty stops meaning "only these decks" and starts meaning "every
    deck", which is the definition running where it never used to.
    """

    def test_a_whitelisted_deck_the_box_cannot_offer_survives_a_save(
        self, col, qapp, widget_parent
    ):
        # No card of a trigger note type is in "Archive", so the box has no row for it: the
        # deck was emptied, or renamed, or its cards moved after the whitelist was written.
        editor, definition = triggers_editor(
            col, widget_parent, note_types=[VOCAB], deck_names=["Archive"]
        )
        assert selected_names(editor.decks_box) == []

        editor.apply()

        assert definition["triggers"]["deck_names"] == ["Archive"]

    def test_a_deck_only_the_dropped_note_type_had_survives_a_save(
        self, col, qapp, widget_parent
    ):
        from anki_shared.testing import real_anki

        real_anki.add_note(col, KANJI, {"Kanji": "犬"}, deck_name="Kanji only")
        editor, definition = triggers_editor(
            col, widget_parent, note_types=[VOCAB, KANJI], deck_names=["Kanji only"]
        )
        assert selected_names(editor.decks_box) == ["Kanji only"]

        choose(editor.note_types_box, VOCAB)
        editor.apply()

        assert definition["triggers"]["deck_names"] == ["Kanji only"]

    def test_a_field_only_the_dropped_note_type_had_survives_a_save(
        self, col, qapp, widget_parent
    ):
        editor, definition = triggers_editor(
            col,
            widget_parent, note_types=[VOCAB, KANJI],
            on_unfocus={"edit_fields": ["Word", "Kanji"], "add_fields": []},
        )

        choose(editor.note_types_box, VOCAB)
        editor.apply()

        assert sorted(definition["triggers"]["on_unfocus"]["edit_fields"]) == ["Kanji", "Word"]

    def test_unticking_an_offered_deck_still_removes_it(self, col, qapp, widget_parent):
        # The other half of the same rule: within what the box does offer, it is the whole
        # answer, so a name the user unticked is gone.
        editor, definition = triggers_editor(
            col, widget_parent, note_types=[VOCAB], deck_names=["Default", "Archive"]
        )
        assert selected_names(editor.decks_box) == ["Default"]

        choose(editor.decks_box)
        editor.apply()

        assert definition["triggers"]["deck_names"] == ["Archive"]


class TestAddingASecondActionToAnEditCardStage:
    """Whether "Add Card Action" survives an Edit Card stage that already has one action.

    `CardActionsEditor` loads the actions a definition arrives with a few at a time, so the
    dialog opens rather than freezing on a large one. While that runs it disables the card
    type selector and the Add button, and `_finish_loading_initial_actions` re-enables them
    by calling `update_card_type_options`.

    That method's whole job is the card type dropdown, so its first line returns early in
    `single_card_mode` -- an Edit Card stage names one card, so there is no card type to
    pick. The re-enable lives after that line. Nothing else in the class enables the button,
    so for the one mode with no dropdown the disable is permanent: opening a stage that has
    an action and trying to add a second one gives a greyed-out button with no explanation
    and nothing the user can do to it. A stage with no actions yet never takes the loading
    path, so the first action can always be added -- which is what makes this look like the
    stage supports exactly one.
    """

    def stage_with(self, action_count):
        edit = default_stage(STAGE_EDIT_CARD, "e")
        edit["target"] = {"binding": "card"}
        edit["card_actions"] = [
            {
                "guid": f"a{index}",
                "card_type_name": "",
                "change_deck": None,
                "set_flag": index,
                "suspend": None,
                "bury": None,
                "set_desired_retention": None,
                "use_code": False,
                "action_code": "",
            }
            for index in range(action_count)
        ]
        return edit

    def editor_for(self, col, stage):
        query = default_stage(STAGE_CARD_QUERY, "cq")
        query["result"] = "C1"
        query["query"] = value_expression(text="deck:Default")
        loop = default_stage(STAGE_FOR_EACH_CARD, "loop")
        loop["input"] = {"binding": "C1"}
        loop["body"] = [stage]
        tree = tree_for(col, query, loop)
        actions = tree.rows["e"].editor.card_actions
        # What the event loop would do once the dialog is up: drain the staged load.
        actions.finish_loading_initial_actions()
        return tree, actions

    def test_the_button_is_enabled_on_a_stage_with_no_actions_yet(self, col, qapp):
        _tree, actions = self.editor_for(col, self.stage_with(0))

        assert actions.add_action_button.isEnabled()

    def test_the_button_is_still_enabled_on_a_stage_that_has_one(self, col, qapp):
        _tree, actions = self.editor_for(col, self.stage_with(1))

        assert actions.add_action_button.isEnabled()

    def test_a_second_action_can_be_added_and_saved(self, col, qapp):
        tree, actions = self.editor_for(col, self.stage_with(1))

        assert actions.add_action_button.isEnabled()
        actions.add_new_action()
        # An action that would do nothing is dropped on the way out, so the new one has to
        # say something before the save can show it survived.
        key = [k for k in actions.action_ui_components if k != "a0"][0]
        actions.action_ui_components[key]["deck_combo"].setCurrentText("Default")
        tree.apply_editors()

        assert len(tree.document.stage("e")["card_actions"]) == 2


class TestDeletingAStageThatWasExported:
    """Deleting an exported stage, and the stale row that puts its export back.

    `StageDocument.remove_stage` strips the exports naming what it removed, and says why in
    a comment: an export whose producer is gone is invisible corruption, because the
    analyser reports a missing producer but the entry belongs to no row the user can see.

    `refresh_status` then undoes it. It calls `apply_editors()` first, and `apply_editors()`
    ends with `exports_editor.apply()`, which writes the panel's `self.rows` back over
    `exports` -- and those rows are still the ones built before the deletion, including a
    ticked row for the stage that is gone. Only afterwards does `rebuild()` relist the
    panel, and the relisted panel correctly has no row for a stage that no longer exists.

    So the export is restored by the panel and then dropped from the panel, which leaves the
    definition in exactly the state `remove_stage` set out to prevent: Save is greyed out
    with "export 'M' names stage '...', which is not a root stage", and there is no control
    anywhere in the dialog that can clear it. Reopening the dialog does not help, because
    the bad export is what was saved. The user's way out is to recreate a stage with the
    same guid, which the editor gives no way to do.
    """

    def dialog_with_an_exported_variable(self, dialog):
        dialog.stage_tree.add_stage(STAGE_VARIABLE, None, None)
        guid = dialog.document.root_block()[0]["guid"]
        dialog.stage_tree.rows[guid].editor.result.setText("M")
        dialog.refresh_status()
        dialog.exports_editor.rows[0][2].setChecked(True)
        dialog.refresh_status()
        assert dialog.document.exports() == [{"name": "M", "stage_guid": guid, "result": "M"}]
        return guid

    def test_the_export_goes_with_the_stage(self, dialog):
        guid = self.dialog_with_an_exported_variable(dialog)

        dialog.stage_tree.remove_stage(guid)
        dialog.refresh_status()

        assert dialog.document.exports() == []

    def test_the_save_button_is_not_left_blocked(self, dialog):
        guid = self.dialog_with_an_exported_variable(dialog)

        dialog.stage_tree.remove_stage(guid)
        dialog.refresh_status()

        assert list(dialog.document.save_blockers()) == []
        assert dialog.ok_button.isEnabled()

    def test_the_panel_has_no_row_offering_to_undo_it(self, dialog):
        # The half that makes it unrecoverable rather than merely wrong: if a row survived,
        # unticking it would clear the export. The rebuild is right to drop the row -- the
        # stage is gone -- so the entry it writes back has nothing to remove it.
        guid = self.dialog_with_an_exported_variable(dialog)

        dialog.stage_tree.remove_stage(guid)
        dialog.refresh_status()

        assert dialog.exports_editor.rows == []
        assert dialog.document.exports() == []


class TestEditingAMigratedJoin:
    """The Reduce editor, on the one shape the migrator actually produces.

    A `reduce` stage has two forms. A fold runs the `value` expression once per item with an
    accumulator; a join ignores `initial`, `item_binding`, `accumulator_binding` and `value`
    entirely and returns `separator.join(...)`. Which one it is comes from `operation`, and
    a Destination-to-sources definition migrates into one join per field write, carrying
    format 1's `select_card_separator`.

    `ReduceStageEditor` offers no control for either key. It never writes them, so the stored
    `operation: "join"` survives a save -- the stage keeps working -- but everything the
    editor does show for it is dead: two expression editors and two binding names that the
    executor will not read, presented exactly as they are on a fold that does read them. The
    separator, the one part of a join a user has any reason to change, cannot be seen or
    changed at all; it was editable in the format-1 editor, so a definition that had one set
    loses the ability to change it on migration.
    """

    def reduce_editor(self, col, operation, separator=", "):
        source = default_stage(STAGE_LIST_VARIABLE, "lv")
        source["result"] = "L1"
        source["item_type"] = "Text"
        stage = default_stage(STAGE_REDUCE, "r")
        stage["input"] = {"binding": "L1"}
        stage["result"] = "joined"
        stage["operation"] = operation
        stage["separator"] = separator
        tree = tree_for(col, source, stage)
        return tree, tree.rows["r"].editor

    def test_the_editor_says_which_of_the_two_it_is(self, col, qapp):
        _tree, editor = self.reduce_editor(col, "join")

        assert combo_value(editor.operation) == "join"

    def test_the_separator_is_shown_and_can_be_changed(self, col, qapp):
        tree, editor = self.reduce_editor(col, "join", separator=" / ")

        assert editor.separator.text() == " / "
        editor.separator.setText(" + ")
        tree.apply_editors()

        assert tree.document.stage("r")["separator"] == " + "

    def test_a_join_does_not_show_the_fold_controls_that_do_nothing(self, col, qapp):
        # A join reads none of these, so showing them beside a filled-in separator invites
        # the user to write a reducer the stage will never run.
        _tree, editor = self.reduce_editor(col, "join")

        assert editor.value.isHidden()
        assert editor.initial.isHidden()

    def test_a_fold_keeps_being_a_fold_through_a_save(self, col, qapp):
        tree, _editor = self.reduce_editor(col, "fold")
        tree.apply_editors()

        assert tree.document.stage("r").get("operation") == "fold"


class TestACallStageWhoseCalleeCannotBeResolved:
    """What happens to a call stage's bound outputs when the callee is not in the config.

    `_rebuild_outputs` lists the callee's exports and builds one row each. When the callee is
    missing -- deleted, renamed away, or still in format 1, which has no exports -- there are
    no rows, and the panel correctly says so. But `apply()` rebuilds `stage["outputs"]` from
    those rows, so with no rows it writes an empty list.

    `apply()` is not something the user triggers. `refresh_status()` calls `apply_editors()`,
    and the dialog calls `refresh_status()` while it is being built, so the bindings are
    gone before the user has seen the stage -- and they are gone from the saved definition,
    not just from the screen. The combo goes on naming the missing callee as "(not found)",
    which is the right call and makes it worse: the stage still says what it wants to run and
    the analyser still reports the callee as missing, so the obvious fix is to put the callee
    back. Do that and the outputs do not come back, because they were deleted on open.
    """

    def dialog_calling(self, col, qapp, callee_definitions):
        from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

        call = default_stage("call_definition", "c")
        call["definition_guid"] = "callee"
        call["outputs"] = [{"export": "H1", "result": "M1"}]
        definition = new_definition("d", "A definition", stages=[call])
        definition["triggers"]["note_types"] = [VOCAB]
        built = EditStagedDefinitionDialog(
            None, definition, [definition, *callee_definitions]
        )
        built._refresh_timer.stop()
        return built, definition

    def test_a_missing_callee_does_not_cost_the_bindings(self, col, qapp):
        # Read from the document, not from the dict handed in: `StageDocument` deep-copies,
        # so the caller's own definition would look untouched however badly this went.
        dialog, _definition = self.dialog_calling(col, qapp, [])
        try:
            assert dialog.document.stage("c")["outputs"] == [
                {"export": "H1", "result": "M1"}
            ]
        finally:
            dialog.deleteLater()

    def test_they_come_back_when_the_callee_does(self, col, qapp):
        # The bindings are the user's work and the callee's absence is temporary, so keeping
        # them is what makes putting the callee back a fix rather than a restart.
        dialog, definition = self.dialog_calling(col, qapp, [])
        try:
            callee = new_definition("callee", "The callee", stages=[variable("x", "H1")])
            callee["exports"] = [{"name": "H1", "stage_guid": "x"}]
            dialog.stage_tree.rows["c"].editor.environment.definitions.append(callee)
            dialog.stage_tree.rows["c"].editor._rebuild_outputs()
            rows = dialog.stage_tree.rows["c"].editor.output_rows
            assert [(name, keep.isChecked(), local.text()) for name, keep, local in rows] == [
                ("H1", True, "M1")
            ]
        finally:
            dialog.deleteLater()


class TestEditsThatDoNotReachTheDefinition:
    """Three controls in the Edit Note editor that once never said they changed.

    Everything else in the dialog reports an edit: a field combo, an expression editor, a
    binding combo and a name box all connect to `changed`, which reaches `contents_changed`,
    which folds the open editors back into the stage dicts, re-analyses, and marks the
    preview's trace stale.

    The tag editor, the card actions editor and a field write's "write if" combo connected to
    nothing. So an edit to any of them stayed in the widget: `apply_editors()` had not run,
    so the definition still held the old value, and `run_preview()` reads `self.definition`
    directly without applying anything first. The preview ran the definition as it was
    before the edit and -- because nothing marked it stale -- presented that result as
    current.

    So each test asks, at the moment `definition_changed` fires, that the stage in the
    document already holds the edit -- and, where the edit shows there, that the row's
    summary and the next stage's scope were worked out from it. Those are built by
    `contents_changed` before it announces anything, so an edit folded in only afterwards
    (`refresh_contexts` writes the widgets back again once its menus are relisted) leaves
    them describing the definition as it was.
    """

    def edit_note_editor(self, col):
        stage = default_stage(STAGE_EDIT_NOTE, "e")
        stage["fields"] = [
            {"field": "Word", "value": value_expression(text="x"), "write_if": "always"}
        ]
        tree = tree_for(col, stage)
        return tree, tree.rows["e"].editor

    def stage_when_changed(self, tree, guid, act):
        """The stage as the document held it each time `definition_changed` fired."""
        seen = []
        tree.definition_changed.connect(
            lambda: seen.append(copy.deepcopy(tree.document.stage(guid)))
        )
        act()
        return seen

    def summary_when_changed(self, tree, guid, act):
        """The stage row's summary each time `definition_changed` fired."""
        seen = []
        tree.definition_changed.connect(
            lambda: seen.append(tree.rows[guid].summary.text())
        )
        act()
        return seen

    def test_changing_write_if_reports_the_change(self, col, qapp):
        tree, editor = self.edit_note_editor(col)
        row = editor.field_rows[0]

        seen = self.stage_when_changed(tree, "e", lambda: row.write_if.setCurrentIndex(1))

        assert seen
        assert seen[-1]["fields"][0]["write_if"] == row.write_if.currentData()
        assert seen[-1]["fields"][0]["write_if"] != "always"

    def test_changing_a_tag_reports_the_change(self, col, qapp):
        from anki_shared.testing import real_anki

        # The box offers the collection's tags, so there has to be one to tick.
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, tags=["known"])
        tree, editor = self.edit_note_editor(col)

        summaries = self.summary_when_changed(
            tree, "e", lambda: tick_first(editor.tag_editor.add_tags_combo_box)
        )

        assert summaries
        assert "tags" in summaries[-1]
        assert tree.document.stage("e")["tags"]["add"] == ["known"]

    def test_a_card_action_reports_the_change(self, col, qapp):
        # An Edit Card stage, because that is where "Add Card Action" needs no card type
        # chosen first; the editor class is the same one an Edit Note stage embeds.
        edit = default_stage(STAGE_EDIT_CARD, "ec")
        edit["target"] = {"binding": "card"}
        query = default_stage(STAGE_CARD_QUERY, "cq")
        query["result"] = "C1"
        query["query"] = value_expression(text="deck:Default")
        loop = default_stage(STAGE_FOR_EACH_CARD, "loop")
        loop["input"] = {"binding": "C1"}
        loop["body"] = [edit]
        tree = tree_for(col, query, loop)
        actions = tree.rows["ec"].editor.card_actions

        def add_and_flag():
            actions.add_new_action()
            # An action that does nothing is not kept, so the edit is choosing its flag.
            (ui,) = actions.action_ui_components.values()
            flag = next(
                button
                for button in ui["flag_group"].buttons()
                if button.property("flag_value") == 2
            )
            flag.setChecked(True)

        summaries = self.summary_when_changed(tree, "ec", add_and_flag)

        assert summaries
        assert summaries[-1].endswith("1 card action")
        assert [action["set_flag"] for action in tree.document.stage("ec")["card_actions"]] == [2]

    def test_a_field_combo_reports_the_change(self, col, qapp):
        # The guard: the wiring the three above were missing, on a control beside them in
        # the same row.
        tree, editor = self.edit_note_editor(col)
        row = editor.field_rows[0]

        summaries = self.summary_when_changed(
            tree, "e", lambda: row.field.setCurrentText("Meaning")
        )

        assert summaries
        assert "Meaning" in summaries[-1]

    def test_a_renamed_result_is_in_the_next_stages_scope_when_announced(self, col, qapp):
        # The scope a later stage's menus are built from is analysed from the stage dicts,
        # so it is only as current as the edit folded in before the analysis ran.
        edit = default_stage(STAGE_EDIT_NOTE, "e")
        tree = tree_for(col, variable("v", "M"), edit)
        scopes = []
        tree.definition_changed.connect(lambda: scopes.append(set(tree.contexts["e"].scope)))

        tree.rows["v"].editor.result.setText("Renamed")

        assert scopes
        assert "Renamed" in scopes[-1]
        assert "M" not in scopes[-1]


def test_a_field_picker_built_with_a_field_does_not_ask_for_one(col, qapp):
    # `field_combo` styles itself as needing a value while it is still empty, and
    # `fill_field_combo` puts the stage's field in it with signals blocked -- which is what
    # refreshes that style. The red border stayed on over a field the stage had all along,
    # in every Edit Note row and on every query stage's sort field.
    stage = default_stage(STAGE_EDIT_NOTE, "e")
    stage["fields"] = [
        {"field": "Word", "value": value_expression(text="x"), "write_if": "always"}
    ]
    tree = tree_for(col, stage)

    field = tree.rows["e"].editor.field_rows[0].field

    assert field.currentText() == "Word"
    assert "darkred" not in field.styleSheet()


def test_a_field_picker_with_nothing_in_it_still_asks_for_one(col, qapp):
    stage = default_stage(STAGE_EDIT_NOTE, "e")
    stage["fields"] = [{"field": "", "value": value_expression(text="x"), "write_if": "always"}]
    tree = tree_for(col, stage)

    assert "darkred" in tree.rows["e"].editor.field_rows[0].field.styleSheet()


class TestChangingTheTriggerNoteType:
    """Whether the stage list follows the trigger note type the dialog is still open on.

    Field pickers are built once, when a stage's editor is created, from the note types the
    definition triggers on. Changing that selection is an ordinary thing to do in this
    dialog -- the trigger editor is at the top of the same scroll area as the stages -- and
    it emits `changed`, which the dialog connects to `schedule_refresh`.

    `refresh_status()` re-applies the editors, relists the exports, re-analyses and refreshes
    the preview. It does not touch the stage tree. So every field dropdown goes on offering
    the old note type's fields, grouped under the old note type's name, with the old
    selection still in it. Picking from it saves a field the trigger note does not have, and
    the run fails per note with "Field 'X' not found in note" -- which is reported against
    the stage, not against the note type change that caused it.
    """

    def dialog_with_an_edit_stage(self, dialog):
        dialog.stage_tree.add_stage(STAGE_EDIT_NOTE, None, None)
        guid = dialog.document.root_block()[0]["guid"]
        editor = dialog.stage_tree.rows[guid].editor
        editor._on_add_field()
        return editor.field_rows[0]

    def offered_fields(self, dialog):
        guid = dialog.document.root_block()[0]["guid"]
        combo = dialog.stage_tree.rows[guid].editor.field_rows[0].field
        return [combo.itemText(index) for index in range(combo.count())]

    def test_the_field_picker_follows_the_new_note_type(self, col, qapp, dialog):
        row = self.dialog_with_an_edit_stage(dialog)
        row.field.setCurrentText("Word")
        assert "Meaning" in self.offered_fields(dialog)

        dialog.triggers_editor.note_types_box.setCurrentText(f'"{KANJI}"')
        dialog.refresh_status()

        offered = self.offered_fields(dialog)
        assert "Kanji" in offered
        assert "Meaning" not in offered

    def test_a_field_the_new_note_type_does_not_have_is_not_left_selected(
        self, col, qapp, dialog
    ):
        # The half that reaches the run: the stage keeps naming `Word`, the trigger note no
        # longer has it, and `run_edit_note` fails the definition per note with "Field
        # 'Word' not found in note".
        row = self.dialog_with_an_edit_stage(dialog)
        row.field.setCurrentText("Word")

        dialog.triggers_editor.note_types_box.setCurrentText(f'"{KANJI}"')
        dialog.refresh_status()

        guid = dialog.document.root_block()[0]["guid"]
        assert dialog.document.stage(guid)["fields"][0]["field"] != "Word"

    def edit_note_editor(self, dialog):
        guid = dialog.document.root_block()[0]["guid"]
        return dialog.stage_tree.rows[guid].editor

    def test_the_card_type_picker_follows_the_new_note_type(self, col, qapp, dialog):
        # The card type list is the other picker built from the trigger note type: a stage
        # editing the trigger offers that note type's own card types, and
        # `StageEditState.selected_models` was assigned once, when the row was built.
        self.dialog_with_an_edit_stage(dialog)
        assert offered_card_note_types(self.edit_note_editor(dialog)) == {VOCAB}

        dialog.triggers_editor.note_types_box.setCurrentText(f'"{KANJI}"')
        dialog.refresh_status()

        assert offered_card_note_types(self.edit_note_editor(dialog)) == {KANJI}

    def test_an_action_for_a_card_type_the_new_note_type_does_not_have_is_dropped(
        self, col, qapp, dialog
    ):
        self.dialog_with_an_edit_stage(dialog)
        actions = self.edit_note_editor(dialog).card_actions
        vocab = f"{VOCAB}{CARD_TYPE_SEPARATOR}Recognition"
        actions.card_type_selector.setCurrentText(vocab)
        actions.add_new_action()
        actions.action_ui_components[vocab]["deck_combo"].setCurrentText("Default")
        dialog.refresh_status()
        guid = dialog.document.root_block()[0]["guid"]
        assert [a["card_type_name"] for a in dialog.document.stage(guid)["card_actions"]] == [vocab]

        dialog.triggers_editor.note_types_box.setCurrentText(f'"{KANJI}"')
        dialog.refresh_status()

        assert dialog.document.stage(guid)["card_actions"] == []


class TestTheUnfocusGateOnAMigratedWrite:
    """The per-write unfocus list, which used to be the one thing on a write with no control.

    A migrated field write carries `unfocus_trigger_fields`: the editor fields format 1 asked
    it to watch, which `run_edit_note` still honours. The row showed a field picker, a "write
    if" combo and a value, and `apply()` wrote those three -- so the key survived every edit
    and every save with no way to see it. Adding a field to the definition's own unfocus list
    widened the gate that starts a run and not this one, and because tags and card actions in
    the same stage are not gated, the result was a note tagged and saved with the field it
    was supposed to fill still empty.

    A write the stage editor produced carries no such key and must not grow one: format 2
    watches fields for the definition as a whole, so there is nothing per write to decide,
    and adding an empty list would mean "never runs on unfocus".
    """

    def row_for(self, col, field_write):
        stage = default_stage(STAGE_EDIT_NOTE, "e")
        stage["fields"] = [field_write]
        tree = tree_for(col, stage)
        return tree, tree.rows["e"].editor.field_rows[0]

    def migrated_write(self, **extra):
        return {
            "field": "Meaning",
            "value": value_expression(text="x"),
            "write_if": "always",
            "unfocus_trigger_fields": ["Word"],
            "unfocus_when_edit": True,
            "unfocus_when_add": False,
            **extra,
        }

    def test_the_row_shows_what_the_write_watches(self, col, qapp):
        _tree, row = self.row_for(col, self.migrated_write())

        assert row.unfocus_fields is not None
        assert selected_names(row.unfocus_fields) == ["Word"]

    def test_it_offers_the_trigger_note_types_fields(self, col, qapp):
        _tree, row = self.row_for(col, self.migrated_write())
        box = row.unfocus_fields

        offered = [box.itemText(index) for index in range(box.count())]
        assert '"Reading"' in offered

    def test_widening_it_is_saved(self, col, qapp):
        tree, row = self.row_for(col, self.migrated_write())

        choose(row.unfocus_fields, "Word", "Reading")
        tree.apply_editors()

        saved = tree.document.stage("e")["fields"][0]["unfocus_trigger_fields"]
        assert sorted(saved) == ["Reading", "Word"]

    def test_a_natively_authored_write_gets_no_gate_at_all(self, col, qapp):
        tree, row = self.row_for(
            col, {"field": "Meaning", "value": value_expression(text="x"), "write_if": "always"}
        )

        assert row.unfocus_fields is None
        tree.apply_editors()
        assert "unfocus_trigger_fields" not in tree.document.stage("e")["fields"][0]

    def test_a_stored_name_the_note_type_no_longer_has_survives_a_save(self, col, qapp):
        # Same rule the trigger editor follows for a whitelisted deck it cannot offer: a box
        # can only hold what is in it, so a name out of its reach would be dropped on the way
        # through and "not on offer" would silently become "not wanted".
        tree, _row = self.row_for(
            col, self.migrated_write(unfocus_trigger_fields=["Word", "Gone"])
        )

        tree.apply_editors()

        saved = tree.document.stage("e")["fields"][0]["unfocus_trigger_fields"]
        assert sorted(saved) == ["Gone", "Word"]


class TestTheCopyConditionMarkerOnAConditionStage:
    """`unmatched_skips_trigger`, and what unticking the search form does to it.

    It is the marker the migrator writes for format 1's copy condition: a non-match skips the
    whole trigger note rather than taking the empty `else`, which is only ever right for the
    one shape the migrator builds, where the condition wraps the entire definition. The
    editor shows nothing about it, so it has to follow the one control that does bear on it.
    """

    def condition_stage(self, **extra):
        stage = default_stage(STAGE_CONDITION, "c")
        stage["predicate"] = value_expression(text="tag:wanted")
        stage["predicate_kind"] = "note_query"
        stage["predicate_target"] = {"binding": "trigger"}
        stage["unmatched_skips_trigger"] = True
        stage.update(extra)
        return stage

    def test_it_survives_a_save_that_leaves_the_search_form_on(self, col, qapp):
        tree = tree_for(col, self.condition_stage())

        tree.apply_editors()

        assert tree.document.stage("c")["unmatched_skips_trigger"] is True

    def test_turning_off_the_search_form_takes_it_too(self, col, qapp):
        tree = tree_for(col, self.condition_stage())
        editor = tree.rows["c"].editor

        editor.match_as_search.setChecked(False)
        tree.apply_editors()

        saved = tree.document.stage("c")
        assert "predicate_kind" not in saved
        assert "unmatched_skips_trigger" not in saved


class TestWhichCardTypesAnEditNoteStageOffers:
    """The card types on offer follow the note the stage edits, which can change.

    `StageEditState.copy_mode` is what `CardActionsEditor.update_card_type_options` reads to
    decide between two lists: a stage editing the trigger offers that note type's own card
    types, and a stage editing a note some query found cannot know its note type, so it
    offers every one in the collection. The stage editor computes it once, from the target
    binding as it stood when the row was built.

    Changing the target afterwards is an ordinary edit -- it is the first control in the row
    -- and nothing rebuilt the list. So a stage retargeted from the trigger to a queried note
    went on offering only the trigger note type's card types, and an action added from that
    list names a card type the queried note will not have, so it silently matches nothing at
    run time.
    """

    def test_a_stage_editing_the_trigger_offers_its_own_note_type(self, col, qapp):
        _tree, editor = edit_note_editor_in_a_loop(col, "trigger")

        assert offered_card_note_types(editor) == {VOCAB}

    def test_a_stage_editing_a_queried_note_offers_them_all(self, col, qapp):
        _tree, editor = edit_note_editor_in_a_loop(col, "note")

        assert KANJI in offered_card_note_types(editor)

    def test_retargeting_the_stage_relists_them(self, col, qapp):
        _tree, editor = edit_note_editor_in_a_loop(col, "trigger")
        assert offered_card_note_types(editor) == {VOCAB}

        editor.target.setCurrentText("note")

        assert KANJI in offered_card_note_types(editor)

    def test_retargeting_back_narrows_them_again(self, col, qapp):
        _tree, editor = edit_note_editor_in_a_loop(col, "note")

        editor.target.setCurrentText("trigger")

        assert offered_card_note_types(editor) == {VOCAB}


class TestWhatElseFollowsARetarget:
    """Everything keyed on which note an Edit Note stage edits, not only the card type list.

    `StageEditState.set_target_is_trigger` was built to reach one callback -- the card type
    relisting -- and fired only that registry. `TagEditor.update_direction_labels` and
    `CardActionsEditor.set_description` read the same `copy_mode`, but the captions are
    registered on the direction registry and the description is called once, from
    `initialize_ui_state`. So after a retarget the list was right and the words around it
    described the other note.

    The relisting itself only refilled the selector. An action added for a card type while
    the stage targeted a queried note survived a retarget to the trigger, was saved with the
    stage, and matched nothing at run time -- the trigger note has no card of that type.
    Such an action is dropped: unlike a stray export there is nothing to move back that
    would make it apply again.
    """

    def action_for(self, editor, card_type):
        actions = editor.card_actions
        actions.card_type_selector.setCurrentText(card_type)
        actions.add_new_action()
        # An action that would do nothing is dropped on the way out.
        actions.action_ui_components[card_type]["deck_combo"].setCurrentText("Default")

    def test_the_tag_captions_stop_naming_the_trigger(self, col, qapp):
        _tree, editor = edit_note_editor_in_a_loop(col, "trigger")
        assert "trigger" in editor.tag_editor.add_tags_label.text()

        editor.target.setCurrentText("note")

        assert "trigger" not in editor.tag_editor.add_tags_label.text()
        assert "trigger" not in editor.tag_editor.remove_tags_label.text()
        # Format 1's wording had the prepositions the wrong way round.
        assert editor.tag_editor.add_tags_label.text() == "Tags to add to the searched note"
        assert (
            editor.tag_editor.remove_tags_label.text() == "Tags to remove from the searched note"
        )

    def test_the_tag_captions_name_the_trigger_again_on_the_way_back(self, col, qapp):
        _tree, editor = edit_note_editor_in_a_loop(col, "note")
        assert "trigger" not in editor.tag_editor.add_tags_label.text()

        editor.target.setCurrentText("trigger")

        assert "trigger" in editor.tag_editor.add_tags_label.text()

    def test_the_description_says_whose_cards_the_actions_reach(self, col, qapp):
        _tree, editor = edit_note_editor_in_a_loop(col, "trigger")
        assert "queried notes" not in editor.card_actions.description_label.text()

        editor.target.setCurrentText("note")

        assert "queried notes" in editor.card_actions.description_label.text()

    def test_an_action_for_a_card_type_the_trigger_cannot_have_is_dropped(self, col, qapp):
        tree, editor = edit_note_editor_in_a_loop(col, "note")
        kanji = f"{KANJI}{CARD_TYPE_SEPARATOR}Card 1"
        vocab = f"{VOCAB}{CARD_TYPE_SEPARATOR}Recognition"
        self.action_for(editor, kanji)
        self.action_for(editor, vocab)

        editor.target.setCurrentText("trigger")
        tree.apply_editors()

        saved = tree.document.stage("e")["card_actions"]
        assert [action["card_type_name"] for action in saved] == [vocab]
        assert kanji not in editor.card_actions.action_ui_components

    def test_retargeting_to_a_queried_note_keeps_every_action(self, col, qapp):
        tree, editor = edit_note_editor_in_a_loop(col, "trigger")
        vocab = f"{VOCAB}{CARD_TYPE_SEPARATOR}Recognition"
        self.action_for(editor, vocab)

        editor.target.setCurrentText("note")
        tree.apply_editors()

        saved = tree.document.stage("e")["card_actions"]
        assert [action["card_type_name"] for action in saved] == [vocab]


class TestTheConditionEditorsCaption:
    """The caption over the predicate box, which says what is expected in it.

    A condition matched as an Anki search wants a search; one matched any other way wants an
    expression. `ConditionStageEditor._apply_kind` relabels the box to say which, and
    `ValueExpressionEditor.set_label` set a tooltip instead of the caption -- so the caption
    kept saying whatever it was built with, and the only thing that changed was text the
    user has to hover to find.
    """

    def condition_editor(self, col):
        stage = default_stage(STAGE_CONDITION, "c")
        stage["predicate"] = value_expression(text="tag:wanted")
        tree = tree_for(col, stage)
        return tree, tree.rows["c"].editor

    def caption(self, editor):
        return editor.predicate.text_layout.main_label.text()

    def test_it_says_expression_for_an_ordinary_predicate(self, col, qapp):
        _tree, editor = self.condition_editor(col)

        assert "search" not in self.caption(editor).lower()

    def test_it_says_search_once_the_search_form_is_chosen(self, col, qapp):
        _tree, editor = self.condition_editor(col)

        editor.match_as_search.setChecked(True)

        assert "search" in self.caption(editor).lower()

    def test_it_goes_back_when_the_search_form_is_turned_off(self, col, qapp):
        _tree, editor = self.condition_editor(col)
        editor.match_as_search.setChecked(True)

        editor.match_as_search.setChecked(False)

        assert "search" not in self.caption(editor).lower()


class TestWhatTheExpressionEditorSaves:
    """The round trip: what the user types in an expression box is what the run resolves.

    The editor has one syntax. It writes `mode`, `text` and `code` back and nothing else --
    a migrated definition arrives promoted, so there is no second spelling to keep track of
    -- and the box marks every reference its menu does not offer, which is the one thing
    standing between a mistyped name and a stage error.
    """

    def variable_editor(self, col, text="{{trigger.Word}}", code="", mode=None):
        stage = default_stage(STAGE_VARIABLE, "v")
        stage["result"] = "M"
        stage["value"] = value_expression(text=text, code=code, mode=mode)
        tree = tree_for(col, stage)
        return tree, tree.rows["v"].editor.value

    def test_a_reference_the_menu_does_not_offer_is_marked_in_the_box(self, col, qapp):
        # A format-1 spelling is the likeliest one to be typed from memory, and it is the
        # one promotion rewrote everywhere: `{{Word}}` names no binding, so the box marks
        # it, as the analyser refuses it on save and the run refuses it per note.
        _tree, editor = self.variable_editor(col)

        editor.text_layout.set_text("{{trigger.Word}} {{Word}}")
        editor.text_layout.validate_text()

        assert "Word" in editor.text_layout.error_label.text()
        assert "trigger.Word" not in editor.text_layout.error_label.text()

    def test_what_the_editor_saves_reaches_the_field(self, col, qapp):
        # End to end: a migrated within-note write, its text replaced through the editor
        # with another reference the menu offers, then run.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        migrated = migrate_definition_v1_to_v2(
            d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")])
        )
        tree = tree_for(col, *migrated["stages"])
        edit_note = tree.rows[migrated["stages"][0]["guid"]].editor
        edit_note.field_rows[0].value.text_layout.set_text("{{trigger.Meaning}}")
        tree.apply_editors()

        copied: list = []
        ok = copy_for_single_trigger_note(
            tree.document.definition, note, copied_into_notes=copied
        )

        assert ok is True
        assert note["Note"] == "cat"


class TestAFieldTheTriggerNoteTypeDoesNotHave:
    """The editor refuses what the run would refuse, where it can know.

    A reference that resolves to nothing fails the stage at run time and writes nothing, so
    a definition holding one is a definition the user cannot use. The analyser is pure and
    the collection is what says which fields a note type has, so the dialog hands it the
    trigger's field lists -- the only note types a definition names -- and the problem is
    reported against the stage that holds the reference, like any other.
    """

    def write_into_the_trigger(self, dialog, text):
        dialog.stage_tree.add_stage(STAGE_EDIT_NOTE, None, None)
        guid = dialog.document.root_block()[0]["guid"]
        editor = dialog.stage_tree.rows[guid].editor
        editor._on_add_field()
        editor.field_rows[0].field.setCurrentText("Note")
        editor.field_rows[0].value.text_layout.set_text(text)
        dialog.refresh_status()
        return guid

    def test_the_save_is_refused_and_the_stage_is_marked(self, col, qapp, dialog):
        guid = self.write_into_the_trigger(dialog, "{{trigger.Nonexistent}}")

        blockers = list(dialog.document.save_blockers())
        assert any("Nonexistent" in blocker for blocker in blockers), blockers
        assert not dialog.ok_button.isEnabled()
        marked = [problem.message for problem in dialog.document.problems_for(guid)]
        assert any("Nonexistent" in message for message in marked), marked
        assert "⚠" in dialog.stage_tree.rows[guid].problem_label.text()

    def test_a_field_the_note_type_has_saves(self, col, qapp, dialog):
        guid = self.write_into_the_trigger(dialog, "{{trigger.Word}}")

        assert list(dialog.document.save_blockers()) == []
        assert dialog.document.problems_for(guid) == []

    def test_the_check_follows_the_trigger_note_type(self, col, qapp, dialog):
        # `Word` is a Vocab field and not a Kanji one, so the same definition becomes
        # invalid the moment the trigger note type is changed under it.
        self.write_into_the_trigger(dialog, "{{trigger.Word}}")

        dialog.triggers_editor.note_types_box.setCurrentText(f'"{KANJI}"')
        dialog.refresh_status()

        assert any("Word" in blocker for blocker in dialog.document.save_blockers())


class TestACallbackWhoseWidgetIsGone:
    """A callback registered by a widget Qt has since deleted is dropped, not called.

    A regex process's dialog is built the first time its Edit button is clicked, and it
    registers `update_field_options` on the state's note type registry. Removing the
    process, or the whole field row it sits in, `deleteLater`s the dialog and leaves the
    callback registered. The next change of the trigger note type fired it, and
    `refresh_contexts` raised "wrapped C/C++ object of type PasteableTextEdit has been
    deleted" -- out of an ordinary edit at the top of the dialog.

    Only that case is dropped. Format 1 swallowed every exception a callback raised, which
    would hide a real bug in one that is still alive.
    """

    def edit_note_with_regex(self):
        stage = default_stage(STAGE_EDIT_NOTE, "e")
        stage["target"] = {"binding": "trigger"}
        expression = value_expression(text="{{trigger.Word}}")
        expression["process_chain"] = [
            dict(processing.NEW_PROCESS_DEFAULTS[processing.REGEX_PROCESS], guid="rx")
        ]
        stage["fields"] = [{"field": "Note", "value": expression, "write_if": "always"}]
        return stage

    def open_regex_dialog(self, editor, monkeypatch):
        monkeypatch.setattr(processing.RegexProcessDialog, "exec", lambda self: 0)
        widget = editor.field_rows[0].value.process_widget
        widget.process_ui_components["rx"]["edit_button"].click()
        dialog = widget.process_ui_components["rx"]["dialog_holder"]["dialog"]
        assert dialog.update_field_options in editor.state.selected_model_callbacks
        return widget, dialog

    def flush_deletes(self, qapp):
        from aqt.qt import QCoreApplication, QEvent

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        qapp.processEvents()

    def change_the_trigger_note_type(self, tree):
        tree.document.definition["triggers"]["note_types"] = [KANJI]
        tree.refresh_contexts()

    def listener(self):
        """A live Qt object whose bound method is a callback, and the calls it heard."""
        from aqt.qt import QObject

        class Listener(QObject):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def heard(self):
                self.calls += 1

            def fails(self):
                raise RuntimeError("a real bug in a live callback")

        return Listener()

    def tree_with_a_deleted_regex_dialog(self, col, qapp, monkeypatch):
        from aqt.qt import sip

        tree = tree_for(col, self.edit_note_with_regex())
        editor = tree.rows["e"].editor
        widget, dialog = self.open_regex_dialog(editor, monkeypatch)
        widget.process_ui_components["rx"]["remove_button"].click()
        self.flush_deletes(qapp)
        assert sip.isdeleted(dialog)
        return tree, editor, dialog

    def test_changing_the_trigger_note_type_after_deleting_the_process(
        self, col, qapp, monkeypatch
    ):
        tree, editor, dialog = self.tree_with_a_deleted_regex_dialog(col, qapp, monkeypatch)

        self.change_the_trigger_note_type(tree)

        assert dialog.update_field_options not in editor.state.selected_model_callbacks

    def test_changing_the_trigger_note_type_after_removing_the_field_row(
        self, col, qapp, monkeypatch
    ):
        from aqt.qt import sip

        tree = tree_for(col, self.edit_note_with_regex())
        editor = tree.rows["e"].editor
        _widget, dialog = self.open_regex_dialog(editor, monkeypatch)
        editor.field_rows[0].removed.emit(editor.field_rows[0])
        self.flush_deletes(qapp)
        assert sip.isdeleted(dialog)

        self.change_the_trigger_note_type(tree)

        assert dialog.update_field_options not in editor.state.selected_model_callbacks

    def test_the_live_callbacks_still_run(self, col, qapp, monkeypatch):
        tree, editor, _dialog = self.tree_with_a_deleted_regex_dialog(col, qapp, monkeypatch)
        # Registered after the dead one, so it only runs if the dead one did not stop the
        # loop; bound to a live Qt object, so it shows the check does not drop those.
        listener = self.listener()
        editor.state.add_selected_model_callback(listener.heard)

        self.change_the_trigger_note_type(tree)

        assert listener.calls == 1
        assert listener.heard in editor.state.selected_model_callbacks
        # The card actions editor's own callback, registered before either.
        assert offered_card_note_types(editor) == {KANJI}

    def test_an_error_in_a_live_callback_is_not_swallowed(self, col, qapp):
        tree = tree_for(col, self.edit_note_with_regex())
        editor = tree.rows["e"].editor
        listener = self.listener()
        editor.state.add_selected_model_callback(listener.fails)

        with pytest.raises(RuntimeError, match="a real bug"):
            self.change_the_trigger_note_type(tree)
        assert listener.fails in editor.state.selected_model_callbacks


class TestCardActionCodeEditorsFollowTheScope:
    """A card action's code editor offers the names in scope now, not when it was built.

    `CardActionsEditor` hands each action's code editor the state's menu once, when the
    action's row is built. Renaming or adding a variable upstream moves the stage's scope
    and `refresh_contexts` gives the stage its new context, but the code editors went on
    offering -- and validating against -- the old names. An Edit Card stage was worse off:
    it has no value editor, and only a value editor passed the new context on to the state,
    so even an action added after the rename got the menu the stage was opened with.
    """

    def edit_card_tree(self, col, variable_name="before"):
        query = default_stage(STAGE_CARD_QUERY, "cq")
        query["result"] = "C1"
        query["query"] = value_expression(text="deck:Default")
        edit = default_stage(STAGE_EDIT_CARD, "ec")
        edit["target"] = {"binding": "card"}
        edit["card_actions"] = [self.code_action("")]
        loop = default_stage(STAGE_FOR_EACH_CARD, "loop")
        loop["input"] = {"binding": "C1"}
        loop["body"] = [edit]
        return tree_for(col, variable("v", variable_name), query, loop)

    def edit_note_tree(self, col, variable_name="before"):
        edit = default_stage(STAGE_EDIT_NOTE, "e")
        edit["target"] = {"binding": "trigger"}
        edit["card_actions"] = [self.code_action(f"{VOCAB}{CARD_TYPE_SEPARATOR}Recognition")]
        return tree_for(col, variable("v", variable_name), edit)

    def code_action(self, card_type_name):
        return {
            "guid": "a",
            "card_type_name": card_type_name,
            "change_deck": None,
            "set_flag": None,
            "suspend": None,
            "bury": None,
            "set_desired_retention": None,
            "use_code": True,
            "action_code": "return {}",
        }

    def code_editors(self, editor):
        actions = editor.card_actions
        actions.finish_loading_initial_actions()
        return [ui["code_editor"] for ui in actions.action_ui_components.values()]

    def offered_variables(self, code_editor):
        # What the editor's right-click menu is built from.
        return set((code_editor.text_edit.options_dict.get("Variables") or {}).keys())

    def rename_the_variable(self, tree, name):
        # Through the widget: `refresh_contexts` writes the widgets back into the stages.
        tree.rows["v"].editor.result.setText(name)
        tree.apply_editors()
        tree.refresh_contexts()

    def test_a_renamed_variable_reaches_an_edit_card_code_editor(self, col, qapp):
        tree = self.edit_card_tree(col)
        (code_editor,) = self.code_editors(tree.rows["ec"].editor)
        assert "before" in self.offered_variables(code_editor)

        self.rename_the_variable(tree, "after")

        assert "after" in self.offered_variables(code_editor)
        assert "before" not in self.offered_variables(code_editor)

    def test_a_renamed_variable_reaches_an_edit_note_code_editor(self, col, qapp):
        tree = self.edit_note_tree(col)
        (code_editor,) = self.code_editors(tree.rows["e"].editor)
        assert "before" in self.offered_variables(code_editor)

        self.rename_the_variable(tree, "after")

        assert "after" in self.offered_variables(code_editor)
        assert "before" not in self.offered_variables(code_editor)

    def test_the_validation_follows_the_rename_too(self, col, qapp):
        tree = self.edit_card_tree(col)
        (code_editor,) = self.code_editors(tree.rows["ec"].editor)
        code_editor.set_text("x = '{{after}}'\nreturn {}")
        assert "Not a valid field" in code_editor.error_label.text()

        self.rename_the_variable(tree, "after")

        assert code_editor.error_label.text() == ""

    def test_an_action_added_after_the_rename_offers_it(self, col, qapp):
        tree = self.edit_card_tree(col)
        actions = tree.rows["ec"].editor.card_actions
        self.rename_the_variable(tree, "after")

        actions.add_new_action()

        new = [ui for key, ui in actions.action_ui_components.items() if key != "a"]
        assert len(new) == 1
        assert "after" in self.offered_variables(new[0]["code_editor"])

    def test_the_edit_card_stages_state_follows_its_context(self, col, qapp):
        tree = self.edit_card_tree(col)

        self.rename_the_variable(tree, "after")

        editor = tree.rows["ec"].editor
        assert editor.state.context is tree.contexts["ec"]


class TestTheFileStagesSayWhatNameTheFileGets:
    """Both file stages store the file under a name with a leading `_`, and say so.

    A file CopyAnywhere reads or writes is a text file no note field refers to, so Anki's
    Check Media lists it as unused and offers to delete it -- unless its name starts with
    `_`. `normalize_media_filename` adds the prefix to a name that lacks one, so a
    `dictionary.txt` put in the media folder by hand is never the file a stage reads. That
    is the user's to fix; the editor says so where the name is typed.
    """

    @pytest.mark.parametrize("stage_type", [STAGE_READ_FILE, STAGE_WRITE_FILE])
    def test_the_name_box_says_a_leading_underscore_is_added(self, col, qapp, stage_type):
        tree = tree_for(col, default_stage(stage_type, "f"))
        filename = tree.rows["f"].editor.filename

        description = filename.text_layout.optional_description.text()

        assert "leading '_' is added" in description
        assert "Check Media" in description
