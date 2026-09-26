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
from anki.notes import Note

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


@pytest.fixture
def card_fetches(monkeypatch):
    """The ids of the notes whose cards were fetched, one entry per `Note.cards` call."""
    calls: list = []
    original = Note.cards

    def counted(self, *args, **kwargs):
        calls.append(self.id)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Note, "cards", counted)
    return calls


@pytest.fixture
def revlog_queries(col, monkeypatch):
    """The review-history queries a run makes, which card values read with `db.first`."""
    calls: list = []
    original = col.db.first

    def counted(sql, *args, **kwargs):
        if "revlog" in sql:
            calls.append(sql)
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(col.db, "first", counted)
    return calls


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


class TestCodeModesCards:
    def test_code_that_does_not_read_cards_fetches_none(
        self, col, trigger, pool, logger, card_fetches
    ):
        # `cards` is in every code expression's namespace. Built up front it cost a query per
        # evaluation -- two, as the sandbox fetched its own list for the caller's to replace --
        # so this definition made 2 + 2 * POOL of them to read nothing but a field.
        definition = d.staged(stages=[
            d.variable("v", d.code("return 'x'")),
            d.note_query("found", "tag:pool"),
            d.list_variable("words"),
            d.for_each_note("found", [d.store("words", d.code("return note['Word']"))]),
        ])

        card_fetches.clear()
        run(definition, trigger, logger)

        assert card_fetches == []

    def test_code_that_reads_cards_fetches_them_once_per_evaluation(
        self, col, trigger, pool, logger, card_fetches
    ):
        # Read three ways in one evaluation, still one fetch; and each one is of the loop
        # note's cards, since `cards` follows `note`, rather than of the trigger's.
        definition = d.staged(stages=[
            d.note_query("found", "tag:pool"),
            d.list_variable("seen"),
            d.for_each_note(
                "found",
                [d.store("seen", d.code(
                    "return f'{len(cards)}:{cards[0].id}:{[c.ord for c in cards]}'"
                ))],
            ),
        ])

        card_fetches.clear()
        run(definition, trigger, logger)

        assert sorted(card_fetches) == sorted(note.id for note in pool)


class TestCardValues:
    def test_three_review_time_values_of_one_card_share_one_query(
        self, col, trigger, logger, revlog_queries
    ):
        # The four review-time values are one aggregate over the revlog, kept by the card's
        # `CardValues`. Building a new one per reference threw that away, so three references
        # to one card ran the same query three times.
        definition = d.staged(stages=[
            d.card_query("found", f"nid:{trigger.id} card:Recognition"),
            d.for_each_card("found", [
                d.edit_note("note", [d.write("Note", d.text(
                    "{{card.__Card_First_Review}}|{{card.__Card_Latest_Review}}"
                    "|{{card.__Card_Average_Time}}"
                ))]),
            ]),
        ])

        revlog_queries.clear()
        run(definition, trigger, logger)

        assert trigger["Note"] == "-|-|-"
        assert len(revlog_queries) == 1

    def test_each_expression_reads_the_card_again(self, col, trigger, logger, revlog_queries):
        # Shared within one expression only: a `CardValues` computes most of its values when
        # it is built, so it is a snapshot of the card, and a later stage may have changed it.
        definition = d.staged(stages=[
            d.card_query("found", f"nid:{trigger.id} card:Recognition"),
            d.for_each_card("found", [
                d.edit_note("note", [d.write("Note", d.text("{{card.__Card_First_Review}}"))]),
                d.edit_note("note", [d.write("Meaning", d.text("{{card.__Card_Total_Time}}"))]),
            ]),
        ])

        revlog_queries.clear()
        run(definition, trigger, logger)

        assert (trigger["Note"], trigger["Meaning"]) == ("-", "-")
        assert len(revlog_queries) == 2


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
