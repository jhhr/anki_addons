"""How the bulk drivers spend the task budget.

The ops used to run notes in fixed windows: spawn a window's worth of tasks, wait for every
one of them, then spawn the next. That left the gate draining from full to empty at every
boundary, because the slowest request in a window held the other slots idle while it finished
- and since the gate only grows while it is saturated, the idle stretches also cost the run
the concurrency it was allowed to grow into. run_plans_rolling replaced the windows with a
budget that is refilled as individual tasks finish.

These drive the driver rather than time it. Each task blocks on a future the test resolves by
hand, so "released one task, exactly one replacement started" is something to assert rather
than a number to sample: under the old window loop, releasing one task of sixteen started
nothing at all.
"""

import asyncio
import logging
import unittest

from addon_modules import (
    RunCollection,
    RunProgress,
    load_ops_module,
    mw,
    patch_nested_run,
    wait_until,
)

base_ops = load_ops_module("base_ops")
concurrency = load_ops_module("concurrency")

NotePlan = base_ops.NotePlan
DEPTH = concurrency.TASK_QUEUE_DEPTH


def setUpModule():
    # The cancel path logs its teardown at info and dumps thread stacks; the tests that assert
    # on logging raise the level back for themselves.
    logging.disable(logging.WARNING)


def tearDownModule():
    logging.disable(logging.NOTSET)


async def settle(turns: int = 50) -> None:
    """Give the driver enough event loop turns to react to whatever the test just did.

    A released task takes a handful of turns to reach the driver: it resumes, finishes, its
    done callback is called soon, that wakes the driver's queue, and only then does the driver
    start replacements. Yielding a fixed number of times keeps the tests off the wall clock.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


class Workload:
    """Plans whose tasks block until the test lets them finish."""

    def __init__(self, count: int, task_count: int = 1, tasks_per_plan: int = 1):
        self.releases: "list[asyncio.Future]" = []  # one per task started, in start order
        self.tasks: "list[asyncio.Task]" = []
        self.finished: "list[int]" = []
        self.plans = [
            NotePlan(task_count=task_count, spawn=self._make_spawn(index, tasks_per_plan))
            for index in range(count)
        ]

    def _make_spawn(self, index: int, tasks_per_plan: int):
        def spawn(tasks: "list[asyncio.Task]") -> None:
            for _ in range(tasks_per_plan):
                release = asyncio.get_running_loop().create_future()
                self.releases.append(release)
                task = asyncio.create_task(self._task(index, release))
                self.tasks.append(task)
                tasks.append(task)

        return spawn

    async def _task(self, index: int, release: "asyncio.Future") -> None:
        await release
        self.finished.append(index)

    @property
    def started(self) -> int:
        """Tasks started since the run began, finished ones included."""
        return len(self.releases)

    @property
    def in_flight(self) -> int:
        return sum(1 for release in self.releases if not release.done())

    def release(self, count: int) -> int:
        """Let `count` of the running tasks finish, oldest first."""
        released = 0
        for future in self.releases:
            if released == count:
                break
            if not future.done():
                future.set_result(None)
                released += 1
        return released

    def release_all(self) -> int:
        return self.release(len(self.releases))


class FakeGate:
    """The gate's interface, with a limit the test moves by hand.

    Deliberately does not gate anything: what is under test is how many tasks the driver keeps
    alive, and putting a real gate underneath would only measure the gate.
    """

    def __init__(self, limit: int = 4):
        self.limit = limit
        self.live_counts: "list[float]" = []
        self.aborted = False

    @property
    def budget(self) -> int:
        return self.limit * DEPTH

    def note_live_tasks(self, count: float) -> None:
        self.live_counts.append(count)

    def abort(self) -> None:
        self.aborted = True


class FakeUpdater:
    gate = None

    def update_progress(self) -> None:
        pass

    def show_cancelling(self) -> None:
        pass


class RollingDriverTest(unittest.TestCase):
    def setUp(self):
        mw.progress.cancel = False

    def tearDown(self):
        mw.progress.cancel = False

    # --- driving helpers ------------------------------------------------------------------

    def start(self, work: Workload, gate: FakeGate) -> "asyncio.Task":
        return asyncio.ensure_future(
            base_ops.run_plans_rolling(
                work.plans,
                gate=gate,
                progress_updater=FakeUpdater(),
                cancel_state=base_ops.CancelState(),
                label="test",
            )
        )

    async def at_full_budget(self, work: Workload, gate: FakeGate) -> "asyncio.Task":
        """Start the run and let it fill the budget.

        Used to be `past_the_first_pass`, which had to release the whole of that pass and wait
        for it: the first pass was a barrier so the memory estimator could measure it against a
        moment with nothing in flight. The estimator fits against the live task count now and
        needs no such moment, so a started run is at its budget immediately.
        """
        runner = self.start(work, gate)
        await settle()
        return runner

    async def finish(self, work: Workload, runner: "asyncio.Task") -> bool:
        """Release everything, in rounds, until the run is over."""
        for _ in range(1000):
            if runner.done():
                return await runner
            work.release_all()
            await settle(10)
        self.fail("the run did not finish")

    def run_async(self, coro):
        return asyncio.run(coro)

    # --- the barrier and the rolling refill -----------------------------------------------

    def test_the_first_pass_is_not_a_barrier(self):
        """It used to be one, so the RSS estimator got a window with nothing in flight.

        The estimator fits memory against the live task count instead, which tells a task's
        cost apart from the run's accumulation without needing a quiet moment - so the first
        pass rolls like every other, and the run stops paying a serialised pass for it.
        """

        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = self.start(work, gate)
            await settle()
            self.assertEqual(work.started, gate.budget)

            # Under the barrier this started nothing at all until the last of the pass landed
            work.release(gate.budget - 1)
            await settle()
            self.assertEqual(work.started, gate.budget * 2 - 1)
            self.assertEqual(work.in_flight, gate.budget)

            await self.finish(work, runner)

        self.run_async(main())

    def test_each_completion_starts_a_replacement(self):
        """The point of the change: a freed slot is refilled at once, not at a boundary."""

        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = await self.at_full_budget(work, gate)
            started = work.started

            work.release(1)
            await settle()
            self.assertEqual(work.started, started + 1)
            self.assertEqual(work.in_flight, gate.budget)

            work.release(5)
            await settle()
            self.assertEqual(work.started, started + 6)
            self.assertEqual(work.in_flight, gate.budget)

            await self.finish(work, runner)

        self.run_async(main())

    def test_a_raised_limit_is_used_at_once(self):
        """A ceiling the gate grows into must not wait for the next boundary to be spent."""

        async def main():
            work, gate = Workload(400), FakeGate(limit=2)
            runner = await self.at_full_budget(work, gate)
            self.assertEqual(work.in_flight, 2 * DEPTH)

            gate.limit = 6
            work.release(1)
            await settle()
            self.assertEqual(work.in_flight, 6 * DEPTH)

            await self.finish(work, runner)

        self.run_async(main())

    def test_a_lowered_limit_is_respected_from_then_on(self):
        """The gate halves its limit under memory pressure; the budget has to follow it down."""

        async def main():
            work, gate = Workload(400), FakeGate(limit=8)
            runner = await self.at_full_budget(work, gate)
            self.assertEqual(work.in_flight, 8 * DEPTH)

            gate.limit = 2
            # Nothing new starts until enough has drained to be back under the new budget
            work.release(8 * DEPTH - 2 * DEPTH)
            await settle()
            self.assertEqual(work.in_flight, 2 * DEPTH)

            work.release(1)
            await settle()
            self.assertEqual(work.in_flight, 2 * DEPTH)

            await self.finish(work, runner)

        self.run_async(main())

    def test_the_budget_is_never_exceeded(self):
        async def main():
            work, gate = Workload(300), FakeGate(limit=4)
            runner = self.start(work, gate)
            await settle()
            for _ in range(200):
                self.assertLessEqual(work.in_flight, gate.budget)
                if runner.done():
                    break
                work.release(3)
                await settle(10)
            await self.finish(work, runner)

        self.run_async(main())

    # --- what the plans cost --------------------------------------------------------------

    def test_the_live_api_task_count_is_reported_as_it_moves(self):
        """What the estimator fits memory against, so it has to rise and fall with the run."""

        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = await self.at_full_budget(work, gate)
            self.assertEqual(gate.live_counts[-1], gate.budget)

            work.release(gate.budget)
            await settle()
            # Reported after the completions were taken off and again after the refill, so the
            # fit sees the count come down as well as go back up
            self.assertLess(min(gate.live_counts), gate.budget)
            self.assertEqual(gate.live_counts[-1], gate.budget)
            await self.finish(work, runner)

        self.run_async(main())

    def test_the_reported_count_is_api_tasks_not_task_objects(self):
        """A plan's bookkeeping tasks hold no prompt and would only dilute the figure."""

        async def main():
            work, gate = Workload(60, task_count=3, tasks_per_plan=3), FakeGate(limit=2)
            runner = self.start(work, gate)
            await settle()
            # Three plans of three API calls started, spread over nine task objects
            self.assertEqual(work.started, 9)
            self.assertEqual(gate.live_counts[-1], 9)
            await self.finish(work, runner)

        self.run_async(main())

    def test_the_budget_is_spent_in_api_tasks_not_task_objects(self):
        """A plan fanning out to several API calls has to cost several places in the budget."""

        async def main():
            work, gate = Workload(60, task_count=3, tasks_per_plan=3), FakeGate(limit=2)
            runner = self.start(work, gate)
            await settle()
            # Plans are started whole, so the budget of 8 is reached by the third plan
            self.assertEqual(work.started, 9)
            cancelled = await self.finish(work, runner)
            self.assertFalse(cancelled)
            self.assertEqual(sorted(work.finished), sorted(list(range(60)) * 3))

        self.run_async(main())

    def test_a_plan_costing_more_than_the_whole_budget_still_runs(self):
        async def main():
            work, gate = Workload(4, task_count=99, tasks_per_plan=5), FakeGate(limit=1)
            runner = self.start(work, gate)
            await settle()
            # One plan only - it is over budget on its own - but it does get started
            self.assertEqual(work.started, 5)
            await self.finish(work, runner)
            self.assertEqual(sorted(work.finished), sorted(list(range(4)) * 5))

        self.run_async(main())

    def test_every_plan_runs_exactly_once(self):
        async def main():
            work, gate = Workload(150), FakeGate(limit=4)
            runner = self.start(work, gate)
            cancelled = await self.finish(work, runner)
            self.assertFalse(cancelled)
            self.assertEqual(sorted(work.finished), list(range(150)))

        self.run_async(main())

    def test_no_plans_at_all(self):
        async def main():
            work, gate = Workload(0), FakeGate(limit=4)
            self.assertFalse(await self.start(work, gate))

        self.run_async(main())

    def test_plans_that_spawn_nothing_are_skipped(self):
        async def main():
            gate = FakeGate(limit=4)
            plans = [NotePlan(task_count=1, spawn=lambda tasks: None) for _ in range(50)]
            cancelled = await base_ops.run_plans_rolling(
                plans,
                gate=gate,
                progress_updater=FakeUpdater(),
                cancel_state=base_ops.CancelState(),
                label="test",
            )
            self.assertFalse(cancelled)

        self.run_async(main())

    def test_a_task_that_raises_is_reported_not_swallowed(self):
        """Nothing else reads these results, so an error getting past process_op ends here."""

        async def main():
            gate = FakeGate(limit=2)

            def spawn(tasks):
                async def boom():
                    raise RuntimeError("boom")

                tasks.append(asyncio.create_task(boom()))

            plans = [NotePlan(task_count=1, spawn=spawn) for _ in range(10)]
            logging.disable(logging.NOTSET)
            try:
                with self.assertLogs(base_ops.logger, level=logging.ERROR) as logs:
                    cancelled = await base_ops.run_plans_rolling(
                        plans,
                        gate=gate,
                        progress_updater=FakeUpdater(),
                        cancel_state=base_ops.CancelState(),
                        label="test",
                    )
            finally:
                logging.disable(logging.WARNING)
            self.assertFalse(cancelled)
            self.assertTrue(any("boom" in line for line in logs.output))

        self.run_async(main())


class RollingDriverCancelTest(unittest.TestCase):
    """Cancelling used to happen at a window boundary, where nothing was in flight.

    A rolling run has live tasks and tasks queued on the gate at every point in it, so every
    way out has to cancel what is live and release what is queued.
    """

    def setUp(self):
        mw.progress.cancel = False

    def tearDown(self):
        mw.progress.cancel = False

    def test_cancelling_mid_run_aborts_the_gate(self):
        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = asyncio.ensure_future(
                base_ops.run_plans_rolling(
                    work.plans,
                    gate=gate,
                    progress_updater=FakeUpdater(),
                    cancel_state=base_ops.CancelState(),
                    label="test",
                )
            )
            await settle()
            work.release_all()
            await settle()

            mw.progress.cancel = True
            # The only wall-clock wait in the suite: the cancel monitor polls the progress
            # dialog rather than being told, so the run learns about it on the next poll.
            for _ in range(40):
                if runner.done():
                    break
                await asyncio.sleep(0.05)

            self.assertTrue(runner.done(), "the run did not notice the cancel")
            self.assertTrue(await runner)
            self.assertTrue(gate.aborted)
            self.assertLess(len(work.finished), 200)

        asyncio.run(main())

    def test_cancelling_while_tasks_are_being_started(self):
        """A cancel arriving inside the fill loop must not be reported as a clean finish."""

        async def main():
            gate = FakeGate(limit=4)
            started = []

            def spawn(tasks):
                # Flips the flag as the very first plan is started, so the fill stops part way
                # through with nothing yet running
                mw.progress.cancel = True
                started.append(1)

            plans = [NotePlan(task_count=1, spawn=spawn) for _ in range(50)]
            cancelled = await base_ops.run_plans_rolling(
                plans,
                gate=gate,
                progress_updater=FakeUpdater(),
                cancel_state=base_ops.CancelState(),
                label="test",
            )
            self.assertTrue(cancelled)
            self.assertTrue(gate.aborted)
            self.assertLess(len(started), 50)

        asyncio.run(main())

    def test_cancelling_before_anything_starts(self):
        async def main():
            work, gate = Workload(50), FakeGate(limit=4)
            mw.progress.cancel = True
            cancelled = await base_ops.run_plans_rolling(
                work.plans,
                gate=gate,
                progress_updater=FakeUpdater(),
                cancel_state=base_ops.CancelState(),
                label="test",
            )
            self.assertTrue(cancelled)
            self.assertTrue(gate.aborted)
            self.assertEqual(work.started, 0)

        asyncio.run(main())

    def test_a_cancel_state_set_mid_run_tears_down_what_is_live(self):
        """The op's own cancel state is only checked at the top of the driver's loop.

        The monitor polls the progress dialog, not this, so this is the way in to the branch
        that used to just break. At a window boundary that was safe - nothing was in flight -
        but a rolling run always has live tasks and tasks queued on the gate, and leaving them
        was the bug: the run reported itself finished with requests still going.
        """

        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            cancel_state = base_ops.CancelState()
            runner = asyncio.ensure_future(
                base_ops.run_plans_rolling(
                    work.plans,
                    gate=gate,
                    progress_updater=FakeUpdater(),
                    cancel_state=cancel_state,
                    label="test",
                )
            )
            await settle()
            work.release_all()
            await settle()
            self.assertEqual(work.in_flight, gate.budget)

            cancel_state.cancel()
            # Wakes the driver so it comes back round to the top of its loop
            work.release(1)
            await settle()

            self.assertTrue(runner.done(), "the run did not notice the cancel")
            self.assertTrue(await runner)
            self.assertTrue(gate.aborted)
            # What was still running was cancelled rather than left going
            self.assertTrue(any(task.cancelled() for task in work.tasks))

        asyncio.run(main())

    def test_a_run_cancelled_by_its_own_work_stops(self):
        """The claude CLI cancels the run from a worker when the usage limit is hit; nothing
        tells the progress dialog, so the monitor has to see the run's flag itself."""
        api = load_ops_module("api_client")
        api.begin_run()
        self.addCleanup(api.end_run)

        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = asyncio.ensure_future(
                base_ops.run_plans_rolling(
                    work.plans,
                    gate=gate,
                    progress_updater=FakeUpdater(),
                    cancel_state=base_ops.CancelState(),
                    label="test",
                )
            )
            await settle()
            api.cancel_run(reason="usage limit")
            for _ in range(40):
                if runner.done():
                    break
                await asyncio.sleep(0.05)

            self.assertTrue(runner.done(), "the run did not notice the cancel")
            self.assertTrue(await runner)
            self.assertTrue(gate.aborted)
            self.assertLess(len(work.finished), 200)

        asyncio.run(main())
        self.assertEqual(api.take_stop_reason(), "usage limit")

    def test_the_ops_own_task_being_cancelled_is_swallowed(self):
        """The run keeps what it has rather than the cancellation tearing the frame down."""

        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = asyncio.ensure_future(
                base_ops.run_plans_rolling(
                    work.plans,
                    gate=gate,
                    progress_updater=FakeUpdater(),
                    cancel_state=base_ops.CancelState(),
                    label="test",
                )
            )
            await settle()
            work.release_all()
            await settle()

            runner.cancel()
            cancelled = await runner
            self.assertTrue(cancelled)
            self.assertTrue(gate.aborted)

        asyncio.run(main())


class FlushStartedPlansTests(unittest.TestCase):
    """A note whose tasks save it together, once all are done, lost the finished ones' work to a
    cancel, which cancels the saving task too. bulk_nested_notes_op flushes the notes it
    started; each op's flush saves once and says whether its own save had run."""

    def setUp(self):
        mw.progress.cancel = False
        self.addCleanup(setattr, mw.progress, "cancel", False)

    def test_a_cancelled_run_flushes_the_started_notes_before_on_end(self):
        events = []
        saved = [False, False, False]
        releases = []

        def plan_note(config, index, **_):
            def save():
                if saved[index]:
                    return False
                saved[index] = True
                return True

            def flush():
                events.append(("flush", index))
                return save()

            async def work(release):
                await release
                save()

            def spawn(tasks):
                release = asyncio.get_running_loop().create_future()
                releases.append(release)
                tasks.append(asyncio.create_task(work(release)))

            return NotePlan(task_count=1, spawn=spawn, flush=flush)

        progress = RunProgress()

        async def main():
            runner = asyncio.ensure_future(
                base_ops.bulk_nested_notes_op(
                    message="test",
                    config={},
                    bulk_inner_op=plan_note,
                    col=RunCollection(),
                    notes=[0, 1, 2],
                    edited_nids=[],
                    progress_updater=progress,
                    notes_to_add_dict={},
                    notes_to_update_dict={},
                    model="model",
                    on_end=lambda: events.append(("on_end",)),
                )
            )
            # A budget of one task: note 1 starts only once note 0 is done, and note 2 waits
            # for note 1, which never finishes
            self.assertTrue(await wait_until(lambda: len(releases) == 1))
            releases[0].set_result(None)
            self.assertTrue(await wait_until(lambda: len(releases) == 2))
            await settle()
            mw.progress.cancel = True
            self.assertTrue(await wait_until(runner.done), "the run did not notice the cancel")
            runner.result()

        with patch_nested_run(base_ops):
            asyncio.run(main())

        # note 0 saved itself and its flush says so; note 2 never started, so never flushed
        self.assertEqual(events, [("flush", 0), ("flush", 1), ("on_end",)])
        self.assertEqual(saved, [True, True, False])

    def test_the_notes_to_add_are_those_registered_before_the_flush(self):
        """A thread a cancel abandoned goes on registering new notes. One registered once the
        results are being flushed has its placeholder in no result saved, so it is left out
        of the notes to add, whose lists are copies the thread's appends do not reach."""
        shared: dict = {}
        early, late, later = object(), object(), object()
        releases = []

        def plan_note(config, index, **_):
            def flush():
                shared["word"].append(late)
                shared.setdefault("other", []).append(late)
                return False

            async def work(release):
                await release
                shared.setdefault("word", []).append(early)

            def spawn(tasks):
                release = asyncio.get_running_loop().create_future()
                releases.append(release)
                tasks.append(asyncio.create_task(work(release)))

            return NotePlan(task_count=1, spawn=spawn, flush=flush)

        async def main():
            runner = asyncio.ensure_future(
                base_ops.bulk_nested_notes_op(
                    message="test",
                    config={},
                    bulk_inner_op=plan_note,
                    col=RunCollection(),
                    notes=[0],
                    edited_nids=[],
                    progress_updater=RunProgress(),
                    notes_to_add_dict=shared,
                    notes_to_update_dict={},
                    model="model",
                    on_end=lambda: shared["word"].append(later),
                )
            )
            self.assertTrue(await wait_until(lambda: len(releases) == 1))
            releases[0].set_result(None)
            self.assertTrue(await wait_until(runner.done), "the run did not finish")
            return runner.result()

        with patch_nested_run(base_ops):
            _, to_add, _, _ = asyncio.run(main())

        self.assertEqual(to_add, {"word": [early]})
        self.assertEqual(shared["word"], [early, late, later])

    def test_each_flush_runs_whatever_the_one_before_did(self):
        calls = []

        def flush(name, outcome):
            def run():
                calls.append(name)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            return run

        plans = [
            NotePlan(task_count=1, spawn=lambda tasks: None, flush=flush("failing", ValueError())),
            NotePlan(task_count=1, spawn=lambda tasks: None),
            NotePlan(task_count=1, spawn=lambda tasks: None, flush=flush("saved", False)),
            NotePlan(task_count=1, spawn=lambda tasks: None, flush=flush("unsaved", True)),
        ]
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs(base_ops.logger, level=logging.ERROR) as logs:
                self.assertEqual(base_ops.flush_started_plans(plans, cancelled=False), 2)
            # a run that was not cancelled leaves nothing unsaved unless something went wrong
            self.assertIn("not cancelled", logs.output[-1])
            with self.assertLogs(base_ops.logger, level=logging.INFO) as logs:
                self.assertEqual(base_ops.flush_started_plans(plans[1:], cancelled=True), 1)
            self.assertIn("cancel left unsaved", logs.output[-1])
            with self.assertNoLogs(base_ops.logger, level=logging.INFO):
                self.assertEqual(base_ops.flush_started_plans(plans[1:3], cancelled=False), 0)
        finally:
            logging.disable(logging.WARNING)
        self.assertEqual(calls, ["failing", "saved", "unsaved", "saved", "unsaved", "saved"])



class RunOnceTests(unittest.TestCase):
    """The rule each nested op's note save used to carry a copy of: whichever of the note's own
    save and its flush comes first runs the save, and says whether it saved anything."""

    def test_the_first_call_saves_and_says_so_the_rest_do_nothing(self):
        calls = []
        save = base_ops.run_once(lambda: calls.append("save") or True)
        self.assertTrue(save())
        self.assertFalse(save())
        self.assertEqual(calls, ["save"])

    def test_a_save_that_found_nothing_to_save_is_no_save(self):
        # A cancel with none of a note's words finished: its flush writes nothing, and the
        # cancelled run's log must not count it among the notes it saved
        save = base_ops.run_once(lambda: False)
        self.assertFalse(save())
        plans = [NotePlan(task_count=1, spawn=lambda tasks: None, flush=base_ops.run_once(bool))]
        self.assertEqual(base_ops.flush_started_plans(plans, cancelled=True), 0)


if __name__ == "__main__":
    unittest.main()
