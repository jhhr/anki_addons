"""What a definition over a large query costs, counted rather than timed.

The thing that makes a bulk run slow is not how fast any one stage is: it is how many times
the definition goes back to the collection. A query re-run per loop iteration, or a note
re-fetched per stage that mentions it, turns a run that should be linear in the notes into
one that is linear in notes times stages, and neither shows up in a correctness test.

So these count collection reads instead of measuring time: a wall-clock budget in a container
is a flaky test, while "one search however many stages ask for it" is the property that has
to hold for the run to stay linear. The one timed case is a ceiling loose enough that only a
change of complexity can cross it.
"""

import time

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note

#: Enough notes that a per-iteration search is unmistakable in the count, and few enough that
#: building the collection stays quick. The point is the shape of the number, not its size.
POOL = 60


@pytest.fixture
def trigger(col):
    return real_anki.add_note(col, VOCAB, {"Word": "trigger", "Meaning": "t"})


@pytest.fixture
def pool(col):
    """`POOL` notes tagged `pool`, which the definitions below all query for."""
    return [
        real_anki.add_note(col, VOCAB, {"Word": f"w{i}", "Freq": str(i)}, tags=["pool"])
        for i in range(POOL)
    ]


class Counter:
    """Counts the collection reads a run makes, by wrapping them on the collection."""

    NAMES = ("find_notes", "find_cards", "get_note", "get_card")

    def __init__(self, col):
        self.col = col
        self.counts = {name: 0 for name in self.NAMES}
        self._originals = {}

    def __enter__(self):
        for name in self.NAMES:
            original = getattr(self.col, name)
            self._originals[name] = original
            setattr(self.col, name, self._wrap(name, original))
        return self

    def __exit__(self, *_exc):
        for name, original in self._originals.items():
            setattr(self.col, name, original)
        return False

    def _wrap(self, name, original):
        def counted(*args, **kwargs):
            self.counts[name] += 1
            return original(*args, **kwargs)

        return counted


def run(definition, trigger, logger):
    copied: list = []
    succeeded = copy_for_single_trigger_note(
        definition, trigger, copied_into_notes=copied
    )
    assert succeeded is True, logger.errors
    return copied


class TestSearching:
    def test_a_query_inside_a_loop_body_searches_once_for_the_whole_loop(
        self, col, trigger, pool, logger
    ):
        # The session caches by the interpolated query text, so a body that asks the same
        # thing every time round pays for one search rather than one per note. Without it
        # this definition runs POOL + 1 searches instead of two.
        definition = d.staged(stages=[
            d.note_query("outer", "tag:pool"),
            d.for_each_note("outer", [d.note_query("inner", "tag:pool")]),
        ])

        with Counter(col) as counter:
            run(definition, trigger, logger)

        assert counter.counts["find_notes"] == 1

    def test_a_query_whose_text_changes_per_iteration_searches_per_iteration(
        self, col, trigger, pool, logger
    ):
        # The other side of the same rule, so the cache cannot be mistaken for "search once
        # per stage": a body asking a different question each time has to ask it each time.
        definition = d.staged(stages=[
            d.note_query("outer", "tag:pool"),
            d.for_each_note("outer", [d.note_query("inner", "Word:{{note.Word}}")]),
        ])

        with Counter(col) as counter:
            run(definition, trigger, logger)

        assert counter.counts["find_notes"] == 1 + POOL

    def test_the_number_of_searches_does_not_grow_with_the_stages_that_read_the_result(
        self, col, trigger, pool, logger
    ):
        definition = d.staged(stages=[
            d.note_query("a", "tag:pool"),
            d.note_query("b", "tag:pool"),
            d.note_query("c", "tag:pool"),
            d.for_each_note("a", [d.variable("seen", d.text("{{note.Word}}"))]),
            d.for_each_note("b", [d.variable("seen", d.text("{{note.Word}}"))]),
        ])

        with Counter(col) as counter:
            run(definition, trigger, logger)

        assert counter.counts["find_notes"] == 1


class TestFetching:
    def test_a_loop_fetches_each_note_once_however_many_stages_touch_it(
        self, col, trigger, pool, logger
    ):
        # A stage targeting a loop binding works on the note the binding already holds, so
        # three stages editing the same note in one iteration cost nothing beyond the fetch
        # the query made. What this guards against is a stage resolving a note reference by
        # going back to the collection for it, which would make the cost stages times notes.
        body = [
            d.edit_note("note", [d.write("Note", d.text("{{note.Word}}"))]),
            d.edit_note("note", [d.write("Meaning", d.text("{{note.Note}}"))]),
            d.edit_note("note", [d.write("Reading", d.text("{{note.Meaning}}"))]),
        ]
        definition = d.staged(stages=[
            d.note_query("found", "tag:pool"),
            d.for_each_note("found", body),
        ])

        with Counter(col) as counter:
            copied = run(definition, trigger, logger)

        assert len(copied) == POOL
        assert counter.counts["get_note"] == POOL

    def test_a_note_two_queries_both_found_is_fetched_once(
        self, col, trigger, pool, logger
    ):
        definition = d.staged(stages=[
            d.note_query("a", "tag:pool"),
            d.note_query("b", "Word:w1"),
            d.for_each_note("a", [d.variable("x", d.text("{{note.Word}}"))]),
            d.for_each_note("b", [d.variable("y", d.text("{{note.Word}}"))]),
        ])

        with Counter(col) as counter:
            run(definition, trigger, logger)

        # `w1` is in both results and converges on one working note (§7.1), so the fetches
        # are the distinct notes rather than the sum of the two result sizes.
        assert counter.counts["get_note"] == POOL


class TestItStaysLinear:
    @pytest.mark.parametrize("size", [20, 80])
    def test_the_searches_are_the_same_whatever_the_result_size(
        self, col, trigger, logger, size
    ):
        for index in range(size):
            real_anki.add_note(col, VOCAB, {"Word": f"n{index}"}, tags=["big"])
        definition = d.staged(stages=[
            d.note_query("found", "tag:big"),
            d.for_each_note(
                "found", [d.edit_note("note", [d.write("Note", d.text("{{index}}"))])]
            ),
        ])

        with Counter(col) as counter:
            copied = run(definition, trigger, logger)

        assert len(copied) == size
        # One search and one fetch per note found: the searching does not grow with the
        # result, and the fetching grows with it exactly once.
        assert counter.counts["find_notes"] == 1
        assert counter.counts["get_note"] == size

    def test_a_definition_over_many_notes_finishes_in_a_sane_time(self, col, trigger, logger):
        # A ceiling, not a benchmark. It is loose enough that only a change of complexity --
        # a query per iteration, a re-fetch per stage -- can cross it on any machine that
        # runs the rest of this suite.
        for index in range(400):
            real_anki.add_note(col, VOCAB, {"Word": f"m{index}"}, tags=["many"])
        definition = d.staged(stages=[
            d.note_query("found", "tag:many"),
            d.list_variable("words"),
            d.for_each_note(
                "found",
                [
                    d.store("words", d.text("{{note.Word}}")),
                    d.edit_note("note", [d.write("Note", d.text("{{index}}/{{count}}"))]),
                ],
            ),
            d.join("words", "joined"),
            d.edit_note("trigger", [d.write("Note", d.text("{{joined}}"))]),
        ])

        started = time.monotonic()
        copied = run(definition, trigger, logger)
        elapsed = time.monotonic() - started

        assert len(copied) == 401
        assert elapsed < 20, f"400 notes took {elapsed:.1f}s"
