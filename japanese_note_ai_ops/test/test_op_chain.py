"""The chain runner: several ops, one after another, over the same notes.

A chain that starts a step from inside the previous one's `on_done` runs it inside aqt's
success handler, or recurses when a step fails synchronously. A chain whose steps each had a
progress dialog of their own would leave a gap between two with nothing modal on screen. So
these drive `OpChain` with fake ops and a scheduler the test runs by hand, and check that a
step starts only from the scheduler, that one progress is held from before the first step to
after the last, that each step gets the ids that still exist, that a stopped step stops the
chain, and what the summary says.

The suite runs on the Anki stand-ins, with no database behind `mw.col`, so `existing_note_ids`
is checked against a table in an in-memory sqlite database holding the same `notes.id` column.
"""

from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from addon_modules import load_ops_module

chain_types = load_ops_module("chain_types")
op_chain = load_ops_module("op_chain")

StepOutcome = chain_types.StepOutcome
COMPLETED = chain_types.STEP_COMPLETED
CANCELLED = chain_types.STEP_CANCELLED
STOPPED = chain_types.STEP_STOPPED
FAILED = chain_types.STEP_FAILED


class FakeSpec:
    """An `OpSpec` whose `start` records the call; the test ends the step through `chain`."""

    def __init__(self, key, needs_generator=False, on_start=None):
        self.key = key
        self.label = f"Op {key}"
        self.needs_generator = needs_generator
        self.on_start = on_start
        self.calls: list = []

    def start(self, nids, parent, chain):
        self.calls.append((list(nids), parent, chain))
        if self.on_start is not None:
            self.on_start(chain)

    @property
    def chain(self):
        return self.calls[-1][2]


class Harness:
    """The hooks `OpChain` takes, as test doubles."""

    def __init__(self, nids=(1, 2, 3)):
        self.existing = set(nids)
        # What the chain did, in order: "hold", "release", ("start", key), ("summary", ...)
        self.events: list = []
        self.pending: list = []
        self.summaries: list = []
        self.errors: list = []
        self.error_titles: list = []
        self.resource_checks: list = []
        self.parent = object()

    def make(self, specs, nids=(1, 2, 3)):
        return op_chain.OpChain(
            specs,
            list(nids),
            self.parent,
            existing_ids=lambda ids: [n for n in ids if n in self.existing],
            schedule=self.pending.append,
            hold_progress=lambda: self.events.append("hold"),
            release_progress=lambda: self.events.append("release"),
            failed_outcome=self.failed_outcome,
            show_summary=self.show_summary,
        )

    def show_summary(self, text, stopped):
        self.summaries.append((text, stopped))
        self.events.append("summary")

    def failed_outcome(self, error, title):
        self.errors.append(error)
        self.error_titles.append(title)
        return StepOutcome(FAILED, error=f"{type(error).__name__}: {error}")

    def run_scheduled(self, limit=50):
        """Fire what is scheduled, and what that schedules, until nothing is left."""
        fired = 0
        while self.pending:
            fired += 1
            if fired > limit:
                raise AssertionError("the chain keeps scheduling")
            self.pending.pop(0)()

    def with_resources(self, answer):
        def check(parent, then):
            self.resource_checks.append(parent)
            if answer:
                then()

        return check


def no_resources(parent, then):
    raise AssertionError("no op needs the generator; nothing should ask about it")


class StepOrderTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.specs = [FakeSpec("a"), FakeSpec("b"), FakeSpec("c")]
        self.chain = self.h.make(self.specs)

    def test_steps_run_in_order_with_their_labels(self):
        self.chain.start(no_resources)
        self.h.run_scheduled()
        for index, spec in enumerate(self.specs):
            self.assertEqual(len(spec.calls), 1)
            nids, parent, chain = spec.calls[0]
            self.assertEqual(nids, [1, 2, 3])
            self.assertIs(parent, self.h.parent)
            self.assertEqual(chain.label, f"Step {index + 1}/3")
            self.assertEqual(chain.title, f"Step {index + 1}/3: Op {spec.key}")
            self.assertEqual(self.specs[index + 1].calls if index < 2 else [], [])
            chain.on_done(StepOutcome(COMPLETED, message=f"done {spec.key}"))
            self.h.run_scheduled()
        self.assertEqual(len(self.h.summaries), 1)
        self.assertFalse(self.h.summaries[0][1])

    def test_nothing_starts_before_the_scheduler_runs(self):
        self.chain.start(no_resources)
        self.assertEqual(self.specs[0].calls, [])
        self.h.run_scheduled()
        self.specs[0].chain.on_done(StepOutcome(COMPLETED))
        # Not from inside on_done: that is aqt's success handler
        self.assertEqual(self.specs[1].calls, [])
        self.assertTrue(self.h.pending)
        self.h.run_scheduled()
        self.assertEqual(len(self.specs[1].calls), 1)

    def test_one_progress_is_held_from_before_the_first_step_to_after_the_last(self):
        for spec in self.specs:
            spec.on_start = lambda chain, key=spec.key: self.h.events.append(("start", key))
        self.chain.start(no_resources)
        self.assertEqual(self.h.events, ["hold"])
        self.h.run_scheduled()
        for spec in self.specs:
            spec.chain.on_done(StepOutcome(COMPLETED))
            # Never let go between two steps: the browser would be usable in the gap
            self.assertNotIn("release", self.h.events)
            self.h.run_scheduled()
        self.assertEqual(
            self.h.events,
            ["hold", ("start", "a"), ("start", "b"), ("start", "c"), "release", "summary"],
        )

    def test_a_stopped_chain_lets_go_of_its_progress_before_the_summary(self):
        self.chain.start(no_resources)
        self.h.run_scheduled()
        self.specs[0].chain.on_done(StepOutcome(CANCELLED))
        self.h.run_scheduled()
        self.assertEqual(self.h.events, ["hold", "release", "summary"])

    def test_a_second_on_done_is_ignored(self):
        self.chain.start(no_resources)
        self.h.run_scheduled()
        step = self.specs[0].chain
        step.on_done(StepOutcome(COMPLETED))
        step.on_done(StepOutcome(FAILED, error="late"))
        self.h.run_scheduled()
        self.assertEqual(len(self.specs[1].calls), 1)
        self.assertEqual([o.status for _, o in self.chain.outcomes], [COMPLETED])


class NoteIdTests(unittest.TestCase):
    def test_each_step_gets_the_ids_that_still_exist(self):
        h = Harness(nids=(1, 2, 3))
        specs = [FakeSpec("a"), FakeSpec("b")]
        chain = h.make(specs, nids=(3, 1, 2))
        chain.start(no_resources)
        h.run_scheduled()
        self.assertEqual(specs[0].calls[0][0], [3, 1, 2])
        # Step 1 removed note 1 and added note 9
        h.existing.discard(1)
        h.existing.add(9)
        specs[0].chain.on_done(StepOutcome(COMPLETED))
        h.run_scheduled()
        self.assertEqual(specs[1].calls[0][0], [3, 2])

    def test_the_first_step_too_gets_only_the_ids_that_still_exist(self):
        # The dialog's ids are as old as its count or the captured selection, and it is modal
        # to the browser alone: the reviewer can delete a note before step 1 starts
        h = Harness(nids=(1, 2, 3))
        specs = [FakeSpec("a")]
        chain = h.make(specs, nids=(3, 1, 2))
        h.existing.discard(1)
        chain.start(no_resources)
        h.run_scheduled()
        self.assertEqual(specs[0].calls[0][0], [3, 2])

    def test_the_chain_ends_when_no_notes_are_left(self):
        h = Harness(nids=(1, 2))
        specs = [FakeSpec("a"), FakeSpec("b"), FakeSpec("c")]
        chain = h.make(specs, nids=(1, 2))
        chain.start(no_resources)
        h.run_scheduled()
        h.existing.clear()
        specs[0].chain.on_done(StepOutcome(COMPLETED, message="removed them"))
        h.run_scheduled()
        self.assertEqual(specs[1].calls, [])
        self.assertEqual(specs[2].calls, [])
        [(text, stopped)] = h.summaries
        self.assertTrue(stopped)
        self.assertIn("stopped before Step 2/3 (Op b): none of its notes exist", text)
        self.assertIn("Did not run:</b><br>Step 2/3: Op b<br>Step 3/3: Op c", text)


class StopTests(unittest.TestCase):
    def run_until_step_two_ends(self, outcome):
        h = Harness()
        specs = [FakeSpec("a"), FakeSpec("b"), FakeSpec("c")]
        chain = h.make(specs)
        chain.start(no_resources)
        h.run_scheduled()
        specs[0].chain.on_done(StepOutcome(COMPLETED, message="3 notes<br>edited"))
        h.run_scheduled()
        specs[1].chain.on_done(outcome)
        h.run_scheduled()
        self.assertEqual(specs[2].calls, [])
        [(text, stopped)] = h.summaries
        self.assertTrue(stopped)
        self.assertIn("<b>Step 1/3: Op a</b><br>3 notes<br>edited", text)
        self.assertIn("Did not run:</b><br>Step 3/3: Op c", text)
        return text

    def test_a_cancelled_step_stops_the_chain(self):
        text = self.run_until_step_two_ends(StepOutcome(CANCELLED, message="1 note edited"))
        self.assertIn(
            "<b>Step 2/3: Op b</b> (cancelled; what it had finished is saved)<br>1 note edited",
            text,
        )
        # Plain that part of the chain was done
        self.assertIn(
            "stopped at Step 2/3 (Op b): it was cancelled. The 1 step before it ran to the end."
            " What the cancelled step had finished is saved",
            text,
        )

    def test_a_stopped_step_stops_the_chain_and_says_why(self):
        text = self.run_until_step_two_ends(
            StepOutcome(STOPPED, message="done", stop_reason="Usage limit <reached> & more")
        )
        self.assertIn("(stopped)", text)
        # The reason is plain text
        self.assertIn("(Op b): Usage limit &lt;reached&gt; &amp; more", text)

    def test_a_failed_step_stops_the_chain_and_says_why(self):
        text = self.run_until_step_two_ends(StepOutcome(FAILED, error="KeyError: 'x<y'"))
        self.assertIn("<b>Step 2/3: Op b</b> (failed)", text)
        self.assertIn("it failed with KeyError: &#x27;x&lt;y&#x27;", text)

    def test_the_last_step_stopping_leaves_nothing_not_run(self):
        h = Harness()
        specs = [FakeSpec("a")]
        chain = h.make(specs)
        chain.start(no_resources)
        h.run_scheduled()
        specs[0].chain.on_done(StepOutcome(CANCELLED))
        h.run_scheduled()
        [(text, stopped)] = h.summaries
        self.assertTrue(stopped)
        self.assertNotIn("Did not run", text)


class SynchronousEndTests(unittest.TestCase):
    def test_a_step_failing_inside_start_ends_the_chain_without_recursion(self):
        h = Harness()
        specs = [
            FakeSpec("a", on_start=lambda chain: chain_types.fail_step(chain, "no config")),
            FakeSpec("b"),
        ]
        chain = h.make(specs)
        chain.start(no_resources)
        h.run_scheduled()
        self.assertEqual(specs[1].calls, [])
        [(text, stopped)] = h.summaries
        self.assertTrue(stopped)
        self.assertIn("it failed with no config", text)

    def test_a_completed_step_reported_inside_start_still_waits_for_the_scheduler(self):
        h = Harness()
        specs = [
            FakeSpec("a", on_start=lambda chain: chain.on_done(StepOutcome(COMPLETED))),
            FakeSpec("b"),
        ]
        chain = h.make(specs)
        chain.start(no_resources)
        h.pending.pop(0)()
        self.assertEqual(len(specs[0].calls), 1)
        self.assertEqual(specs[1].calls, [])
        h.run_scheduled()
        self.assertEqual(len(specs[1].calls), 1)

    def test_a_start_that_raises_fails_the_step_and_ends_the_chain(self):
        h = Harness()

        def boom(chain):
            raise ValueError("bad <input>")

        specs = [FakeSpec("a", on_start=boom), FakeSpec("b")]
        chain = h.make(specs)
        chain.start(no_resources)
        h.run_scheduled()
        self.assertEqual(specs[1].calls, [])
        self.assertEqual(len(h.errors), 1)
        # Named as the chain lists it, for the error pane
        self.assertEqual(h.error_titles, ["Step 1/2: Op a"])
        [(text, stopped)] = h.summaries
        self.assertTrue(stopped)
        # The dialog, and its pane, are gone by now: the summary names the step and the error
        self.assertIn("<b>Step 1/2: Op a</b> (failed)", text)
        self.assertIn(
            "The chain stopped at Step 1/2 (Op a): it failed with ValueError: bad &lt;input&gt;",
            text,
        )

    def test_a_start_that_raises_after_failing_its_step_counts_once(self):
        h = Harness()

        def fail_then_raise(chain):
            chain_types.fail_step(chain, "no config")
            raise RuntimeError("and then this")

        specs = [FakeSpec("a", on_start=fail_then_raise), FakeSpec("b")]
        chain = h.make(specs)
        chain.start(no_resources)
        h.run_scheduled()
        self.assertEqual([o.error for _, o in chain.outcomes], ["no config"])
        # Shown all the same
        self.assertEqual(len(h.errors), 1)
        self.assertEqual(len(h.summaries), 1)


class SummaryTests(unittest.TestCase):
    def test_a_chain_that_ran_every_step_shows_info_with_each_message(self):
        h = Harness()
        specs = [FakeSpec("a"), FakeSpec("b")]
        chain = h.make(specs)
        chain.start(no_resources)
        h.run_scheduled()
        specs[0].chain.on_done(StepOutcome(COMPLETED, message="2 notes edited"))
        h.run_scheduled()
        specs[1].chain.on_done(StepOutcome(COMPLETED, message="1 note<br>added"))
        h.run_scheduled()
        [(text, stopped)] = h.summaries
        self.assertFalse(stopped)
        self.assertEqual(
            text,
            "<b>Step 1/2: Op a</b><br>2 notes edited<br><br>"
            "<b>Step 2/2: Op b</b><br>1 note<br>added",
        )

    def test_the_summary_waits_for_the_scheduler(self):
        h = Harness()
        specs = [FakeSpec("a")]
        chain = h.make(specs)
        chain.start(no_resources)
        h.run_scheduled()
        specs[0].chain.on_done(StepOutcome(COMPLETED))
        # Not from inside on_done, which is aqt's success handler
        self.assertEqual(h.summaries, [])
        h.run_scheduled()
        self.assertEqual(len(h.summaries), 1)

    def test_labels_are_escaped(self):
        h = Harness()
        spec = FakeSpec("a")
        spec.label = "Extract words + <Judge>"
        chain = h.make([spec])
        chain.start(no_resources)
        h.run_scheduled()
        spec.chain.on_done(StepOutcome(COMPLETED))
        h.run_scheduled()
        self.assertIn("Extract words + &lt;Judge&gt;", h.summaries[0][0])


class GeneratorResourceTests(unittest.TestCase):
    def test_asked_once_when_any_op_needs_the_generator(self):
        h = Harness()
        specs = [FakeSpec("a"), FakeSpec("b", needs_generator=True), FakeSpec("c", True)]
        chain = h.make(specs)
        chain.start(h.with_resources(True))
        h.run_scheduled()
        for spec in specs:
            spec.chain.on_done(StepOutcome(COMPLETED))
            h.run_scheduled()
        self.assertEqual(h.resource_checks, [h.parent])
        self.assertEqual([len(s.calls) for s in specs], [1, 1, 1])

    def test_not_asked_when_no_op_needs_it(self):
        h = Harness()
        chain = h.make([FakeSpec("a")])
        chain.start(no_resources)
        h.run_scheduled()
        self.assertEqual(len(chain.specs[0].calls), 1)

    def test_declining_starts_nothing_and_shows_no_summary(self):
        h = Harness()
        specs = [FakeSpec("a", needs_generator=True), FakeSpec("b")]
        chain = h.make(specs)
        chain.start(h.with_resources(False))
        h.run_scheduled()
        self.assertEqual(h.resource_checks, [h.parent])
        self.assertEqual([s.calls for s in specs], [[], []])
        self.assertEqual(h.summaries, [])
        # Nor any progress held for a chain that never began
        self.assertEqual(h.events, [])

    def test_the_real_check_with_sudachipy_missing_starts_nothing(self):
        generator_resources = load_ops_module("generator_resources", subdir="")
        h = Harness()
        specs = [FakeSpec("a", needs_generator=True)]
        chain = h.make(specs)
        with mock.patch.object(
            generator_resources.resources, "has_sudachipy", return_value=False
        ), mock.patch.object(generator_resources, "showWarning") as warning:
            chain.start(generator_resources.with_generator_resources)
        h.run_scheduled()
        warning.assert_called_once()
        self.assertEqual(specs[0].calls, [])
        self.assertEqual(h.summaries, [])


class RunOpChainTests(unittest.TestCase):
    def test_run_op_chain_uses_the_generator_check(self):
        with mock.patch.object(op_chain, "with_generator_resources") as check:
            op_chain.run_op_chain([FakeSpec("a", needs_generator=True)], [1], object())
        check.assert_called_once()


class FakeDb:
    """`col.db` over an sqlite table with Anki's `notes.id` column, recording each query."""

    def __init__(self, ids):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("create table notes (id integer primary key)")
        self.conn.executemany("insert into notes values (?)", [(i,) for i in ids])
        self.queries: list[str] = []

    def list(self, sql, *args):
        self.queries.append(sql)
        return [row[0] for row in self.conn.execute(sql, args)]


class ExistingNoteIdsTests(unittest.TestCase):
    def col(self, ids):
        db = FakeDb(ids)
        return type("Col", (), {"db": db})(), db

    def test_keeps_the_order_given_and_drops_the_missing(self):
        col, _ = self.col([10, 20, 30])
        self.assertEqual(op_chain.existing_note_ids(col, [30, 99, 10, 20]), [30, 10, 20])

    def test_a_whole_collection_is_one_query(self):
        # The ids are inlined, so SQLite's bound-variable limit does not apply; a statement
        # over 100k note ids is ~1.4 MB, far under SQLite's default statement length limit
        ids = [1_600_000_000_000 + i for i in range(100_000)]
        col, db = self.col(ids[::2])
        self.assertEqual(op_chain.existing_note_ids(col, list(reversed(ids))), ids[::2][::-1])
        self.assertEqual(len(db.queries), 1)

    def test_no_ids_asks_nothing(self):
        col, db = self.col([1])
        self.assertEqual(op_chain.existing_note_ids(col, []), [])
        self.assertEqual(db.queries, [])


if __name__ == "__main__":
    unittest.main()
