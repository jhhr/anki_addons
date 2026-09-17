"""Characterization tests for `copy_for_single_trigger_note`: the two copy modes.

Within note is the common shape (a field read and written on the same note); Across notes is
the one that runs a query and then has to decide which side of it the trigger note is on.
The asymmetries between the two directions are the interesting part, and most of them are
not stated anywhere -- they fall out of which list the guards happen to look at.
"""

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note


@pytest.fixture
def note(col):
    return real_anki.add_note(
        col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat", "Freq": "5"}
    )


class TestWithinNote:
    def test_a_plain_field_to_field_copy(self, col, note):
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}: {{Meaning}}")]
        )
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == "neko: cat"

    def test_the_trigger_note_is_modified_in_place_not_written_to_the_database(
        self, col, note
    ):
        # Nothing here writes; the caller is expected to run update_notes over
        # copied_into_notes. A definition run in the editor relies on exactly that.
        definition = d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")])
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"
        assert col.get_note(note.id)["Note"] == ""

    def test_two_fields_can_be_swapped(self, col, note):
        # The source note is a duplicate taken before any def runs, so both defs read the
        # pre-copy values. Without that, the second def would read what the first wrote.
        definition = d.within_note(
            field_to_field_defs=[
                d.field_to_field("Word", "{{Meaning}}"),
                d.field_to_field("Meaning", "{{Word}}"),
            ]
        )
        copy_for_single_trigger_note(definition, note)
        assert note["Word"] == "cat"
        assert note["Meaning"] == "neko"

    def test_copy_if_empty_skips_a_field_that_already_has_a_value(self, note):
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Meaning", "{{Word}}", copy_if_empty=True)]
        )
        copy_for_single_trigger_note(definition, note)
        assert note["Meaning"] == "cat"

    def test_copy_if_empty_fills_a_field_that_is_empty(self, note):
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}", copy_if_empty=True)]
        )
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"

    def test_a_field_that_is_not_on_the_note_aborts_the_definition(self, note, logger):
        # KeyError on the destination field is a CopyFailedException, so the defs after it
        # do not run and the whole note is reported as a failure.
        definition = d.within_note(
            field_to_field_defs=[
                d.field_to_field("Nonexistent", "{{Word}}"),
                d.field_to_field("Note", "{{Word}}"),
            ]
        )
        assert copy_for_single_trigger_note(definition, note) is False
        assert note["Note"] == ""
        assert logger.has_error("not found in note")

    def test_query_note_index_is_set_to_one_even_in_within_note_mode(self, note):
        # The UI only offers this variable for Across mode, but the destination loop sets it
        # regardless, so it resolves rather than being an invalid field.
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{__Query_Note_Index}}")]
        )
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "1"

    def test_target_notes_count_is_not_set_in_within_note_mode(self, note, logger):
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{__Target_Notes_Count}}")]
        )
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == ""
        assert logger.has_error("__target_notes_count")

    def test_a_missing_copy_mode_is_an_error(self, note, logger):
        definition = d.within_note()
        definition["copy_mode"] = None
        assert copy_for_single_trigger_note(definition, note) is False
        assert logger.has_error("missing copy mode value")


class TestAcrossNotesDirections:
    @pytest.fixture
    def sources(self, col):
        return [
            real_anki.add_note(col, VOCAB, {"Word": "a", "Meaning": "A", "Freq": "1"}),
            real_anki.add_note(col, VOCAB, {"Word": "b", "Meaning": "B", "Freq": "2"}),
        ]

    def test_a_missing_across_mode_direction_is_an_error(self, note, logger):
        definition = d.destination_to_sources(copy_from_cards_query="Word:a")
        definition["across_mode_direction"] = None
        assert copy_for_single_trigger_note(definition, note) is False
        assert logger.has_error("missing across mode direction value")

    def test_a_bogus_across_mode_direction_is_an_error(self, note, logger):
        definition = d.destination_to_sources(copy_from_cards_query="Word:a")
        definition["across_mode_direction"] = "Sideways"
        assert copy_for_single_trigger_note(definition, note) is False
        assert logger.has_error("missing across mode direction value")

    def test_destination_to_sources_joins_the_query_results_into_the_trigger_note(
        self, note, sources
    ):
        definition = d.destination_to_sources(
            copy_from_cards_query='"Word:a" OR "Word:b"',
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            select_card_count="0",
            select_card_separator="+",
        )
        assert copy_for_single_trigger_note(definition, note) is True
        assert sorted(note["Note"].split("+")) == ["a", "b"]

    def test_source_to_destinations_writes_into_every_query_result(
        self, col, note, sources
    ):
        # The destination notes are the ones the query loaded, not the objects the fixture
        # holds, and nothing is written to the database here -- the caller is expected to run
        # update_notes over copied_into_notes -- so that list is what a test has to look at.
        definition = d.source_to_destinations(
            copy_from_cards_query='"Word:a" OR "Word:b"',
            field_to_field_defs=[d.field_to_field("Note", "from {{Word}}")],
            select_card_count="0",
        )
        copied_into_notes = []
        assert (
            copy_for_single_trigger_note(
                definition, note, copied_into_notes=copied_into_notes
            )
            is True
        )
        assert sorted(n.id for n in copied_into_notes) == sorted(s.id for s in sources)
        assert [n["Note"] for n in copied_into_notes] == ["from neko", "from neko"]
        assert note["Note"] == ""

    def test_source_to_destinations_increments_query_note_index_across_destinations(
        self, note, sources
    ):
        # There is one source note, so get_field_values_from_notes leaves the index alone and
        # the outer destination loop's value survives into each destination.
        definition = d.source_to_destinations(
            copy_from_cards_query='"Word:a" OR "Word:b"',
            field_to_field_defs=[d.field_to_field("Note", "{{__Query_Note_Index}}")],
            select_card_count="0",
        )
        copied_into_notes = []
        copy_for_single_trigger_note(
            definition, note, copied_into_notes=copied_into_notes
        )
        assert sorted(n["Note"] for n in copied_into_notes) == ["1", "2"]

    def test_a_destination_can_rewrite_its_own_field_through_the_dest_prefix(
        self, col, note
    ):
        # The most intricate live shape: each destination note rewrites its own field using
        # values interpolated from the trigger note. `{{__Dest__Note}}` is the destination's
        # own pre-copy value, `{{Word}}` the trigger note's.
        target = real_anki.add_note(col, VOCAB, {"Word": "a", "Note": "existing"})
        definition = d.source_to_destinations(
            copy_from_cards_query="Word:a",
            field_to_field_defs=[d.field_to_field("Note", "{{__Dest__Note}}, {{Word}}")],
            select_card_count="0",
        )
        copied_into_notes = []
        copy_for_single_trigger_note(
            definition, note, copied_into_notes=copied_into_notes
        )
        assert [n.id for n in copied_into_notes] == [target.id]
        assert copied_into_notes[0]["Note"] == "existing, neko"


class TestNoSourcesFound:
    def test_without_the_flag_zero_sources_leaves_the_target_field_alone(self, note):
        definition = d.destination_to_sources(
            copy_from_cards_query="Word:nothing-matches-this",
            field_to_field_defs=[d.field_to_field("Meaning", "{{Word}}")],
        )
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Meaning"] == "cat"

    def test_with_the_flag_zero_sources_wipes_the_target_field(self, note):
        # The whole point of the flag: the definition runs with an empty source list, so
        # get_field_values_from_notes returns "" and that "" is written.
        definition = d.destination_to_sources(
            copy_from_cards_query="Word:nothing-matches-this",
            field_to_field_defs=[d.field_to_field("Meaning", "{{Word}}")],
            run_also_if_no_sources_found=True,
        )
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Meaning"] == ""

    def test_the_flag_is_effectively_destination_to_sources_only(self, col, note):
        # In Source-to-destinations `source_notes` is always `[trigger_note]`, so the
        # zero-source guard never fires; an empty query instead means the destination loop
        # body never runs, and nothing is wiped whatever the flag says.
        definition = d.source_to_destinations(
            copy_from_cards_query="Word:nothing-matches-this",
            field_to_field_defs=[d.field_to_field("Meaning", "{{Word}}")],
            run_also_if_no_sources_found=True,
        )
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Meaning"] == "cat"


class TestDuplicateQueryResults:
    def test_two_cards_of_one_note_duplicate_the_value(self, col, note):
        # select_card_by "None" pops card ids without de-duplicating their notes, so a note
        # whose two cards both match is joined in twice. Only the select_card_count 0 path
        # goes through SELECT DISTINCT.
        real_anki.add_note(col, VOCAB, {"Word": "dup", "Meaning": "twice"})
        definition = d.destination_to_sources(
            copy_from_cards_query="Word:dup",
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            select_card_by="None",
            select_card_count="2",
            select_card_separator="+",
        )
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "dup+dup"

    def test_select_card_count_zero_de_duplicates_them(self, col, note):
        real_anki.add_note(col, VOCAB, {"Word": "dup", "Meaning": "twice"})
        definition = d.destination_to_sources(
            copy_from_cards_query="Word:dup",
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            select_card_count="0",
            select_card_separator="+",
        )
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "dup"
