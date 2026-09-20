"""Characterization tests for Step 3 of `copy_for_single_trigger_note`: the copy condition.

The condition is a search run as `f"{interpolated} nid:{trigger_note.id}"`, which makes it a
per-note gate rather than a note selector -- and gives it two failure modes that look alike
from the outside but are not: a condition that does not match is benign and returns `True`,
while a condition that cannot be interpolated returns `False` and stops the caller's bulk
loop dead. This file pins that split, the `nid:` scoping (including what it does to a
not-yet-added note), the `condition_only_on_sync` escape hatch, and the fact that variables
are resolved before any of it happens.
"""

import time

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import ProgressUpdater, copy_for_single_trigger_note


@pytest.fixture
def note(col):
    return real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat", "Freq": ""})


def copy_note_field(**extra):
    """A Within-note definition that writes Word into Note, so a run is visible."""
    return d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")], **extra)


def make_progress_updater(total_notes_count: int = 1) -> ProgressUpdater:
    return ProgressUpdater(
        start_time=time.time(),
        definition_name="conditional",
        total_notes_count=total_notes_count,
        is_across=False,
        title=None,
    )


class TestConditionMatching:
    def test_a_matching_condition_lets_the_copy_run(self, note):
        definition = copy_note_field(copy_condition_query="Word:neko")
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == "neko"

    def test_a_non_matching_condition_skips_the_note_benignly(self, note, logger):
        # True, not False: a note the condition rejects is a normal outcome and the caller's
        # bulk loop keeps going. Compare the interpolation failure below, which returns False.
        definition = copy_note_field(copy_condition_query="Word:inu")
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == ""
        assert logger.has_debug("did not match for note id")

    def test_no_condition_query_means_no_condition_check(self, note):
        definition = copy_note_field()
        assert definition["copy_condition_query"] is None
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"

    def test_an_empty_condition_query_means_no_condition_check(self, note):
        definition = copy_note_field(copy_condition_query="")
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"

    def test_a_skipped_note_is_not_counted_by_the_progress_updater(self, note):
        # The count increment sits after the condition check on purpose, so the progress
        # label reports notes actually copied into rather than notes considered.
        updater = make_progress_updater()
        definition = copy_note_field(copy_condition_query="Word:inu")
        copy_for_single_trigger_note(definition, note, progress_updater=updater)
        assert updater.get_counts() == (0, 0, 0, 0, 0)

    def test_a_matched_note_is_counted_by_the_progress_updater(self, note):
        updater = make_progress_updater()
        definition = copy_note_field(copy_condition_query="Word:neko")
        copy_for_single_trigger_note(definition, note, progress_updater=updater)
        note_cnt, sources, destinations, _files, _cards = updater.get_counts()
        assert (note_cnt, sources, destinations) == (1, 1, 1)


class TestTheQueryIsScopedToTheTriggerNote:
    def test_a_condition_matching_a_different_note_does_not_match_this_one(
        self, col, note
    ):
        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        definition = copy_note_field(copy_condition_query="Word:inu")
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == ""

    def test_a_condition_that_matches_everything_matches_the_trigger_note(self, note):
        definition = copy_note_field(copy_condition_query="deck:Default")
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"

    def test_a_new_note_never_matches_because_its_nid_is_zero(self, col, logger):
        # A note that has not been added yet has id 0, so `nid:0` matches nothing no matter
        # what the rest of the condition says. Every conditional definition is therefore
        # silently skipped on add -- the note here would match "Word:neko" once it existed.
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        definition = copy_note_field(copy_condition_query="Word:neko")
        assert copy_for_single_trigger_note(definition, new_note) is True
        assert new_note["Note"] == ""
        assert logger.has_debug("did not match for note id 0")

    def test_the_same_new_note_is_copied_into_when_there_is_no_condition(self, col):
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        copy_for_single_trigger_note(copy_note_field(), new_note)
        assert new_note["Note"] == "neko"


class TestConditionOnlyOnSync:
    def test_the_condition_is_skipped_entirely_when_not_syncing(self, note):
        # This is what "Vocab note - sentence furigana processing" relies on: its
        # `tag:0-vocab-sentence-reprocess` condition narrows the nightly sync, while a
        # manual run copies into everything regardless of the tag.
        definition = copy_note_field(
            copy_condition_query="tag:0-vocab-sentence-reprocess",
            condition_only_on_sync=True,
        )
        assert copy_for_single_trigger_note(definition, note, is_sync=False) is True
        assert note["Note"] == "neko"

    def test_the_condition_is_enforced_when_syncing(self, note):
        definition = copy_note_field(
            copy_condition_query="tag:0-vocab-sentence-reprocess",
            condition_only_on_sync=True,
        )
        assert copy_for_single_trigger_note(definition, note, is_sync=True) is True
        assert note["Note"] == ""

    def test_a_matching_condition_on_sync_still_runs_the_copy(self, col):
        tagged = real_anki.add_note(
            col,
            VOCAB,
            {"Word": "neko", "Meaning": "cat"},
            tags=["0-vocab-sentence-reprocess"],
        )
        definition = copy_note_field(
            copy_condition_query="tag:0-vocab-sentence-reprocess",
            condition_only_on_sync=True,
        )
        copy_for_single_trigger_note(definition, tagged, is_sync=True)
        assert tagged["Note"] == "neko"

    def test_is_sync_defaults_to_false_so_the_condition_is_skipped_by_default(self, note):
        definition = copy_note_field(copy_condition_query="Word:inu", condition_only_on_sync=True)
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"

    def test_an_uninterpolatable_condition_is_harmless_when_not_syncing(self, note, logger):
        # Skipping the condition skips the interpolation too, so the abort-the-loop failure
        # pinned below cannot happen off-sync for a definition with this flag set.
        definition = copy_note_field(copy_condition_query="{{Freq}}", condition_only_on_sync=True)
        assert copy_for_single_trigger_note(definition, note, is_sync=False) is True
        assert note["Note"] == "neko"
        assert logger.errors == []


class TestConditionInterpolation:
    def test_a_condition_can_interpolate_a_field_of_the_trigger_note(self, note):
        definition = copy_note_field(copy_condition_query="Word:{{Word}}")
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"

    def test_a_condition_that_interpolates_to_empty_returns_false(self, note, logger):
        # Freq is a real but empty field, so interpolation succeeds and yields "". The falsy
        # result is treated as a failure, and False stops the caller's whole bulk loop --
        # unlike the benign True of a condition that merely did not match.
        definition = copy_note_field(copy_condition_query="{{Freq}}")
        assert copy_for_single_trigger_note(definition, note) is False
        assert note["Note"] == ""
        assert logger.has_error("resolved to nothing for note id")

    def test_the_empty_interpolation_error_names_the_query_it_resolved(self, note, logger):
        # Migration names the note the reference meant, so the message can quote the search
        # it actually ran rather than blaming "missing fields" -- which, for a field that
        # exists and is empty, printed an empty list of them (§11).
        definition = copy_note_field(copy_condition_query="{{Freq}}")
        copy_for_single_trigger_note(definition, note)
        assert logger.errors == [
            "Error in copy fields: Condition query '{{trigger.Freq}}' resolved to nothing"
            f" for note id {note.id}"
        ]

    def test_an_unknown_field_fails_the_definition_rather_than_truncating_the_search(
        self, note, logger
    ):
        # Format 1 never consulted its own invalid-field list: an unknown field became ""
        # and the truncated `Word:neko` ran, matched, and copied. A reference that resolves
        # to nothing is a stage error now, so the condition is not guessed at (§11).
        definition = copy_note_field(copy_condition_query="Word:neko {{Nonexistent}}")
        assert copy_for_single_trigger_note(definition, note) is False
        assert note["Note"] == ""
        assert logger.has_error("Nonexistent")

    def test_a_syntactically_invalid_condition_raises_out_of_the_function(self, note):
        # find_notes is not guarded here, so a search error is neither logged nor turned into
        # a False return; it propagates past the caller's bulk loop as an exception.
        from anki.errors import SearchError

        definition = copy_note_field(copy_condition_query="Word:(")
        with pytest.raises(SearchError):
            copy_for_single_trigger_note(definition, note)


class TestVariablesAreResolvedBeforeTheCondition:
    def test_a_variable_can_supply_the_condition_query(self, note):
        definition = copy_note_field(copy_condition_query="{{search}}")
        definition["field_to_variable_defs"] = [d.field_to_variable("search", "Word:{{Word}}")]
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == "neko"

    def test_variables_are_computed_even_for_a_note_the_condition_skips(self, note, logger):
        # Step 1 runs before Step 3, so the cost of resolving variables is paid for every
        # trigger note, including the ones the condition is about to throw away. A variable
        # naming a field the note has not is now what proves it: it used to log an invalid
        # field and carry on, and it fails the definition instead (§11).
        definition = copy_note_field(copy_condition_query="Word:inu")
        definition["field_to_variable_defs"] = [d.field_to_variable("v", "{{Nonexistent}}")]
        assert copy_for_single_trigger_note(definition, note) is False
        assert note["Note"] == ""
        assert logger.has_error("Nonexistent")


class TestTheConditionGatesTheSourceQuery:
    def test_a_non_matching_condition_stops_across_mode_before_its_query(self, col, note):
        destination = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        definition = d.source_to_destinations(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            copy_from_cards_query="Word:inu",
            copy_condition_query="Word:zzz",
        )
        copied_into_notes = []
        assert (
            copy_for_single_trigger_note(
                definition, note, copied_into_notes=copied_into_notes
            )
            is True
        )
        assert copied_into_notes == []
        assert destination["Note"] == ""

    def test_a_matching_condition_lets_across_mode_run_its_query(self, col, note):
        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        definition = d.source_to_destinations(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            copy_from_cards_query="Word:inu",
            copy_condition_query="Word:neko",
        )
        copied_into_notes = []
        copy_for_single_trigger_note(
            definition, note, copied_into_notes=copied_into_notes
        )
        assert [n["Note"] for n in copied_into_notes] == ["neko"]


class TestCardPropertyConditions:
    """The condition is `find_notes`, a card-aware search, so card state is fair game."""

    def test_a_condition_on_is_new_matches_an_unreviewed_note(self, note):
        definition = copy_note_field(copy_condition_query="-is:suspended is:new")
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"

    def test_suspending_the_cards_makes_a_minus_is_suspended_condition_stop_matching(
        self, col, note
    ):
        col.sched.suspend_cards([card.id for card in note.cards()])
        definition = copy_note_field(copy_condition_query="-is:suspended is:new")
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == ""

    def test_one_suspended_card_of_two_is_enough_for_an_is_suspended_condition(
        self, col, note
    ):
        # find_notes returns the note if any of its cards matches, so a card-property
        # condition on a multi-card note type is an "any card" test, not an "all cards" one.
        assert len(note.cards()) == 2
        col.sched.suspend_cards([note.cards()[0].id])
        definition = copy_note_field(copy_condition_query="is:suspended")
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"

    def test_prop_due_does_not_match_a_new_card(self, note):
        definition = copy_note_field(copy_condition_query="prop:due>0")
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == ""

    def test_prop_due_matches_a_review_card_scheduled_in_the_future(self, col, note):
        card = note.cards()[0]
        card.type = 2
        card.queue = 2
        card.due = col.sched.today + 5
        col.update_card(card)
        definition = copy_note_field(copy_condition_query="prop:due>0")
        copy_for_single_trigger_note(definition, note)
        assert note["Note"] == "neko"
