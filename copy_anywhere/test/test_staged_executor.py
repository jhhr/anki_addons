"""Tests for what format 2 can express that format 1 could not.

The characterization suite proves migrated definitions still behave the way they did. This
file is the other half: stages an editor will be able to author but the old model had no
shape for -- several queries, loops, lists and reductions, branches, explicit note and card
targets, file reads, and calls into other definitions with declared exports.

Everything runs against a real collection through `copy_for_single_trigger_note`, the same
entry point the browser, the hooks and the sync path use.
"""

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import KANJI, VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note
from copy_anywhere.logic.execution.commit import PreviewCommitter
from copy_anywhere.logic.execution.context import ExecutionSession
from copy_anywhere.logic.execution.runner import run_definition_for_trigger_note


@pytest.fixture
def note(col):
    return real_anki.add_note(
        col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat", "Freq": "5"}
    )


def run(definition, note, logger, **kwargs):
    copied: list = []
    ok = copy_for_single_trigger_note(
        definition, note, copied_into_notes=copied, logger=logger, **kwargs
    )
    return ok, copied


class TestVariables:
    def test_a_later_variable_can_consume_an_earlier_one(self, note, logger):
        # The whole point of stages: format 1 evaluated every variable against the trigger
        # note alone, so this was impossible to write.
        definition = d.staged(stages=[
            d.variable("first", d.text("{{trigger.Word}}")),
            d.variable("second", d.text("<{{first}}>")),
            d.edit_note("trigger", [d.write("Note", d.text("{{second}}"))]),
        ])
        assert run(definition, note, logger)[0] is True
        assert note["Note"] == "<neko>"

    def test_a_code_variable_receives_the_earlier_bindings(self, note, logger):
        definition = d.staged(stages=[
            d.variable("first", d.text("abc")),
            d.variable("second", d.code("return first.upper()")),
            d.edit_note("trigger", [d.write("Note", d.text("{{second}}"))]),
        ])
        assert run(definition, note, logger)[0] is True
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
        ok, copied = run(definition, note, logger)
        assert ok is True, logger.errors
        assert [n["Word"] for n in copied] == ["a"]

    def test_selection_first_takes_them_in_search_order(self, col, note, others, logger):
        definition = d.staged(stages=[
            d.note_query("found", '"Word:a" OR "Word:b"', strategy="first", count=1),
            d.list_variable("words"),
            d.for_each_note("found", [d.store("words", d.text("{{note.Word}}"))]),
            d.join("words", "joined", "+"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        run(definition, note, logger)
        assert note["Note"] in ("a", "b")
        assert "+" not in note["Note"]

    def test_a_sort_field_orders_the_result(self, col, note, others, logger):
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
        run(definition, note, logger)
        assert note["Note"] == "b+a"

    def test_query_membership_ignores_edits_that_have_not_been_saved(
        self, col, note, others, logger
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
        run(definition, note, logger)
        assert note["Word"] == "a"
        assert note["Note"] == "A"


class TestPendingEditsAreVisible:
    def test_a_later_stage_reads_what_an_earlier_one_wrote(self, note, logger):
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("first"))]),
            d.edit_note("trigger", [d.write("Meaning", d.text("{{trigger.Note}} second"))]),
        ])
        run(definition, note, logger)
        assert note["Meaning"] == "first second"

    def test_one_stage_still_reads_its_own_entry_snapshot_so_a_swap_works(self, note, logger):
        definition = d.staged(stages=[
            d.edit_note(
                "trigger",
                [
                    d.write("Word", d.text("{{trigger.Meaning}}")),
                    d.write("Meaning", d.text("{{trigger.Word}}")),
                ],
            )
        ])
        run(definition, note, logger)
        assert (note["Word"], note["Meaning"]) == ("cat", "neko")

    def test_the_trigger_can_be_edited_before_and_after_a_loop(self, col, note, logger):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("before"))]),
            d.note_query("found", "Word:a"),
            d.list_variable("seen"),
            d.for_each_note("found", [d.store("seen", d.text("{{trigger.Note}}"))]),
            d.join("seen", "joined"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}/after"))]),
        ])
        run(definition, note, logger)
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
        ok, copied = run(definition, note, logger)
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
        ok, copied = run(definition, note, logger)
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

    def test_appends_keep_the_loop_order(self, note, three, logger):
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
        run(definition, note, logger)
        assert note["Note"] == "w1-w2-w3"

    def test_the_loop_index_and_count_are_available(self, note, three, logger):
        definition = d.staged(stages=[
            d.note_query("found", "Word:w*"),
            d.list_variable("marks"),
            d.for_each_note("found", [d.store("marks", d.text("{{index}}/{{count}}"))]),
            d.join("marks", "joined", " "),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])
        run(definition, note, logger)
        assert note["Note"] == "1/3 2/3 3/3"

    def test_an_empty_list_reduces_to_the_initial_value(self, note, logger):
        definition = d.staged(stages=[
            d.list_variable("empty"),
            d.reduce("empty", "total", d.code("return accumulator + item"), initial=d.text("0")),
            d.edit_note("trigger", [d.write("Note", d.text("{{total}}"))]),
        ])
        run(definition, note, logger)
        assert note["Note"] == "0"

    def test_a_code_reducer_folds_in_order(self, note, three, logger):
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
        run(definition, note, logger)
        assert note["Note"] == "123"

    def test_a_reducer_that_cannot_add_its_accumulator_fails_the_definition(
        self, note, three, logger
    ):
        definition = d.staged(stages=[
            d.note_query("found", "Word:w*"),
            d.list_variable("words"),
            d.for_each_note("found", [d.store("words", d.text("{{note.Word}}"))]),
            d.reduce("words", "folded", d.code("return accumulator + 1")),
            d.edit_note("trigger", [d.write("Note", d.text("{{folded}}"))]),
        ])
        ok, copied = run(definition, note, logger)
        assert ok is False
        assert copied == []

    def test_a_loop_local_variable_does_not_escape(self, note, three, logger):
        definition = d.staged(stages=[
            d.note_query("found", "Word:w*"),
            d.for_each_note("found", [d.variable("inner", d.text("{{note.Word}}"))]),
            d.edit_note("trigger", [d.write("Note", d.text("{{inner}}"))]),
        ])
        run(definition, note, logger)
        # `inner` is not a binding out here, so the reference falls through and resolves to
        # nothing rather than to the last iteration's value.
        assert note["Note"] == ""


class TestConditions:
    def test_the_then_branch_runs_when_the_predicate_is_true(self, note, logger):
        definition = d.staged(stages=[
            d.condition(
                d.code("return True"),
                [d.edit_note("trigger", [d.write("Note", d.text("yes"))])],
                [d.edit_note("trigger", [d.write("Note", d.text("no"))])],
            )
        ])
        run(definition, note, logger)
        assert note["Note"] == "yes"

    def test_the_else_branch_runs_when_it_is_false(self, note, logger):
        definition = d.staged(stages=[
            d.condition(
                d.code("return False"),
                [d.edit_note("trigger", [d.write("Note", d.text("yes"))])],
                [d.edit_note("trigger", [d.write("Note", d.text("no"))])],
            )
        ])
        run(definition, note, logger)
        assert note["Note"] == "no"

    def test_a_skip_inside_a_branch_ends_the_branch_and_nothing_more(self, note, logger):
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
        assert run(definition, note, logger)[0] is True
        # The rest of the branch is skipped...
        assert note["Meaning"] == "cat"
        # ...and the definition carries on from the stage after the condition, which is what
        # the analyser assumes when it accepts an export of a result declared there (§5.9).
        assert note["Note"] == "after"

    def test_a_skip_in_the_else_branch_behaves_the_same_way(self, note, logger):
        definition = d.staged(stages=[
            d.condition(
                d.code("return False"),
                [],
                [d.note_query("none", "tag:nothing-has-this", if_empty="skip_block")],
            ),
            d.edit_note("trigger", [d.write("Note", d.text("after"))]),
        ])
        assert run(definition, note, logger)[0] is True
        assert note["Note"] == "after"

    def test_a_skip_at_the_root_still_ends_the_definition(self, note, logger):
        definition = d.staged(stages=[
            d.note_query("none", "tag:nothing-has-this", if_empty="skip_block"),
            d.edit_note("trigger", [d.write("Note", d.text("after"))]),
        ])
        assert run(definition, note, logger)[0] is True
        assert note["Note"] == ""

    def test_a_text_predicate_is_true_when_it_produced_something(self, note, logger):
        definition = d.staged(stages=[
            d.condition(
                d.text("{{trigger.Freq}}"),
                [d.edit_note("trigger", [d.write("Note", d.text("has freq"))])],
            )
        ])
        run(definition, note, logger)
        assert note["Note"] == "has freq"

    def test_an_empty_text_predicate_is_false(self, note, logger):
        note["Freq"] = ""
        definition = d.staged(stages=[
            d.condition(
                d.text("{{trigger.Freq}}"),
                [d.edit_note("trigger", [d.write("Note", d.text("has freq"))])],
            )
        ])
        run(definition, note, logger)
        assert note["Note"] == ""


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
            definition, note, copied_into_cards_dict=cards, logger=logger
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
        assert run(definition, note, logger)[0] is True, logger.errors
        assert note["Note"] in ("Recognition", "Recall")


class TestFiles:
    def test_a_write_can_be_read_back_in_the_same_run(self, col, note, media_dir, logger):
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("first line")),
            d.read_file("back", "log.txt"),
            d.edit_note("trigger", [d.write("Note", d.text("{{back}}"))]),
        ])
        assert run(definition, note, logger)[0] is True, logger.errors
        assert note["Note"] == "first line"
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "first line"

    def test_read_modify_write_is_how_appending_is_spelled(self, col, note, media_dir, logger):
        (media_dir / "_log.txt").write_text("a\n", encoding="utf-8")
        definition = d.staged(stages=[
            d.read_file("current", "log.txt"),
            d.write_file("log.txt", d.text("{{current}}b\n")),
        ])
        assert run(definition, note, logger)[0] is True, logger.errors
        # Byte-exact: the newline that went in is the newline that comes back.
        assert (media_dir / "_log.txt").read_bytes() == b"a\nb\n"

    def test_a_missing_file_reads_as_empty_by_default(self, col, note, media_dir, logger):
        definition = d.staged(stages=[
            d.read_file("current", "nope.txt"),
            d.edit_note("trigger", [d.write("Note", d.text("[{{current}}]"))]),
        ])
        run(definition, note, logger)
        assert note["Note"] == "[]"

    def test_if_missing_error_fails_the_definition(self, col, note, media_dir, logger):
        definition = d.staged(stages=[
            d.read_file("current", "nope.txt", if_missing="error"),
            d.edit_note("trigger", [d.write("Note", d.text("x"))]),
        ])
        assert run(definition, note, logger)[0] is False
        assert note["Note"] == ""

    def test_if_missing_skip_block_stops_the_rest_benignly(self, col, note, media_dir, logger):
        definition = d.staged(stages=[
            d.read_file("current", "nope.txt", if_missing="skip_block"),
            d.edit_note("trigger", [d.write("Note", d.text("x"))]),
        ])
        assert run(definition, note, logger)[0] is True
        assert note["Note"] == ""

    def test_overwrite_false_refuses_an_existing_file(self, col, note, media_dir, logger):
        (media_dir / "_log.txt").write_text("keep", encoding="utf-8")
        definition = d.staged(stages=[d.write_file("log.txt", d.text("new"), overwrite=False)])
        assert run(definition, note, logger)[0] is False
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "keep"

    def test_skip_if_exists_leaves_an_existing_file_alone(self, col, note, media_dir, logger):
        (media_dir / "_log.txt").write_text("keep", encoding="utf-8")
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("new"), overwrite=False, skip_if_exists=True)
        ])

        assert run(definition, note, logger)[0] is True, logger.errors
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

        assert run(definition, note, logger)[0] is True, logger.errors
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "first"

    def test_overwrite_false_refuses_a_file_this_run_has_already_written(
        self, col, note, media_dir, logger
    ):
        # The sibling of the `skip_if_exists` case above, and the reason it is worth its own
        # test: checking only the disk made the answer depend on whether a previous run had
        # committed. The same definition overwrote silently the first time and refused the
        # second, with nothing in between to explain the difference.
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("first")),
            d.write_file("log.txt", d.text("second"), overwrite=False),
        ])

        assert run(definition, note, logger)[0] is False
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

        run(definition, note, logger)
        assert logger.has_error("already written earlier in this run")

    def test_overwrite_false_still_writes_a_name_nothing_else_has_taken(
        self, col, note, media_dir, logger
    ):
        definition = d.staged(stages=[
            d.write_file("first.txt", d.text("one")),
            d.write_file("second.txt", d.text("two"), overwrite=False),
        ])

        assert run(definition, note, logger)[0] is True, logger.errors
        assert (media_dir / "_second.txt").read_text(encoding="utf-8") == "two"

    def test_skip_if_exists_still_writes_when_nothing_is_there(
        self, col, note, media_dir, logger
    ):
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("new"), overwrite=False, skip_if_exists=True)
        ])

        assert run(definition, note, logger)[0] is True, logger.errors
        assert (media_dir / "_log.txt").read_text(encoding="utf-8") == "new"

    def test_a_filename_with_a_path_separator_is_refused(self, col, note, media_dir, logger):
        definition = d.staged(stages=[d.write_file("../escape.txt", d.text("x"))])
        assert run(definition, note, logger)[0] is False
        assert logger.has_error("path separator") or logger.has_error("'..'")

    def test_a_failed_definition_writes_no_file_at_all(self, col, note, media_dir, logger):
        # File writes are queued during evaluation and applied only once the definition has
        # validated, so a later failure leaves the media folder alone.
        definition = d.staged(stages=[
            d.write_file("log.txt", d.text("x")),
            d.edit_note("trigger", [d.write("Nonexistent", d.text("y"))]),
        ])
        assert run(definition, note, logger)[0] is False
        assert not (media_dir / "_log.txt").exists()

    def test_invalid_utf8_fails_the_read(self, col, note, media_dir, logger):
        (media_dir / "_log.txt").write_bytes(b"\xff\xfe not utf 8")
        definition = d.staged(stages=[d.read_file("current", "log.txt")])
        assert run(definition, note, logger)[0] is False
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
        ok, _copied = run(parent, note, logger, definitions_for_calls=[child, parent])
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
            parent, note, logger, definitions_for_calls=[child, middle, parent]
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
            parent, note, logger, definitions_for_calls=[child, middle, parent]
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
        ok, _copied = run(parent, note, logger)
        assert ok is True, logger.errors
        assert note["Note"] == "from neko"

    def test_a_definition_that_calls_nothing_does_not_read_the_config(
        self, col, note, logger, stub_mw
    ):
        # An unreadable definition sitting in the config is no reason for one that does not
        # call it to fail.
        stub_mw.addonManager.configs["copy_anywhere"]["copy_definitions"] = [
            {"guid": "broken", "copy_mode": "Within note", "copy_into_note_types": ["a list"]}
        ]
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("fine"))]),
        ])
        assert run(definition, note, logger)[0] is True
        assert note["Note"] == "fine"

    def test_the_callee_sees_note_edits_but_not_the_callers_variables(self, col, note, logger):
        child = d.staged(
            "child",
            guid="child-guid",
            stages=[
                # `{{M}}` is the caller's variable, which the callee has no access to, so it
                # resolves to nothing rather than to the caller's value.
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
        ok, _copied = run(parent, note, logger, definitions_for_calls=[child, parent])
        assert ok is True, logger.errors
        assert note["Note"] == "[][edited first]"

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
        ok, copied = run(parent, note, logger, definitions_for_calls=[child, parent])
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

        ok, _copied = run(parent, note, logger, definitions_for_calls=[child, parent])

        assert ok is False
        assert logger.has_error("missing copy mode value")
        assert note["Note"] == ""

    def test_a_failing_callee_stops_the_parent_committing_anything(self, col, note, logger):
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
        ok, copied = run(parent, note, logger, definitions_for_calls=[child, parent])
        assert ok is False
        assert copied == []

    def test_a_direct_cycle_is_refused_at_run_time_too(self, col, note, logger):
        # Saving refuses these, but the JSON can be hand-edited, so the runtime checks the
        # active guid stack as well.
        a = d.staged("a", guid="a", stages=[d.call_definition("b")])
        b = d.staged("b", guid="b", stages=[d.call_definition("a")])
        ok, _copied = run(a, note, logger, definitions_for_calls=[a, b])
        assert ok is False
        assert logger.has_error("call cycle")

    def test_an_indirect_cycle_is_refused_too(self, col, note, logger):
        a = d.staged("a", guid="a", stages=[d.call_definition("b")])
        b = d.staged("b", guid="b", stages=[d.call_definition("c")])
        c = d.staged("c", guid="c", stages=[d.call_definition("a")])
        ok, _copied = run(a, note, logger, definitions_for_calls=[a, b, c])
        assert ok is False
        assert logger.has_error("call cycle")


class TestFacades:
    def test_code_cannot_write_through_a_facade(self, note, logger):
        definition = d.staged(stages=[
            d.variable("x", d.code("note['Note'] = 'nope'\nreturn 'ok'")),
        ])
        ok, _copied = run(definition, note, logger)
        assert ok is False
        assert note["Note"] == ""

    def test_code_sees_an_earlier_stages_pending_edit(self, note, logger):
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("pending"))]),
            d.variable("seen", d.code("return trigger['Note']")),
            d.edit_note("trigger", [d.write("Meaning", d.text("{{seen}}"))]),
        ])
        assert run(definition, note, logger)[0] is True, logger.errors
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
        assert run(definition, note, logger)[0] is True, logger.errors
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
        assert run(definition, note, logger)[0] is True, logger.errors
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
        ok, copied = run(definition, note, logger)
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
        session = ExecutionSession(logger=logger, collect_trace=True)
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

    def test_every_stage_leaves_a_trace_event(self, col, note, logger):
        definition = d.staged(stages=[
            d.variable("M", d.text("x")),
            d.edit_note("trigger", [d.write("Note", d.text("{{M}}"))]),
        ])
        session = ExecutionSession(logger=logger, collect_trace=True)
        run_definition_for_trigger_note(definition, note, session, committer=PreviewCommitter())
        assert [event.stage_type for event in session.trace] == ["variable", "edit_note"]
        assert all(event.status == "ok" for event in session.trace)
        assert session.trace[0].result == "x"


class TestCancellation:
    def test_a_cancel_between_loop_iterations_commits_nothing(self, col, note, logger):
        real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A"})
        real_anki.add_note(col, VOCAB, {"Word": "b", "Meaning": "B"})
        definition = d.staged(stages=[
            d.note_query("found", '"Word:a" OR "Word:b"'),
            d.for_each_note(
                "found", [d.edit_note("note", [d.write("Note", d.text("touched"))])]
            ),
        ])
        session = ExecutionSession(logger=logger, want_cancel=lambda: True)
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


class TestAddNoteCompatibility:
    def test_an_incompatible_definition_cannot_commit_under_the_add_backstop(
        self, col, logger
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
            logger=logger,
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
            definition, new_note, logger=logger, add_note_compatible_only=True
        )
        assert ok is True, logger.errors
        assert new_note["Meaning"] == "猫"


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

    def test_a_natively_authored_write_runs(self, note, other, logger):
        # The regression this guards: every write in a definition built in the new editor
        # was skipped on unfocus, while its tags still applied, so the definition looked
        # like it had run and only the field writes were missing.
        ok, copied = run(self.definition(), note, logger, field_only="Word")
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]

    def test_a_migrated_write_is_still_limited_to_its_own_trigger_fields(
        self, note, other, logger
    ):
        ok, copied = run(
            self.definition(unfocus_trigger_fields=["Reading"]), note, logger, field_only="Word"
        )
        assert ok is True
        # The tag in the same stage is not gated, so the note is still touched -- which is
        # exactly what made the skipped writes so hard to see.
        assert [n["Note"] for n in copied] == [""]
        assert copied[0].has_tag("ran")

    def test_a_migrated_write_whose_trigger_field_matches_runs(self, note, other, logger):
        ok, copied = run(
            self.definition(unfocus_trigger_fields=["Word"]), note, logger, field_only="Word"
        )
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]

    def test_a_migrated_write_that_watches_nothing_never_runs(self, note, other, logger):
        # An empty list is format 1 saying this write has no editor field to trigger it,
        # which is not the same as a write that was never migrated at all.
        ok, copied = run(
            self.definition(unfocus_trigger_fields=[]), note, logger, field_only="Word"
        )
        assert ok is True
        assert [n["Note"] for n in copied] == [""]

    def test_a_run_that_is_not_an_unfocus_gates_nothing(self, note, other, logger):
        ok, copied = run(self.definition(unfocus_trigger_fields=["Reading"]), note, logger)
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]

    def test_a_write_turned_off_for_editing_does_not_run_while_editing(
        self, note, other, logger
    ):
        # Format 1's `copy_on_unfocus_when_edit`, which is how a slow write -- downloading
        # audio, say -- was kept for the bulk action instead of running on every keystroke
        # that left a watched field.
        ok, copied = run(
            self.definition(unfocus_trigger_fields=["Word"], unfocus_when_edit=False),
            note,
            logger,
            field_only="Word",
        )
        assert ok is True
        assert [n["Note"] for n in copied] == [""]

    def test_that_same_write_runs_while_adding_if_it_is_on_for_adding(
        self, note, other, logger
    ):
        ok, copied = run(
            self.definition(
                unfocus_trigger_fields=["Word"], unfocus_when_edit=False, unfocus_when_add=True
            ),
            note,
            logger,
            field_only="Word",
            unfocus_is_add=True,
        )
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]

    def test_a_write_on_for_editing_only_does_not_run_while_adding(self, note, other, logger):
        ok, copied = run(
            self.definition(
                unfocus_trigger_fields=["Word"], unfocus_when_edit=True, unfocus_when_add=False
            ),
            note,
            logger,
            field_only="Word",
            unfocus_is_add=True,
        )
        assert ok is True
        assert [n["Note"] for n in copied] == [""]

    def test_the_flags_do_not_reach_a_run_that_is_not_an_unfocus(self, note, other, logger):
        # The bulk action the slow write was being saved for.
        ok, copied = run(
            self.definition(unfocus_when_edit=False, unfocus_when_add=False), note, logger
        )
        assert ok is True
        assert [n["Note"] for n in copied] == ["neko"]
