"""Tests for the scope, type, effect and call-graph analysis.

The analyser is what both the editor and the runtime ask about a definition, so these are
tests about what a definition *means* before it runs: which bindings a stage can see, what
each result holds, what the definition does to the collection, and which of those things
make it invalid. No collection is involved -- the analyser never runs a query, it only
reads the program.
"""

import pytest

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
        # The recorded scope is what the editor's menu offers, and `{{N}}` is not in it yet.
        # Reading it there is the same mistake as misspelling it, and reported the same way.
        assert "N" not in result.scopes[first["guid"]]
        assert "'N' is not a binding or a runtime value" in messages(result)

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


class TestMigratedNames:
    """Format 1 let a variable be called anything, so a migrated definition keeps its names.

    Both validators run on every save, so the exemption has to hold in both or it holds in
    neither: a user could not even open the definition to rename the variable.
    """

    def build(self, name, migrated):
        definition = d.staged(stages=[d.variable(name, d.text("a"))])
        if migrated:
            definition["migrated_from_format"] = 1
        return definition

    def structure(self, definition):
        return [str(problem) for problem in validate_definition_structure(definition)]

    def test_a_free_text_name_is_kept_on_a_migrated_definition(self):
        definition = self.build("My Word", migrated=True)
        assert self.structure(definition) == []
        assert analyze_definition(definition).problem_messages() == []

    def test_the_same_name_is_refused_on_a_new_definition(self):
        definition = self.build("My Word", migrated=False)
        assert any("identifier" in problem for problem in self.structure(definition))
        assert "identifier" in messages(analyze_definition(definition))

    @pytest.mark.parametrize("migrated", [True, False])
    @pytest.mark.parametrize(
        "name, reason",
        [("trigger", "reserved"), ("__x", "__"), ("", "missing")],
        ids=["reserved", "dunder", "missing"],
    )
    def test_the_runtimes_own_names_are_refused_on_both(self, name, reason, migrated):
        definition = self.build(name, migrated)
        assert any(reason in problem for problem in self.structure(definition))
        assert reason in messages(analyze_definition(definition))


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

    def test_a_code_result_can_be_stored_into_and_reduced_over(self):
        # A code-mode result's type is only known at run time; the executor checks it when
        # the value arrives, the same way a loop over one is accepted here.
        result = analyze([
            d.variable("L", d.code("return ['a']")),
            d.store("L", d.text("b")),
            d.join("L", "joined"),
        ])
        assert result.problem_messages() == []


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


class TestAConditionsPredicate:
    def test_an_empty_text_predicate_is_refused(self):
        # Text counts as true when it is not empty, so an empty text predicate is a
        # condition that never runs its branch -- which nobody writes on purpose. Nothing
        # else caught it: the structural check reads the mode and the process chain only.
        stage = d.condition(d.text(""), [d.edit_note("trigger")])
        result = analyze([stage])
        assert "predicate is empty" in messages(result)
        assert validate_definition_structure(d.staged(stages=[stage])) == []

    def test_a_whitespace_predicate_is_as_empty(self):
        result = analyze([d.condition(d.text("  "), [d.edit_note("trigger")])])
        assert "predicate is empty" in messages(result)

    def test_an_empty_search_predicate_is_refused_too(self):
        result = analyze([
            d.condition(
                d.text(""),
                [d.edit_note("trigger")],
                predicate_kind="note_query",
                predicate_target={"binding": "trigger"},
            )
        ])
        assert "predicate is empty" in messages(result)

    def test_a_reference_is_not_empty(self):
        result = analyze([d.condition(d.text("{{trigger.Word}}"), [d.edit_note("trigger")])])
        assert result.is_valid, messages(result)

    def test_an_empty_code_predicate_is_left_to_the_run(self):
        # Code returning nothing reads false too, but a blank code box is the state every
        # new condition starts in; the analyser has never judged what code will return.
        result = analyze([d.condition(d.code(""), [d.edit_note("trigger")])])
        assert "predicate is empty" not in messages(result)


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

    def test_a_card_action_on_the_trigger_stays_add_note_compatible(self):
        # A note being added has no cards, so the action cannot run -- but it cannot leave
        # anything behind either, so it does not keep the definition out of the add hook.
        effects = self.effects([
            d.edit_note("trigger", card_actions=[d.card_action("CA Vocab", "Recognition")])
        ])
        assert effects["edits_cards"] is True
        assert effects["edits_trigger_cards"] is True
        assert effects["edits_other_cards"] is False
        assert effects["add_note_compatible"] is True

    def test_a_card_action_on_a_queried_note_is_not(self):
        # That note exists, so the flag outlives an add the user then cancels.
        effects = self.effects([
            d.note_query("found", "deck:x"),
            d.for_each_note("found", [
                d.edit_note("note", card_actions=[d.card_action("CA Vocab", "Recognition")])
            ]),
        ])
        assert effects["edits_trigger_cards"] is False
        assert effects["edits_other_cards"] is True
        assert effects["add_note_compatible"] is False

    def test_an_edit_card_stage_is_not(self):
        effects = self.effects([
            d.card_query("cards", "deck:x"),
            d.for_each_card("cards", [
                d.edit_card("card", [d.card_action("CA Vocab", "Recognition")])
            ]),
        ])
        assert effects["edits_trigger_cards"] is False
        assert effects["edits_other_cards"] is True
        assert effects["add_note_compatible"] is False

    def test_files_are_reported_separately_in_each_direction(self):
        effects = self.effects([
            d.read_file("t", "log.txt"),
            d.write_file("log.txt", d.text("{{t}}")),
        ])
        assert (effects["reads_files"], effects["writes_files"]) == (True, True)
        # The written file survives a cancelled add, so writing one is out of the add hook.
        assert effects["add_note_compatible"] is False

    def test_reading_a_file_stays_add_note_compatible(self):
        # Reads leave nothing behind: the principle forbids edits, not knowledge.
        effects = self.effects([d.read_file("t", "log.txt")])
        assert (effects["reads_files"], effects["writes_files"]) == (True, False)
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

    def test_a_callee_flagging_the_callers_own_trigger_stays_compatible(self):
        # The callee's trigger is this definition's trigger, so its card action is the same
        # impossible-but-harmless one it would be written here.
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[
                d.edit_note("trigger", card_actions=[d.card_action("CA Vocab", "Recognition")])
            ],
        )
        parent = d.staged(
            "parent", guid="parent-guid", stages=[d.call_definition("child-guid")]
        )
        result = analyze_definition(parent, lookup=make_lookup([child, parent]))
        assert result.effects["edits_trigger_cards"] is True
        assert result.effects["edits_other_cards"] is False
        assert result.effects["add_note_compatible"] is True

    def test_a_callee_flagging_a_note_the_caller_found_is_not(self):
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[
                d.edit_note("trigger", card_actions=[d.card_action("CA Vocab", "Recognition")])
            ],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.note_query("found", "deck:x"),
                d.for_each_note("found", [d.call_definition("child-guid", trigger="note")]),
            ],
        )
        result = analyze_definition(parent, lookup=make_lookup([child, parent]))
        assert result.effects["edits_trigger_cards"] is False
        assert result.effects["edits_other_cards"] is True
        assert result.effects["add_note_compatible"] is False

    def test_a_call_with_nothing_to_look_the_callee_up_in_assumes_every_card(self):
        parent = d.staged(
            "parent", guid="parent-guid", stages=[d.call_definition("child-guid")]
        )
        effects = analyze_definition(parent).effects
        assert effects["edits_trigger_cards"] is True
        assert effects["edits_other_cards"] is True
        assert effects["add_note_compatible"] is False

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

    def test_disabling_a_call_breaks_the_cycle(self):
        a = d.staged("a", guid="a", stages=[d.call_definition("b", enabled=False)])
        b = d.staged("b", guid="b", stages=[d.call_definition("a")])
        assert find_call_cycles([a, b]) == []

    @pytest.mark.parametrize("disable", ["the call", "its block"])
    def test_a_disabled_call_inside_a_block_does_not_close_a_cycle(self, disable):
        call = d.call_definition("b", enabled=disable != "the call")
        block = d.condition(d.code("return True"), [call], enabled=disable != "its block")
        a = d.staged("a", guid="a", stages=[block])
        b = d.staged("b", guid="b", stages=[d.call_definition("a")])
        assert find_call_cycles([a, b]) == []

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


class TestASearchConditionCanEndTheBlockToo:
    """A migrated copy condition is a third way the rest of the root block may not run.

    `analyze_exports` refuses an export a skip before it can leave unset, and knows about two
    stages that can skip: a query with `if_empty: skip_block` and a read with
    `if_missing: skip_block`. There is a third. A condition carrying
    `unmatched_skips_trigger` -- the marker the migrator writes for format 1's copy
    condition -- does not fall through when it fails to match: the executor raises
    `TriggerSkipped`, and `execute_definition` swallows that for a called definition and goes
    straight to collecting exports.

    So the result a later stage would have declared is simply absent from the exports, and
    the caller's `call_definition` stage, finding nothing under the name it asked for, fails
    the whole run with "exports no 'X'". The analyser is the thing meant to catch that at
    edit time -- that is what §5.9 is for -- and it said nothing, because a condition never
    registered as a stage that can skip. Reachable because a migrated definition can be given
    exports in the editor afterwards, which is the only way it gets any.
    """

    def child(self, producer_first: bool):
        producer = d.variable("H1", d.text("x"))
        gate = d.condition(
            d.text("tag:wanted"),
            [],
            predicate_kind="note_query",
            predicate_target={"binding": "trigger"},
            unmatched_skips_trigger=True,
        )
        stages = [producer, gate] if producer_first else [gate, producer]
        return d.staged("child", guid="child-guid", stages=stages, exports=[d.export("H1", producer)])

    def test_exporting_a_result_it_can_leave_unset_is_refused(self):
        result = analyze_definition(self.child(producer_first=False))

        assert "cannot be guaranteed" in messages(result)

    def test_exporting_a_result_produced_before_it_is_still_allowed(self):
        # The same positional rule the other two skipping stages get: a result already set by
        # the time the condition runs survives it, so refusing that export would refuse
        # something that cannot happen.
        result = analyze_definition(self.child(producer_first=True))

        assert messages(result) == ""
        assert result.export_types["H1"] == T_TEXT

    def test_an_unmarked_search_condition_does_not_skip(self):
        # A condition authored in the editor is an ordinary branch however its predicate is
        # matched: it takes an empty `else` and the definition carries on, so nothing after
        # it is left unset and the export is guaranteed.
        producer = d.variable("H1", d.text("x"))
        gate = d.condition(
            d.text("tag:wanted"),
            [],
            predicate_kind="note_query",
            predicate_target={"binding": "trigger"},
        )
        child = d.staged(
            "child", guid="child-guid", stages=[gate, producer], exports=[d.export("H1", producer)]
        )

        result = analyze_definition(child)

        assert messages(result) == ""

    @staticmethod
    def marked_gate():
        return d.condition(
            d.text("tag:wanted"),
            [],
            predicate_kind="note_query",
            predicate_target={"binding": "note"},
            unmatched_skips_trigger=True,
        )

    def test_a_marked_condition_inside_a_loop_body_ends_the_definition_too(self):
        # `TriggerSkipped` is not scoped to the block it is raised in -- a loop body and a
        # branch catch only `SkipBlock` -- so a marked condition at any depth ends the
        # definition, and the root stage holding it is the one that can leave the results
        # after it unset. Refusing only a root-level condition let this save clean and fail
        # the caller at run time with "exports no 'H1'".
        producer = d.variable("H1", d.text("x"))
        loop = d.for_each_note("found", [self.marked_gate()], name="each found note")
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[d.note_query("found", "deck:x"), loop, producer],
            exports=[d.export("H1", producer)],
        )

        result = analyze_definition(child)

        # The root stage is what the message names: it is what the exports panel shows and
        # where the rest of the block stops.
        assert "'each found note' can skip the rest of the block" in messages(result)

    def test_a_marked_condition_inside_a_branch_ends_it_too(self):
        producer = d.variable("H1", d.text("x"))
        gate = self.marked_gate()
        gate["predicate_target"] = {"binding": "trigger"}
        outer = d.condition(d.code("return True"), [gate])
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[outer, producer],
            exports=[d.export("H1", producer)],
        )

        result = analyze_definition(child)

        assert "cannot be guaranteed" in messages(result)

    def test_a_result_produced_before_the_enclosing_root_stage_is_still_allowed(self):
        producer = d.variable("H1", d.text("x"))
        loop = d.for_each_note("found", [self.marked_gate()])
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[producer, d.note_query("found", "deck:x"), loop],
            exports=[d.export("H1", producer)],
        )

        result = analyze_definition(child)

        assert messages(result) == ""
        assert result.export_types["H1"] == T_TEXT

    def test_a_skip_block_inside_a_loop_body_does_not(self):
        # `skip_block` names the block the stage is in: inside a loop body it ends that
        # iteration and the loop carries on, so the root block runs to the end and the
        # result after the loop is set whichever way the inner query goes.
        producer = d.variable("H1", d.text("x"))
        loop = d.for_each_note(
            "found", [d.note_query("inner", "tag:{{note.Word}}", if_empty="skip_block")]
        )
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[d.note_query("found", "deck:x"), loop, producer],
            exports=[d.export("H1", producer)],
        )

        result = analyze_definition(child)

        assert messages(result) == ""
        assert result.export_types["H1"] == T_TEXT

    def test_an_ordinary_predicate_does_not_skip(self):
        # Only the search kind raises; a plain text or code predicate with no `else` just
        # runs nothing and carries on.
        producer = d.variable("H1", d.text("x"))
        gate = d.condition(d.code("return False"), [])
        child = d.staged(
            "child", guid="child-guid", stages=[gate, producer], exports=[d.export("H1", producer)]
        )

        result = analyze_definition(child)

        assert messages(result) == ""


class TestABareNameThatNamesNothing:
    """The edit-time half of the protection: a reference that resolves to nothing is refused.

    The run refuses a bare name that is neither a binding nor one of the values the run
    itself supplies (§11). Saving a definition that would fail that way is a save the user
    did not mean, so the analyser reports the same thing from the same text -- which is all
    it needs, since a bare name says nothing about the collection.
    """

    def test_a_typo_in_a_write_is_refused(self):
        result = analyze([d.edit_note("trigger", [d.write("Note", d.text("{{Wrod}}"))])])
        assert "'Wrod' is not a binding or a runtime value" in messages(result)

    def test_a_typo_in_a_query_is_refused(self):
        result = analyze([d.note_query("found", "Word:{{Wrod}}")])
        assert "'Wrod' is not a binding or a runtime value" in messages(result)

    def test_a_typo_in_code_is_refused_too(self):
        # Code is interpolated before it is executed, so its references are the same
        # references; what the code then does with them is still left to the run.
        result = analyze([d.variable("M", d.code("return '{{Wrod}}'"))])
        assert "'Wrod' is not a binding or a runtime value" in messages(result)

    def test_a_runtime_value_is_not_a_typo(self):
        # No stage declares these, so nothing is in scope under the name; the run supplies
        # them and the interpolation menu offers them.
        result = analyze([
            d.note_query("found", "deck:x"),
            d.for_each_note("found", [
                d.edit_note("note", [
                    d.write(
                        "Note",
                        d.text("{{__Query_Note_Index}}/{{__Target_Notes_Count}}"),
                    )
                ]),
            ]),
        ])
        assert result.is_valid, messages(result)

    def test_a_cloze_marker_is_not_a_reference(self):
        result = analyze([
            d.edit_note("trigger", [d.write("Note", d.text("{{c1::{{trigger.Word}}}}"))]),
        ])
        assert result.is_valid, messages(result)


class TestReferencesInsideACloze:
    """The run resolves every reference a cloze wraps, so saving checks every one of them.

    A reference ends at the first `}}`, so read without taking the cloze apart first,
    `{{c1::{{Wrod}}}}` is one "reference" named `c1::{{Wrod` -- which looks like a cloze
    marker and was skipped whole, typo included.
    """

    def write(self, text):
        return analyze([d.edit_note("trigger", [d.write("Note", d.text(text))])])

    def test_a_typo_inside_a_cloze_is_refused(self):
        result = self.write("{{c1::{{Wrod}}}}")
        assert "'Wrod' is not a binding or a runtime value" in messages(result)

    def test_so_is_the_first_of_several(self):
        result = self.write("{{c1::{{Wrod}} and {{Wrod2}}}}")
        assert "'Wrod'" in messages(result)
        assert "'Wrod2'" in messages(result)

    def test_and_one_in_a_cloze_inside_a_cloze(self):
        result = self.write("{{c1::a {{c2::{{Wrod}}}}}}")
        assert "'Wrod' is not a binding or a runtime value" in messages(result)

    def test_a_field_inside_a_cloze_is_checked_against_the_trigger_fields(self):
        result = analyze_definition(
            d.staged(stages=[
                d.edit_note("trigger", [d.write("Note", d.text("{{c1::{{trigger.Wrod}}}}"))]),
            ]),
            known_fields={"trigger": {"Word", "Note"}},
        )
        assert "'Wrod' is not a field or value" in messages(result)

    def test_a_cloze_that_is_never_closed_is_refused_as_that(self):
        # Rather than as the binding `c1::abc {{trigger` that splitting the swallowed
        # reference on its dot would name.
        result = self.write("{{c1::abc {{trigger.Word}}")
        assert messages(result).endswith(
            "field 'Note' has a cloze '{{c1::' that is never closed"
        ), messages(result)

    def test_so_is_any_other_brace_that_is_never_closed(self):
        result = self.write("{{abc {{trigger.Word}}")
        assert messages(result).endswith(
            "field 'Note' has a '{{' that is never closed, before 'abc'"
        ), messages(result)


class TestAReferenceThroughACardBinding:
    """`{{card.X}}` names a card property or a card value, and nothing else resolves.

    The card binding is the card, so a card value takes no card type in front: the run
    reads the key off the card in hand and refuses anything else, once per card.
    """

    def read(self, rest):
        return analyze([
            d.card_query("cards", "deck:x"),
            d.for_each_card("cards", [
                d.edit_note("note", [d.write("Note", d.text("{{card." + rest + "}}"))]),
            ]),
        ])

    @pytest.mark.parametrize("rest", [
        "deck_name",
        "template_name",
        "created",
        "total_review_time",
        "__Card_Due",
        "__Card_Custom_Data_Prop==seen",
    ])
    def test_a_property_or_a_value_is_fine(self, rest):
        result = self.read(rest)
        assert result.is_valid, messages(result)

    @pytest.mark.parametrize("rest", [
        "template_nme",
        "__Card_Do",
        # The spelling a note offers for a cloze card's value. Through the card binding
        # there is no card type to name.
        "Cloze 1__Card_Due",
    ])
    def test_anything_else_is_refused(self, rest):
        result = self.read(rest)
        assert f"'{rest}' is not a card property or card value" in messages(result)


#: The default for the helper below, so a test can pass `None` and mean it.
_TRIGGER_FIELDS_OF_THE_TEST = {"trigger": {"Word", "Meaning", "Note"}}


class TestTheFieldsTheEditorKnowsAbout:
    """What a `trigger.X` reference can be checked against at edit time.

    A definition names its trigger note types, so the editor can hand the analyser their
    field lists. Nothing else can be checked: a binding from a query or a loop over one
    holds whatever the query matched.
    """

    def analyze(self, stages, known_fields=_TRIGGER_FIELDS_OF_THE_TEST):
        return analyze_definition(d.staged(stages=stages), known_fields=known_fields)

    def write(self, text):
        return d.edit_note("trigger", [d.write("Note", d.text(text))])

    def test_a_field_the_trigger_note_types_do_not_have_is_refused(self):
        result = self.analyze([self.write("{{trigger.Nonexistent}}")])
        assert "'Nonexistent'" in messages(result)

    def test_a_field_they_do_have_is_fine(self):
        result = self.analyze([self.write("{{trigger.Word}}")])
        assert result.is_valid, messages(result)

    def test_the_spelling_is_matched_the_way_the_run_matches_it(self):
        # `get_from_note_fields` lowercases both sides, so a field reference is
        # case-insensitive at run time and has to be here too.
        result = self.analyze([self.write("{{trigger.word}}")])
        assert result.is_valid, messages(result)

    def test_a_note_value_is_not_a_field(self):
        result = self.analyze([self.write("{{trigger.__Note_Type_ID}}")])
        assert result.is_valid, messages(result)

    def test_a_card_value_read_through_a_card_type_name_is_not_either(self):
        result = self.analyze([self.write("{{trigger.Recognition__Card_Due}}")])
        assert result.is_valid, messages(result)

    def test_a_card_value_without_a_card_type_name_is_not_either(self):
        result = self.analyze([self.write("{{trigger.__Card_Due}}")])
        assert result.is_valid, messages(result)

    def test_a_note_value_key_that_does_not_exist_is_refused(self):
        # `__Note_Type` is spelled like a note value and is not one -- the key is
        # `__Note_Type_ID`. The run says so per note; the shape alone cannot tell them
        # apart, so the key itself is what is checked.
        result = self.analyze([self.write("{{trigger.__Note_Type}}")])
        assert "'__Note_Type'" in messages(result)

    def test_a_card_value_key_that_does_not_exist_is_refused(self):
        result = self.analyze([self.write("{{trigger.Recognition__Card_Do}}")])
        assert "'Recognition__Card_Do'" in messages(result)

    def test_a_value_that_takes_an_argument_is_read_without_it(self):
        # `__Note_Has_Tag==x` and `__Card_Last_Reps==5` carry their argument after the
        # separator; the key the lists hold ends with it.
        result = self.analyze([
            self.write("{{trigger.__Note_Has_Tag==done}} {{trigger.Recognition__Card_Last_Reps==3}}")
        ])
        assert result.is_valid, messages(result)

    def test_a_binding_a_query_produced_is_not_checked(self):
        result = self.analyze([
            d.note_query("found", "deck:x"),
            d.for_each_note("found", [
                d.edit_note("note", [d.write("Note", d.text("{{note.Nonexistent}}"))]),
            ]),
        ])
        assert result.is_valid, messages(result)

    def test_without_the_field_lists_only_the_head_is_checked(self):
        # `refresh_effects` and everything else outside the editor analyses the same
        # definition with no note types to hand, and must not start refusing it.
        result = self.analyze([self.write("{{trigger.Nonexistent}}")], known_fields=None)
        assert result.is_valid, messages(result)

    def test_no_trigger_note_type_chosen_yet_checks_nothing(self):
        result = self.analyze([self.write("{{trigger.Nonexistent}}")], known_fields={})
        assert result.is_valid, messages(result)
