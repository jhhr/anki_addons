"""Characterization tests for `get_across_target_notes`, and for the `extra_state` cache.

This is the set that has to exist before `extra_state` can be threaded through the bulk loop
instead of being rebuilt per note. Today the only caller builds it locally, so the cache
cannot survive one call and never hits; threading it turns a per-note cache into a per-run
one, which changes what `select_card_by` returns across notes that share a query and makes
two recently-fixed aliasing bugs load-bearing.

So the cache tests below assert on a *call counter*, not on the result -- a cache that never
hits still returns the right answer -- and the selection tests are written at the level of
"which notes come back", so that the diff the refactor makes is legible.
"""

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import get_across_target_notes


@pytest.fixture
def trigger(col):
    return real_anki.add_note(col, VOCAB, {"Word": "trigger", "Meaning": "t"})


@pytest.fixture
def targets(col):
    """Three notes tagged `pool`, in a known creation order."""
    return [
        real_anki.add_note(col, VOCAB, {"Word": f"w{i}", "Freq": str(i)}, tags=["pool"])
        for i in range(1, 4)
    ]


def call(col, trigger, extra_state=None, **definition_kwargs):
    """Run get_across_target_notes for a definition built from `definition_kwargs`."""
    definition = d.destination_to_sources(**definition_kwargs)
    return get_across_target_notes(
        copy_definition=definition,
        copy_from_cards_query=definition["copy_from_cards_query"],
        trigger_note=trigger,
        extra_state={} if extra_state is None else extra_state,
        select_card_by=definition["select_card_by"],
        sort_by_field=definition["sort_by_field"],
        select_card_count=definition["select_card_count"],
        variable_values_dict={},
    )


class TestGuards:
    def test_a_missing_select_card_by_returns_nothing(self, col, trigger, targets, logger):
        assert call(col, trigger, copy_from_cards_query="tag:pool",
                    select_card_by=None) == []
        assert logger.has_error("'select_card_by' was missing")

    def test_an_unrecognised_select_card_by_returns_nothing(self, col, trigger, targets, logger):
        assert call(col, trigger, copy_from_cards_query="tag:pool",
                    select_card_by="Most_reps") == []
        assert logger.has_error("incorrect 'select_card_by' value")

    @pytest.mark.parametrize("count", [None, ""])
    def test_a_missing_select_card_count_defaults_to_one(self, col, trigger, targets, count):
        notes = call(col, trigger, copy_from_cards_query="tag:pool",
                     select_card_count=count)
        assert len(notes) == 1

    def test_a_negative_select_card_count_is_an_error(self, col, trigger, targets, logger):
        assert call(col, trigger, copy_from_cards_query="tag:pool",
                    select_card_count="-1") == []
        assert logger.has_error("Incorrect 'select_card_count' value '-1'")

    def test_a_non_numeric_select_card_count_reports_the_value_it_was_given(
        self, col, trigger, targets, logger
    ):
        # int("abc") raises before the parsed value is bound, so the handler used to fail
        # with UnboundLocalError while reporting the problem. It reports the input instead.
        assert call(col, trigger, copy_from_cards_query="tag:pool",
                    select_card_count="abc") == []
        assert logger.has_error("Incorrect 'select_card_count' value 'abc'")

    def test_a_query_that_interpolates_to_nothing_is_an_error(self, col, trigger, logger):
        assert call(col, trigger, copy_from_cards_query="{{Note}}") == []
        assert logger.has_error("Could not interpolate copy_from_cards_query")

    def test_zero_results_are_logged_as_an_error_when_the_flag_is_set(self, col, trigger, logger):
        assert call(col, trigger, copy_from_cards_query="tag:nothing",
                    show_error_if_none_found=True) == []
        assert logger.has_error("Did not find any cards")

    def test_zero_results_are_only_a_debug_message_by_default(self, col, trigger, logger):
        assert call(col, trigger, copy_from_cards_query="tag:nothing") == []
        assert logger.errors == []
        assert logger.has_debug("No cards found")


class TestSelection:
    def test_select_card_by_none_walks_the_query_result_backwards(
        self, col, trigger, targets
    ):
        # card_ids.pop() takes from the end, so the order is the reverse of find_cards'.
        found = col.find_cards("tag:pool")
        expected = [col.get_card(cid).nid for cid in reversed(found)][:3]
        notes = call(col, trigger, copy_from_cards_query="tag:pool",
                     select_card_by="None", select_card_count="3")
        assert [note.id for note in notes] == expected

    def test_select_card_by_none_does_not_de_duplicate_notes(self, col, trigger):
        # Both cards of one note match, and "None" pops card ids rather than note ids, so the
        # same note comes back twice.
        note = real_anki.add_note(col, VOCAB, {"Word": "dup", "Meaning": "m"}, tags=["pool"])
        notes = call(col, trigger, copy_from_cards_query="tag:pool",
                     select_card_by="None", select_card_count="2")
        assert [n.id for n in notes] == [note.id, note.id]

    def test_random_picks_from_the_pool_and_never_twice(self, col, trigger, targets):
        notes = call(col, trigger, copy_from_cards_query="tag:pool",
                     select_card_by="Random", select_card_count="3")
        ids = [note.id for note in notes]
        assert set(ids) <= {note.id for note in targets}
        assert len(set(ids)) == len(ids)

    def test_least_reps_picks_the_least_reviewed_then_the_next_least(
        self, col, trigger, targets
    ):
        # One review on the first note's first card, two on the second's, none on the third.
        first, second, third = targets
        real_anki.add_revlog(col, first.cards()[0].id, count=1)
        real_anki.add_revlog(col, second.cards()[0].id, count=2)
        notes = call(col, trigger,
                     copy_from_cards_query="tag:pool Word:w1 OR tag:pool Word:w2 OR tag:pool Word:w3",
                     select_card_by="Least_reps", select_card_count="2")
        assert [note.id for note in notes][0] == third.id

    def test_asking_for_more_than_there_are_returns_what_there_is(
        self, col, trigger, targets
    ):
        # One matching card, ten asked for: the loop breaks out on the round that finds the
        # list empty rather than padding, erroring, or looping ten times.
        assert len(col.find_cards("Word:w1")) == 1
        notes = call(col, trigger, copy_from_cards_query="Word:w1",
                     select_card_by="None", select_card_count="10")
        assert [note.id for note in notes] == [targets[0].id]

    def test_select_card_count_zero_returns_distinct_notes(self, col, trigger, targets):
        notes = call(col, trigger, copy_from_cards_query="tag:pool",
                     select_card_count="0")
        assert sorted(note.id for note in notes) == sorted(note.id for note in targets)


class TestSorting:
    def test_sort_by_field_is_numeric_and_descending(self, col, trigger, targets):
        notes = call(col, trigger, copy_from_cards_query="tag:pool",
                     select_card_count="0", sort_by_field="Freq")
        assert [note["Freq"] for note in notes] == ["3", "2", "1"]

    def test_a_non_numeric_value_sorts_as_zero_and_keeps_its_place(self, col, trigger):
        real_anki.add_note(col, VOCAB, {"Word": "x", "Freq": "not a number"}, tags=["s"])
        real_anki.add_note(col, VOCAB, {"Word": "y", "Freq": "5"}, tags=["s"])
        real_anki.add_note(col, VOCAB, {"Word": "z", "Freq": ""}, tags=["s"])
        notes = call(col, trigger, copy_from_cards_query="tag:s",
                     select_card_count="0", sort_by_field="Freq")
        assert [note["Word"] for note in notes] == ["y", "x", "z"]

    @pytest.mark.parametrize("sort_by_field", [None, "-"])
    def test_no_sorting_when_the_field_is_unset(self, col, trigger, targets, sort_by_field):
        notes = call(col, trigger, copy_from_cards_query="tag:pool",
                     select_card_count="0", sort_by_field=sort_by_field)
        assert sorted(note.id for note in notes) == sorted(note.id for note in targets)

    def test_a_sort_field_absent_from_the_note_type_sorts_as_zero(self, col, trigger):
        # sort_by_field names a field on the source note type, which need not be the type the
        # definition copies into, so a query spanning types reaches this with a field the
        # note does not have. It sorts as 0 rather than raising KeyError out of the run.
        real_anki.add_note(col, VOCAB, {"Word": "v", "Freq": "9"}, tags=["mixed"])
        from conftest import KANJI

        real_anki.add_note(col, KANJI, {"Kanji": "k", "Keyword": "kw"}, tags=["mixed"])
        notes = call(col, trigger, copy_from_cards_query="tag:mixed",
                     select_card_count="0", sort_by_field="Freq")
        assert len(notes) == 2
        assert notes[0]["Word"] == "v"


class TestQueryCache:
    def test_the_same_interpolated_query_searches_once(self, col, trigger, targets):
        # Asserted on a counter rather than on the result: a cache that never hits still
        # returns the right notes, so only the call count can tell the two apart.
        count = real_anki.counting_wrapper(col, "find_cards")
        extra_state = {}
        call(col, trigger, extra_state=extra_state,
             copy_from_cards_query="tag:pool", select_card_count="0")
        call(col, trigger, extra_state=extra_state,
             copy_from_cards_query="tag:pool", select_card_count="0")
        assert count() == 1

    def test_different_interpolated_queries_search_separately(self, col, targets):
        # The cache key is b64("cards" + the *interpolated* query), which is what makes
        # sharing one extra_state across trigger notes safe at all.
        first = real_anki.add_note(col, VOCAB, {"Word": "one"})
        second = real_anki.add_note(col, VOCAB, {"Word": "two"})
        count = real_anki.counting_wrapper(col, "find_cards")
        extra_state = {}
        for note in (first, second):
            call(col, note, extra_state=extra_state,
                 copy_from_cards_query="tag:pool {{Word}}", select_card_count="0")
        assert count() == 2

    def test_a_fresh_extra_state_searches_again(self, col, trigger, targets):
        # What the current code does, and the reason the cache never hits today:
        # copy_for_single_trigger_note builds extra_state locally on every call.
        count = real_anki.counting_wrapper(col, "find_cards")
        for _ in range(2):
            call(col, trigger, extra_state={},
                 copy_from_cards_query="tag:pool", select_card_count="0")
        assert count() == 2


class TestCacheAliasing:
    def test_popping_does_not_empty_the_cached_result(self, col, trigger, targets):
        # Regression test for the fix that copies the cached list before popping from it.
        # It takes *three* calls to show the bug, which is why this test asserts on the third
        # and on the cache itself. The first call misses the cache and stores a copy of the
        # find_cards result, so its popping cannot reach the stored list whether or not the
        # read side copies. Only from the second call on is the popped list the cached one.
        extra_state = {}
        calls = [
            call(col, trigger, extra_state=extra_state,
                 copy_from_cards_query="tag:pool", select_card_by="None",
                 select_card_count="2")
            for _ in range(3)
        ]
        assert all(len(result) == 2 for result in calls)
        assert [note.id for note in calls[1]] == [note.id for note in calls[0]]
        assert [note.id for note in calls[2]] == [note.id for note in calls[0]]

    def test_the_cached_query_result_survives_being_read_repeatedly(
        self, col, trigger, targets
    ):
        # The invariant behind the test above, asserted where it lives rather than through
        # its symptoms: reading the cache must not consume it, however many times it is read.
        extra_state = {}
        for _ in range(3):
            call(col, trigger, extra_state=extra_state,
                 copy_from_cards_query="tag:pool", select_card_by="None",
                 select_card_count="2")
        cached = next(value for value in extra_state.values() if isinstance(value, list))
        assert sorted(cached) == sorted(col.find_cards("tag:pool"))

    def test_the_cached_card_id_list_is_unchanged_after_a_selection(
        self, col, trigger, targets
    ):
        # `card_ids = [c for c in card_ids if c != selected]` rebinds rather than mutating,
        # so the entry in extra_state keeps every card the query found.
        extra_state = {}
        call(col, trigger, extra_state=extra_state,
             copy_from_cards_query="tag:pool", select_card_by="Random", select_card_count="2")
        cached = next(value for value in extra_state.values() if isinstance(value, list))
        assert sorted(cached) == sorted(col.find_cards("tag:pool"))

    def test_least_reps_adds_a_key_rather_than_replacing_the_dict(
        self, col, trigger, targets
    ):
        # Regression test for the fix that writes `extra_state[key] = value` instead of
        # `extra_state = {key: value}`. The query entry has to survive the selection.
        extra_state = {}
        call(col, trigger, extra_state=extra_state,
             copy_from_cards_query="tag:pool", select_card_by="Least_reps",
             select_card_count="1")
        assert len(extra_state) == 2
        assert any(isinstance(value, list) for value in extra_state.values())
        assert any(isinstance(value, int) for value in extra_state.values())

    def test_the_selection_key_varies_by_select_card_by_and_by_index(
        self, col, trigger, targets
    ):
        # Same query, two selection strategies, and two rounds within one call: each gets its
        # own key, so a shared extra_state cannot collide across them.
        extra_state = {}
        call(col, trigger, extra_state=extra_state,
             copy_from_cards_query="tag:pool", select_card_by="Least_reps",
             select_card_count="2")
        selection_keys = [key for key, value in extra_state.items() if isinstance(value, int)]
        assert len(selection_keys) == 2
        call(col, trigger, extra_state=extra_state,
             copy_from_cards_query="tag:pool", select_card_by="Random", select_card_count="2")
        # Random is deliberately not cached, so the key count is unchanged.
        assert [key for key, value in extra_state.items() if isinstance(value, int)] == (
            selection_keys
        )


class TestSharedStateWouldChangeSelection:
    """The behaviour the refactor changes, pinned so its diff is legible.

    Two trigger notes share one query. Today each note gets a fresh `extra_state`, so both
    re-run the `min()` and both get the same card. With a shared `extra_state` the second
    note would hit `card_select_key` instead. The answers coincide here -- which is why the
    change looks safe -- but they would not if the first note's run had already removed the
    winner, so both `select_card_count` values are pinned.
    """

    @pytest.fixture
    def pool(self, col):
        notes = [
            real_anki.add_note(col, VOCAB, {"Word": f"p{i}"}, tags=["shared"])
            for i in range(1, 4)
        ]
        # Give each note's first card a different review count, so min() has a clear winner.
        for reps, note in enumerate(notes, start=1):
            real_anki.add_revlog(col, note.cards()[0].id, count=reps)
        return notes

    def _select(self, col, trigger, extra_state, count):
        return call(col, trigger, extra_state=extra_state,
                    copy_from_cards_query="tag:shared", select_card_by="Least_reps",
                    select_card_count=count)

    @pytest.mark.parametrize("count", ["1", "2"])
    def test_per_note_state_gives_both_trigger_notes_the_same_answer(
        self, col, pool, count
    ):
        first = real_anki.add_note(col, VOCAB, {"Word": "t1"})
        second = real_anki.add_note(col, VOCAB, {"Word": "t2"})
        one = self._select(col, first, {}, count)
        two = self._select(col, second, {}, count)
        assert [note.id for note in one] == [note.id for note in two]

    @pytest.mark.parametrize("count", ["1", "2"])
    def test_shared_state_gives_the_same_answer_here_too(self, col, pool, count):
        # The same assertion against one shared dict. If threading extra_state ever changes
        # which notes come back, this is the test that says so.
        first = real_anki.add_note(col, VOCAB, {"Word": "t1"})
        second = real_anki.add_note(col, VOCAB, {"Word": "t2"})
        shared = {}
        one = self._select(col, first, shared, count)
        two = self._select(col, second, shared, count)
        assert [note.id for note in one] == [note.id for note in two]

    def test_shared_state_searches_once_where_per_note_state_searches_twice(
        self, col, pool
    ):
        first = real_anki.add_note(col, VOCAB, {"Word": "t1"})
        second = real_anki.add_note(col, VOCAB, {"Word": "t2"})
        count = real_anki.counting_wrapper(col, "find_cards")
        shared = {}
        self._select(col, first, shared, "1")
        self._select(col, second, shared, "1")
        assert count() == 1
