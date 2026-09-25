"""The preview pane, driven headlessly.

This covers the one item of §12.2's UI list the phase-4 suite could not: choosing a stage
in the preview, and the trace correlation that goes with it in both directions. It also
pins the stale badge, because a trace that silently describes an older definition is worse
than no trace.
"""

import pytest

from anki_shared.testing import real_anki
from copy_anywhere.logic.definition_schema import (
    STAGE_EDIT_NOTE,
    STAGE_VARIABLE,
    new_definition,
)
from copy_anywhere.ui.stage_document import StageDocument
from copy_anywhere.ui.stage_preview import STALE_TEXT, UserRole, PreviewPane

from conftest import VOCAB


@pytest.fixture
def note(col):
    return real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})


def definition_with_stages():
    """A definition that names a variable and writes it into the trigger note."""
    definition = new_definition("d", "A definition")
    definition["triggers"]["note_types"] = [VOCAB]
    document = StageDocument(definition)
    variable = document.add_stage(STAGE_VARIABLE, None, None)
    variable["result"] = "M"
    variable["value"]["text"] = "{{trigger.Word}}!"
    edit = document.add_stage(STAGE_EDIT_NOTE, None, None)
    edit["target"] = {"binding": "trigger"}
    edit["fields"] = [
        {"field": "Note", "value": {"mode": "text", "text": "{{M}}", "code": "",
                                    "process_chain": []}, "write_if": "always"}
    ]
    return document.definition, variable, edit


@pytest.fixture
def pane(col, qapp, widget_parent, note):
    definition, _variable, _edit = definition_with_stages()
    return PreviewPane(widget_parent, definition)


def stage(pane, index: int) -> dict:
    return pane.definition["stages"][index]


class TestChoosingATriggerNote:
    def test_the_list_offers_notes_of_the_definitions_note_type(self, pane, note):
        assert pane.note_list.count() == 1
        assert pane.note_list.item(0).text() == "neko"
        assert pane.selected_note_id() == note.id

    def test_extra_search_terms_narrow_the_list(self, pane, col):
        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        pane.search_notes()
        assert pane.note_list.count() == 2

        pane.query_edit.setText("Word:inu")
        pane.search_notes()

        assert [pane.note_list.item(0).text()] == ["inu"]

    def test_an_unparsable_search_is_reported_rather_than_raised(self, pane):
        pane.query_edit.setText('"unclosed')
        pane.search_notes()

        assert pane.note_list.count() == 0
        assert pane.summary_label.text() != ""


class TestRunningIt:
    def test_a_run_fills_the_trace_with_one_row_per_stage(self, pane):
        pane.run_preview()

        assert pane.trace_tree.topLevelItemCount() == 2
        assert "Variable" in pane.trace_tree.topLevelItem(0).text(0)
        assert pane.trace_tree.topLevelItem(0).text(1) == "neko!"

    def test_a_run_says_what_it_would_change_and_changes_nothing(self, pane, col, note):
        pane.run_preview()

        assert "1 note(s)" in pane.summary_label.text()
        assert col.get_note(note.id)["Note"] == ""

    def test_a_failing_definition_marks_the_stage_that_failed(self, pane):
        stage(pane, 1)["fields"][0]["field"] = "Nope"
        pane.run_preview()

        assert "✗" in pane.trace_tree.topLevelItem(1).text(0)
        assert "failed" in pane.summary_label.text()

    def test_running_with_no_note_chosen_asks_for_one(self, pane):
        pane.note_list.clear()

        pane.run_preview()

        assert pane.run is None
        assert "Choose a note" in pane.summary_label.text()


class TestLoops:
    def test_a_loop_bodys_rows_are_grouped_by_iteration(self, col, qapp, widget_parent, note):
        from copy_anywhere.logic.definition_schema import STAGE_FOR_EACH_NOTE, STAGE_NOTE_QUERY

        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        definition = new_definition("d", "A definition")
        definition["triggers"]["note_types"] = [VOCAB]
        document = StageDocument(definition)
        query = document.add_stage(STAGE_NOTE_QUERY, None, None)
        query["result"] = "found"
        query["query"]["text"] = f'note:"{VOCAB}"'
        loop = document.add_stage(STAGE_FOR_EACH_NOTE, None, None)
        loop["input"] = {"binding": "found"}
        body = document.add_stage(STAGE_VARIABLE, loop["guid"], "body")
        body["result"] = "each"
        body["value"]["text"] = "{{index}}"
        pane = PreviewPane(widget_parent, document.definition)

        pane.run_preview()

        loop_item = pane.trace_tree.topLevelItem(1)
        assert [loop_item.child(i).text(0) for i in range(loop_item.childCount())] == [
            "Iteration 1",
            "Iteration 2",
        ]
        # The grouping rows stand for no stage, so selecting one shows no stage details.
        pane.trace_tree.setCurrentItem(loop_item.child(0))
        assert loop_item.child(0).child(0).text(1) == "1"

    def test_showing_a_looped_stage_selects_its_last_iteration(
        self, col, qapp, widget_parent, note
    ):
        from copy_anywhere.logic.definition_schema import STAGE_FOR_EACH_NOTE, STAGE_NOTE_QUERY

        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        definition = new_definition("d", "A definition")
        definition["triggers"]["note_types"] = [VOCAB]
        document = StageDocument(definition)
        query = document.add_stage(STAGE_NOTE_QUERY, None, None)
        query["result"] = "found"
        query["query"]["text"] = f'note:"{VOCAB}"'
        loop = document.add_stage(STAGE_FOR_EACH_NOTE, None, None)
        loop["input"] = {"binding": "found"}
        body = document.add_stage(STAGE_VARIABLE, loop["guid"], "body")
        body["result"] = "each"
        body["value"]["text"] = "{{index}}"
        pane = PreviewPane(widget_parent, document.definition)
        pane.run_preview()

        pane.show_stage(body["guid"])

        # The run ended on the second pass, so that is the one the pane opens on.
        assert pane.trace_tree.currentItem().text(1) == "2"


class TestStageSelection:
    def test_selecting_a_trace_row_shows_that_stages_details(self, pane):
        pane.run_preview()

        pane.trace_tree.setCurrentItem(pane.trace_tree.topLevelItem(1))

        details = pane.details.toHtml()
        assert "Would change" in details
        assert "neko!" in details

    def test_selecting_a_trace_row_asks_the_stage_list_to_open_that_stage(self, pane):
        asked: list = []
        pane.stage_selected.connect(asked.append)
        pane.run_preview()

        pane.trace_tree.setCurrentItem(pane.trace_tree.topLevelItem(0))

        assert asked == [stage(pane, 0)["guid"]]

    def test_being_asked_to_show_a_stage_selects_it_without_asking_back(self, pane):
        asked: list = []
        pane.stage_selected.connect(asked.append)
        pane.run_preview()

        pane.show_stage(stage(pane, 1)["guid"])

        assert pane.trace_tree.currentItem().data(0, UserRole) == stage(pane, 1)["guid"]
        # Otherwise opening a stage in the list would bounce straight back to the list.
        assert asked == []

    def test_showing_a_stage_before_any_run_does_nothing(self, pane):
        pane.show_stage(stage(pane, 0)["guid"])

        assert pane.trace_tree.currentItem() is None


class TestStaleness:
    def test_nothing_is_stale_before_the_first_run(self, pane):
        assert pane.stale_label.text() == ""

    def test_a_run_clears_the_badge(self, pane):
        pane.run_preview()

        assert pane.stale is False
        assert pane.stale_label.text() == ""

    def test_being_marked_stale_after_a_run_raises_the_badge(self, pane):
        # The pane's side only: what marks it stale is the dialog's, and is tested there.
        pane.run_preview()

        pane.mark_stale()

        assert pane.stale_label.text() == STALE_TEXT

    def test_choosing_a_different_note_makes_the_trace_stale(self, pane, col):
        other = real_anki.add_note(col, VOCAB, {"Word": "inu"})
        pane.search_notes()
        pane.note_list.setCurrentRow(0)
        pane.run_preview()
        assert pane.stale is False

        pane.note_list.setCurrentRow(
            [index for index in range(pane.note_list.count())
             if pane.note_list.item(index).data(UserRole) == other.id][0]
        )

        assert pane.stale is True


class TestInsideTheDialog:
    @pytest.fixture
    def dialog(self, col, qapp, note):
        from copy_anywhere.ui.edit_staged_definition_dialog import (
            EditStagedDefinitionDialog,
        )

        definition, _variable, _edit = definition_with_stages()
        built = EditStagedDefinitionDialog(None, definition)
        yield built
        built.deleteLater()

    def test_the_stage_list_and_the_preview_share_a_splitter(self, dialog):
        assert dialog.splitter.count() == 2
        assert dialog.splitter.widget(1) is dialog.preview

    def test_editing_a_stage_marks_the_preview_stale(self, dialog, qtbot):
        dialog.preview.run_preview()
        assert dialog.preview.stale is False
        guid = dialog.document.definition["stages"][0]["guid"]
        dialog.stage_tree.rows[guid].expand_button.click()

        # A real edit in the stage list, and the dialog's own debounce timer after it.
        dialog.stage_tree.rows[guid].editor.result.setText("Renamed")

        qtbot.waitUntil(lambda: dialog.preview.stale, timeout=5000)
        assert dialog.preview.stale_label.text() == STALE_TEXT

    def test_opening_a_stage_shows_its_trace_row(self, dialog):
        dialog.preview.run_preview()
        guid = dialog.document.definition["stages"][1]["guid"]

        dialog.stage_tree.rows[guid].expand_button.click()

        assert dialog.preview.trace_tree.currentItem().data(0, UserRole) == guid

    def test_choosing_a_trace_row_opens_that_stage_in_the_list(self, dialog):
        dialog.preview.run_preview()
        guid = dialog.document.definition["stages"][1]["guid"]
        row = dialog.stage_tree.rows[guid]
        assert guid not in dialog.stage_tree.expanded

        # Chosen the way a click chooses it: by selecting the row in the trace.
        (item,) = [
            dialog.preview.trace_tree.topLevelItem(index)
            for index in range(dialog.preview.trace_tree.topLevelItemCount())
            if dialog.preview.trace_tree.topLevelItem(index).data(0, UserRole) == guid
        ]
        item.setSelected(True)

        assert guid in dialog.stage_tree.expanded
        assert not row.body.isHidden()
