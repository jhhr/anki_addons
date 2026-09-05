"""Serialising collection access, and what waiting for a turn costs.

One worker thread owns the collection and everyone submits work to it, so the properties worth
pinning down are the ones that used to come from a semaphore every caller blocked on: exactly
one call inside the backend at a time, a queue that a cancelled run empties without running,
and a nested call that does not queue behind itself. On top of those, the reason for the
rewrite: a caller on the event loop waits without holding a pool thread.

Cancellation is exercised through real Run objects rather than a stubbed-out cancelled flag,
because everything that can go wrong with it here is about *which thread* is asked. The worker
belongs to no run and never will, so a stub that answers the same on every thread passes
whether or not the run actually reaches the worker - which is the one thing worth testing.
"""

import asyncio
import threading
import time
import unittest

from anki_stubs import load_ops_module, mw

ca = load_ops_module("collection_access")
conc = load_ops_module("concurrency")
# The same module object collection_access imported, so the runs begun here are the ones it
# reads
api = load_ops_module("api_client")


class FakeCollection:
    """Records how many callers are inside it at once, which is the thing being serialised."""

    def __init__(self, dwell: float = 0.005):
        self.dwell = dwell
        self.inside = 0
        self.most_at_once = 0
        self.finds: list[str] = []
        self.fetched: list[int] = []
        self._lock = threading.Lock()
        self.gate: "threading.Event | None" = None

    def _enter(self) -> None:
        with self._lock:
            self.inside += 1
            self.most_at_once = max(self.most_at_once, self.inside)

    def _leave(self) -> None:
        with self._lock:
            self.inside -= 1

    def find_notes(self, query: str):
        self._enter()
        try:
            if self.gate is not None:
                self.gate.wait(5)
            time.sleep(self.dwell)
            self.finds.append(query)
            return [1, 2, 3]
        finally:
            self._leave()

    def get_note(self, note_id):
        self._enter()
        try:
            time.sleep(self.dwell)
            self.fetched.append(int(note_id))
            return f"note{note_id}"
        finally:
            self._leave()


class CollectionAccessTestCase(unittest.TestCase):
    def setUp(self):
        self.col = FakeCollection()
        mw.col = self.col
        # This thread is the op's thread: it submits collection work and is the one enrolled
        self.run = api.begin_run()
        ca.end_cleanup_phase()
        conc.collection_pressure.reset()

    def tearDown(self):
        api.end_run()
        ca.end_cleanup_phase()

    def submit_in_run(self, target):
        """A worker thread of this run, which is how ops reach the collection in a real op."""
        def enrolled():
            api.join_run(self.run)
            target()

        thread = threading.Thread(target=enrolled)
        thread.start()
        return thread

    def wait_until(self, predicate, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.002)
        return False


class SerialisationTests(CollectionAccessTestCase):
    def test_only_one_caller_is_inside_the_collection_at_a_time(self):
        threads = [
            threading.Thread(target=lambda i=i: ca.find_notes(f"q{i}")) for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(self.col.most_at_once, 1)
        self.assertEqual(len(self.col.finds), 20)

    def test_a_nested_call_runs_instead_of_queueing_behind_itself(self):
        # With one worker, queueing behind your own turn is a deadlock rather than a slowdown
        def outer():
            return ca.find_notes("inner")

        result = ca._run_on_collection("outer", outer)
        self.assertEqual(list(result), [1, 2, 3])

    def test_a_batch_is_one_turn_rather_than_one_per_note(self):
        ca.get_notes([1, 2, 3, 4])
        self.assertEqual(self.col.most_at_once, 1)
        self.assertEqual(self.col.fetched, [1, 2, 3, 4])

    def test_an_error_reaches_the_caller(self):
        def boom():
            raise ValueError("no")

        with self.assertRaises(ValueError):
            ca._run_on_collection("boom", boom)

    def test_holding_the_collection_is_what_gets_recorded(self):
        conc.collection_pressure.reset()
        ca.find_notes("q")
        sample = conc.collection_pressure.sample()
        self.assertIsNotNone(sample)
        _, mean_hold = sample
        self.assertGreaterEqual(mean_hold, self.col.dwell)


class CancellationTests(CollectionAccessTestCase):
    def test_queued_work_is_abandoned_without_being_run(self):
        self.run.cancelled.set()
        with self.assertRaises(ca.RunCancelled):
            ca.find_notes("never runs")
        self.assertEqual(self.col.finds, [])

    def test_the_cleanup_phase_still_reaches_the_collection_after_a_cancel(self):
        # The cleanup phase runs after a cancel precisely so what was done gets saved
        self.run.cancelled.set()
        ca.begin_cleanup_phase()
        try:
            self.assertEqual(list(ca.find_notes("cleanup")), [1, 2, 3])
        finally:
            ca.end_cleanup_phase()

    def test_the_exemption_follows_the_job_and_not_the_worker_thread(self):
        # The thread that runs the job is the shared worker, which is nobody's cleanup thread,
        # so the exemption has to be read where the job was submitted
        self.run.cancelled.set()
        result: list = []

        def submit():
            ca.begin_cleanup_phase()
            try:
                result.append(ca.find_notes("from an exempt thread"))
            finally:
                ca.end_cleanup_phase()

        self.submit_in_run(submit).join(10)
        self.assertEqual(len(result), 1)

    def test_a_batch_gives_up_partway_rather_than_running_to_the_end(self):
        # A broad search hands get_notes thousands of ids; finishing them all is exactly the
        # uninterruptible stretch this module exists to avoid. The batch runs on the worker,
        # so the per-note check has to see the submitting thread's run, not the worker's.
        fetch = self.col.get_note

        def cancel_after_two(note_id):
            note = fetch(note_id)
            if len(self.col.fetched) >= 2:
                self.run.cancelled.set()
            return note

        self.col.get_note = cancel_after_two
        with self.assertRaises(ca.RunCancelled):
            ca.get_notes(list(range(50)))
        self.assertLess(len(self.col.fetched), 50)

    def test_a_cancel_that_lands_after_a_job_was_queued_still_drops_it(self):
        # The submitting side's own check cannot cover this: these were queued while the run
        # was still live, and nothing looks at them again until the worker pops them. The
        # worker is enrolled in no run of its own, so it can only judge them by the run each
        # job carries - and when it could not, a cancelled whole-collection search ran every
        # queued job to completion, which is what the module exists to stop.
        self.col.gate = threading.Event()
        outcomes: "list[str]" = []
        lock = threading.Lock()

        def search(query: str):
            def attempt():
                try:
                    ca.find_notes(query)
                    outcome = "ran"
                except ca.RunCancelled:
                    outcome = "dropped"
                with lock:
                    outcomes.append(outcome)

            return attempt

        holder = self.submit_in_run(search("held"))
        self.assertTrue(self.wait_until(lambda: self.col.inside == 1))
        queued = [self.submit_in_run(search(f"q{i}")) for i in range(10)]
        self.assertTrue(self.wait_until(lambda: ca._jobs.qsize() >= 10))

        self.run.cancelled.set()
        self.col.gate.set()
        for thread in [holder, *queued]:
            thread.join(10)

        self.assertEqual(outcomes.count("dropped"), 10)
        self.assertEqual(self.col.finds, ["held"])


class EventLoopTests(CollectionAccessTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_awaiting_a_turn_returns_the_result(self):
        self.assertEqual(list(await ca.find_notes_async("q")), [1, 2, 3])
        self.assertEqual(await ca.get_notes_async([7, 8]), ["note7", "note8"])

    async def test_an_awaited_error_reaches_the_caller(self):
        def boom():
            raise ValueError("no")

        with self.assertRaises(ValueError):
            await ca.run_on_collection_async("boom", boom)

    async def test_a_cancelled_run_abandons_awaited_work_too(self):
        self.run.cancelled.set()
        with self.assertRaises(ca.RunCancelled):
            await ca.find_notes_async("never runs")
        self.assertEqual(self.col.finds, [])

    async def test_waiting_for_a_turn_does_not_cost_a_thread_per_waiter(self):
        # The reason for the rewrite. Thirty callers queue for a collection held by one slow
        # turn; under a semaphore that was thirty pool threads parked in acquire.
        self.col.gate = threading.Event()
        waiters = [asyncio.create_task(ca.find_notes_async(f"q{i}")) for i in range(30)]
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            # The one worker running the held turn, and no thread for any of the thirty
            self.assertLessEqual(threading.active_count(), self._baseline_threads + 1)
        finally:
            self.col.gate.set()
            await asyncio.gather(*waiters)
        self.assertEqual(self.col.most_at_once, 1)
        self.assertEqual(len(self.col.finds), 30)

    async def test_the_loop_keeps_running_while_a_turn_is_waited_for(self):
        # Cancellation polling, adapting and progress all live on the loop
        ticks = 0

        async def tick():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        ticker = asyncio.create_task(tick())
        await ca.find_notes_async("q")
        ticker.cancel()
        self.assertGreater(ticks, 5)

    def setUp(self):
        super().setUp()
        # Taken before any waiter exists, with the worker already started by an earlier test
        ca._worker_thread()
        self._baseline_threads = threading.active_count()


if __name__ == "__main__":
    unittest.main()
