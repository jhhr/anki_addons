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

from anki_stubs import load_ops_module, mw

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
        self.begun = 0
        self.ended = 0
        self.window_tasks: "list[int]" = []
        self.aborted = False

    @property
    def budget(self) -> int:
        return self.limit * DEPTH

    def begin_window(self) -> None:
        self.begun += 1

    def note_window_tasks(self, count: int) -> None:
        self.window_tasks.append(count)

    def end_window(self) -> None:
        self.ended += 1

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

    async def past_the_first_pass(self, work: Workload, gate: FakeGate) -> "asyncio.Task":
        """Start the run and let the measured barrier pass complete."""
        runner = self.start(work, gate)
        await settle()
        work.release_all()
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

    def test_the_first_pass_is_a_barrier(self):
        """The estimator needs a pass that both starts and ends with nothing in flight."""

        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = self.start(work, gate)
            await settle()
            self.assertEqual(work.started, gate.budget)

            # All but one released: a rolling refill would top the budget back up, but the
            # measured pass must wait for the last of it
            work.release(gate.budget - 1)
            await settle()
            self.assertEqual(work.started, gate.budget)
            self.assertEqual(gate.ended, 0)

            work.release_all()
            await settle()
            self.assertEqual(gate.ended, 1)
            self.assertEqual(work.started, gate.budget * 2)
            await self.finish(work, runner)

        self.run_async(main())

    def test_each_completion_starts_a_replacement(self):
        """The point of the change: a freed slot is refilled at once, not at a boundary."""

        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = await self.past_the_first_pass(work, gate)
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
            runner = await self.past_the_first_pass(work, gate)
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
            runner = await self.past_the_first_pass(work, gate)
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

    def test_the_first_pass_is_measured_against_its_api_tasks(self):
        async def main():
            work, gate = Workload(200), FakeGate(limit=4)
            runner = await self.past_the_first_pass(work, gate)
            self.assertEqual(gate.begun, 1)
            self.assertEqual(gate.ended, 1)
            # Measured before any of the pass finished, so it saw the whole of it
            self.assertEqual(gate.window_tasks, [gate.budget])
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


if __name__ == "__main__":
    unittest.main()
