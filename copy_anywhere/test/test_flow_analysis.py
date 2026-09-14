"""Tests for the scope, type, effect and call-graph analysis.

The analyser is what both the editor and the runtime ask about a definition, so these are
tests about what a definition *means* before it runs: which bindings a stage can see, what
each result holds, what the definition does to the collection, and which of those things
make it invalid. No collection is involved -- the analyser never runs a query, it only
reads the program.
"""

import definitions as d
from copy_anywhere.logic.definition_schema import (
    T_CARD,
    T_CARD_LIST,
    T_NOTE,
    T_NOTE_LIST,
    T_TEXT,
    list_of,
    validate_definition_structure,
)
from copy_anywhere.logic.flow_analysis import (
    analyze_definition,
    callers_of,
    find_call_cycles,
    make_lookup,
)


def analyze(stages, **extra):
    return analyze_definition(d.staged(stages=stages, **extra))


def messages(result):
    return " | ".join(result.problem_messages())


class TestScope:
    def test_a_stage_sees_the_results_declared_before_it(self):
        first = d.variable("M", d.text("x"))
        second = d.variable("N", d.text("{{M}}"))
        result = analyze([first, second])
        assert result.is_valid, messages(result)
        assert set(result.scopes[first["guid"]]) == {"trigger"}
        assert set(result.scopes[second["guid"]]) == {"trigger", "M"}

    def test_it_does_not_see_the_ones_declared_after_it(self):
        first = d.variable("M", d.text("{{N}}"))
        result = analyze([first, d.variable("N", d.text("x"))])
        # `{{N}}` is not a binding yet, so it is left to the runtime as a note value rather
        # than reported here -- but the recorded scope is what the editor's menu offers.
        assert "N" not in result.scopes[first["guid"]]

    def test_a_loop_body_sees_the_loop_bindings(self):
        edit = d.edit_note("note")
        query = d.note_query("found", "deck:x")
        result = analyze([query, d.for_each_note("found", [edit])])
        assert result.is_valid, messages(result)
        assert {"note", "index", "count", "found"} <= set(result.scopes[edit["guid"]])

    def test_a_card_loop_binds_both_the_card_and_its_note(self):
        inner = d.edit_card("card")
        result = analyze([d.card_query("cards", "deck:x"), d.for_each_card("cards", [inner])])
        assert result.is_valid, messages(result)
        scope = result.scopes[inner["guid"]]
        assert scope["card"].type == T_CARD
        assert scope["note"].type == T_NOTE

    def test_loop_locals_do_not_escape_the_loop(self):
        after = d.edit_note("trigger")
        result = analyze([
            d.note_query("found", "deck:x"),
            d.for_each_note("found", [d.variable("inner", d.text("x"))]),
            after,
        ])
        assert "inner" not in result.scopes[after["guid"]]

    def test_branch_locals_do_not_escape_the_branch(self):
        after = d.edit_note("trigger")
        result = analyze([
            d.condition(d.text("x"), [d.variable("inner", d.text("x"))]),
            after,
        ])
        assert "inner" not in result.scopes[after["guid"]]

    def test_shadowing_a_result_is_refused(self):
        result = analyze([d.variable("M", d.text("a")), d.variable("M", d.text("b"))])
        assert "shadows" in messages(result)

    def test_a_reserved_name_is_refused(self):
        assert "reserved" in messages(analyze([d.variable("trigger", d.text("a"))]))

    def test_a_non_identifier_name_is_refused(self):
        assert "identifier" in messages(analyze([d.variable("not a name", d.text("a"))]))


class TestTypes:
    def test_a_query_produces_a_note_list_and_a_card_query_a_card_list(self):
        notes = d.note_query("found", "deck:x")
        cards = d.card_query("their_cards", "deck:x")
        result = analyze([notes, cards])
        assert result.result_types[notes["guid"]] == T_NOTE_LIST
        assert result.result_types[cards["guid"]] == T_CARD_LIST

    def test_a_list_variable_declares_its_item_type(self):
        stage = d.list_variable("L", "Text")
        result = analyze([stage])
        assert result.result_types[stage["guid"]] == list_of(T_TEXT)

    def test_looping_over_something_that_is_not_a_list_is_refused(self):
        result = analyze([d.variable("M", d.text("x")), d.for_each_note("M", [])])
        assert "loop input must be NoteList" in messages(result)

    def test_looping_a_card_list_with_a_note_loop_is_refused(self):
        result = analyze([d.card_query("cards", "deck:x"), d.for_each_note("cards", [])])
        assert "must be NoteList" in messages(result)

    def test_editing_a_note_list_as_if_it_were_a_note_is_refused(self):
        result = analyze([d.note_query("found", "deck:x"), d.edit_note("found")])
        assert "edit_note target must be NoteRef" in messages(result)

    def test_storing_into_something_that_is_not_a_list_is_refused(self):
        result = analyze([d.variable("M", d.text("x")), d.store("M", d.text("y"))])
        assert "not a list" in messages(result)

    def test_an_unknown_binding_is_refused(self):
        assert "unknown binding" in messages(analyze([d.edit_note("nope")]))


class TestInterpolationRules:
    def test_interpolating_a_note_list_is_refused(self):
        result = analyze([
            d.note_query("found", "deck:x"),
            d.edit_note("trigger", [d.write("Note", d.text("{{found}}"))]),
        ])
        assert "reduce it to a scalar first" in messages(result)

    def test_interpolating_a_list_is_refused(self):
        result = analyze([
            d.list_variable("L"),
            d.edit_note("trigger", [d.write("Note", d.text("{{L}}"))]),
        ])
        assert "reduce it to a scalar first" in messages(result)

    def test_a_qualified_note_reference_is_fine(self):
        result = analyze([d.edit_note("trigger", [d.write("Note", d.text("{{trigger.Word}}"))])])
        assert result.is_valid, messages(result)

    def test_qualifying_a_scalar_is_refused(self):
        result = analyze([
            d.variable("M", d.text("x")),
            d.edit_note("trigger", [d.write("Note", d.text("{{M.Word}}"))]),
        ])
        assert "not a note or card" in messages(result)

    def test_a_reduced_list_can_be_interpolated(self):
        result = analyze([
            d.list_variable("L"),
            d.join("L", "joined", "+"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        assert result.is_valid, messages(result)


class TestEffects:
    def effects(self, stages, **extra):
        return analyze(stages, **extra).effects

    def test_a_trigger_edit_is_add_note_compatible(self):
        effects = self.effects([d.edit_note("trigger", [d.write("Note", d.text("x"))])])
        assert effects["edits_trigger"] is True
        assert effects["edits_other_notes"] is False
        assert effects["add_note_compatible"] is True

    def test_a_read_only_query_stays_add_note_compatible(self):
        effects = self.effects([d.note_query("found", "deck:x")])
        assert effects["queries_collection"] is True
        assert effects["add_note_compatible"] is True

    def test_editing_a_queried_note_is_not(self):
        effects = self.effects([
            d.note_query("found", "deck:x"),
            d.for_each_note("found", [d.edit_note("note")]),
        ])
        assert effects["edits_other_notes"] is True
        assert effects["add_note_compatible"] is False

    def test_a_card_action_on_the_trigger_is_not(self):
        # A note being added has no persisted cards, so there is nothing to act on.
        effects = self.effects([
            d.edit_note("trigger", card_actions=[d.card_action("CA Vocab", "Recognition")])
        ])
        assert effects["edits_cards"] is True
        assert effects["add_note_compatible"] is False

    def test_files_are_reported_separately_in_each_direction(self):
        effects = self.effects([
            d.read_file("t", "log.txt"),
            d.write_file("log.txt", d.text("{{t}}")),
        ])
        assert (effects["reads_files"], effects["writes_files"]) == (True, True)
        assert effects["add_note_compatible"] is True


class TestCalls:
    def build(self):
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[d.edit_note("trigger", [d.write("Note", d.text("x"))])],
        )
        producer = d.variable("H1", d.text("hello"))
        child["stages"].insert(0, producer)
        child["exports"] = [d.export("H1", producer)]
        return child

    def test_an_export_is_typed_and_bindable_by_the_caller(self):
        child = self.build()
        call = d.call_definition("child-guid", outputs=[{"export": "H1", "result": "H1_here"}])
        after = d.edit_note("trigger", [d.write("Note", d.text("{{H1_here}}"))])
        parent = d.staged("parent", guid="parent-guid", stages=[call, after])
        result = analyze_definition(parent, lookup=make_lookup([child, parent]))
        assert result.is_valid, messages(result)
        assert result.scopes[after["guid"]]["H1_here"].type == T_TEXT

    def test_binding_an_export_the_callee_does_not_have_is_refused(self):
        child = self.build()
        call = d.call_definition("child-guid", outputs=[{"export": "nope", "result": "x"}])
        parent = d.staged("parent", guid="parent-guid", stages=[call])
        result = analyze_definition(parent, lookup=make_lookup([child, parent]))
        assert "exports no 'nope'" in messages(result)

    def test_effects_are_transitive_through_a_call(self):
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[
                d.note_query("found", "deck:x"),
                d.for_each_note("found", [d.edit_note("note")]),
            ],
        )
        parent = d.staged(
            "parent", guid="parent-guid", stages=[d.call_definition("child-guid")]
        )
        result = analyze_definition(parent, lookup=make_lookup([child, parent]))
        assert result.effects["edits_other_notes"] is True
        assert result.effects["add_note_compatible"] is False

    def test_exporting_a_result_an_earlier_skip_can_leave_unset_is_refused(self):
        producer = d.variable("H1", d.text("x"))
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[d.note_query("found", "deck:x", if_empty="skip_block"), producer],
            exports=[d.export("H1", producer)],
        )
        result = analyze_definition(child)
        assert "cannot be guaranteed" in messages(result)

    def test_exporting_a_result_produced_before_the_skip_is_allowed(self):
        # The skip stops the stages *after* it. A result already produced by the time it
        # fires is set whichever way the skip goes, so refusing to export it refuses
        # something that cannot happen -- and says the skip is earlier when it is later.
        producer = d.variable("H1", d.text("x"))
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[producer, d.note_query("found", "deck:x", if_empty="skip_block")],
            exports=[d.export("H1", producer)],
        )
        result = analyze_definition(child)
        assert messages(result) == ""
        assert result.export_types["H1"] == T_TEXT

    def test_exporting_the_skipping_stages_own_result_is_refused(self):
        # When the skip fires, the stage that skipped never declared its result either.
        query = d.note_query("found", "deck:x", if_empty="skip_block")
        child = d.staged(
            "child", guid="child-guid", stages=[query], exports=[d.export("found", query)]
        )
        result = analyze_definition(child)
        assert "cannot be guaranteed" in messages(result)

    def test_a_skipping_read_file_does_not_invalidate_what_came_before(self):
        producer = d.variable("H1", d.text("x"))
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[producer, d.read_file("body", "f.txt", if_missing="skip_block")],
            exports=[d.export("H1", producer)],
        )
        result = analyze_definition(child)
        assert messages(result) == ""

    def test_exporting_a_loop_local_result_is_refused(self):
        inner = d.variable("H1", d.text("x"))
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[
                d.note_query("found", "deck:x"),
                d.for_each_note("found", [inner]),
            ],
            exports=[d.export("H1", inner)],
        )
        result = analyze_definition(child)
        assert "not a root stage" in messages(result)

    def test_calling_an_unknown_definition_is_refused(self):
        parent = d.staged("parent", stages=[d.call_definition("nope")])
        result = analyze_definition(parent, lookup=make_lookup([parent]))
        assert "calls unknown definition" in messages(result)


class TestCallCycles:
    def test_a_direct_cycle_is_found(self):
        a = d.staged("a", guid="a", stages=[d.call_definition("b")])
        b = d.staged("b", guid="b", stages=[d.call_definition("a")])
        assert find_call_cycles([a, b])

    def test_an_indirect_cycle_is_found_with_its_whole_path(self):
        a = d.staged("a", guid="a", stages=[d.call_definition("b")])
        b = d.staged("b", guid="b", stages=[d.call_definition("c")])
        c = d.staged("c", guid="c", stages=[d.call_definition("a")])
        cycles = find_call_cycles([a, b, c])
        assert cycles and set(cycles[0]) == {"a", "b", "c"}

    def test_a_self_call_is_a_cycle(self):
        a = d.staged("a", guid="a", stages=[d.call_definition("a")])
        assert find_call_cycles([a]) == [["a", "a"]]

    def test_a_diamond_is_not_a_cycle(self):
        top = d.staged("top", guid="top", stages=[d.call_definition("l"), d.call_definition("r")])
        left = d.staged("l", guid="l", stages=[d.call_definition("bottom")])
        right = d.staged("r", guid="r", stages=[d.call_definition("bottom")])
        bottom = d.staged("bottom", guid="bottom", stages=[])
        assert find_call_cycles([top, left, right, bottom]) == []

    def test_the_analyser_refuses_a_cycle_too(self):
        a = d.staged("a", guid="a", stages=[d.call_definition("b")])
        b = d.staged("b", guid="b", stages=[d.call_definition("a")])
        result = analyze_definition(a, lookup=make_lookup([a, b]))
        assert "call cycle" in messages(result)

    def test_callers_of_finds_the_definitions_to_revalidate(self):
        a = d.staged("a", guid="a", stages=[d.call_definition("b")])
        b = d.staged("b", guid="b", stages=[])
        c = d.staged("c", guid="c", stages=[])
        assert [definition["guid"] for definition in callers_of("b", [a, b, c])] == ["a"]


class TestStructuralValidation:
    def test_an_unknown_stage_type_makes_the_definition_invalid(self):
        definition = d.staged(stages=[{"guid": "g", "type": "teleport", "enabled": True}])
        problems = [str(problem) for problem in validate_definition_structure(definition)]
        assert any("unknown stage type" in problem for problem in problems)

    def test_a_disabled_stage_neither_runs_nor_declares_its_result(self):
        disabled = d.variable("M", d.text("x"), enabled=False)
        after = d.edit_note("trigger")
        result = analyze([disabled, after])
        assert "M" not in result.scopes[after["guid"]]
        # Its own scope is still recorded, so the editor can show it where it sits.
        assert disabled["guid"] in result.scopes
