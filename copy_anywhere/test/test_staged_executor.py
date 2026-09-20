"""Tests for what format 2 can express that format 1 could not.

The characterization suite proves migrated definitions still behave the way they did. This
file is the other half: stages an editor will be able to author but the old model had no
shape for -- several queries, loops, lists and reductions, branches, explicit note and card
targets, file reads, and calls into other definitions with declared exports.

Everything runs against a real collection through `copy_for_single_trigger_note`, the same
entry point the browser, the hooks and the sync path use.
"""

import json

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import CLOZE, KANJI, VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note
from copy_anywhere.logic.definition_migration import migrate_definition_v1_to_v2
from copy_anywhere.logic.definition_schema import SYNTAX_VERSION_CURRENT
from copy_anywhere.logic.execution.commit import PreviewCommitter
from copy_anywhere.logic.execution.context import ExecutionSession
from copy_anywhere.logic.execution.runner import run_definition_for_trigger_note
from copy_anywhere.logic.flow_analysis import analyze_definition


@pytest.fixture
def note(col):
    return real_anki.add_note(
        col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat", "Freq": "5"}
    )


def run(definition, note, **kwargs):
    copied: list = []
    ok = copy_for_single_trigger_note(
        definition, note, copied_into_notes=copied, **kwargs
    )
    return ok, copied


class TestVariables:
    def test_a_later_variable_can_consume_an_earlier_one(self, note):
        # The whole point of stages: format 1 evaluated every variable against the trigger
        # note alone, so this was impossible to write.
        definition = d.staged(stages=[
            d.variable("first", d.text("{{trigger.Word}}")),
            d.variable("second", d.text("<{{first}}>")),
            d.edit_note("trigger", [d.write("Note", d.text("{{second}}"))]),
        ])
        assert run(definition, note)[0] is True
        assert note["Note"] == "<neko>"

    def test_a_code_variable_receives_the_earlier_bindings(self, note):
        definition = d.staged(stages=[
            d.variable("first", d.text("abc")),
            d.variable("second", d.code("return first.upper()")),
            d.edit_note("trigger", [d.write("Note", d.text("{{second}}"))]),
        ])
        assert run(definition, note)[0] is True
        assert note["Note"] == "ABC"


class TestQueries:
    @pytest.fixture
    def others(self, col):
        return [
            real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A", "Freq": "1"}),
            real_anki.add_note(col, VOCAB, {"Word": "b", "Meaning": "B", "Freq": "9"}),
        ]

    def test_a_second_query_can_be_built_from_the_first_ones_result(
        self, col, note, others, logger
    ):
        definition = d.staged(stages=[
            d.note_query("first", "Word:a"),
            d.for_each_note("first", [d.variable("meaning", d.text("{{note.Meaning}}"))]),
            # The loop variable is local, so the second query is built from a value stored
            # outside it.
            d.list_variable("meanings"),
            d.for_each_note("first", [d.store("meanings", d.text("{{note.Meaning}}"))]),
            d.join("meanings", "joined"),
            d.note_query("second", "Meaning:{{joined}}"),
            d.for_each_note(
                "second", [d.edit_note("note", [d.write("Note", d.text("second pass"))])]
            ),
        ])
        ok, copied = run(definition, note)
        assert ok is True, logger.errors
        assert [n["Word"] for n in copied] == ["a"]

    def test_selection_first_takes_them_in_search_order(self, col, note, others):
        definition = d.staged(stages=[
            d.note_query("found", '"Word:a" OR "Word:b"', strategy="first", count=1),
            d.list_variable("words"),
            d.for_each_note("found", [d.store("words", d.text("{{note.Word}}"))]),
            d.join("words", "joined", "+"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        run(definition, note)
        assert note["Note"] in ("a", "b")
        assert "+" not in note["Note"]

    def test_a_sort_field_orders_the_result(self, col, note, others):
        definition = d.staged(stages=[
            d.note_query(
                "found",
                '"Word:a" OR "Word:b"',
                selection={
                    "strategy": "all",
                    "count": None,
                    "sort_field": "Freq",
                    "sort_order": "descending",
                    "sort_numeric": True,
                },
            ),
            d.list_variable("words"),
            d.for_each_note("found", [d.store("words", d.text("{{note.Word}}"))]),
            d.join("words", "joined", "+"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        run(definition, note)
        assert note["Note"] == "b+a"

    def test_query_membership_ignores_edits_that_have_not_been_saved(
        self, col, note, others
    ):
        # The search reads the persisted collection, so a value a stage has only written in
        # memory cannot change which notes come back. Without that rule, whether a query
        # matched would depend on when a flush happened to run.
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Word", d.text("a"))]),
            d.note_query("found", "Word:a"),
            d.list_variable("ids"),
            d.for_each_note("found", [d.store("ids", d.text("{{note.Meaning}}"))]),
            d.join("ids", "joined", "+"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        run(definition, note)
        assert note["Word"] == "a"
        assert note["Note"] == "A"


class TestPendingEditsAreVisible:
    def test_a_later_stage_reads_what_an_earlier_one_wrote(self, note):
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("first"))]),
            d.edit_note("trigger", [d.write("Meaning", d.text("{{trigger.Note}} second"))]),
        ])
        run(definition, note)
        assert note["Meaning"] == "first second"

    def test_one_stage_still_reads_its_own_entry_snapshot_so_a_swap_works(self, note):
        definition = d.staged(stages=[
            d.edit_note(
                "trigger",
                [
                    d.write("Word", d.text("{{trigger.Meaning}}")),
                    d.write("Meaning", d.text("{{trigger.Word}}")),
                ],
            )
        ])
        run(definition, note)
        assert (note["Word"], note["Meaning"]) == ("cat", "neko")

    def test_the_trigger_can_be_edited_before_and_after_a_loop(self, col, note):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("before"))]),
            d.note_query("found", "Word:a"),
            d.list_variable("seen"),
            d.for_each_note("found", [d.store("seen", d.text("{{trigger.Note}}"))]),
            d.join("seen", "joined"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}/after"))]),
        ])
        run(definition, note)
        assert note["Note"] == "before/after"

    def test_the_trigger_and_the_queried_notes_can_be_edited_in_one_definition(
        self, col, note, logger
    ):
        # Format 1 assigned one global source role and one destination role, so this needed
        # two definitions and an ordering between them.
        other = real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        definition = d.staged(stages=[
            d.note_query("found", "Word:a"),
            d.for_each_note(
                "found", [d.edit_note("note", [d.write("Note", d.text("from trigger"))])]
            ),
            d.edit_note("trigger", [d.write("Note", d.text("edited too"))]),
        ])
        ok, copied = run(definition, note)
        assert ok is True, logger.errors
        assert note["Note"] == "edited too"
        assert other["Note"] == ""  # nothing is written to the database here
        assert sorted(n.id for n in copied) == sorted([note.id, other.id])

    def test_two_references_to_one_note_converge_on_one_working_note(
        self, col, note, logger
    ):
        other = real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        definition = d.staged(stages=[
            d.note_query("first", "Word:a"),
            d.note_query("second", "Meaning:A"),
            d.for_each_note("first", [d.edit_note("note", [d.write("Note", d.text("one"))])]),
            d.for_each_note(
                "second",
                [d.edit_note("note", [d.write("Reading", d.text("{{note.Note}} two"))])],
            ),
        ])
        ok, copied = run(definition, note)
        assert ok is True, logger.errors
        assert {n.id for n in copied} == {other.id}
        # The second loop read what the first wrote, so both edits landed on one object.
        written = next(n for n in copied if n.id == other.id)
        assert (written["Note"], written["Reading"]) == ("one", "one two")


class TestListsAndReductions:
    @pytest.fixture
    def three(self, col):
        return [
            real_anki.add_note(col, VOCAB, {"Word": f"w{i}", "Meaning": f"M{i}", "Freq": str(i)})
            for i in range(1, 4)
        ]

    def test_appends_keep_the_loop_order(self, note, three):
        definition = d.staged(stages=[
            d.note_query(
                "found",
                "Word:w*",
                selection={
                    "strategy": "all",
                    "count": None,
                    "sort_field": "Freq",
                    "sort_order": "ascending",
                    "sort_numeric": True,
                },
            ),
            d.list_variable("words"),
            d.for_each_note("found", [d.store("words", d.text("{{note.Word}}"))]),
            d.join("words", "joined", "-"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        run(definition, note)
        assert note["Note"] == "w1-w2-w3"

    def test_the_loop_index_and_count_are_available(self, note, three):
        definition = d.staged(stages=[
            d.note_query("found", "Word:w*"),
            d.list_variable("marks"),
            d.for_each_note("found", [d.store("marks", d.text("{{index}}/{{count}}"))]),
            d.join("marks", "joined", " "),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        run(definition, note)
        assert note["Note"] == "1/3 2/3 3/3"

    def test_an_empty_list_reduces_to_the_initial_value(self, note):
        definition = d.staged(stages=[
            d.list_variable("empty"),
            d.reduce("empty", "total", d.code("return accumulator + item"), initial=d.text("0")),
            d.edit_note("trigger", [d.write("Note", d.text("{{total}}"))]),
        ])
        run(definition, note)
        assert note["Note"] == "0"

    def test_a_code_reducer_folds_in_order(self, note, three):
        definition = d.staged(stages=[
            d.note_query(
                "found",
                "Word:w*",
                selection={
                    "strategy": "all",
                    "count": None,
                    "sort_field": "Freq",
                    "sort_order": "ascending",
                    "sort_numeric": True,
                },
            ),
            d.list_variable("words"),
            d.for_each_note("found", [d.store("words", d.text("{{note.Word}}"))]),
            d.reduce("words", "folded", d.code("return accumulator + item[-1]")),
            d.edit_note("trigger", [d.write("Note", d.text("{{folded}}"))]),
        ])
        run(definition, note)
        assert note["Note"] == "123"

    def test_a_reducer_that_cannot_add_its_accumulator_fails_the_definition(
        self, note, three
    ):
        definition = d.staged(stages=[
            d.note_query("found", "Word:w*"),
            d.list_variable("words"),
            d.for_each_note("found", [d.store("words", d.text("{{note.Word}}"))]),
            d.reduce("words", "folded", d.code("return accumulator + 1")),
            d.edit_note("trigger", [d.write("Note", d.text("{{folded}}"))]),
        ])
        ok, copied = run(definition, note)
        assert ok is False
        assert copied == []

    def test_a_list_built_in_code_can_be_stored_into_and_reduced(self, note):
        definition = d.staged(stages=[
            d.variable("words", d.code("return ['a']")),
            d.store("words", d.text("b")),
            d.join("words", "joined", "-"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        assert run(definition, note)[0] is True
        assert note["Note"] == "a-b"

    def test_storing_into_a_code_result_that_holds_text_is_a_stage_error(self, note, logger):
        # The analyser lets a code result through as a store target because its type is only
        # known now; this is the other half of that contract.
        definition = d.staged(stages=[
            d.variable("words", d.code("return 'not a list'")),
            d.store("words", d.text("b")),
            d.edit_note("trigger", [d.write("Note", d.text("{{words}}"))]),
        ])
        ok, copied = run(definition, note)
        assert ok is False
        assert copied == []
        assert logger.has_error("store target is not a list")

    def test_reducing_over_a_code_result_that_holds_text_is_a_stage_error(self, note, logger):
        definition = d.staged(stages=[
            d.variable("words", d.code("return 'not a list'")),
            d.join("words", "joined", "-"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        ok, copied = run(definition, note)
        assert ok is False
        assert copied == []
        assert logger.has_error("reduce input is not a list")

    def test_a_loop_local_variable_does_not_escape(self, note, three):
        definition = d.staged(stages=[
            d.note_query("found", "Word:w*"),
            d.for_each_note("found", [d.variable("inner", d.text("{{note.Word}}"))]),
            d.edit_note("trigger", [d.write("Note", d.text("{{inner}}"))]),
        ])
        run(definition, note)
        # `inner` is not a binding out here, so the reference falls through and resolves to
        # nothing rather than to the last iteration's value.
        assert note["Note"] == ""


class TestConditions:
    def test_the_then_branch_runs_when_the_predicate_is_true(self, note):
        definition = d.staged(stages=[
            d.condition(
                d.code("return True"),
                [d.edit_note("trigger", [d.write("Note", d.text("yes"))])],
                [d.edit_note("trigger", [d.write("Note", d.text("no"))])],
            )
        ])
        run(definition, note)
        assert note["Note"] == "yes"

    def test_the_else_branch_runs_when_it_is_false(self, note):
        definition = d.staged(stages=[
            d.condition(
                d.code("return False"),
                [d.edit_note("trigger", [d.write("Note", d.text("yes"))])],
                [d.edit_note("trigger", [d.write("Note", d.text("no"))])],
            )
        ])
        run(definition, note)
        assert note["Note"] == "no"

    def test_a_skip_inside_a_branch_ends_the_branch_and_nothing_more(self, note):
        # "Stop running the rest of this block" is what the editor calls this policy, and
        # the block a stage is in is its branch. It used to leave the branch, leave the root
        # block and end the definition, so every stage after the condition was skipped
        # whenever the branch's query came back empty.
        definition = d.staged(stages=[
            d.condition(
                d.code("return True"),
                [
                    d.note_query("none", "tag:nothing-has-this", if_empty="skip_block"),
                    d.edit_note("trigger", [d.write("Meaning", d.text("reached"))]),
                ],
            ),
            d.edit_note("trigger", [d.write("Note", d.text("after"))]),
        ])
        assert run(definition, note)[0] is True
        # The rest of the branch is skipped...
        assert note["Meaning"] == "cat"
        # ...and the definition carries on from the stage after the condition, which is what
        # the analyser assumes when it accepts an export of a result declared there (§5.9).
        assert note["Note"] == "after"

    def test_a_skip_in_the_else_branch_behaves_the_same_way(self, note):
        definition = d.staged(stages=[
            d.condition(
                d.code("return False"),
                [],
                [d.note_query("none", "tag:nothing-has-this", if_empty="skip_block")],
            ),
            d.edit_note("trigger", [d.write("Note", d.text("after"))]),
        ])
        assert run(definition, note)[0] is True
        assert note["Note"] == "after"

    def test_a_skip_at_the_root_still_ends_the_definition(self, note):
        definition = d.staged(stages=[
            d.note_query("none", "tag:nothing-has-this", if_empty="skip_block"),
            d.edit_note("trigger", [d.write("Note", d.text("after"))]),
        ])
        assert run(definition, note)[0] is True
        assert note["Note"] == ""

    def test_a_text_predicate_is_true_when_it_produced_something(self, note):
        definition = d.staged(stages=[
            d.condition(
                d.text("{{trigger.Freq}}"),
                [d.edit_note("trigger", [d.write("Note", d.text("has freq"))])],
            )
        ])
        run(definition, note)
        assert note["Note"] == "has freq"

    def test_an_empty_text_predicate_is_false(self, note):
        note["Freq"] = ""
        definition = d.staged(stages=[
            d.condition(
                d.text("{{trigger.Freq}}"),
                [d.edit_note("trigger", [d.write("Note", d.text("has freq"))])],
            )
        ])
        run(definition, note)
        assert note["Note"] == ""


class TestASearchConditionThatDoesNotMatch:
    """Where a non-matching search condition stops, once it is not the outermost stage.

    A condition whose predicate is an Anki search carries `predicate_kind: note_query`, and a
    non-match there does not take the `else` branch: it raises `TriggerSkipped`, which
    discards everything queued for this trigger note and reports the note as skipped. That is
    right for the one shape the migrator builds -- format 1 evaluated the copy condition
    before anything ran, so the migrated stage wraps the entire definition and there is
    nothing queued yet to lose.

    It is not right anywhere else, and anywhere else is now authorable: the condition editor
    offers "Match it as an Anki search", so the same marker can sit after other stages or
    inside a loop body. There the skip reaches out of the block it is in, past stages that
    already wrote, and throws their work away -- which no other stage in format 2 can do, and
    which the definition reports as a success.
    """

    def test_a_write_before_it_is_not_thrown_away(self, col, note, logger):
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("written"))]),
            d.condition(
                d.text("tag:nothing-has-this"),
                [d.edit_note("trigger", [d.write("Meaning", d.text("matched"))])],
                predicate_kind="note_query",
                predicate_target={"binding": "trigger"},
            ),
        ])
        ok, copied = run(definition, note)

        assert ok is True, logger.errors
        # The branch did not run...
        assert note["Meaning"] == "cat"
        # ...but the stage before the condition did, and a stage that ran is committed.
        assert note["Note"] == "written"
        assert [n.id for n in copied] == [note.id]

    def test_it_ends_its_block_rather_than_the_definition(self, note, logger):
        definition = d.staged(stages=[
            d.condition(
                d.text("tag:nothing-has-this"),
                [d.edit_note("trigger", [d.write("Meaning", d.text("matched"))])],
                predicate_kind="note_query",
                predicate_target={"binding": "trigger"},
            ),
            d.edit_note("trigger", [d.write("Note", d.text("after"))]),
        ])
        ok, _ = run(definition, note)

        assert ok is True, logger.errors
        assert note["Meaning"] == "cat"
        assert note["Note"] == "after"

    def test_inside_a_loop_it_stops_at_the_iteration(self, col, note, logger):
        # The sharpest case: one non-matching note out of two silently discards the write the
        # other one earned, so the definition writes nothing at all and reports no problem.
        keep = real_anki.add_note(col, VOCAB, {"Word": "keep"}, tags=["pool", "wanted"])
        drop = real_anki.add_note(col, VOCAB, {"Word": "drop"}, tags=["pool"])
        definition = d.staged(stages=[
            d.note_query("found", "tag:pool"),
            d.for_each_note("found", [
                d.condition(
                    d.text("tag:wanted"),
                    [d.edit_note("note", [d.write("Note", d.text("kept"))])],
                    predicate_kind="note_query",
                    predicate_target={"binding": "note"},
                ),
            ]),
        ])
        ok, copied = run(definition, note)

        assert ok is True, logger.errors
        assert [n["Note"] for n in copied] == ["kept"]
        assert [n.id for n in copied] == [keep.id]
        assert col.get_note(drop.id)["Note"] == ""


class TestAMigratedExpressionEditedIntoTheCurrentSyntax:
    """What the stage editor now saves when a migrated expression's text is changed.

    The editor promotes `syntax_version` along with the text (the UI suite pins that); this
    is the executor's half of the contract, that the promotion is all it takes for the
    format-2 reference the editor's menu offered to resolve.
    """

    def test_the_reference_the_menu_offers_reaches_the_field(self, col):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = migrate_definition_v1_to_v2(
            d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")])
        )
        write = definition["stages"][0]["fields"][0]["value"]
        write["text"] = "{{trigger.Word}}"
        write["syntax_version"] = SYNTAX_VERSION_CURRENT

        ok, copied = run(definition, note)

        assert ok is True
        assert copied == [note]
        assert note["Note"] == "neko"


class TestASearchConditionsPredicate:
    """Which interpolation resolves a predicate matched as an Anki search.

    Every other expression in the executor goes through `evaluate_raw`, whose one job is to
    ask `expression_is_legacy_syntax` and send a migrated expression to
    `get_field_values_from_notes` and an editor-authored one to `resolve_references`. The
    search predicate did not ask: it called format-1 `interpolate_from_text` outright.

    So a reference the editor itself offers -- the stage's interpolation menu is built from
    its recorded input scope, and the analyser validates what it produces -- was not a field
    name format 1 knew, and was dropped. Three different outcomes, none of them the one the
    definition asked for: a predicate narrowed by a reference silently matched nothing, a
    broad predicate narrowed by one silently matched everything the broad half did, and a
    predicate that was only a reference failed with "missing fields: trigger.note", which
    blames the note for lacking a field by that name rather than naming the real cause.

    The middle case is the one that writes: the branch runs for notes the condition was
    written to exclude.
    """

    def gate(self, predicate_text, **extra):
        return d.condition(
            d.text(predicate_text),
            [d.edit_note("trigger", [d.write("Note", d.text("matched"))])],
            predicate_kind="note_query",
            predicate_target={"binding": "trigger"},
            **extra,
        )

    def test_a_reference_narrowing_the_search_is_resolved(self, col):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        ok, _copied = run(d.staged(stages=[self.gate("Word:{{trigger.Word}}")]), note)

        assert ok is True
        assert note["Note"] == "matched"

    def test_a_reference_the_search_narrows_by_is_not_dropped(self, col):
        # `-tag:skip` alone matches this note; the reference is the half that must exclude
        # it. Dropping the reference is what turns "do not run" into a write.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Note": "tag:special"})

        ok, _copied = run(d.staged(stages=[self.gate("-tag:skip {{trigger.Note}}")]), note)

        assert ok is True
        assert note["Note"] == "tag:special"

    def test_a_reference_that_is_the_whole_search_is_resolved(self, col):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Note": "Word:neko"})

        ok, _copied = run(d.staged(stages=[self.gate("{{trigger.Note}}")]), note)

        assert ok is True
        assert note["Note"] == "matched"

    def test_a_reference_to_nothing_in_scope_says_so(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})

        ok, _copied = run(d.staged(stages=[self.gate("Word:{{nowhere.Word}}")]), note)

        assert ok is False
        assert logger.has_error("nowhere")

    def test_a_predicate_that_resolves_to_nothing_is_refused(self, col):
        # The guard that must survive any rewrite: an empty query reaches
        # `find_notes(" nid:<id>")`, which matches the trigger whatever it says, so every
        # condition would read as true.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Note": ""})

        ok, _copied = run(d.staged(stages=[self.gate("{{trigger.Note}}")]), note)

        assert ok is False
        assert note["Note"] == ""

    def test_whitespace_is_as_empty_as_nothing(self, col):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Note": "   "})

        ok, _copied = run(d.staged(stages=[self.gate("{{trigger.Note}}")]), note)

        assert ok is False

    def test_a_migrated_predicate_still_reads_the_note_the_old_way(self, col):
        # The path this must not disturb: a migrated copy condition carries
        # `syntax_version: 1` and spells its reference `{{Word}}`, with no binding.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "ran")],
            copy_condition_query="Word:{{Word}}",
        )

        ok, copied = run(definition, note)

        assert ok is True
        assert [n["Note"] for n in copied] == ["ran"]

    def test_a_migrated_predicate_that_does_not_match_still_skips(self, col):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "ran")],
            copy_condition_query="Word:not-this-one",
        )

        ok, copied = run(definition, note)

        assert ok is True
        assert copied == []

    def test_a_migrated_predicate_resolving_to_whitespace_is_refused(self, col, logger):
        # `find_notes("  nid:<id>")` matches the note, so a migrated condition that is one
        # reference to a field holding a space read true and ran the copy. It is as empty as
        # a field holding nothing, and it is refused. Migration names the note the reference
        # meant, so the refusal is the one message every predicate gets (§11).
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Note": "   "})
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Meaning", "ran")],
            copy_condition_query="{{Note}}",
        )

        ok, copied = run(definition, note)

        assert ok is False
        assert copied == []
        assert logger.errors == [
            "Error in copy fields: Condition query '{{trigger.Note}}' resolved to nothing"
            f" for note id {note.id}"
        ]


class TestABareNameThatNamesNothing:
    """The protection format 1 never had: a reference that resolves to nothing is an error.

    Format 1 read any bare name off whichever note the stage happened to be holding and
    wrote an empty string when it found none, so a typo ran a truncated search, or filled a
    field with nothing, and the definition reported success either way. Format 2 names its
    bindings, so a bare name is a binding, or one of the two values the run itself supplies,
    or a mistake -- and the stage says which name it could not resolve (§11).
    """

    def test_a_typo_in_a_field_write_fails_the_stage_and_writes_nothing(self, note, logger):
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("{{trigger.Word}}/{{Wrod}}"))]),
        ])

        ok, copied = run(definition, note)

        assert ok is False
        assert copied == []
        assert logger.has_error("'Wrod' is not a binding or a runtime value")
        assert note["Note"] == ""

    def test_a_typo_in_a_query_fails_the_stage_and_the_loop_never_runs(
        self, col, note, logger
    ):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        definition = d.staged(stages=[
            d.note_query("found", "Word:{{Wrod}}"),
            d.for_each_note("found", [
                d.edit_note("note", [d.write("Note", d.text("reached"))]),
            ]),
        ])

        ok, copied = run(definition, note)

        assert ok is False
        assert copied == []
        assert logger.has_error("'Wrod' is not a binding or a runtime value")

    def test_a_typo_in_a_condition_fails_the_stage_and_neither_branch_runs(
        self, note, logger
    ):
        definition = d.staged(stages=[
            d.condition(
                d.text("Word:{{Wrod}}"),
                [d.edit_note("trigger", [d.write("Note", d.text("then"))])],
                [d.edit_note("trigger", [d.write("Note", d.text("else"))])],
                predicate_kind="note_query",
                predicate_target={"binding": "trigger"},
            ),
        ])

        ok, copied = run(definition, note)

        assert ok is False
        assert copied == []
        assert logger.has_error("'Wrod' is not a binding or a runtime value")
        assert note["Note"] == ""

    def test_a_typo_in_a_file_name_fails_the_stage_and_writes_no_file(
        self, note, media_dir, logger
    ):
        definition = d.staged(stages=[d.write_file("{{Wrod}}.txt", d.text("content"))])

        ok, _copied = run(definition, note)

        assert ok is False
        assert logger.has_error("'Wrod' is not a binding or a runtime value")
        assert list(media_dir.iterdir()) == []

    def test_the_two_runtime_values_are_not_typos(self, col, note, logger):
        # `__Target_Notes_Count` and `__Query_Note_Index` are supplied by the run rather
        # than declared by a stage, so no binding holds them; they are what a bare name is
        # asked against once the bindings have said no.
        for word in ("a", "b"):
            real_anki.add_note(col, VOCAB, {"Word": word, "Meaning": word.upper()})
        definition = d.staged(stages=[
            d.note_query("found", "Word:a OR Word:b"),
            d.for_each_note("found", [
                d.edit_note("note", [
                    d.write(
                        "Note", d.text("{{__Query_Note_Index}}/{{__Target_Notes_Count}}")
                    ),
                ]),
            ]),
        ])

        ok, copied = run(definition, note)

        assert ok is True, logger.errors
        assert sorted(one["Note"] for one in copied) == ["1/2", "2/2"]


class TestCardsAndCardStages:
    def test_a_card_query_and_card_loop_move_each_card_on_its_own(self, col, note, logger):
        definition = d.staged(stages=[
            d.card_query("cards", f"nid:{note.id}"),
            d.for_each_card(
                "cards",
                [
                    d.edit_card(
                        "card",
                        [
                            {
                                "guid": "ca",
                                "card_type_name": "",
                                "change_deck": "JP vocab",
                                "set_flag": None,
                                "suspend": None,
                                "bury": None,
                                "set_desired_retention": None,
                                "action_code": None,
                                "use_code": False,
                            }
                        ],
                    )
                ],
            ),
        ])
        cards: dict = {}
        ok = copy_for_single_trigger_note(
            definition, note, copied_into_cards_dict=cards
        )
        assert ok is True, logger.errors
        # No card type selector: the action applied to exactly the card in hand, and both of
        # this note's cards went round the loop.
        target = col.decks.id_for_name("JP vocab")
        assert len(cards) == 2
        assert all(card.did == target for card in cards.values())

    def test_a_card_loop_binds_the_cards_note_too(self, col, note, logger):
        definition = d.staged(stages=[
            d.card_query("cards", f"nid:{note.id}", strategy="first", count=1),
            d.for_each_card(
                "cards",
                [d.edit_note("note", [d.write("Note", d.text("{{card.template_name}}"))])],
            ),
        ])
        assert run(definition, note)[0] is True, logger.errors
        assert note["Note"] in ("Recognition", "Recall")


class TestACardValueReadThroughACardBinding:
    """`{{card.__Card_Due}}` and friends, which name a card value through the card binding.

    Format 1 had no card binding. It had a note, so a card value was keyed by card template
    name on that note -- `{{Recognition__Card_Due}}` -- and `get_from_note_fields` looks it
    up in a per-note dict keyed that way. `_card_reference` reused that path by throwing away
    the card it was handed and rebuilding the key from `card.template_name`, which answers a
    question format 2 no longer has to ask: the binding already names one card.

    Two things fall out of the indirection, and the editor's own interpolation menu offers
    the spelling that hits both (`test_stage_editor_context` pins that it does).

    A definition listing more than one trigger note type switches `interpolate_from_text` to
    the spelling that forbids a template-name prefix, so the prefixed key matches nothing and
    the definition fails per note with "is not a field or value of that note".

    A cloze note keys its card values `"Cloze 1"`, `"Cloze 2"` -- all cloze cards share one
    template, so the name alone cannot tell them apart -- while `template_name` is the bare
    `"Cloze"`. That misses, and a miss returns the type-appropriate default rather than
    erroring, so every card in a loop silently reported the same 0.
    """

    def test_it_resolves_when_the_definition_has_several_trigger_note_types(self, col):
        note = real_anki.add_note(col, KANJI, {"Kanji": "a", "Keyword": ""})
        card = note.cards()[0]
        definition = d.staged(
            stages=[
                d.card_query("cards", f"nid:{note.id}"),
                d.for_each_card(
                    "cards",
                    [d.edit_note("note", [d.write("Keyword", d.text("{{card.__Card_ID}}"))])],
                ),
            ],
            note_types=[KANJI, VOCAB],
        )

        ok, copied = run(definition, note)

        assert ok is True
        assert [n["Keyword"] for n in copied] == [str(card.id)]

    def test_each_cloze_card_reports_its_own_value(self, col):
        note = real_anki.add_note(
            col, CLOZE, {"Text": "{{c1::one}} and {{c2::two}}", "Extra": ""}
        )
        assert len(note.cards()) == 2
        # Collected through a field rather than a list: a list declared inside a loop body
        # is rebuilt per iteration, so the field is what accumulates across the two cards.
        definition = d.staged(
            stages=[
                d.card_query("cards", f"nid:{note.id}"),
                d.for_each_card(
                    "cards",
                    [
                        d.edit_note(
                            "note",
                            [d.write("Extra", d.text("{{note.Extra}}[{{card.__Card_ID}}]"))],
                        )
                    ],
                ),
            ],
            note_types=[CLOZE],
        )

        ok, copied = run(definition, note)

        assert ok is True
        written = copied[0]["Extra"]
        for card in note.cards():
            assert f"[{card.id}]" in written, written

    def test_a_key_that_is_not_a_card_value_is_still_refused(self, col, note, logger):
        definition = d.staged(stages=[
            d.card_query("cards", f"nid:{note.id}", strategy="first", count=1),
            d.for_each_card(
                "cards",
                [d.edit_note("note", [d.write("Note", d.text("{{card.__Not_A_Value}}"))])],
            ),
        ])

        ok, _copied = run(definition, note)

        assert ok is False
        assert any("'__Not_A_Value' is not a card value" in error for error in logger.errors)

    def test_malformed_custom_data_reads_as_empty_rather_than_as_a_misspelling(
        self, col, note, logger
    ):
        # `get_card_custom_data_prop` answers None when the JSON does not parse -- another
        # add-on's doing, not this reference's. Format 1 rendered a None as "", and the same
        # reference must not be refused as "not a card value" for it: it is spelled right.
        # `update_card` refuses malformed custom data, so it is written under it, the way
        # something that bypassed the backend would have.
        card = note.cards()[0]
        col.db.execute(
            "update cards set data = ? where id = ?", json.dumps({"cd": "{bad"}), card.id
        )
        assert col.get_card(card.id).custom_data == "{bad"
        definition = d.staged(stages=[
            d.card_query("cards", f"cid:{card.id}"),
            d.for_each_card(
                "cards",
                [
                    d.edit_note(
                        "note",
                        [d.write("Note", d.text("[{{card.__Card_Custom_Data_Prop==v}}]"))],
                    )
                ],
            ),
        ])

        ok, copied = run(definition, note)

        assert ok is True, logger.errors
        assert copied[0]["Note"] == "[]"

    def test_an_argument_still_reaches_the_value_that_takes_one(self, col, note):
        card = note.cards()[0]
        card.custom_data = '{"v": "kept"}'
        col.update_card(card)
        definition = d.staged(stages=[
            d.card_query("cards", f"cid:{card.id}"),
            d.for_each_card(
                "cards",
                [
                    d.edit_note(
                        "note",
                        [d.write("Note", d.text("{{card.__Card_Custom_Data_Prop==v}}"))],
                    )
                ],
            ),
        ])

        ok, copied = run(definition, note)

        assert ok is True
        assert copied[0]["Note"] == "kept"

    def test_reading_the_card_id_runs_no_revlog_query(self, col, note, logger, monkeypatch):
        # Every card value was built to answer one key. Four of them share an aggregate over
        # `revlog`, which the id has no use for -- so reading it cost a query it never read.
        from copy_anywhere.shared.interpolate import interpolate_fields

        def refuse(card_id):
            raise AssertionError(f"revlog read for card {card_id} to answer __Card_ID")

        monkeypatch.setattr(interpolate_fields, "get_card_time_values", refuse)
        card = note.cards()[0]
        definition = d.staged(stages=[
            d.card_query("cards", f"cid:{card.id}"),
            d.for_each_card(
                "cards",
                [d.edit_note("note", [d.write("Note", d.text("{{card.__Card_ID}}"))])],
            ),
        ])

        ok, copied = run(definition, note)

        assert ok is True, logger.errors
        assert copied[0]["Note"] == str(card.id)

    def test_the_format_1_spelling_on_the_note_still_works(self, col, note):
        # The path this does not touch: a migrated definition names a card value by template
        # on the note, and `get_from_note_fields` is still what answers it.
        card = [c for c in note.cards() if c.template()["name"] == "Recognition"][0]
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Recognition__Card_ID}}")]
        )

        ok, copied = run(definition, note)

        assert ok is True
        assert copied[0]["Note"] == str(card.id)


class TestFiles:
    def test_a_write_can_be_read_back_in_the_same_run(self, col, note, media_dir, logger):
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("first line")),
            d.read_file("back", "log.txt"),
            d.edit_note("trigger", [d.write("Note", d.text("{{back}}"))]),
        ])
        assert run(definition, note)[0] is True, logger.errors
        assert note["Note"] == "first line"
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "first line"

    def test_read_modify_write_is_how_appending_is_spelled(self, col, note, media_dir, logger):
        (media_dir / "_log.txt").write_text("a\n", encoding="utf-8")
        definition = d.staged(stages=[
            d.read_file("current", "log.txt"),
            d.write_file("log.txt", d.text("{{current}}b\n")),
        ])
        assert run(definition, note)[0] is True, logger.errors
        # Byte-exact: the newline that went in is the newline that comes back.
        assert (media_dir / "_log.txt").read_bytes() == b"a\nb\n"

    def test_a_missing_file_reads_as_empty_by_default(self, col, note, media_dir):
        definition = d.staged(stages=[
            d.read_file("current", "nope.txt"),
            d.edit_note("trigger", [d.write("Note", d.text("[{{current}}]"))]),
        ])
        run(definition, note)
        assert note["Note"] == "[]"

    def test_if_missing_error_fails_the_definition(self, col, note, media_dir):
        definition = d.staged(stages=[
            d.read_file("current", "nope.txt", if_missing="error"),
            d.edit_note("trigger", [d.write("Note", d.text("x"))]),
        ])
        assert run(definition, note)[0] is False
        assert note["Note"] == ""

    def test_if_missing_skip_block_stops_the_rest_benignly(self, col, note, media_dir):
        definition = d.staged(stages=[
            d.read_file("current", "nope.txt", if_missing="skip_block"),
            d.edit_note("trigger", [d.write("Note", d.text("x"))]),
        ])
        assert run(definition, note)[0] is True
        assert note["Note"] == ""

    def test_overwrite_false_refuses_an_existing_file(self, col, note, media_dir):
        (media_dir / "_log.txt").write_text("keep", encoding="utf-8")
        definition = d.staged(stages=[d.write_file("log.txt", d.text("new"), overwrite=False)])
        assert run(definition, note)[0] is False
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "keep"

    def test_skip_if_exists_leaves_an_existing_file_alone(self, col, note, media_dir, logger):
        (media_dir / "_log.txt").write_text("keep", encoding="utf-8")
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("new"), overwrite=False, skip_if_exists=True)
        ])

        assert run(definition, note)[0] is True, logger.errors
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "keep"

    def test_skip_if_exists_counts_a_file_this_run_has_already_written(
        self, col, note, media_dir, logger
    ):
        # The queued write lands the moment the trigger commits, so the second stage is
        # writing over a file that is about to be there -- which is what the stage said not
        # to do. The overlay is keyed by the stored name, the one with the leading
        # underscore, so checking it under the name the user typed never matched.
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("first")),
            d.write_file("log.txt", d.text("second"), overwrite=False, skip_if_exists=True),
        ])

        assert run(definition, note)[0] is True, logger.errors
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "first"

    def test_overwrite_false_refuses_a_file_this_run_has_already_written(
        self, col, note, media_dir
    ):
        # The sibling of the `skip_if_exists` case above, and the reason it is worth its own
        # test: checking only the disk made the answer depend on whether a previous run had
        # committed. The same definition overwrote silently the first time and refused the
        # second, with nothing in between to explain the difference.
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("first")),
            d.write_file("log.txt", d.text("second"), overwrite=False),
        ])

        assert run(definition, note)[0] is False
        # Nothing lands at all, the first write included: the refusal is a stage error and a
        # failed definition discards its queued files. That is what `overwrite: false` has
        # always done to a file already on disk, and now the same two stages behave the same
        # way whether or not an earlier run put one there.
        assert not (media_dir / "_log.txt").exists()

    def test_the_refusal_says_the_file_came_from_this_run(
        self, col, note, media_dir, logger
    ):
        # "already exists" would point at the media folder, where there is nothing to find:
        # the first write has not been committed yet either.
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("first")),
            d.write_file("log.txt", d.text("second"), overwrite=False),
        ])

        run(definition, note)
        assert logger.has_error("already written earlier in this run")

    def test_overwrite_false_still_writes_a_name_nothing_else_has_taken(
        self, col, note, media_dir, logger
    ):
        definition = d.staged(stages=[
            d.write_file("first.txt", d.text("one")),
            d.write_file("second.txt", d.text("two"), overwrite=False),
        ])

        assert run(definition, note)[0] is True, logger.errors
        assert (media_dir / "_second.txt").read_text(encoding="utf-8") == "two"

    def test_skip_if_exists_still_writes_when_nothing_is_there(
        self, col, note, media_dir, logger
    ):
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("new"), overwrite=False, skip_if_exists=True)
        ])

        assert run(definition, note)[0] is True, logger.errors
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "new"

    def test_a_filename_with_a_path_separator_is_refused(self, col, note, media_dir, logger):
        definition = d.staged(stages=[d.write_file("../escape.txt", d.text("x"))])
        assert run(definition, note)[0] is False
        assert logger.has_error("path separator") or logger.has_error("'..'")

    def test_a_failed_definition_writes_no_file_at_all(self, col, note, media_dir):
        # File writes are queued during evaluation and applied only once the definition has
        # validated, so a later failure leaves the media folder alone.
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("x")),
            d.edit_note("trigger", [d.write("Nonexistent", d.text("y"))]),
        ])
        assert run(definition, note)[0] is False
        assert not (media_dir / "_log.txt").exists()

    def test_a_failed_write_keeps_what_came_before_and_fails_the_run(
        self, col, note, media_dir, logger, monkeypatch
    ):
        # Files reach the disk after the notes are handed to the caller, and a media write
        # can fail in ways no stage could have checked for. What was committed stays; the
        # failing file and everything queued after it are not attempted; the run fails and
        # says which file.
        from copy_anywhere.logic.execution import commit

        real_write = commit.write_media_file

        def failing_write(filename, text):
            if filename == "_second.txt":
                raise OSError("disk full")
            real_write(filename, text)

        monkeypatch.setattr(commit, "write_media_file", failing_write)
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("written"))]),
            d.write_file("first.txt", d.text("one")),
            d.write_file("second.txt", d.text("two")),
            d.write_file("third.txt", d.text("three")),
        ])
        ok, copied = run(definition, note)
        assert ok is False
        assert logger.has_error("_second.txt"), logger.errors
        assert logger.has_error("disk full"), logger.errors
        assert [n.id for n in copied] == [note.id]
        assert note["Note"] == "written"
        assert (media_dir / "_first.txt").read_text(encoding="utf-8") == "one"
        assert not (media_dir / "_second.txt").exists()
        assert not (media_dir / "_third.txt").exists()

    def test_a_failed_write_leaves_no_queued_file_behind(
        self, col, note, media_dir, monkeypatch
    ):
        # The overlay is what a later read sees "as if the file were written". Once the
        # commit has given up on a file, nothing is going to write it, so the session must
        # not keep claiming it is there.
        from copy_anywhere.logic.execution import commit

        def failing_write(filename, text):
            raise OSError("read-only")

        monkeypatch.setattr(commit, "write_media_file", failing_write)
        definition = d.staged(stages=[d.write_file("log.txt", d.text("x"))])
        session = ExecutionSession()
        assert run_definition_for_trigger_note(definition, note, session) is False
        assert session.file_overlay == {}
        assert session.pending_files == []
        assert session.read_file("log.txt") is None

    def test_invalid_utf8_fails_the_read(self, col, note, media_dir, logger):
        (media_dir / "_log.txt").write_bytes(b"\xff\xfe not utf 8")
        definition = d.staged(stages=[d.read_file("current", "log.txt")])
        assert run(definition, note)[0] is False
        assert logger.has_error("not valid UTF-8")


class TestCalls:
    def child(self, guid="child-guid"):
        producer = d.variable("H1", d.text("from {{trigger.Word}}"))
        return d.staged(
            "child",
            guid=guid,
            stages=[
                producer,
                d.edit_note("trigger", [d.write("Reading", d.text("child was here"))]),
            ],
            exports=[d.export("H1", producer)],
        )

    def test_an_export_comes_back_under_the_callers_own_name(self, col, note, logger):
        child = self.child()
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.call_definition(
                    "child-guid", outputs=[{"export": "H1", "result": "from_child"}]
                ),
                d.edit_note("trigger", [d.write("Note", d.text("{{from_child}}"))]),
            ],
        )
        ok, _copied = run(parent, note, definitions_for_calls=[child, parent])
        assert ok is True, logger.errors
        assert note["Note"] == "from neko"

    def test_a_middle_definition_can_re_export_what_it_called(self, col, note, logger):
        # The editor's Exports panel has always offered a call stage's outputs, and the
        # analyser rejected them: `stage_result_name` has no case for a call stage, which
        # binds one result per output rather than one of its own. The panel offered it, the
        # validator called it a stage that produces no result, and there was no way through.
        child = self.child()
        call = d.call_definition("child-guid", outputs=[{"export": "H1", "result": "passed"}])
        middle = d.staged(
            "middle", guid="middle-guid", stages=[call],
            exports=[{"name": "passed", "stage_guid": call["guid"], "result": "passed"}],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.call_definition(
                    "middle-guid", outputs=[{"export": "passed", "result": "from_middle"}]
                ),
                d.edit_note("trigger", [d.write("Note", d.text("{{from_middle}}"))]),
            ],
        )
        ok, _copied = run(
            parent, note, definitions_for_calls=[child, middle, parent]
        )
        assert ok is True, logger.errors
        assert note["Note"] == "from neko"

    def test_one_call_stages_two_outputs_export_separately(self, col, note, logger):
        producer = d.variable("H1", d.text("one"))
        second = d.variable("H2", d.text("two"))
        child = d.staged(
            "child", guid="child-guid", stages=[producer, second],
            exports=[d.export("H1", producer), d.export("H2", second)],
        )
        call = d.call_definition(
            "child-guid",
            outputs=[{"export": "H1", "result": "a"}, {"export": "H2", "result": "b"}],
        )
        middle = d.staged(
            "middle", guid="middle-guid", stages=[call],
            exports=[
                {"name": "a", "stage_guid": call["guid"], "result": "a"},
                {"name": "b", "stage_guid": call["guid"], "result": "b"},
            ],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.call_definition(
                    "middle-guid",
                    outputs=[
                        {"export": "a", "result": "first"},
                        {"export": "b", "result": "second"},
                    ],
                ),
                d.edit_note("trigger", [d.write("Note", d.text("{{first}}/{{second}}"))]),
            ],
        )
        ok, _copied = run(
            parent, note, definitions_for_calls=[child, middle, parent]
        )
        assert ok is True, logger.errors
        assert note["Note"] == "one/two"

    def test_a_call_resolves_through_the_config_when_the_caller_passes_nothing(
        self, col, note, logger, stub_mw
    ):
        # A call names a definition by guid, and that definition need not be one the run was
        # asked for -- a bulk run over one definition can still call another. So the lookup
        # falls back to the stored config, which is where the hooks and the bulk loop get
        # theirs from without having to thread it through every call site.
        child = self.child()
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.call_definition(
                    "child-guid", outputs=[{"export": "H1", "result": "H1_here"}]
                ),
                d.edit_note("trigger", [d.write("Note", d.text("{{H1_here}}"))]),
            ],
        )
        stub_mw.addonManager.configs["copy_anywhere"]["copy_definitions"] = [child, parent]
        ok, _copied = run(parent, note)
        assert ok is True, logger.errors
        assert note["Note"] == "from neko"

    def test_a_definition_that_calls_nothing_does_not_read_the_config(
        self, col, note, stub_mw
    ):
        # An unreadable definition sitting in the config is no reason for one that does not
        # call it to fail.
        stub_mw.addonManager.configs["copy_anywhere"]["copy_definitions"] = [
            {"guid": "broken", "copy_mode": "Within note", "copy_into_note_types": ["a list"]}
        ]
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("fine"))]),
        ])
        assert run(definition, note)[0] is True
        assert note["Note"] == "fine"

    def test_the_callee_sees_note_edits_but_not_the_callers_variables(self, col, note, logger):
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[
                # `{{trigger.Meaning}}` is the edit the caller made before calling; `{{M}}`
                # is the caller's variable, which is not in the callee's scope. A name the
                # scope does not hold fails the stage rather than reading as nothing, so the
                # isolation is reported instead of being written into the note.
                d.edit_note(
                    "trigger", [d.write("Note", d.text("[{{M}}][{{trigger.Meaning}}]"))]
                )
            ],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.variable("M", d.text("secret")),
                d.edit_note("trigger", [d.write("Meaning", d.text("edited first"))]),
                d.call_definition("child-guid"),
            ],
        )
        ok, _copied = run(parent, note, definitions_for_calls=[child, parent])
        assert ok is False
        assert any("'M' is not a binding" in error for error in logger.errors)
        assert note["Note"] == ""

    def test_the_callee_sees_the_note_edits_the_caller_made(self, col, note, logger):
        # The other half of the same rule, with nothing out of scope in the way: what the
        # callee reads off the trigger is what the caller wrote to it a stage earlier.
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[d.edit_note("trigger", [d.write("Note", d.text("{{trigger.Meaning}}"))])],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.edit_note("trigger", [d.write("Meaning", d.text("edited first"))]),
                d.call_definition("child-guid"),
            ],
        )
        ok, _copied = run(parent, note, definitions_for_calls=[child, parent])
        assert ok is True, logger.errors
        assert note["Note"] == "edited first"

    def test_a_call_inside_a_loop_reaches_each_note(self, col, note, logger):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        real_anki.add_note(col, VOCAB, {"Word": "b", "Meaning": "B"})
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[d.edit_note("trigger", [d.write("Note", d.text("visited"))])],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.note_query("found", '"Word:a" OR "Word:b"'),
                d.for_each_note("found", [d.call_definition("child-guid", trigger="note")]),
            ],
        )
        ok, copied = run(parent, note, definitions_for_calls=[child, parent])
        assert ok is True, logger.errors
        assert sorted(n["Word"] for n in copied) == ["a", "b"]
        assert all(n["Note"] == "visited" for n in copied)

    def test_an_unmigratable_callee_fails_the_definition_rather_than_the_op(
        self, col, note, logger
    ):
        # The callee is migrated when the call actually looks it up, so the migrator can
        # raise from inside the run. `MigrationError` is not a `StageError`, so nothing
        # between the lookup and the `CollectionOp` caught it: the whole op ended in
        # Anki's error dialog instead of the definition failing and saying why.
        child = d.within_note("child")
        child["guid"] = "child-guid"
        child["copy_mode"] = None
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.call_definition("child-guid"),
                d.edit_note("trigger", [d.write("Note", d.text("ran"))]),
            ],
        )

        ok, _copied = run(parent, note, definitions_for_calls=[child, parent])

        assert ok is False
        assert logger.has_error("missing copy mode value")
        assert note["Note"] == ""

    def test_a_failing_callee_stops_the_parent_committing_anything(self, col, note):
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[d.edit_note("trigger", [d.write("Nonexistent", d.text("x"))])],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.edit_note("trigger", [d.write("Note", d.text("written first"))]),
                d.call_definition("child-guid"),
            ],
        )
        ok, copied = run(parent, note, definitions_for_calls=[child, parent])
        assert ok is False
        assert copied == []

    def test_a_direct_cycle_is_refused_at_run_time_too(self, col, note, logger):
        # Saving refuses these, but the JSON can be hand-edited, so the runtime checks the
        # active guid stack as well.
        a = d.staged("a", guid="a", stages=[d.call_definition("b")])
        b = d.staged("b", guid="b", stages=[d.call_definition("a")])
        ok, _copied = run(a, note, definitions_for_calls=[a, b])
        assert ok is False
        assert logger.has_error("call cycle")

    def test_an_indirect_cycle_is_refused_too(self, col, note, logger):
        a = d.staged("a", guid="a", stages=[d.call_definition("b")])
        b = d.staged("b", guid="b", stages=[d.call_definition("c")])
        c = d.staged("c", guid="c", stages=[d.call_definition("a")])
        ok, _copied = run(a, note, definitions_for_calls=[a, b, c])
        assert ok is False
        assert logger.has_error("call cycle")



    def test_a_callee_skipped_by_its_copy_condition_fails_its_caller(
        self, col, note, logger
    ):
        # Not a defect on its own -- it is the runtime consequence the analyser is there to
        # predict, and it is pinned here so the analyser test beside it
        # (`TestASearchConditionCanEndTheBlockToo`) is about something real: a callee whose
        # migrated copy condition does not match hands back no exports, and the caller fails
        # on the one it asked for.
        producer = d.variable("H1", d.text("x"))
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[
                d.condition(
                    d.text("tag:nothing-has-this"),
                    [],
                    predicate_kind="note_query",
                    predicate_target={"binding": "trigger"},
                    unmatched_skips_trigger=True,
                ),
                producer,
            ],
            exports=[d.export("H1", producer)],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.call_definition("child-guid", outputs=[{"export": "H1", "result": "got"}]),
                d.edit_note("trigger", [d.write("Note", d.text("{{got}}"))]),
            ],
        )

        ok, _copied = run(parent, note, definitions_for_calls=[child, parent])

        assert ok is False
        assert logger.has_error("exports no 'H1'")

    def test_a_callee_skipped_from_inside_a_loop_fails_its_caller_the_same_way(
        self, col, note, logger
    ):
        # `TriggerSkipped` is not scoped to the block it is raised in: the loop and the
        # branch catch only `SkipBlock`, so a marked condition inside a loop body unwinds
        # through the loop, out of the root block and ends the definition -- the export
        # declared after the loop is never set and the caller fails exactly as above. The
        # analyser has to predict that from any depth, not only for a root-level condition.
        producer = d.variable("H1", d.text("x"))
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[
                d.note_query("found", "Word:neko"),
                d.for_each_note(
                    "found",
                    [
                        d.condition(
                            d.text("tag:nothing-has-this"),
                            [],
                            predicate_kind="note_query",
                            predicate_target={"binding": "note"},
                            unmatched_skips_trigger=True,
                        ),
                    ],
                ),
                producer,
            ],
            exports=[d.export("H1", producer)],
        )
        parent = d.staged(
            "parent",
            guid="parent-guid",
            stages=[
                d.call_definition("child-guid", outputs=[{"export": "H1", "result": "got"}]),
                d.edit_note("trigger", [d.write("Note", d.text("{{got}}"))]),
            ],
        )

        ok, _copied = run(parent, note, definitions_for_calls=[child, parent])

        assert ok is False
        assert logger.has_error("exports no 'H1'")
        # The other half: the definition that fails at run time must not analyse clean.
        assert any(
            "cannot be guaranteed" in message
            for message in analyze_definition(child).problem_messages()
        )


class TestFacades:
    def test_code_cannot_write_through_a_facade(self, note):
        definition = d.staged(stages=[
            d.variable("x", d.code("note['Note'] = 'nope'\nreturn 'ok'")),
        ])
        ok, _copied = run(definition, note)
        assert ok is False
        assert note["Note"] == ""

    def test_code_sees_an_earlier_stages_pending_edit(self, note, logger):
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("pending"))]),
            d.variable("seen", d.code("return trigger['Note']")),
            d.edit_note("trigger", [d.write("Meaning", d.text("{{seen}}"))]),
        ])
        assert run(definition, note)[0] is True, logger.errors
        assert note["Meaning"] == "pending"

    def test_a_note_list_facade_is_iterable_indexable_and_sliceable(self, col, note, logger):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        real_anki.add_note(col, VOCAB, {"Word": "b", "Meaning": "B"})
        definition = d.staged(stages=[
            d.note_query("found", '"Word:a" OR "Word:b"'),
            d.variable(
                "report",
                d.code(
                    "names = sorted(n['Word'] for n in found)\n"
                    "return f\"{len(found)}:{found[0]['Word'] in names}:{len(found[:1])}\""
                ),
            ),
            d.edit_note("trigger", [d.write("Note", d.text("{{report}}"))]),
        ])
        assert run(definition, note)[0] is True, logger.errors
        assert note["Note"] == "2:True:1"

    def test_find_notes_returns_ids_and_get_note_returns_a_facade(self, col, note, logger):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        definition = d.staged(stages=[
            d.variable(
                "found",
                d.code(
                    "ids = find_notes('Word:a')\n"
                    "return f'{isinstance(ids[0], int)}:{get_note(ids[0])[\"Meaning\"]}'"
                ),
            ),
            d.edit_note("trigger", [d.write("Note", d.text("{{found}}"))]),
        ])
        assert run(definition, note)[0] is True, logger.errors
        assert note["Note"] == "True:A"

    def test_code_can_return_a_filtered_note_list_for_a_loop_to_walk(self, col, note, logger):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "keep"})
        real_anki.add_note(col, VOCAB, {"Word": "b", "Meaning": "drop"})
        definition = d.staged(stages=[
            d.note_query("found", '"Word:a" OR "Word:b"'),
            d.variable("kept", d.code("return [n for n in found if n['Meaning'] == 'keep']")),
            d.for_each_note(
                "kept", [d.edit_note("note", [d.write("Note", d.text("chosen"))])]
            ),
        ])
        ok, copied = run(definition, note)
        assert ok is True, logger.errors
        assert [n["Word"] for n in copied] == ["a"]


class TestPreview:
    def test_preview_computes_the_same_values_and_persists_nothing(
        self, col, note, media_dir, logger
    ):
        other = real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        definition = d.staged(stages=[
            d.note_query("found", "Word:a"),
            d.for_each_note(
                "found", [d.edit_note("note", [d.write("Note", d.text("touched"))])]
            ),
            d.write_file("preview.txt", d.text("{{trigger.Word}}")),
        ])
        committer = PreviewCommitter()
        session = ExecutionSession(collect_trace=True)
        copied: list = []
        ok = run_definition_for_trigger_note(
            definition, note, session, committer=committer, copied_into_notes=copied
        )
        assert ok is True, logger.errors
        # Nothing reaches the caller's update list and nothing reaches the media folder.
        assert copied == []
        assert not (media_dir / "_preview.txt").exists()
        assert [planned["fields"]["Note"] for planned in committer.planned_notes] == ["touched"]
        assert [planned["filename"] for planned in committer.planned_files] == ["_preview.txt"]
        assert col.get_note(other.id)["Note"] == ""

    def test_every_stage_leaves_a_trace_event(self, col, note):
        definition = d.staged(stages=[
            d.variable("M", d.text("x")),
            d.edit_note("trigger", [d.write("Note", d.text("{{M}}"))]),
        ])
        session = ExecutionSession(collect_trace=True)
        run_definition_for_trigger_note(definition, note, session, committer=PreviewCommitter())
        assert [event.stage_type for event in session.trace] == ["variable", "edit_note"]
        assert all(event.status == "ok" for event in session.trace)
        assert session.trace[0].result == "x"


class TestCancellation:
    def test_a_cancel_between_loop_iterations_commits_nothing(self, col, note):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        real_anki.add_note(col, VOCAB, {"Word": "b", "Meaning": "B"})
        definition = d.staged(stages=[
            d.note_query("found", '"Word:a" OR "Word:b"'),
            d.for_each_note(
                "found", [d.edit_note("note", [d.write("Note", d.text("touched"))])]
            ),
        ])
        session = ExecutionSession(want_cancel=lambda: True)
        copied: list = []
        # A cancelled run is not a failure: the caller stops, and the half-evaluated frame
        # is dropped rather than committed.
        assert (
            run_definition_for_trigger_note(
                definition, note, session, copied_into_notes=copied
            )
            is True
        )
        assert copied == []

    def test_a_cancel_before_a_query_stops_before_the_search_and_the_write(self, col, note):
        # A query stage asks the session whether to stop before it searches, the same as a
        # loop does before each iteration. Ignoring the answer meant the search ran, the
        # block wrote and the run committed after the user had asked it to stop.
        searches = []
        original = col.find_notes
        col.find_notes = lambda query: (searches.append(query), original(query))[1]
        definition = d.staged(stages=[
            d.note_query("found", "Word:neko"),
            d.edit_note("trigger", [d.write("Note", d.text("touched"))]),
        ])
        session = ExecutionSession(want_cancel=lambda: True)
        copied: list = []
        try:
            ok = run_definition_for_trigger_note(
                definition, note, session, copied_into_notes=copied
            )
        finally:
            col.find_notes = original

        assert ok is True
        assert searches == []
        assert copied == []


class TestAddNoteCompatibility:
    def test_an_incompatible_definition_cannot_commit_under_the_add_backstop(
        self, col
    ):
        other = real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        definition = d.staged(stages=[
            d.note_query("found", "Word:a"),
            d.for_each_note("found", [d.edit_note("note", [d.write("Note", d.text("x"))])]),
        ])
        # Its own effects already say it is not compatible; the backstop is what catches a
        # hand-edited definition claiming otherwise.
        assert definition["effects"]["add_note_compatible"] is False
        copied: list = []
        ok = copy_for_single_trigger_note(
            definition,
            new_note,
            copied_into_notes=copied,
            add_note_compatible_only=True,
        )
        assert ok is False
        assert copied == []
        assert col.get_note(other.id)["Note"] == ""

    def test_a_compatible_definition_may_still_query_during_add(self, col, logger):
        real_anki.add_note(col, KANJI, {"Kanji": "猫", "Keyword": "cat"})
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        definition = d.staged(stages=[
            d.note_query("found", "Keyword:cat"),
            d.list_variable("keywords"),
            d.for_each_note("found", [d.store("keywords", d.text("{{note.Kanji}}"))]),
            d.join("keywords", "joined"),
            d.edit_note("trigger", [d.write("Meaning", d.text("{{joined}}"))]),
        ])
        assert definition["effects"]["add_note_compatible"] is True
        ok = copy_for_single_trigger_note(
            definition, new_note, add_note_compatible_only=True
        )
        assert ok is True, logger.errors
        assert new_note["Meaning"] == "猫"

    def test_a_queued_file_write_is_refused_under_the_add_backstop(self, col, media_dir, logger):
        # A file survives a cancelled add the way another note's edit does, so the backstop
        # has to refuse it too: the analyser already says such a definition is incompatible,
        # and this is what catches a hand-edited one claiming otherwise.
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Meaning", d.text("{{trigger.Word}}"))]),
            d.write_file("log.txt", d.text("{{trigger.Word}}")),
        ])
        assert definition["effects"]["add_note_compatible"] is False
        ok = copy_for_single_trigger_note(
            definition, new_note, add_note_compatible_only=True
        )
        assert ok is False
        assert not (media_dir / "_log.txt").exists()
        assert logger.has_error("another note, card or file"), logger.errors

    def test_a_card_action_on_the_note_being_added_is_not_a_queued_change(
        self, col, logger
    ):
        # The whole point of telling the new note's own cards from everyone else's: the
        # stage skips the action on an id-0 note before anything is queued, so a
        # fill-and-flag definition reaches the commit and its field write lands.
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        definition = d.staged(stages=[
            d.edit_note(
                "trigger",
                [d.write("Meaning", d.text("{{trigger.Word}}"))],
                card_actions=[d.card_action(VOCAB, "Recognition", set_flag=3)],
            ),
        ])
        assert definition["effects"]["add_note_compatible"] is True
        ok = copy_for_single_trigger_note(
            definition, new_note, add_note_compatible_only=True
        )
        assert ok is True, logger.errors
        assert new_note["Meaning"] == "neko"
        assert any("no cards yet" in message for message in logger.warnings), logger.warnings


class TestRunningForOneEditorField:
    """`field_only`, which the unfocus hook sets to the field that just lost focus.

    A migrated write says which editor fields trigger it, because format 1 asked that
    question per field write. A write the stage editor produced does not: format 2 watches
    fields for the definition as a whole (§8), so by the time a stage runs the question has
    already been answered.

    The writes here land on a queried note, so they show up in `copied_into_notes` -- the
    list the hook hands to `update_notes` -- rather than in the collection.
    """

    @pytest.fixture
    def other(self, col):
        return real_anki.add_note(col, VOCAB, {"Word": "other", "Note": ""}, tags=["pool"])

    def definition(self, **write_extra):
        return d.staged(stages=[
            d.note_query("found", "tag:pool"),
            d.for_each_note("found", [
                d.edit_note(
                    "note",
                    [dict(d.write("Note", d.text("{{trigger.Word}}")), **write_extra)],
                    tags={"add": ["ran"], "remove": []},
                ),
            ]),
        ])

    def test_a_natively_authored_write_runs(self, note, other):
        # The regression this guards: every write in a definition built in the new editor
        # was skipped on unfocus, while its tags still applied, so the definition looked
        # like it had run and only the field writes were missing.
        ok, copied = run(self.definition(), note, field_only="Word")
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]

    def test_a_migrated_write_is_still_limited_to_its_own_trigger_fields(
        self, note, other
    ):
        ok, copied = run(
            self.definition(unfocus_trigger_fields=["Reading"]), note, field_only="Word"
        )
        assert ok is True
        # The tag in the same stage is not gated, so the note is still touched -- which is
        # exactly what made the skipped writes so hard to see.
        assert [n["Note"] for n in copied] == [""]
        assert copied[0].has_tag("ran")

    def test_a_migrated_write_whose_trigger_field_matches_runs(self, note, other):
        ok, copied = run(
            self.definition(unfocus_trigger_fields=["Word"]), note, field_only="Word"
        )
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]

    def test_a_migrated_write_that_watches_nothing_never_runs(self, note, other):
        # An empty list is format 1 saying this write has no editor field to trigger it,
        # which is not the same as a write that was never migrated at all.
        ok, copied = run(
            self.definition(unfocus_trigger_fields=[]), note, field_only="Word"
        )
        assert ok is True
        assert [n["Note"] for n in copied] == [""]

    def test_a_run_that_is_not_an_unfocus_gates_nothing(self, note, other):
        ok, copied = run(self.definition(unfocus_trigger_fields=["Reading"]), note)
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]

    def test_a_write_turned_off_for_editing_does_not_run_while_editing(
        self, note, other
    ):
        # Format 1's `copy_on_unfocus_when_edit`, which is how a slow write -- downloading
        # audio, say -- was kept for the bulk action instead of running on every keystroke
        # that left a watched field.
        ok, copied = run(
            self.definition(unfocus_trigger_fields=["Word"], unfocus_when_edit=False),
            note,
            field_only="Word",
        )
        assert ok is True
        assert [n["Note"] for n in copied] == [""]

    def test_that_same_write_runs_while_adding_if_it_is_on_for_adding(
        self, note, other
    ):
        ok, copied = run(
            self.definition(
                unfocus_trigger_fields=["Word"], unfocus_when_edit=False, unfocus_when_add=True
            ),
            note,
            field_only="Word",
            unfocus_is_add=True,
        )
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]

    def test_a_write_on_for_editing_only_does_not_run_while_adding(self, note, other):
        ok, copied = run(
            self.definition(
                unfocus_trigger_fields=["Word"], unfocus_when_edit=True, unfocus_when_add=False
            ),
            note,
            field_only="Word",
            unfocus_is_add=True,
        )
        assert ok is True
        assert [n["Note"] for n in copied] == [""]

    def test_the_flags_do_not_reach_a_run_that_is_not_an_unfocus(self, note, other):
        # The bulk action the slow write was being saved for.
        ok, copied = run(
            self.definition(unfocus_when_edit=False, unfocus_when_add=False), note
        )
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]


class TestADefinitionWhoseTriggersKeyIsNull:
    """A stored `triggers: null`, which every reader but one already tolerates.

    `triggers` is optional in the sense that matters here: the config is hand-editable JSON,
    a definition written by a half-finished edit or an older build can carry the key as
    `null`, and nine readers across `configuration.py`, `preview.py` and `stage_document.py`
    spell it `(definition.get("triggers") or {})` for exactly that reason -- a null is read as
    "no triggers configured", which is a definition that runs against whatever it is given.

    `copy_for_single_trigger_note` is the tenth, and spells it `.get("triggers", {})`. The
    default only applies when the key is absent, so a present null comes back as `None` and
    the next line calls `.get` on it. That is not a definition failure -- it is an
    `AttributeError` escaping the `CollectionOp`, which Anki shows as its error dialog with a
    traceback, and which stops the bulk run over every remaining note.
    """

    def definition(self):
        staged = d.staged(stages=[d.edit_note("trigger", [d.write("Note", d.text("ran"))])])
        staged["triggers"] = None
        return staged

    def test_it_runs_rather_than_raising(self, note, logger):
        ok, copied = run(self.definition(), note)

        assert ok is True, logger.errors
        assert note["Note"] == "ran"
        assert [n.id for n in copied] == [note.id]

    def test_a_null_is_read_as_no_deck_whitelist(self, col, note, logger):
        # The line that dereferences it is the deck whitelist check, and "no whitelist" is
        # what every other reader concludes from the same null.
        deck_id = col.decks.id("Somewhere Else")
        ok, _copied = run(self.definition(), note, deck_id=deck_id)

        assert ok is True, logger.errors
        assert note["Note"] == "ran"


class TestWideningTheUnfocusListOfAMigratedDefinition:
    """Two unfocus gates, only one of which the editor used to be able to reach.

    Format 2 watches editor fields for the definition as a whole: `triggers.on_unfocus` names
    them, the trigger editor is where they are chosen, and passing that test is what starts a
    run. A migrated write carries a second, older gate -- `unfocus_trigger_fields`, the
    per-write list format 1 asked separately -- and `run_edit_note` consults it for every
    write that has the key.

    Nothing in the editor showed or wrote that key, so it was the one thing about a write
    that could not be changed: `FieldWriteRow` had a field picker, a "write if" combo and a
    value, and `apply()` wrote those three. Adding a field to the definition's unfocus list
    therefore widened the first gate and not the second -- the definition ran on that field
    and every migrated write in it was skipped.

    What made that silent rather than merely ineffective is that the gate is per write and
    nothing else in the stage has one: tags and card actions in the same `edit_note` apply
    unconditionally, so the note really was modified, saved and reported as copied into, with
    the tag added and the field it was meant to fill still empty. That last part is format
    1's own behaviour, pinned deliberately in `test_writing_one_note.py`, so it is the gate
    that has to become reachable, not the tags that have to stop.
    """

    @pytest.fixture
    def note(self, col):
        return real_anki.add_note(
            col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": ""}
        )

    def migrated(self, watched_fields, write_watches="Word"):
        from copy_anywhere.logic.definition_migration import migrate_definition_v1_to_v2

        definition = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_field_defs=[
                    d.field_to_field(
                        "Meaning",
                        "{{Word}}",
                        copy_on_unfocus_when_edit=True,
                        copy_on_unfocus_trigger_field=write_watches,
                    )
                ],
                add_tags=d.quoted_list(["ran"]),
                copy_on_unfocus_when_edit=True,
            )
        )
        definition["triggers"]["on_unfocus"]["edit_fields"] = list(watched_fields)
        return definition

    def test_the_write_still_runs_for_the_field_it_was_migrated_with(self, note, logger):
        ok, _copied = run(self.migrated(["Word"]), note, field_only="Word")

        assert ok is True, logger.errors
        assert note["Meaning"] == "neko"

    def test_widening_the_writes_own_list_is_what_makes_it_run_for_another(
        self, note, logger
    ):
        # What the row's "only when leaving" box now writes. Widening the definition's list
        # alone still does not reach this gate -- the two are separate questions, and format
        # 1 asked both -- but the gate is no longer invisible, so the answer can be changed.
        definition = self.migrated(["Word", "Reading"], write_watches='"Word", "Reading"')

        ok, _copied = run(definition, note, field_only="Reading")

        assert ok is True, logger.errors
        assert note["Meaning"] == "neko"

    def test_a_field_neither_gate_claims_still_writes_nothing(self, note):
        _ok, _copied = run(self.migrated(["Word"]), note, field_only="Meaning")

        assert note["Meaning"] == ""

