"""What a preview run reports, and what it must not do.

The preview's value rests on two claims: that it computed what a real run would have
computed, and that it changed nothing. Both are checked here against a real collection, and
so is the trace the preview pane reads -- inputs, planned changes, query counts and the
per-iteration events a loop produces.
"""

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note
from copy_anywhere.logic.preview import (
    find_trigger_notes,
    preview_note,
    run_preview,
    trigger_note_query,
)


@pytest.fixture
def note(col):
    return real_anki.add_note(
        col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat", "Freq": "5"}
    )


class TestPreviewRun:
    def test_a_preview_reports_the_field_it_would_write_and_writes_nothing(self, col, note):
        definition = d.staged(stages=[
            d.variable("M", d.text("{{trigger.Word}}!")),
            d.edit_note("trigger", [d.write("Note", d.text("{{M}}"))]),
        ])
        run = run_preview(definition, preview_note(note.id))

        assert run.succeeded is True
        assert [planned["fields"]["Note"] for planned in run.notes] == ["neko!"]
        # The note in the collection is untouched, and so is the caller's own note object.
        assert col.get_note(note.id)["Note"] == ""
        assert note["Note"] == ""

    def test_a_preview_computes_what_a_real_run_writes(self, col, note):
        definition = d.staged(stages=[
            d.note_query("found", "Word:neko"),
            d.for_each_note(
                "found",
                [d.edit_note("note", [d.write("Note", d.text("{{index}}/{{count}}"))])],
            ),
        ])
        previewed = run_preview(definition, preview_note(note.id))
        assert copy_for_single_trigger_note(definition, note) is True

        assert [planned["fields"]["Note"] for planned in previewed.notes] == [note["Note"]]

    def test_a_previewed_file_write_never_reaches_the_media_folder(self, col, note, media_dir):
        definition = d.staged(stages=[d.write_file("out.txt", d.text("{{trigger.Word}}"))])
        run = run_preview(definition, preview_note(note.id))

        assert [planned["content"] for planned in run.files] == ["neko"]
        assert not (media_dir / "_out.txt").exists()

    def test_a_failing_stage_is_reported_rather_than_raised(self, col, note):
        definition = d.staged(stages=[d.edit_note("trigger", [d.write("Nope", d.text("x"))])])
        run = run_preview(definition, preview_note(note.id))

        assert run.succeeded is False
        assert any("Nope" in message for message in run.messages)

    def test_a_note_the_deck_whitelist_rejects_says_so_instead_of_running(self, col, note):
        definition = d.staged(
            stages=[d.edit_note("trigger", [d.write("Note", d.text("x"))])],
            deck_names=["Nonexistent deck"],
        )
        run = run_preview(definition, preview_note(note.id))

        assert run.notes == []
        assert any("trigger settings" in message for message in run.messages)

    def test_a_called_definition_runs_inside_the_preview(self, col, note):
        # A name of its own gives the callee a guid of its own: the builder derives the guid
        # from the name, and a callee sharing the caller's guid is a call to itself.
        callee = d.staged(
            "callee",
            stages=[d.variable("H", d.text("from the callee"))],
            exports=[{"name": "H", "stage_guid": None}],
        )
        callee["exports"][0]["stage_guid"] = callee["stages"][0]["guid"]
        definition = d.staged(stages=[
            d.call_definition(
                callee["guid"], outputs=[{"export": "H", "result": "got"}]
            ),
            d.edit_note("trigger", [d.write("Note", d.text("{{got}}"))]),
        ])
        run = run_preview(definition, preview_note(note.id), definitions_for_calls=[callee])

        assert run.succeeded is True
        assert [planned["fields"]["Note"] for planned in run.notes] == ["from the callee"]


class TestTrace:
    def test_a_stage_records_the_bindings_it_could_see(self, col, note):
        definition = d.staged(stages=[
            d.variable("M", d.text("x")),
            d.variable("N", d.text("y")),
        ])
        run = run_preview(definition, preview_note(note.id))

        first, second = run.trace
        # The first stage has only the trigger; the second can also see the first's result.
        assert set(first.inputs) == {"trigger"}
        assert set(second.inputs) == {"trigger", "M"}
        assert second.inputs["M"] == "x"

    def test_a_note_edit_records_the_fields_and_tags_it_changed(self, col, note):
        definition = d.staged(stages=[
            d.edit_note(
                "trigger", [d.write("Note", d.text("hello"))], tags={"add": ["seen"]}
            ),
        ])
        run = run_preview(definition, preview_note(note.id))

        mutations = run.trace[0].mutations
        assert any("Note:" in line and "hello" in line for line in mutations)
        assert any("+tags seen" in line for line in mutations)

    def test_a_field_an_edit_left_alone_is_not_reported_as_a_change(self, col, note):
        definition = d.staged(stages=[
            d.edit_note("trigger", [d.write("Note", d.text("hello"))]),
        ])
        run = run_preview(definition, preview_note(note.id))

        assert not any("Word" in line for line in run.trace[0].mutations)

    def test_a_query_records_what_it_searched_and_how_much_it_found(self, col, note):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "other"})
        definition = d.staged(stages=[d.note_query("found", "Word:neko", "first", 1)])
        run = run_preview(definition, preview_note(note.id))

        details = run.trace[0].details
        assert details["query"] == "Word:neko"
        assert details["found"] == 2
        assert details["selected"] == 1

    def test_a_search_condition_records_what_it_searched(self, col, note):
        # The pane a user opens to see why a branch did not run has to show the search the
        # condition actually made, resolved and scoped to its note, the way a query stage's
        # event does.
        definition = d.staged(stages=[
            d.condition(
                d.text("Word:{{trigger.Word}}"),
                [d.variable("M", d.text("matched"))],
                predicate_kind="note_query",
                predicate_target={"binding": "trigger"},
            ),
        ])
        run = run_preview(definition, preview_note(note.id))

        details = run.trace[0].details
        assert details["query"] == f"(Word:neko) nid:{note.id}"
        assert details["found"] == 1

    def test_a_search_condition_on_a_note_not_added_yet_says_how_it_was_judged(self, col):
        # `nid:0` would find nothing, so the search is answered without the collection, and
        # the trace says so rather than showing a search nobody ran.
        definition = d.staged(stages=[
            d.condition(
                d.text("Word:{{trigger.Word}}"),
                [d.variable("M", d.text("matched"))],
                predicate_kind="note_query",
                predicate_target={"binding": "trigger"},
            ),
        ])
        unsaved = col.new_note(col.models.by_name(VOCAB))
        unsaved["Word"] = "neko"
        run = run_preview(definition, unsaved, deck_id=col.decks.id("JP vocab"))

        details = run.trace[0].details
        assert details["query"] == "(Word:neko)"
        assert details["found"] == 1
        assert "without the collection" in details["judged"]

    def test_a_file_write_records_the_name_it_would_write(self, col, note, media_dir):
        definition = d.staged(stages=[d.write_file("out.txt", d.text("body"))])
        run = run_preview(definition, preview_note(note.id))

        assert run.trace[0].mutations == ["write '_out.txt' (4 characters)"]

    def test_a_card_edit_records_what_the_card_would_become(self, col, note):
        definition = d.staged(stages=[
            d.card_query("cards", f"nid:{note.id}", "first", 1),
            d.for_each_card(
                "cards",
                [
                    d.edit_card("card", [{
                        "guid": "ca",
                        "card_type_name": "",
                        "change_deck": "JP vocab",
                        "set_flag": 2,
                        "suspend": None,
                        "bury": None,
                        "set_desired_retention": None,
                        "action_code": None,
                        "use_code": False,
                    }])
                ],
            ),
        ])
        run = run_preview(definition, preview_note(note.id))

        edit_event = run.events_for(definition["stages"][1]["body"][0]["guid"])[0]
        assert len(edit_event.mutations) == 1
        assert "flag 2" in edit_event.mutations[0]
        # And the card in the collection kept the deck it was in.
        assert run.cards and col.get_card(run.cards[0]["card_id"]).did != run.cards[0]["deck_id"]

    def test_a_loop_body_leaves_one_event_per_iteration(self, col, note):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "other"})
        edit = d.edit_note("note", [d.write("Note", d.text("{{index}}"))])
        definition = d.staged(stages=[
            d.note_query("found", "Word:neko"),
            d.for_each_note("found", [edit]),
        ])
        run = run_preview(definition, preview_note(note.id))

        events = run.events_for(edit["guid"])
        assert [event.iteration_label() for event in events] == ["1", "2"]
        assert run.events_for(definition["stages"][1]["guid"])[0].details["iterations"] == 2

    def test_a_failed_stage_keeps_its_message_on_its_own_event(self, col, note):
        definition = d.staged(stages=[
            d.variable("M", d.text("x")),
            d.edit_note("trigger", [d.write("Nope", d.text("y"))]),
        ])
        run = run_preview(definition, preview_note(note.id))

        assert [event.status for event in run.trace] == ["ok", "failed"]
        assert "Nope" in (run.trace[1].error or "")

    def test_a_called_definitions_stages_are_children_of_the_call(self, col, note):
        callee = d.staged("callee", stages=[d.variable("H", d.text("hi"))])
        definition = d.staged(stages=[d.call_definition(callee["guid"])])
        run = run_preview(definition, preview_note(note.id), definitions_for_calls=[callee])

        call_event = run.trace[0]
        assert [child.stage_type for child in call_event.children] == ["variable"]
        assert call_event.details["calls"] == callee["definition_name"]

    def test_a_note_binding_is_summarized_by_its_first_field(self, col, note):
        definition = d.staged(stages=[d.variable("M", d.text("x"))])
        run = run_preview(definition, preview_note(note.id))

        assert run.trace[0].inputs["trigger"] == f"<note {note.id}: neko>"


class TestTriggerNoteBrowser:
    def test_the_query_is_scoped_to_the_definitions_note_types(self, col):
        definition = d.staged(note_types=[VOCAB])

        assert trigger_note_query(definition) == f'(note:"{VOCAB}")'
        assert 'Word:neko' in trigger_note_query(definition, "Word:neko")

    def test_the_browser_lists_notes_of_that_type_with_a_readable_label(self, col, note):
        definition = d.staged(note_types=[VOCAB])

        found = find_trigger_notes(definition)

        assert (note.id, "neko") in found

    def test_the_browser_stops_at_the_limit(self, col):
        for index in range(5):
            real_anki.add_note(col, VOCAB, {"Word": f"w{index}"})
        definition = d.staged(note_types=[VOCAB])

        assert len(find_trigger_notes(definition, limit=3)) == 3
