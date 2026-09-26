"""The Select Note stage: one note of a note list, used as a note from then on.

A query produces a `NoteList`, which nothing can edit or interpolate, so before this stage a
definition that wanted "the first note found" had to loop over the list, store a value per
note and join them, once per field it read. Select Note picks one note by index -- a number,
or code that gets the list as `notes` -- and binds it, or binds nothing when no note is at
that index.
"""

import pytest

import definitions as d
from anki_shared.testing import real_anki
from note_types import VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note
from copy_anywhere.logic.definition_schema import validate_definition_structure
from copy_anywhere.logic.flow_analysis import analyze_definition

POOL_BY_FREQ = {
    "strategy": "all",
    "count": None,
    "sort_field": "Freq",
    "sort_order": "ascending",
    "sort_numeric": True,
}


@pytest.fixture
def trigger(col):
    return real_anki.add_note(
        col, VOCAB, {"Word": "trigger", "Freq": "1", "Note": "untouched"}
    )


@pytest.fixture
def pool(col):
    """w1, w2, w3, tagged `pool`, found in that order by `POOL_BY_FREQ`."""
    return [
        real_anki.add_note(
            col, VOCAB, {"Word": f"w{i}", "Freq": str(i), "Meaning": "m" * i}, tags=["pool"]
        )
        for i in range(1, 4)
    ]


def run(trigger, *stages, copied_into_notes=None):
    definition = d.staged(stages=[
        d.note_query("found", "tag:pool", selection=POOL_BY_FREQ),
        *stages,
    ])
    return copy_for_single_trigger_note(
        definition,
        trigger,
        copied_into_notes=copied_into_notes if copied_into_notes is not None else [],
    )


def write_picked_word(field="Note"):
    return d.edit_note("trigger", [d.write(field, d.text("{{Picked.Word}}"))])


class TestByIndex:
    @pytest.mark.parametrize(
        "index, expected",
        [("0", "w1"), ("1", "w2"), ("2", "w3"), ("-1", "w3"), ("-3", "w1"), (" 1 ", "w2")],
    )
    def test_the_note_at_the_index_is_selected(self, trigger, pool, logger, index, expected):
        assert run(
            trigger, d.select_note("found", "Picked", d.text(index)), write_picked_word()
        ) is True, logger.errors
        assert trigger["Note"] == expected

    def test_the_index_can_be_read_from_a_note(self, trigger, pool, logger):
        # The trigger's Freq is "1", so the second note.
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("{{trigger.Freq}}")),
            write_picked_word(),
        ) is True, logger.errors
        assert trigger["Note"] == "w2"

    def test_an_index_that_is_not_a_number_fails_the_stage(self, trigger, pool, logger):
        assert run(
            trigger, d.select_note("found", "Picked", d.text("second")), write_picked_word()
        ) is False
        assert logger.has_error("index 'second' is not a whole number")


class TestNoNoteAtTheIndex:
    def test_by_default_its_fields_read_as_empty(self, trigger, pool, logger):
        assert run(
            trigger, d.select_note("found", "Picked", d.text("7")), write_picked_word()
        ) is True, logger.errors
        assert trigger["Note"] == ""

    def test_editing_no_note_does_nothing(self, trigger, pool, logger):
        written = []
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("7")),
            d.edit_note("Picked", [d.write("Meaning", d.text("changed"))]),
            copied_into_notes=written,
        ) is True, logger.errors
        assert written == []
        assert [note["Meaning"] for note in pool] == ["m", "mm", "mmm"]

    def test_an_empty_list_has_no_note_at_index_zero(self, trigger, logger):
        assert run(trigger, d.select_note("found", "Picked"), write_picked_word()) is True
        assert trigger["Note"] == ""

    def test_skip_block_stops_the_rest_of_the_block(self, trigger, pool, logger):
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("7"), if_missing="skip_block"),
            d.edit_note("trigger", [d.write("Note", d.text("ran"))]),
        ) is True, logger.errors
        assert trigger["Note"] == "untouched"

    def test_error_fails_the_definition_and_says_where(self, trigger, pool, logger):
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("7"), if_missing="error"),
            write_picked_word(),
        ) is False
        assert logger.has_error("no note at index 7 of 3")


class TestByCode:
    def test_code_gets_the_list_as_notes_and_returns_an_index(self, trigger, pool, logger):
        index = d.code("return [n['Word'] for n in notes].index('w2')")
        assert run(trigger, d.select_note("found", "Picked", index), write_picked_word()) is True
        assert trigger["Note"] == "w2", logger.errors

    def test_the_list_is_also_there_under_its_own_name(self, trigger, pool, logger):
        index = d.code("return len(found) - 1")
        assert run(trigger, d.select_note("found", "Picked", index), write_picked_word()) is True
        assert trigger["Note"] == "w3", logger.errors

    def test_code_returning_none_selects_no_note(self, trigger, pool, logger):
        assert run(
            trigger, d.select_note("found", "Picked", d.code("return None")), write_picked_word()
        ) is True, logger.errors
        assert trigger["Note"] == ""

    @pytest.mark.parametrize("returned", ["'1'", "1.0", "True"])
    def test_code_returning_anything_else_fails_the_stage(self, trigger, pool, logger, returned):
        assert run(
            trigger,
            d.select_note("found", "Picked", d.code(f"return {returned}")),
            write_picked_word(),
        ) is False
        assert logger.has_error("index code returned")


class TestUsingTheSelectedNote:
    def test_it_can_be_edited(self, trigger, pool, logger):
        written = []
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("1")),
            d.edit_note("Picked", [d.write("Meaning", d.text("from {{trigger.Word}}"))]),
            copied_into_notes=written,
        ) is True, logger.errors
        assert [(note.id, note["Meaning"]) for note in written] == [(pool[1].id, "from trigger")]

    def test_code_reads_it_as_a_note(self, trigger, pool, logger):
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("2")),
            d.edit_note("trigger", [d.write("Note", d.code("return Picked['Meaning']"))]),
        ) is True, logger.errors
        assert trigger["Note"] == "mmm"


def analysed(*stages):
    definition = d.staged(stages=[d.note_query("found", "tag:pool"), *stages])
    return [problem.message for problem in analyze_definition(definition).problems]


class TestWhatTheEditorChecks:
    def test_a_selected_note_is_a_note_for_every_stage_after(self):
        assert analysed(
            d.select_note("found", "Picked"),
            d.edit_note("Picked", [d.write("Meaning", d.text("{{Picked.Word}}"))]),
        ) == []

    def test_the_input_has_to_be_a_note_list(self):
        problems = analysed(d.variable("words", d.text("x")), d.select_note("words", "Picked"))
        assert any("select input must be NoteList" in problem for problem in problems)

    def test_a_selected_note_cannot_be_interpolated_whole(self):
        problems = analysed(
            d.select_note("found", "Picked"),
            d.edit_note("trigger", [d.write("Note", d.text("{{Picked}}"))]),
        )
        assert problems

    def test_it_may_be_called_note_as_a_loop_item_may(self):
        assert analysed(d.select_note("found", "note")) == []
        assert validate_definition_structure(
            d.staged(stages=[d.note_query("found", "x"), d.select_note("found", "note")])
        ) == []

    def test_no_other_reserved_name_is_allowed(self):
        assert any("reserved" in problem for problem in analysed(d.select_note("found", "trigger")))

    def test_a_query_still_may_not_be_called_note(self):
        definition = d.staged(stages=[d.note_query("note", "x")])
        assert any(
            "reserved" in problem.message for problem in validate_definition_structure(definition)
        )

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"index": None}, "'index' is missing"),
            ({"input": {}}, "'input.binding' is missing"),
            ({"if_missing": "shrug"}, "if_missing"),
        ],
    )
    def test_its_shape_is_checked(self, change, message):
        stage = d.select_note("found", "Picked")
        stage.update(change)
        if change.get("index", "") is None:
            del stage["index"]
        definition = d.staged(stages=[d.note_query("found", "x"), stage])
        assert any(message in problem.message for problem in validate_definition_structure(definition))
