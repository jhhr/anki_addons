"""The Select Note and Select Card stages: one item of a list, used as that item from then on.

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
from copy_anywhere.logic.definition_migration import migrate_definition_v1_to_v2
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

    def test_an_index_that_reads_blank_is_no_index(self, trigger, pool, logger):
        # An empty field read as the index says "no position", as code returning None does:
        # `if_missing` decides, rather than the run failing on '' not being a number.
        trigger["Freq"] = ""
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("{{trigger.Freq}}")),
            write_picked_word(),
        ) is True, logger.errors
        assert trigger["Note"] == ""

    def test_and_error_says_there_was_none(self, trigger, pool, logger):
        trigger["Freq"] = ""
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("{{trigger.Freq}}"), if_missing="error"),
            write_picked_word(),
        ) is False
        assert logger.has_error("no note at no index of 3")

    def test_a_call_given_no_note_fails_naming_it(self, trigger, pool, logger):
        # Editing no note does nothing, but a call has to run its definition on some note.
        from copy_anywhere.logic.flow_analysis import make_lookup

        callee = d.staged(
            "callee",
            stages=[d.edit_note("trigger", [d.write("Note", d.text("called"))])],
            guid="callee",
        )
        definition = d.staged(stages=[
            d.note_query("found", "tag:pool", selection=POOL_BY_FREQ),
            d.select_note("found", "Picked", d.text("7")),
            d.call_definition("callee", trigger="Picked"),
        ])
        assert copy_for_single_trigger_note(
            definition, trigger, copied_into_notes=[], definition_lookup=make_lookup([callee])
        ) is False
        assert logger.has_error("call trigger 'Picked' holds no note")

    def test_so_does_a_search_condition(self, trigger, pool, logger):
        assert run(
            trigger,
            d.select_note("found", "Picked", d.text("7")),
            d.condition(
                d.text("Word:w1"),
                then=[d.edit_note("trigger", [d.write("Note", d.text("matched"))])],
                predicate_kind="note_query",
                predicate_target={"binding": "Picked"},
            ),
        ) is False
        assert logger.has_error("predicate target 'Picked' holds no note")
        assert trigger["Note"] == "untouched"


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

    def test_code_reads_no_note_as_none_with_no_cards(self, trigger, pool, logger):
        # Called `note`, which is what `cards` follows: with no note there are no cards,
        # where it used to fall back to the trigger's and hand code those as the note's.
        assert run(
            trigger,
            d.select_note("found", "note", d.text("7")),
            d.edit_note(
                "trigger",
                [d.write("Note", d.code("return f'{note is None}:{len(cards)}'"))],
            ),
        ) is True, logger.errors
        assert trigger["Note"] == "True:0"


class TestNothingElseIsNoNote:
    """Only a select stage that found nothing reads as "no note". A None that reaches a
    binding any other way -- a list code built -- is the mistake it always was."""

    def test_editing_a_none_from_a_code_list_fails_the_stage(self, trigger, logger):
        definition = d.staged(stages=[
            d.variable("found", d.code("return [None]")),
            d.for_each_note("found", [d.edit_note("note", [d.write("Note", d.text("x"))])]),
        ])
        assert copy_for_single_trigger_note(definition, trigger, copied_into_notes=[]) is False
        assert logger.has_error("target must be a note, but it holds NoneType")

    def test_reading_one_fails_the_stage_too(self, trigger, logger):
        definition = d.staged(stages=[
            d.variable("found", d.code("return [None]")),
            d.for_each_note(
                "found",
                [d.edit_note("trigger", [d.write("Note", d.text("[{{note.Word}}]"))])],
            ),
        ])
        assert copy_for_single_trigger_note(definition, trigger, copied_into_notes=[]) is False
        assert logger.has_error("'note' is not a note or card, so 'note.Word' has no value")
        assert trigger["Note"] == "untouched"


def analysed(*stages):
    definition = d.staged(stages=[d.note_query("found", "tag:pool"), *stages])
    return [problem.message for problem in analyze_definition(definition).problems]


class TestWhatTheEditorChecks:
    def test_a_selected_note_is_a_note_for_every_stage_after(self):
        assert analysed(
            d.select_note("found", "Picked"),
            d.edit_note("Picked", [d.write("Meaning", d.text("{{Picked.Word}}"))]),
        ) == []

    def test_an_empty_index_box_is_refused(self):
        problems = analysed(d.select_note("found", "Picked", d.text("  ")))
        assert any("index is empty" in problem for problem in problems), problems

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


def run_cards(trigger, *stages, copied_into_cards_dict=None):
    """Recognition cards of the pool, ordered by their note's Freq: w1's, w2's, w3's."""
    definition = d.staged(stages=[
        d.card_query("found", "tag:pool card:Recognition", selection=POOL_BY_FREQ),
        *stages,
    ])
    return copy_for_single_trigger_note(
        definition,
        trigger,
        copied_into_notes=[],
        copied_into_cards_dict=copied_into_cards_dict if copied_into_cards_dict is not None else {},
    )


def write_picked_card_word():
    return d.edit_note("trigger", [d.write("Note", d.code("return Picked.note['Word'] if Picked else ''"))])


def flag_picked(flag=3):
    return d.edit_card("Picked", [dict(d.card_action(VOCAB, "Recognition", set_flag=flag), card_type_name="")])


class TestSelectCard:
    @pytest.mark.parametrize("index, expected", [("0", "w1"), ("-1", "w3"), ("1", "w2")])
    def test_the_card_at_the_index_is_selected(self, trigger, pool, logger, index, expected):
        assert run_cards(
            trigger, d.select_card("found", "Picked", d.text(index)), write_picked_card_word()
        ) is True, logger.errors
        assert trigger["Note"] == expected

    def test_code_gets_the_list_as_cards(self, trigger, pool, logger):
        index = d.code("return [c.note['Word'] for c in cards].index('w2')")
        assert run_cards(
            trigger, d.select_card("found", "Picked", index), write_picked_card_word()
        ) is True, logger.errors
        assert trigger["Note"] == "w2"

    def test_the_selected_card_can_be_edited(self, trigger, pool, logger):
        edited = {}
        assert run_cards(
            trigger,
            d.select_card("found", "Picked", d.text("1")),
            flag_picked(),
            copied_into_cards_dict=edited,
        ) is True, logger.errors
        assert [(card.nid, card.flags) for card in edited.values()] == [(pool[1].id, 3)]

    def test_no_card_at_the_index_reads_as_empty(self, trigger, pool, logger):
        assert run_cards(
            trigger,
            d.select_card("found", "Picked", d.text("9")),
            d.edit_note("trigger", [d.write("Note", d.text("[{{Picked.deck_name}}]"))]),
        ) is True, logger.errors
        assert trigger["Note"] == "[]"

    def test_editing_no_card_does_nothing(self, trigger, pool, logger):
        edited = {}
        assert run_cards(
            trigger,
            d.select_card("found", "Picked", d.text("9")),
            flag_picked(),
            copied_into_cards_dict=edited,
        ) is True, logger.errors
        assert edited == {}

    def test_error_names_a_card(self, trigger, pool, logger):
        assert run_cards(
            trigger, d.select_card("found", "Picked", d.text("9"), if_missing="error")
        ) is False
        assert logger.has_error("no card at index 9 of 3")

    def test_the_input_has_to_be_a_card_list(self):
        problems = analysed(d.select_card("found", "Picked"))
        assert any("select input must be CardList" in problem for problem in problems)

    def test_it_may_be_called_card_but_not_note(self):
        definition_stages = [d.card_query("found", "x")]
        ok = d.staged(stages=[*definition_stages, d.select_card("found", "card")])
        assert [p.message for p in analyze_definition(ok).problems] == []
        bad = d.staged(stages=[*definition_stages, d.select_card("found", "note")])
        assert any("reserved" in p.message for p in analyze_definition(bad).problems)


class TestMigratedDestinationToOneSource:
    """A migrated Destination to sources definition that reads one note does what the join
    shape did: each is run on its own copy of the same notes and they are compared."""

    def run(
        self, col, monkeypatch, field_to_field_defs, join: bool, note_before="untouched", **extra
    ) -> str:
        from copy_anywhere.logic import definition_migration

        if join:
            monkeypatch.setattr(definition_migration, "_takes_one_source", lambda *_: False)
        trigger = real_anki.add_note(col, VOCAB, {"Word": "trigger", "Note": note_before})
        definition = migrate_definition_v1_to_v2(
            d.destination_to_sources(
                copy_from_cards_query="tag:pool",
                field_to_field_defs=field_to_field_defs,
                select_card_count="1",
                **extra,
            )
        )
        uses_select = "select_note" in [stage["type"] for stage in definition["stages"]]
        assert uses_select is not join
        assert copy_for_single_trigger_note(definition, trigger, copied_into_notes=[]) is True
        monkeypatch.undo()
        return trigger["Note"]

    @pytest.mark.parametrize(
        "field_def",
        [
            d.field_to_field("Note", "{{Word}} for {{__Dest__Word}}"),
            d.field_to_field(
                "Note", copy_as_code="return note['Meaning'] + destination['Word']",
                use_code=True,
            ),
            # The join's loop numbered the one source 1, and the query counted it.
            d.field_to_field("Note", "{{Word}} {{__Query_Note_Index}}/{{__Target_Notes_Count}}"),
            # Ran once on the joined text; with one source, once on that note's.
            d.field_to_field(
                "Note", "{{Meaning}}", process_chain=[d.regex_process("m", "M")]
            ),
            d.field_to_field("Note", "{{Meaning}}", copy_if_empty=True),
        ],
        ids=["text", "code", "runtime values", "process chain", "copy if empty"],
    )
    @pytest.mark.parametrize("note_before", ["untouched", ""])
    @pytest.mark.parametrize("sort_by_field", ["Freq", None])
    def test_it_writes_what_the_join_wrote(
        self, col, pool, monkeypatch, field_def, note_before, sort_by_field
    ):
        one = self.run(
            col, monkeypatch, [field_def], join=False, note_before=note_before,
            sort_by_field=sort_by_field,
        )
        joined = self.run(
            col, monkeypatch, [field_def], join=True, note_before=note_before,
            sort_by_field=sort_by_field,
        )
        assert one == joined
        # Copy-if-empty writes only into the empty field; everything else always writes.
        wrote = note_before == "" or not field_def.get("copy_if_empty")
        assert (one != note_before) is wrote, one

    def test_a_file_is_written_as_the_join_wrote_it(self, col, pool, monkeypatch, media_dir):
        written = []
        for join in (False, True):
            self.run(
                col, monkeypatch, [], join=join,
                field_to_file_defs=[d.field_to_file("one-source.txt", "{{Word}}-{{Meaning}}")],
                sort_by_field="Freq",
            )
            path = media_dir / "_one-source.txt"
            written.append(path.read_text(encoding="utf-8"))
            path.unlink()
        assert written[0] == written[1] == "w1-m"

    def test_no_note_found_leaves_the_trigger_alone(self, col, monkeypatch, logger):
        assert self.run(
            col, monkeypatch, [d.field_to_field("Note", "{{Word}}")], join=False
        ) == "untouched"
