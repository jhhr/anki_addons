"""What a query stage finds, selects and orders.

These were characterization tests for `get_across_target_notes()`, the format-1 function
that ran the one query a definition was allowed and assigned the notes it found to the
across-note roles. The rollout retired it, so the cases that still describe something format
2 does were rewritten against a query stage, and the ones that described what format 2
deliberately replaced were dropped with it: selection by card rather than by note, the
`None` strategy walking `find_cards` backwards, the duplicate notes that fell out of both,
and `Least_reps` (§11, §14.1).

A query stage produces a `NoteList`, which nothing can interpolate (§4.1), so these read the
selection the way a definition would: loop over it, collect one field per note, join, and
write the result somewhere the test can see it.
"""

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import KANJI, VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note

SEPARATOR = "+"


@pytest.fixture
def trigger(col):
    return real_anki.add_note(col, VOCAB, {"Word": "trigger", "Meaning": "t"})


@pytest.fixture
def targets(col):
    """Three notes tagged `pool`, in a known creation order, with ascending `Freq`."""
    return [
        real_anki.add_note(col, VOCAB, {"Word": f"w{i}", "Freq": str(i)}, tags=["pool"])
        for i in range(1, 4)
    ]


def selected(query_stage, trigger, logger, field="Word"):
    """The `field` of each note the query selected, in the order it selected them."""
    definition = d.staged(stages=[
        query_stage,
        d.list_variable("collected"),
        d.for_each_note(
            query_stage["result"],
            [d.store("collected", d.text("{{note.%s}}" % field))],
        ),
        d.join("collected", "joined", SEPARATOR),
        d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
    ])
    succeeded = copy_for_single_trigger_note(
        definition, trigger, copied_into_notes=[]
    )
    assert succeeded is True, logger.errors
    return [value for value in trigger["Note"].split(SEPARATOR) if value]


def run_legacy(trigger, **definition_kwargs):
    """Run a migrated across-note definition, for the cases about migrated markers.

    The guards below -- an unusable `select_card_by`, the empty-result flag -- are format-1
    settings the migrator turns into markers on the query stage, so they can only be reached
    through a definition that was migrated rather than one authored as stages.
    """
    definition = d.destination_to_sources(**definition_kwargs)
    return copy_for_single_trigger_note(
        definition, trigger, copied_into_notes=[]
    )


class TestSelection:
    def test_all_returns_every_note_the_query_found(self, col, trigger, targets, logger):
        found = selected(d.note_query("found", "tag:pool"), trigger, logger)

        assert sorted(found) == ["w1", "w2", "w3"]

    def test_a_note_with_two_matching_cards_is_selected_once(self, col, trigger, logger):
        # Both cards of this note match. Format 1 selected cards and handed back the note
        # twice; selecting over `find_notes` is what makes that impossible (§5.2).
        real_anki.add_note(col, VOCAB, {"Word": "dup", "Meaning": "m"}, tags=["pool"])

        found = selected(d.note_query("found", "tag:pool"), trigger, logger)

        assert found == ["dup"]

    def test_first_takes_them_in_search_order_and_stops_at_the_count(
        self, col, trigger, targets, logger
    ):
        expected = [
            col.get_note(note_id)["Word"] for note_id in col.find_notes("tag:pool")
        ][:2]

        found = selected(
            d.note_query("found", "tag:pool", strategy="first", count=2), trigger, logger
        )

        assert found == expected

    def test_random_picks_from_the_pool_and_never_twice(self, col, trigger, targets, logger):
        found = selected(
            d.note_query("found", "tag:pool", strategy="random", count=3), trigger, logger
        )

        assert set(found) <= {"w1", "w2", "w3"}
        assert len(set(found)) == len(found)

    @pytest.mark.parametrize("strategy", ["first", "random"])
    def test_asking_for_more_than_there_are_returns_what_there_is(
        self, col, trigger, targets, logger, strategy
    ):
        found = selected(
            d.note_query("found", "Word:w1", strategy=strategy, count=10), trigger, logger
        )

        assert found == ["w1"]

    @pytest.mark.parametrize("count", [0, None])
    def test_a_count_of_nothing_means_every_note(self, col, trigger, targets, logger, count):
        # Format 1 spelled "all of them" as a count of 0, and the migrator maps that to the
        # `all` strategy; a count that is falsy means the same thing whatever the strategy.
        found = selected(
            d.note_query("found", "tag:pool", strategy="first", count=count), trigger, logger
        )

        assert sorted(found) == ["w1", "w2", "w3"]


class TestSorting:
    def sorted_by_freq(self, trigger, logger, query="tag:pool", field="Word", **selection):
        options = {
            "strategy": "all",
            "count": None,
            "sort_field": "Freq",
            "sort_order": "descending",
            "sort_numeric": True,
        }
        options.update(selection)
        return selected(
            d.note_query("found", query, selection=options), trigger, logger, field=field
        )

    def test_a_numeric_sort_is_descending(self, col, trigger, targets, logger):
        assert self.sorted_by_freq(trigger, logger) == ["w3", "w2", "w1"]

    def test_ascending_reverses_it(self, col, trigger, targets, logger):
        assert self.sorted_by_freq(trigger, logger, sort_order="ascending") == [
            "w1",
            "w2",
            "w3",
        ]

    def test_a_non_numeric_value_sorts_as_zero_and_keeps_its_place(self, col, trigger, logger):
        real_anki.add_note(col, VOCAB, {"Word": "x", "Freq": "not a number"}, tags=["s"])
        real_anki.add_note(col, VOCAB, {"Word": "y", "Freq": "5"}, tags=["s"])
        real_anki.add_note(col, VOCAB, {"Word": "z", "Freq": ""}, tags=["s"])

        assert self.sorted_by_freq(trigger, logger, query="tag:s") == ["y", "x", "z"]

    def test_no_sort_field_leaves_the_search_order_alone(self, col, trigger, targets, logger):
        expected = [
            col.get_note(note_id)["Word"] for note_id in col.find_notes("tag:pool")
        ]

        assert self.sorted_by_freq(trigger, logger, sort_field=None) == expected

    def test_a_sort_field_the_note_type_does_not_have_sorts_as_zero(
        self, col, trigger, logger
    ):
        # The sort field names a field of whatever the query found, and a query may span note
        # types. A note without that field sorts as 0 rather than raising out of the run.
        vocab = real_anki.add_note(col, VOCAB, {"Word": "v", "Freq": "9"}, tags=["mixed"])
        kanji = real_anki.add_note(col, KANJI, {"Kanji": "k", "Keyword": "kw"}, tags=["mixed"])

        found = self.sorted_by_freq(trigger, logger, query="tag:mixed", field="__Note_ID")

        assert found == [str(vocab.id), str(kanji.id)]


class TestAnEmptyResult:
    def test_finding_nothing_is_not_an_error_by_default(self, col, trigger, logger):
        found = selected(d.note_query("found", "tag:nothing"), trigger, logger)

        assert found == []
        assert logger.errors == []

    def test_if_empty_error_fails_the_definition(self, col, trigger):
        definition = d.staged(stages=[d.note_query("found", "tag:nothing", if_empty="error")])

        succeeded = copy_for_single_trigger_note(definition, trigger)

        assert succeeded is False

    def test_the_migrated_show_error_flag_still_says_so(self, col, trigger, logger):
        # `show_error_if_none_found` became the `error_if_empty` marker, which logs without
        # failing the definition -- exactly what the checkbox did.
        run_legacy(
            trigger,
            copy_from_cards_query="tag:nothing",
            show_error_if_none_found=True,
        )

        assert logger.has_error("Did not find any notes")


class TestGuardsCarriedOverFromFormat1:
    def test_a_query_that_interpolates_to_nothing_is_reported(self, col, trigger, logger):
        run_legacy(trigger, copy_from_cards_query="{{Note}}")

        assert logger.has_error("Could not interpolate copy_from_cards_query")

    @pytest.mark.parametrize("blank", [" ", "   ", "\t"])
    def test_a_query_that_resolves_to_whitespace_selects_nothing(
        self, col, trigger, targets, logger, blank
    ):
        # `find_notes("   ")` matches every note in the collection, so a query that is one
        # reference to a field holding a space is the same refusal as one holding nothing,
        # not a selection of everything the block then edits.
        trigger["Note"] = blank
        col.update_note(trigger)

        found = selected(d.note_query("found", "{{trigger.Note}}"), trigger, logger)

        assert found == []
        assert logger.has_error("Could not interpolate copy_from_cards_query")

    @pytest.mark.parametrize(
        "select_card_by, message",
        [
            (None, "'select_card_by' was missing"),
            ("Most_reps", "incorrect 'select_card_by' value"),
        ],
    )
    def test_an_unusable_select_card_by_selects_nothing_and_says_why(
        self, col, trigger, targets, logger, select_card_by, message
    ):
        # The migrator cannot turn these into a strategy, so it records the complaint on the
        # stage and the query stage reports it instead of guessing.
        run_legacy(
            trigger, copy_from_cards_query="tag:pool", select_card_by=select_card_by
        )

        assert logger.has_error(message)

    @pytest.mark.parametrize("count", ["-1", "abc"])
    def test_an_unusable_select_card_count_reports_the_value_it_was_given(
        self, col, trigger, targets, logger, count
    ):
        run_legacy(
            trigger, copy_from_cards_query="tag:pool", select_card_count=count
        )

        assert logger.has_error(f"Incorrect 'select_card_count' value '{count}'")


class TestTheQueryCache:
    def test_the_same_query_run_twice_searches_once(self, col, trigger, targets):
        # Two stages, one search. The session caches by the interpolated query text, which is
        # what keeps a definition that asks the same thing twice from paying for it twice.
        searches = []
        original = col.find_notes
        col.find_notes = lambda query: (searches.append(query), original(query))[1]
        try:
            definition = d.staged(stages=[
                d.note_query("first", "tag:pool"),
                d.note_query("second", "tag:pool"),
            ])
            copy_for_single_trigger_note(definition, trigger)
        finally:
            col.find_notes = original

        assert searches == ["tag:pool"]

    def test_a_different_query_searches_again(self, col, trigger, targets):
        searches = []
        original = col.find_notes
        col.find_notes = lambda query: (searches.append(query), original(query))[1]
        try:
            definition = d.staged(stages=[
                d.note_query("first", "tag:pool"),
                d.note_query("second", "Word:w1"),
            ])
            copy_for_single_trigger_note(definition, trigger)
        finally:
            col.find_notes = original

        assert searches == ["tag:pool", "Word:w1"]

    def test_a_search_condition_asked_twice_searches_once(self, col, trigger, targets):
        # A condition matched as an Anki search is a search like any other, so two of them
        # asking the same thing of the same note share one trip to the collection -- which
        # is what keeps a condition inside a loop from costing a search per iteration on
        # top of the loop's own.
        searches = []
        original = col.find_notes
        col.find_notes = lambda query: (searches.append(query), original(query))[1]
        try:

            def gate():
                return d.condition(
                    d.text("Word:trigger"),
                    [d.variable("seen", d.text("yes"))],
                    predicate_kind="note_query",
                    predicate_target={"binding": "trigger"},
                )

            definition = d.staged(stages=[gate(), gate()])
            copy_for_single_trigger_note(definition, trigger)
        finally:
            col.find_notes = original

        assert searches == [f"Word:trigger nid:{trigger.id}"]


class TestARefusedQueryDoesNotWipeTheDestination:
    """The guards above select nothing, and selecting nothing must stop the writes too.

    Format 1 returned an empty source list from `get_across_target_notes` and its caller
    then returned early "so that the target fields aren't wiped". In format 2 the early
    return is the query stage's `if_empty` marker, so a refusal that hands back an empty
    list without going through it leaves the rest of the block to interpolate nothing and
    write it (§11 step 4).
    """

    @pytest.fixture
    def trigger_with_a_note(self, col, trigger):
        trigger["Note"] = "kept"
        col.update_note(trigger)
        return trigger

    def test_an_unusable_select_card_by_writes_nothing(
        self, col, trigger_with_a_note, targets
    ):
        run_legacy(
            trigger_with_a_note,
            copy_from_cards_query="tag:pool",
            select_card_by=None,
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
        )

        assert trigger_with_a_note["Note"] == "kept"

    def test_an_unusable_select_card_count_writes_nothing(
        self, col, trigger_with_a_note, targets
    ):
        run_legacy(
            trigger_with_a_note,
            copy_from_cards_query="tag:pool",
            select_card_count="abc",
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
        )

        assert trigger_with_a_note["Note"] == "kept"

    def test_a_query_that_interpolates_to_nothing_writes_nothing(
        self, col, trigger_with_a_note
    ):
        run_legacy(
            trigger_with_a_note,
            copy_from_cards_query="{{Nonexistent}}",
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
        )

        assert trigger_with_a_note["Note"] == "kept"

    def test_the_empty_result_flag_still_lets_the_write_through(
        self, col, trigger_with_a_note
    ):
        # `run_also_if_no_sources_found` is what format 1 offered for the opposite case, and
        # it has to keep working: there the empty write is what the user asked for.
        run_legacy(
            trigger_with_a_note,
            copy_from_cards_query="tag:nothing",
            run_also_if_no_sources_found=True,
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
        )

        assert trigger_with_a_note["Note"] == ""
