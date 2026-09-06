"""Tests for async_api_ops/concurrency.py.

Two things here are worth pinning down. One is the arithmetic that turns "how much memory is
free" into "how many requests may be in flight", which is easy to change in a way that looks
reasonable and quietly produces a limit of 4 or of 4000. The other is the gate itself, whose
awkward case is a limit that shrinks while tasks are queued behind it - the point at which a
lost wakeup would hang a run for good.

Memory probing is stubbed for all of that: a test that asked the machine how much RAM it had
would assert different things on different machines. The exception is
MemoryProbeContractTests, which runs against the real probe on purpose - the platform
underneath is exactly where those go wrong, so a fake would only prove the fake works.

Not covered here: base_ops wiring the gate's ceiling to the connection pool size. That call
needs aqt. The gate's half of it - reporting the ceiling when it moves - is covered by
CeilingReportingTests.
"""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from addon_modules import load_addon_module  # type: ignore

conc = load_addon_module("concurrency")

MB = conc.MB
GB = 1024 * MB


class StubMemory:
    """Stands in for the platform memory probes.

    `total`/`available` answer system_memory; `rss` answers process_memory and can be moved
    during a test to simulate the process growing or shrinking.

    `rss` is the raw reading, as the real probe gives it: whatever tracemalloc's trace table
    weighs is already inside it. Tests about that exclusion have to set it that way round, or
    they pass whether or not the exclusion happens.
    """

    def __init__(self, total: int = 32 * GB, available: int = 16 * GB, rss: int = 500 * MB):
        self.total = total
        self.available = available
        self.rss = rss
        self.probe_failed = False

    def system_memory(self):
        return None if self.probe_failed else (self.total, self.available)

    def process_memory(self):
        return None if self.probe_failed else self.rss


class StubTracemalloc:
    """Stands in for the tracemalloc module, so a test can move the traced total by hand.

    Swapped in for the module rather than for concurrency's traced_memory(), so that probe's
    own handling of "is anything tracing at all?" is under test too.
    """

    def __init__(self, current: int = 100 * MB, tracing: bool = False, overhead: int = 0):
        self.current = current
        self.tracing = tracing
        # What the trace table itself occupies. Real tracemalloc charges about 53 bytes per
        # live traced allocation, and it lands in RSS like anything else.
        self.overhead = overhead
        self.starts = 0
        self.stops = 0

    def is_tracing(self) -> bool:
        return self.tracing

    def start(self, frames: int = 1) -> None:
        self.tracing = True
        self.starts += 1

    def stop(self) -> None:
        self.tracing = False
        self.stops += 1

    def get_traced_memory(self):
        return self.current, self.current

    def get_tracemalloc_memory(self):
        return self.overhead


class StubClock:
    """Stands in for the time module. The estimator throttles its probing and stops tracing
    after a while, and neither should be tested by making a test wait."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    # CollectionPressure times on perf_counter, for the resolution; same stub clock behind it
    def perf_counter(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# The real functions, captured once at import. A test that wants one of them back must take
# it from here rather than reading it off the module, which by then holds a stub.
REAL_SYSTEM_MEMORY = conc.system_memory
REAL_PROCESS_MEMORY = conc.process_memory
REAL_LOAD_PER_TASK_ESTIMATES = conc.load_per_task_estimates
REAL_SAVE_PER_TASK_ESTIMATE = conc.save_per_task_estimate


def install_memory_stubs(memory: StubMemory) -> None:
    conc.system_memory = memory.system_memory
    conc.process_memory = memory.process_memory
    # Never read the real user_files estimates: they differ per machine and per run
    conc.load_per_task_estimates = lambda: {}
    # ...and never write them. The gate persists a measurement as soon as it has one rather
    # than only from finish(), so without this every gate test that measures anything leaves
    # a "test op" entry in the add-on's own user_files.
    conc.save_per_task_estimate = lambda op_key, value: None


def restore_memory_probes() -> None:
    conc.system_memory = REAL_SYSTEM_MEMORY
    conc.process_memory = REAL_PROCESS_MEMORY
    conc.load_per_task_estimates = REAL_LOAD_PER_TASK_ESTIMATES
    conc.save_per_task_estimate = REAL_SAVE_PER_TASK_ESTIMATE


class MemoryStubs:
    """Replaces the memory probes, the tracing module, the clock and the estimates file.

    A mixin because the gate's tests are async and so need a different TestCase underneath,
    while wanting exactly the same machine stubbed out from under them.
    """

    def install_stubs(self) -> None:
        self.memory = StubMemory()
        install_memory_stubs(self.memory)
        self.tracing = StubTracemalloc()
        self.clock = StubClock()
        self._real_tracemalloc = conc.tracemalloc
        self._real_time = conc.time
        conc.tracemalloc = self.tracing
        conc.time = self.clock
        # Its window opened on the real clock at import; reopen it on the stub one
        conc.collection_pressure.reset()

    def remove_stubs(self) -> None:
        conc.tracemalloc = self._real_tracemalloc
        conc.time = self._real_time
        restore_memory_probes()


class MemoryStubTestCase(MemoryStubs, unittest.TestCase):
    def setUp(self):
        self.install_stubs()

    def tearDown(self):
        self.remove_stubs()


# --- Reading the machine -------------------------------------------------------------------


class FormatBytesTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(conc.format_bytes(512 * MB), "512 MB")
        self.assertEqual(conc.format_bytes(2 * GB), "2.0 GB")
        self.assertEqual(conc.format_bytes(None), "?")

    def test_a_small_per_task_cost_is_not_rounded_away(self):
        """The progress dialog showed an honestly cheap task as "0 MB/task", which reads as a
        broken measurement rather than a small one. Per-task costs live below a megabyte: the
        estimate is per *live* task and three in four are parked on the gate."""
        self.assertEqual(conc.format_bytes(conc.MIN_PER_TASK_MEMORY), "512 KB")
        self.assertEqual(conc.format_bytes(400 * 1024), "400 KB")
        self.assertEqual(conc.format_bytes(MB), "1 MB")


class MemoryProbeTests(unittest.TestCase):
    def test_the_reserve_is_a_share_of_total_memory_with_a_floor(self):
        # A tenth of a big machine
        self.assertEqual(conc.memory_reserve(32 * GB), int(32 * GB * conc.RESERVE_TOTAL_FRACTION))
        # ...but never less than the floor, or a small machine would be left with nothing
        self.assertEqual(conc.memory_reserve(1 * GB), conc.MIN_RESERVE_BYTES)

    def test_the_probes_report_this_machine(self):
        # Whatever platform the suite runs on, the probes must work: everything below is
        # stubbed, so this is the one check that the real ones are wired up
        memory = conc.system_memory()
        self.assertIsNotNone(memory, "system memory probe failed on this platform")
        total, available = memory
        self.assertGreater(total, 0)
        self.assertGreater(available, 0)
        self.assertLessEqual(available, total)
        rss = conc.process_memory()
        self.assertIsNotNone(rss, "process memory probe failed on this platform")
        self.assertGreater(rss, 0)

    def test_the_probes_report_bytes(self):
        # psutil reports bytes on every platform, so this is here to catch the probes being
        # rewritten against something that does not - and magnitude alone catches it: a
        # machine with under a gigabyte of RAM will not be running Anki, and a Python process
        # holding under 5MB does not exist
        total, _ = conc.system_memory()
        self.assertGreater(total, 1 * GB, "system memory looks like kilobytes, not bytes")
        rss = conc.process_memory()
        self.assertGreater(rss, 5 * MB, "process memory looks like kilobytes, not bytes")
        self.assertLess(rss, total, "process memory is larger than the machine")


class MemoryBudgetTests(MemoryStubTestCase):
    def test_the_reserve_comes_off_before_anything_is_budgeted(self):
        # Budgeting from raw available memory would plan a limit that immediately triggers the
        # pressure response it is supposed to stay clear of
        self.memory.total = 32 * GB
        self.memory.available = 16 * GB
        reserve = conc.memory_reserve(32 * GB)
        spendable = (16 * GB - reserve) * conc.MEMORY_TARGET_FRACTION
        self.assertEqual(conc.memory_budget(), min(spendable, 32 * GB * conc.MEMORY_TOTAL_FRACTION))

    def test_the_budget_is_capped_as_a_share_of_total_memory(self):
        # Free memory right after a reboot is not an invitation to use all of it
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        self.assertEqual(conc.memory_budget(), 8 * GB * conc.MEMORY_TOTAL_FRACTION)

    def test_no_budget_when_less_is_free_than_the_reserve(self):
        self.memory.total = 32 * GB
        self.memory.available = 1 * GB
        self.assertEqual(conc.memory_budget(), 0)

    def test_no_budget_when_the_probe_is_unavailable(self):
        self.memory.probe_failed = True
        self.assertIsNone(conc.memory_budget())


class MemoryLimitConfigTests(MemoryStubTestCase):
    def test_a_numeric_memory_limit_is_interpreted_as_megabytes(self):
        self.assertEqual(
            conc.configured_memory_limit_bytes({"memory_limit": 900}, 32 * GB), 900 * MB
        )

    def test_a_percent_memory_limit_is_interpreted_as_a_share_of_total_ram(self):
        self.assertEqual(
            conc.configured_memory_limit_bytes({"memory_limit": "12.5%"}, 32 * GB), 4 * GB
        )

    def test_percent_limits_need_total_memory_to_be_known(self):
        self.assertEqual(conc.configured_memory_limit_bytes({"memory_limit": "25%"}, 0), 0)

    def test_invalid_limits_are_treated_as_disabled(self):
        self.assertEqual(conc.configured_memory_limit_bytes({"memory_limit": "oops"}, 32 * GB), 0)
        self.assertEqual(conc.configured_memory_limit_bytes({"memory_limit": "0%"}, 32 * GB), 0)
        self.assertEqual(conc.configured_memory_limit_bytes({"memory_limit": -1}, 32 * GB), 0)

    def test_invalid_limits_are_logged(self):
        with mock.patch.object(conc.logger, "warning") as warning:
            self.assertEqual(conc.configured_memory_limit_bytes({"memory_limit": "oops"}, 32 * GB), 0)
        self.assertTrue(warning.called)


class MaxConcurrencyTests(MemoryStubTestCase):
    def test_the_ceiling_is_the_budget_divided_by_what_a_slot_costs(self):
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB  # budget = 2GB
        self.assertEqual(conc.max_concurrency_for(16 * MB), 2 * GB // (16 * MB * 4))

    def test_a_slot_is_budgeted_for_the_queue_behind_it_too(self):
        # A limit of N keeps N * TASK_QUEUE_DEPTH tasks alive, all holding their note and
        # prompt. Budgeting a slot at one task's cost plans a window the budget can't hold.
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        self.assertEqual(conc.memory_per_slot(16 * MB), 16 * MB * conc.TASK_QUEUE_DEPTH)
        self.assertEqual(
            conc.max_concurrency_for(16 * MB) * conc.TASK_QUEUE_DEPTH * 16 * MB,
            conc.memory_budget(),
        )

    def test_a_cheap_task_does_not_lift_the_ceiling_without_limit(self):
        self.assertEqual(conc.max_concurrency_for(1), conc.MAX_AUTO_CONCURRENCY)

    def test_a_configured_maximum_takes_the_place_of_the_backstop(self):
        # The 256 is only there so an unconfigured run stays sane; someone who names a number
        # has decided for themselves, above it as well as below
        self.memory.total = 32 * GB
        self.memory.available = 32 * GB
        self.assertEqual(conc.max_concurrency_for(1, 1000), 1000)
        self.assertEqual(conc.max_concurrency_for(1, 12), 12)

    def test_memory_still_caps_a_configured_maximum(self):
        # It raises what a run may grow to, not what it gets
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB  # budget = 2GB
        self.assertEqual(conc.max_concurrency_for(16 * MB, 1000), 2 * GB // (16 * MB * 4))

    def test_an_expensive_task_still_leaves_a_workable_floor(self):
        self.assertEqual(conc.max_concurrency_for(100 * GB), conc.MIN_AUTO_CONCURRENCY)

    def test_a_conservative_ceiling_when_memory_cannot_be_probed(self):
        self.memory.probe_failed = True
        self.assertEqual(conc.max_concurrency_for(8 * MB), conc.NO_PROBE_CONCURRENCY)

    def test_the_thread_pool_is_sized_for_the_highest_reachable_limit(self):
        # The pool is created before the gate, so it has to cover a ceiling that may be raised
        # later once the op has been measured
        self.assertEqual(conc.max_possible_concurrency({}), conc.MAX_AUTO_CONCURRENCY)
        self.assertEqual(conc.max_possible_concurrency({"max_concurrent_requests": 400}), 400)
        self.assertEqual(conc.max_possible_concurrency(None), conc.MAX_AUTO_CONCURRENCY)


class CpuBoundGateTests(unittest.TestCase):
    """The bound on work that computes rather than waits.

    Separate from the gate above and sized differently on purpose: the gate rations memory and
    lets hundreds of tasks wait at once, which is right for sockets and wrong for cores. What
    is checked here is that the limit follows the machine, that it actually holds under
    threads, and that a slot comes back when a section raises - a leaked slot would shrink the
    limit by one for the life of the process, silently.
    """

    def tearDown(self):
        conc._set_cpu_bound_limit(None)

    def test_the_limit_follows_the_core_count_not_the_gate_ceiling(self):
        with mock.patch.object(conc.os, "cpu_count", return_value=4):
            self.assertEqual(conc.cpu_bound_concurrency(), 4 + conc.CPU_BOUND_HEADROOM)
        with mock.patch.object(conc.os, "cpu_count", return_value=32):
            self.assertEqual(conc.cpu_bound_concurrency(), 32 + conc.CPU_BOUND_HEADROOM)
        # Far below MAX_AUTO_CONCURRENCY, which is what the pool running these sections is
        # sized against: that is the whole point of the separate limit
        with mock.patch.object(conc.os, "cpu_count", return_value=4):
            self.assertLess(conc.cpu_bound_concurrency(), conc.MAX_AUTO_CONCURRENCY)

    def test_an_unknown_core_count_still_allows_work_through(self):
        # os.cpu_count returns None when the platform will not say. A limit of 0 would park
        # every lookup forever
        with mock.patch.object(conc.os, "cpu_count", return_value=None):
            self.assertGreaterEqual(conc.cpu_bound_concurrency(), 1)

    def test_no_more_sections_run_at_once_than_the_limit_allows(self):
        conc._set_cpu_bound_limit(3)
        # Party of 4: the three holders plus this thread, so the assertions below start only
        # once all three slots are provably taken
        everyone_in = threading.Barrier(4, timeout=5)
        release = threading.Event()

        def hold():
            with conc.cpu_bound_section():
                everyone_in.wait()
                release.wait(5)

        # daemon: a regression that leaks a slot leaves these blocked for good, and a
        # non-daemon thread in that state hangs the whole test run instead of failing it
        holders = [threading.Thread(target=hold, daemon=True) for _ in range(3)]
        for thread in holders:
            thread.start()
        try:
            everyone_in.wait()

            got_in = threading.Event()

            def fourth():
                with conc.cpu_bound_section():
                    got_in.set()

            extra = threading.Thread(target=fourth, daemon=True)
            extra.start()
            # The fourth is over the limit and must wait, however long the three hold
            self.assertFalse(got_in.wait(0.2))
            release.set()
            # ...and must get through the moment one of them lets go
            self.assertTrue(got_in.wait(5))
            extra.join(5)
        finally:
            release.set()
            for thread in holders:
                thread.join(5)

    def test_a_section_that_raises_gives_its_slot_back(self):
        conc._set_cpu_bound_limit(1)
        with self.assertRaises(ValueError):
            with conc.cpu_bound_section():
                raise ValueError("lookup blew up")

        entered = threading.Event()

        def after():
            with conc.cpu_bound_section():
                entered.set()

        thread = threading.Thread(target=after, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(5))
        thread.join(5)


class ConcurrencyLimitsTests(MemoryStubTestCase):
    def test_a_run_starts_below_its_ceiling_and_adapts_upward(self):
        start, ceiling, adaptive = conc.concurrency_limits({}, 8 * MB)
        self.assertTrue(adaptive)
        self.assertEqual(start, conc.ADAPTIVE_START_CONCURRENCY)
        self.assertGreater(ceiling, start)

    def test_a_low_ceiling_also_lowers_the_starting_limit(self):
        self.memory.total = 2 * GB
        self.memory.available = 2 * GB
        start, ceiling, adaptive = conc.concurrency_limits({}, 64 * MB)
        self.assertTrue(adaptive)
        self.assertEqual(start, ceiling)

    def test_a_configured_maximum_lowers_the_ceiling_but_keeps_adapting(self):
        # The memory-pressure response underneath is what protects the machine, so a
        # configured limit must not switch it off
        start, ceiling, adaptive = conc.concurrency_limits({"max_concurrent_requests": 6}, 8 * MB)
        self.assertTrue(adaptive)
        self.assertEqual(ceiling, 6)
        self.assertEqual(start, 6)

    def test_a_configured_maximum_above_the_backstop_is_honoured(self):
        self.memory.total = 32 * GB
        self.memory.available = 32 * GB
        _, ceiling, adaptive = conc.concurrency_limits({"max_concurrent_requests": 1000}, 1)
        self.assertTrue(adaptive)
        self.assertEqual(ceiling, 1000)

    def test_without_a_probe_the_limit_is_static(self):
        self.memory.probe_failed = True
        self.assertEqual(
            conc.concurrency_limits({}, 8 * MB),
            (conc.NO_PROBE_CONCURRENCY, conc.NO_PROBE_CONCURRENCY, False),
        )
        self.assertEqual(
            conc.concurrency_limits({"max_concurrent_requests": 3}, 8 * MB), (3, 3, False)
        )


# --- Learning what an op costs ---------------------------------------------------------------


class MemoryEstimatorTests(MemoryStubTestCase):
    """What one live task costs, fitted against how many of them are alive.

    The measurement this replaced differenced RSS across a window that had to begin and end
    with nothing in flight, which is why every run serialised its first pass. These cases are
    mostly about the two things that bought: that memory the run accumulates is separated out
    rather than charged to the tasks, and that a task count coming back down is information
    rather than a spoiled window.
    """

    def feed(self, estimator, points, rss=None):
        """Sample at each (live tasks, traced bytes) point, a sampling interval apart."""
        for index, (live, traced) in enumerate(points):
            self.tracing.current = traced
            if rss is not None:
                self.memory.rss = rss(index, live)
            self.clock.advance(conc.SAMPLE_INTERVAL_SECONDS)
            estimator.note_live_tasks(live)

    def line(self, per_task, base=100 * MB, counts=None):
        """A run whose memory is `base` plus `per_task` for every live task."""
        counts = range(0, 40, 2) if counts is None else counts
        return [(live, base + per_task * live) for live in counts]

    def measure(self, points, **kwargs):
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.feed(estimator, points, **kwargs)
        return estimator, estimator.refit()

    # --- the fit --------------------------------------------------------------------------

    def test_the_slope_is_what_one_live_task_costs(self):
        _, per_task = self.measure(self.line(4 * MB))
        self.assertAlmostEqual(per_task, 4 * MB, delta=1024)

    def test_memory_the_run_accumulates_is_not_charged_to_the_tasks(self):
        """The reason there is no barrier any more.

        A run collects results as it goes, and that memory is never given back. Differencing
        against a baseline could only tell it apart from per-task cost by taking the baseline
        at a moment when nothing was in flight. Here it is the intercept: the task count goes
        up and down while the accumulation only goes up, so the fit separates them.
        """
        counts = [4, 20, 36, 8, 28, 12, 32, 16, 24, 40, 6, 30]
        points = [
            (live, 100 * MB + 4 * MB * live + 3 * MB * index)
            for index, live in enumerate(counts)
        ]
        _, per_task = self.measure(points)
        self.assertAlmostEqual(per_task, 4 * MB, delta=MB // 2)

    def test_a_growing_run_is_not_charged_the_memory_it_accumulates_while_growing(self):
        """The case a fit on task count alone gets wrong, and the reason time is a term too.

        The adapt loop raises the limit as a run settles in, so the live count climbs with the
        clock - and the memory the run is keeping climbs with it. Against task count alone the
        two cannot be told apart and the accumulation lands in the per-task figure: on a
        workload built to have a known answer, that read 1.5x high.
        """
        counts, live = [], 8
        for step in range(60):
            live = min(80, int(live * 1.06) + 1)
            # tasks finishing and being replaced, on top of the trend
            counts.append(live + (4 if step % 2 else -4))
        points = [
            (live, 100 * MB + 4 * MB * live + 2 * MB * index)
            for index, live in enumerate(counts)
        ]
        _, per_task = self.measure(points)
        self.assertAlmostEqual(per_task, 4 * MB, delta=MB // 2)

    def test_a_task_count_that_moves_only_with_the_clock_falls_back_to_one_predictor(self):
        """Nothing in these samples can say whether memory followed the tasks or the time.

        A single monotone ramp and no jitter, which a real run does not produce - tasks finish
        at their own pace - but an answer is still better than a division by a determinant of
        nearly zero.
        """
        points = [(live, 100 * MB + 4 * MB * live) for live in range(0, 40, 2)]
        _, per_task = self.measure(points)
        self.assertAlmostEqual(per_task, 4 * MB, delta=1024)

    def test_a_task_count_that_comes_back_down_is_a_measurement_not_a_spoiled_window(self):
        """Under RSS this read as zero growth and taught nothing; traced memory really falls."""
        points = self.line(4 * MB, counts=[0, 8, 16, 24, 32, 40, 32, 24, 16, 8, 0, 8])
        _, per_task = self.measure(points)
        self.assertAlmostEqual(per_task, 4 * MB, delta=1024)

    def test_a_run_whose_memory_does_not_move_teaches_nothing(self):
        _, per_task = self.measure(self.line(0))
        self.assertIsNone(per_task)

    def test_too_few_samples_teach_nothing(self):
        _, per_task = self.measure(self.line(4 * MB, counts=range(0, 12, 2)))
        self.assertIsNone(per_task)

    def test_samples_without_spread_teach_nothing(self):
        """Every reading taken at the same task count is noise divided by nearly nothing."""
        points = [(8, 100 * MB + index * MB) for index in range(20)]
        _, per_task = self.measure(points)
        self.assertIsNone(per_task)

    def test_an_implausibly_large_measurement_is_clamped(self):
        _, per_task = self.measure(self.line(512 * MB))
        self.assertEqual(per_task, conc.MAX_PER_TASK_MEMORY)

    def test_an_implausibly_small_measurement_is_declined_rather_than_clamped(self):
        """A fit under the floor is the absence of a measurement, not a small one.

        Clamping it up produced a number that looked measured and was not, and because the
        ceiling is the budget divided by it, acting on it *raised* the ceiling. So it holds
        whatever estimate it already had - here the starting guess.
        """
        estimator, per_task = self.measure(self.line(1024))
        self.assertIsNone(per_task)
        self.assertIsNone(estimator.measured)
        self.assertEqual(estimator.estimate, conc.DEFAULT_PER_TASK_MEMORY)

    def test_a_clamped_fit_does_not_replace_what_the_last_run_measured(self):
        """The tablet's failure, in one test.

        A stored 7 MB/task, a fit of 11 KB because tracemalloc cannot see sqlite3, and the
        clamp turning that into 512 KB - which divided the budget into a ceiling six times
        higher, admitted six times the tasks, and left 0.1 GB free on a 15.8 GB machine.
        """
        estimator = conc.MemoryEstimator("op", stored=7 * MB)
        estimator.start()
        self.feed(estimator, self.line(11 * 1024))
        self.assertIsNone(estimator.refit())
        self.assertEqual(estimator.estimate, 7 * MB)

    def test_a_fit_above_the_floor_still_replaces_a_stored_estimate(self):
        """Only the clamp is declined. A real reading is what the stored value is for."""
        estimator = conc.MemoryEstimator("op", stored=7 * MB)
        estimator.start()
        self.feed(estimator, self.line(3 * MB))
        self.assertAlmostEqual(estimator.refit(), 3 * MB, delta=1024)
        self.assertAlmostEqual(estimator.estimate, 3 * MB, delta=1024)

    def test_within_a_run_the_largest_fit_wins(self):
        # What has to fit in RAM is the peak, not the average
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.feed(estimator, self.line(4 * MB))
        self.assertAlmostEqual(estimator.refit(), 4 * MB, delta=1024)

        self.feed(estimator, self.line(1 * MB, base=200 * MB))
        self.assertIsNone(estimator.refit(), "a cheaper stretch must not lower the estimate")
        self.assertAlmostEqual(estimator.measured, 4 * MB, delta=1024)

    # --- the two series -------------------------------------------------------------------

    def test_rss_measures_it_when_nothing_is_tracing(self):
        """Old Anki, another addon's tracing turned off - the fit still has a series to use."""
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.tracing.tracing = False
        self.feed(
            estimator,
            self.line(0),
            rss=lambda index, live: 500 * MB + 4 * MB * live,
        )
        self.assertAlmostEqual(estimator.refit(), 4 * MB, delta=1024)

    def test_traced_allocations_win_over_rss(self):
        """RSS ratchets, so on a warm run it reads high; the traced figure is the honest one."""
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.feed(
            estimator,
            self.line(4 * MB),
            rss=lambda index, live: 500 * MB + 20 * MB * live,
        )
        self.assertAlmostEqual(estimator.refit(), 4 * MB, delta=1024)

    def test_a_traced_total_that_does_not_rise_does_not_fall_back_to_rss(self):
        """RSS would answer with the run's accumulation, which is the thing being excluded."""
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.feed(
            estimator,
            self.line(0),
            rss=lambda index, live: 500 * MB + 20 * MB * live,
        )
        self.assertIsNone(estimator.refit())

    # --- tracing lifecycle ----------------------------------------------------------------

    def test_tracing_is_started_and_stopped_again(self):
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.assertTrue(self.tracing.is_tracing())
        estimator.stop()
        self.assertFalse(self.tracing.is_tracing())
        self.assertEqual((self.tracing.starts, self.tracing.stops), (1, 1))

    def test_somebody_elses_tracing_is_read_but_not_taken_over(self):
        """Another addon, or a developer with a profiler open. Stopping it is not ours to do."""
        self.tracing.tracing = True
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        estimator.stop()
        self.assertTrue(self.tracing.is_tracing())
        self.assertEqual((self.tracing.starts, self.tracing.stops), (0, 0))

    def test_tracing_stops_once_there_has_been_long_enough_to_fit(self):
        """Tracing charges every allocation in the process; a run should not pay it throughout."""
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.feed(estimator, self.line(4 * MB))
        estimator.refit()
        self.assertTrue(self.tracing.is_tracing())

        self.clock.advance(conc.MEASURE_SECONDS)
        estimator.refit()
        self.assertFalse(self.tracing.is_tracing())

    def test_sampling_stops_when_measuring_does(self):
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.feed(estimator, self.line(4 * MB))
        estimator.stop()
        before = len(estimator._traced)
        self.feed(estimator, self.line(4 * MB))
        self.assertEqual(len(estimator._traced), before)

    def test_probing_is_throttled(self):
        """The driver reports on every completion, which on a fast op is thousands of times."""
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        for live in range(50):
            self.tracing.current = 100 * MB + 4 * MB * live
            estimator.note_live_tasks(live)  # no clock advance: all within one interval
        self.assertEqual(len(estimator._traced), 1)

    # --- what it starts from --------------------------------------------------------------

    def test_a_stored_estimate_is_used_until_something_is_measured(self):
        estimator = conc.MemoryEstimator("op", stored=3 * MB)
        self.assertEqual(estimator.estimate, 3 * MB)
        self.assertTrue(estimator.from_measurement)

    def test_the_default_guess_is_used_when_nothing_was_stored(self):
        estimator = conc.MemoryEstimator("op")
        self.assertEqual(estimator.estimate, conc.DEFAULT_PER_TASK_MEMORY)
        self.assertFalse(estimator.from_measurement)

    def test_refitting_nothing_teaches_nothing(self):
        self.assertIsNone(conc.MemoryEstimator("op").refit())

    def test_a_probe_that_fails_for_a_tick_only_costs_that_series_that_sample(self):
        """psutil can stop answering part way through - a WMI hiccup, a missing /proc."""
        estimator = conc.MemoryEstimator("op")
        estimator.start()
        self.feed(estimator, self.line(4 * MB, counts=range(0, 20, 2)))
        self.memory.probe_failed = True
        self.feed(estimator, self.line(4 * MB, counts=range(20, 40, 2)))
        self.memory.probe_failed = False
        self.assertAlmostEqual(estimator.refit(), 4 * MB, delta=1024)
        self.assertLess(len(estimator._rss), len(estimator._traced))



class EstimatesFileTests(MemoryStubTestCase):
    def setUp(self):
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tempdir.name) / "memory_estimates.json"
        self._real_path = conc._estimates_path
        conc._estimates_path = lambda: self.path
        # Undo MemoryStubTestCase's stubs - these tests exercise the real reader and writer,
        # pointed at a temporary file rather than the add-on's own user_files
        conc.load_per_task_estimates = REAL_LOAD_PER_TASK_ESTIMATES
        conc.save_per_task_estimate = REAL_SAVE_PER_TASK_ESTIMATE

    def tearDown(self):
        conc._estimates_path = self._real_path
        self._tempdir.cleanup()
        super().tearDown()

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(conc.load_per_task_estimates(), {})

    def test_a_corrupt_file_is_not_an_error(self):
        # Rather than breaking every run until someone deletes it by hand
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(conc.load_per_task_estimates(), {})

    def test_a_file_holding_something_other_than_a_mapping_is_ignored(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(conc.load_per_task_estimates(), {})

    def test_the_first_measurement_is_stored_as_is(self):
        conc.save_per_task_estimate("Making meanings", 2 * MB)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"version": conc.ESTIMATES_VERSION, "estimates": {"Making meanings": 2 * MB}},
        )

    def test_a_version_1_file_is_converted_rather_than_thrown_away(self):
        # Its numbers were a slot's worth of tasks, measured before the queue behind the gate
        # was counted. Read as they stand they would be TASK_QUEUE_DEPTH times too high.
        self.path.write_text(json.dumps({"Making meanings": 8 * MB}), encoding="utf-8")
        self.assertEqual(
            conc.load_per_task_estimates()["Making meanings"], 8 * MB / conc.TASK_QUEUE_DEPTH
        )

    def test_a_file_from_a_later_version_is_ignored(self):
        # Measuring again beats guessing at what its numbers mean
        self.path.write_text(
            json.dumps({"version": conc.ESTIMATES_VERSION + 1, "estimates": {"op": 1 * MB}}),
            encoding="utf-8",
        )
        self.assertEqual(conc.load_per_task_estimates(), {})

    def test_a_file_from_a_later_version_is_left_alone_rather_than_overwritten(self):
        # Ignoring what it holds is right; destroying it is not. Running an older version for
        # one session would otherwise throw away everything the newer one had measured.
        contents = {"version": conc.ESTIMATES_VERSION + 1, "estimates": {"op": 1 * MB}}
        self.path.write_text(json.dumps(contents), encoding="utf-8")
        conc.save_per_task_estimate("Making meanings", 2 * MB)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), contents)

    def test_a_corrupt_file_is_left_alone_rather_than_overwritten(self):
        # It is unreadable here, but it may still hold what every other op measured; replacing
        # it with only this op's value would throw those away
        self.path.write_text("{not json", encoding="utf-8")
        conc.save_per_task_estimate("Making meanings", 2 * MB)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_a_file_holding_something_other_than_a_mapping_is_left_alone(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        conc.save_per_task_estimate("Making meanings", 2 * MB)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_a_failed_write_leaves_the_previous_file_intact(self):
        # The file is written beside itself and moved into place, so a crash partway through
        # cannot leave half a file behind - which is how it came to be unreadable above
        conc.save_per_task_estimate("Making meanings", 1 * MB)
        before = self.path.read_text(encoding="utf-8")
        with mock.patch.object(conc.json, "dump", side_effect=OSError("disk full")):
            conc.save_per_task_estimate("Translating sentences", 4 * MB)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse(self.path.with_name(f"{self.path.name}.tmp").exists())

    def test_later_measurements_are_blended_so_one_odd_run_cannot_skew_it(self):
        conc.save_per_task_estimate("Making meanings", 1 * MB)
        conc.save_per_task_estimate("Making meanings", 2 * MB)
        expected = int(1 * MB * (1 - conc.ESTIMATE_BLEND) + 2 * MB * conc.ESTIMATE_BLEND)
        self.assertEqual(conc.load_per_task_estimates()["Making meanings"], expected)

    def test_estimates_are_kept_per_op(self):
        conc.save_per_task_estimate("Making meanings", 1 * MB)
        conc.save_per_task_estimate("Translating sentences", 4 * MB)
        stored = conc.load_per_task_estimates()
        self.assertEqual(stored["Making meanings"], 1 * MB)
        self.assertEqual(stored["Translating sentences"], 4 * MB)

    def test_nothing_is_persisted_when_nothing_was_measured(self):
        conc.MemoryEstimator("Making meanings").persist()
        self.assertFalse(self.path.exists())

    def test_an_unwritable_location_is_not_an_error(self):
        # Losing a measurement is not worth failing a run over
        conc._estimates_path = lambda: Path(self._tempdir.name) / "nope" / "\0bad" / "x.json"
        conc.save_per_task_estimate("Making meanings", 1 * MB)


# --- Paying for the measurement --------------------------------------------------------------


class TracingOverheadTests(MemoryStubTestCase):
    """The trace table is in RSS, and has to come off before either reader uses one."""

    def test_nothing_to_subtract_when_nothing_is_tracing(self):
        self.tracing.tracing = False
        self.tracing.overhead = 40 * MB
        # Real tracemalloc answers with a small fixed figure even when stopped; the question
        # asked here is whether tracing is on, not what the module weighs.
        self.assertEqual(conc.tracing_overhead(), 0)

    def test_the_table_is_reported_while_tracing(self):
        self.tracing.tracing = True
        self.tracing.overhead = 40 * MB
        self.assertEqual(conc.tracing_overhead(), 40 * MB)

    def test_a_probe_that_raises_is_not_an_error(self):
        def boom():
            raise RuntimeError("no")

        self.tracing.tracing = True
        self.tracing.get_tracemalloc_memory = boom
        self.assertEqual(conc.tracing_overhead(), 0)


class CeilingThresholdTests(MemoryStubTestCase):
    """Where the per-task cost has to get before the fit stops being multiplied by zero."""

    def test_the_threshold_is_the_budget_spread_over_a_full_backstop(self):
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB  # budget = 2GB
        self.assertEqual(
            conc.ceiling_threshold(),
            2 * GB / (conc.MAX_AUTO_CONCURRENCY * conc.TASK_QUEUE_DEPTH),
        )

    def test_a_configured_max_takes_the_backstops_place(self):
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        self.assertEqual(conc.ceiling_threshold(16), 2 * GB / (16 * conc.TASK_QUEUE_DEPTH))

    def test_no_budget_means_no_threshold(self):
        self.memory.probe_failed = True
        self.assertIsNone(conc.ceiling_threshold())

    def test_a_tight_machine_puts_the_threshold_within_reach(self):
        # The asymmetry that matters: less free memory, lower threshold, so the machine that
        # needs the measurement is the one that gets it
        self.memory.total = 4 * GB
        self.memory.available = 4 * GB
        roomy = conc.ceiling_threshold()

        self.memory.available = 700 * MB
        tight = conc.ceiling_threshold()

        self.assertLess(tight, roomy)


class TracingWouldPayTests(MemoryStubTestCase):
    """Tracing is bought, not assumed - it is the one part of this module that is not free."""

    def setUp(self):
        super().setUp()
        # An ordinary desktop: 8GB free of 16GB, so a budget of 3.2GB and a ceiling that only
        # moves once a task costs more than 3.2MB
        self.memory.total = 16 * GB
        self.memory.available = 8 * GB

    # 4MB clears the margin on this machine, so these two isolate the length gate: whatever
    # they answer is about the number of tasks and not about the ceiling arithmetic.
    WORTH_MEASURING = 4 * MB

    def test_a_run_too_short_to_fit_is_not_traced(self):
        worth_it, why = conc.tracing_would_pay(self.WORTH_MEASURING, conc.MIN_SLOPE_SPREAD)
        self.assertFalse(worth_it)
        self.assertIn("too few", why)

    def test_a_run_of_one_more_task_than_the_spread_is_long_enough(self):
        # The spread is a floor on the count the fit needs to see, not on the run
        worth_it, _ = conc.tracing_would_pay(
            self.WORTH_MEASURING, conc.MIN_SLOPE_SPREAD + 1
        )
        self.assertTrue(worth_it)

    def test_a_static_limit_is_not_traced(self):
        self.memory.probe_failed = True
        worth_it, why = conc.tracing_would_pay(conc.MIN_PER_TASK_MEMORY, 500)
        self.assertFalse(worth_it)
        self.assertIn("static", why)

    def test_a_threshold_past_the_clamp_can_never_be_reached(self):
        # A machine so empty that no fitted value could bring the ceiling down, because
        # fitted values are clamped well below where it would have to get
        self.memory.total = 4096 * GB
        self.memory.available = 4096 * GB
        self.assertGreater(conc.ceiling_threshold(), conc.MAX_PER_TASK_MEMORY)
        worth_it, why = conc.tracing_would_pay(conc.MIN_PER_TASK_MEMORY, 5000)
        self.assertFalse(worth_it)
        self.assertIn("clamp", why)

    def test_an_op_measured_at_the_floor_is_not_measured_again(self):
        # The threshold is 3.2MB and the op is known to cost the 512KB floor: it would have to
        # have grown sixfold for the ceiling to move, so there is nothing here to learn
        worth_it, why = conc.tracing_would_pay(conc.MIN_PER_TASK_MEMORY, 5000)
        self.assertFalse(worth_it)
        self.assertIn("believed to cost", why)

    def test_an_op_believed_expensive_enough_is_measured(self):
        worth_it, _ = conc.tracing_would_pay(4 * MB, 5000)
        self.assertTrue(worth_it)

    def test_the_margin_is_inclusive_at_its_edge(self):
        """A threshold exactly at the margin is measured, not skipped.

        Which way the boundary falls is not arbitrary: an 8GB machine with all of it free
        lands precisely here, and measuring one run it did not need costs a run, where
        skipping one it did need costs the ceiling for as long as the estimate stands.
        """
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB  # budget 2GB -> threshold exactly 2MB
        self.assertEqual(
            conc.ceiling_threshold(), conc.MIN_PER_TASK_MEMORY * conc.MEASUREMENT_MARGIN
        )

        worth_it, _ = conc.tracing_would_pay(conc.MIN_PER_TASK_MEMORY, 5000)

        self.assertTrue(worth_it)

    def test_an_unmeasured_op_is_measured_once(self):
        # A new op is believed to cost DEFAULT_PER_TASK_MEMORY, which clears the margin here -
        # so it is measured, and the value it stores is what makes the next run skip it
        worth_it, _ = conc.tracing_would_pay(conc.DEFAULT_PER_TASK_MEMORY, 5000)
        self.assertTrue(worth_it)

    def test_a_tight_machine_measures_even_a_cheap_op(self):
        self.memory.total = 4 * GB
        self.memory.available = 700 * MB
        worth_it, _ = conc.tracing_would_pay(conc.MIN_PER_TASK_MEMORY, 5000)
        self.assertTrue(worth_it)


# --- The gate ---------------------------------------------------------------------------------


class GateTestCase(MemoryStubs, unittest.IsolatedAsyncioTestCase):
    """Async tests against a gate with stubbed memory."""

    def setUp(self):
        self.install_stubs()

    def tearDown(self):
        self.remove_stubs()

    def make_gate(self, limit=None, max_limit=None, config=None):
        # Every gate records the ceilings it reports, so any test can assert on them
        self.reported_ceilings: list[int] = []
        gate = conc.ConcurrencyGate(
            config or {},
            op_key="test op",
            on_ceiling_changed=self.reported_ceilings.append,
        )
        if limit is not None:
            gate.limit = limit
        if max_limit is not None:
            gate.max_limit = max_limit
        return gate

    def measure_cost(self, gate, per_task, base=100 * MB):
        """Report a run whose traced memory rises by `per_task` for every live task.

        The drivers report the live count on every refill and every batch of completions; this
        is that, shaped into a straight line so the fit has one answer to find.
        """
        gate.estimator.start()
        for live in range(0, 40, 2):
            self.tracing.current = int(base + per_task * live)
            self.clock.advance(conc.SAMPLE_INTERVAL_SECONDS)
            gate.note_live_tasks(live)
        gate._apply_estimate()

    async def settle(self):
        """Let queued tasks reach their next await."""
        for _ in range(3):
            await asyncio.sleep(0)


class GateAcquireReleaseTests(GateTestCase):
    async def test_slots_are_handed_out_up_to_the_limit(self):
        gate = self.make_gate(limit=2)
        await gate.acquire()
        await gate.acquire()
        self.assertEqual(gate.in_flight, 2)

    async def test_a_full_gate_makes_the_next_task_wait(self):
        gate = self.make_gate(limit=1)
        await gate.acquire()
        waiter = asyncio.ensure_future(gate.acquire())
        await self.settle()
        self.assertFalse(waiter.done())

        gate.release()
        await asyncio.wait_for(waiter, timeout=1)
        self.assertEqual(gate.in_flight, 1)

    async def test_releasing_wakes_exactly_one_waiter(self):
        gate = self.make_gate(limit=1)
        await gate.acquire()
        first = asyncio.ensure_future(gate.acquire())
        second = asyncio.ensure_future(gate.acquire())
        await self.settle()

        gate.release()
        await asyncio.wait_for(first, timeout=1)
        await self.settle()
        self.assertFalse(second.done())
        self.assertEqual(gate.in_flight, 1)

        gate.release()
        await asyncio.wait_for(second, timeout=1)

    async def test_a_limit_that_shrinks_holds_waiters_until_enough_have_drained(self):
        """The awkward case: the adapt loop halves the limit while tasks are queued.

        Tasks already running keep their slot, so the gate is over its limit until enough of
        them finish. Every release still wakes a waiter, and that waiter has to put itself back
        in the queue rather than slipping through - without losing the wakeup for the others.
        """
        gate = self.make_gate(limit=4)
        for _ in range(4):
            await gate.acquire()

        gate.limit = 2  # memory pressure
        waiter = asyncio.ensure_future(gate.acquire())
        await self.settle()

        gate.release()  # 3 in flight, still over the limit
        await self.settle()
        self.assertFalse(waiter.done())

        gate.release()  # 2 in flight, at the limit
        await self.settle()
        self.assertFalse(waiter.done())

        gate.release()  # 1 in flight, room at last
        await asyncio.wait_for(waiter, timeout=1)
        self.assertEqual(gate.in_flight, 2)

    async def test_a_raised_limit_lets_waiting_tasks_through(self):
        gate = self.make_gate(limit=1, max_limit=8)
        await gate.acquire()
        waiter = asyncio.ensure_future(gate.acquire())
        await self.settle()

        gate.limit = 2
        gate._wake_waiters(1)
        await asyncio.wait_for(waiter, timeout=1)
        self.assertEqual(gate.in_flight, 2)

    async def test_a_cancelled_waiter_passes_its_wakeup_on(self):
        """A task cancelled at the moment it is handed a slot must not swallow it.

        Losing that wakeup would leave the remaining tasks queued behind a gate with a free
        slot and nothing left to trigger it.
        """
        gate = self.make_gate(limit=1)
        await gate.acquire()
        first = asyncio.ensure_future(gate.acquire())
        second = asyncio.ensure_future(gate.acquire())
        await self.settle()

        # Hand a slot to `first` and cancel it in the same tick, before it can run
        gate.release()
        first.cancel()
        await self.settle()

        self.assertTrue(first.cancelled())
        await asyncio.wait_for(second, timeout=1)

    async def test_abort_releases_everything_queued_at_once(self):
        # Cancelling a run with hundreds of queued tasks must unwind in one step rather than
        # relying on each one being cancelled individually
        gate = self.make_gate(limit=1)
        await gate.acquire()
        waiters = [asyncio.ensure_future(gate.acquire()) for _ in range(5)]
        await self.settle()

        gate.abort()
        await self.settle()
        for waiter in waiters:
            with self.assertRaises(asyncio.CancelledError):
                await waiter

    async def test_nothing_gets_through_a_gate_that_has_been_aborted(self):
        gate = self.make_gate(limit=8)
        gate.abort()
        with self.assertRaises(asyncio.CancelledError):
            await gate.acquire()

    async def test_status_text_reports_the_gate_and_what_it_knows(self):
        gate = self.make_gate(limit=4)
        await gate.acquire()
        self.assertIn("1/4", gate.status_text())


class CollectionPressureTests(MemoryStubTestCase):
    """The reading the gate leans on: how busy the collection's one permit is."""

    def setUp(self):
        super().setUp()
        self.pressure = conc.collection_pressure

    def test_nothing_to_report_when_no_turn_was_taken(self):
        self.clock.advance(2.0)
        self.assertIsNone(self.pressure.sample())

    def test_utilisation_is_the_share_of_the_window_spent_holding(self):
        self.clock.advance(4.0)
        self.pressure.record(1.0)
        self.pressure.record(2.0)
        use, mean_hold = self.pressure.sample()
        self.assertAlmostEqual(use, 0.75)
        self.assertAlmostEqual(mean_hold, 1.5)

    def test_a_turn_spanning_the_window_start_cannot_read_above_one(self):
        # A turn that began before this window lands its whole hold time inside it
        self.clock.advance(1.0)
        self.pressure.record(30.0)
        use, _ = self.pressure.sample()
        self.assertEqual(use, 1.0)

    def test_a_sample_covers_only_the_window_since_the_last_one(self):
        self.clock.advance(2.0)
        self.pressure.record(2.0)
        self.assertAlmostEqual(self.pressure.sample()[0], 1.0)
        # The busy window is spent; an idle one that follows reads idle rather than averaging
        self.clock.advance(2.0)
        self.pressure.record(0.1)
        self.assertAlmostEqual(self.pressure.sample()[0], 0.05)

    def test_resetting_drops_what_was_collected(self):
        self.clock.advance(2.0)
        self.pressure.record(2.0)
        self.pressure.reset()
        self.clock.advance(2.0)
        self.assertIsNone(self.pressure.sample())


class GateAdaptationTests(GateTestCase):
    async def test_a_saturated_gate_grows_while_memory_is_comfortable(self):
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertGreater(gate.limit, 16)

    async def test_an_idle_gate_does_not_grow(self):
        # If tasks are not queueing up, a bigger limit would not be used anyway
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = 2
        await gate._adapt_once()
        self.assertEqual(gate.limit, 16)

    async def test_a_busy_collection_stops_the_gate_growing(self):
        # Every slot taken, but by tasks queueing for a resource that serves one at a time
        self.clock.advance(2.0)
        conc.collection_pressure.record(1.9)
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertEqual(gate.limit, 16)

    async def test_a_collection_with_headroom_leaves_growth_alone(self):
        self.clock.advance(2.0)
        conc.collection_pressure.record(0.2)
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertGreater(gate.limit, 16)

    async def test_an_op_that_never_touches_the_collection_is_not_held_back(self):
        # No turns at all: nothing to report, and nothing that should limit this op
        self.clock.advance(2.0)
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertGreater(gate.limit, 16)

    async def test_a_busy_collection_seen_once_caps_the_ticks_that_read_idle(self):
        """The bug this exists for: vetoing a tick caps nothing when growth is geometric.

        A run whose collection is the constraint throughout still reads below the threshold on
        some ticks - the sample covers two seconds and a few turns. One measured run had
        sixteen such ticks, and 1.25 ** 16 is 35x: the limit went from 16 to its 512 backstop
        in 86 seconds past a collection that was busy 80% of the time.
        """
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit

        self.clock.advance(2.0)
        conc.collection_pressure.record(1.9)
        await gate._adapt_once()
        self.assertEqual(gate.limit, 16)

        # Sixteen ticks that read idle, exactly the shape that reached 512 before
        for _ in range(16):
            self.clock.advance(2.0)
            conc.collection_pressure.record(0.1)
            gate.in_flight = gate.limit
            await gate._adapt_once()
        self.assertEqual(gate.limit, 32)

    async def test_the_cap_follows_the_limit_down_and_back_up_again(self):
        # Recovery is linear, so a collection that has genuinely stopped being the constraint
        # gets the limit back - it just takes ticks proportional to the slots, not four of them
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        self.clock.advance(2.0)
        conc.collection_pressure.record(1.9)
        await gate._adapt_once()
        self.assertEqual(gate.collection_ceiling, 16)

        for _ in range(60):
            self.clock.advance(2.0)
            gate.in_flight = gate.limit
            await gate._adapt_once()
        self.assertEqual(gate.limit, 76)

    async def test_a_collection_that_stays_busy_never_lifts_the_cap(self):
        gate = self.make_gate(limit=16, max_limit=256)
        for _ in range(20):
            self.clock.advance(2.0)
            conc.collection_pressure.record(1.9)
            gate.in_flight = gate.limit
            await gate._adapt_once()
        self.assertEqual(gate.limit, 16)

    async def test_an_op_that_never_touches_the_collection_is_never_capped(self):
        # Nothing has been shown about a collection nobody used, so growth stays geometric
        gate = self.make_gate(limit=16, max_limit=256)
        for _ in range(3):
            self.clock.advance(2.0)
            gate.in_flight = gate.limit
            await gate._adapt_once()
        self.assertIsNone(gate.collection_ceiling)
        self.assertEqual(gate.limit, 31)

    async def test_the_cap_is_what_the_collection_could_serve_not_where_memory_left_us(self):
        # Memory pressure halves the limit regardless; the cap it grows back to is still the
        # one the collection showed, so the run does not have to re-learn it
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        self.clock.advance(2.0)
        conc.collection_pressure.record(1.9)
        await gate._adapt_once()

        self.memory.available = 100 * MB
        gate.in_flight = gate.limit
        self.clock.advance(2.0)
        await gate._adapt_once()
        self.assertEqual(gate.limit, 8)
        self.assertEqual(gate.collection_ceiling, 16)

    async def test_a_busy_collection_does_not_stop_the_gate_shrinking(self):
        # Memory pressure still wins: the machine matters more than the throughput argument
        self.clock.advance(2.0)
        conc.collection_pressure.record(2.0)
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        self.memory.available = 100 * MB
        await gate._adapt_once()
        self.assertEqual(gate.limit, 8)

    async def test_the_collection_share_reaches_the_progress_dialog(self):
        self.clock.advance(2.0)
        conc.collection_pressure.record(1.0)
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertIn("Collection 50%", gate.status_text())

    async def test_the_gate_never_grows_past_its_ceiling(self):
        gate = self.make_gate(limit=8, max_limit=9)
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertEqual(gate.limit, 9)

    async def test_the_limit_halves_when_free_memory_drops_below_the_reserve(self):
        gate = self.make_gate(limit=16, max_limit=256)
        gate.in_flight = gate.limit
        self.memory.available = 100 * MB
        await gate._adapt_once()
        self.assertEqual(gate.limit, 8)

    async def test_the_limit_halves_when_the_process_passes_a_configured_memory_limit(self):
        gate = self.make_gate(config={"memory_limit": 900})
        gate.limit, gate.max_limit = 16, 256
        gate.in_flight = gate.limit
        self.memory.rss = 1000 * MB
        await gate._adapt_once()
        self.assertEqual(gate.limit, 8)

    async def test_the_limit_halves_when_the_process_passes_a_percent_memory_limit(self):
        self.memory.total = 32 * GB
        gate = self.make_gate(config={"memory_limit": "10%"})
        gate.limit, gate.max_limit = 16, 256
        gate.in_flight = gate.limit
        self.memory.rss = int(3.3 * GB)
        await gate._adapt_once()
        self.assertEqual(gate.limit, 8)

    async def test_the_limit_never_falls_below_one(self):
        gate = self.make_gate(limit=1, max_limit=256)
        self.memory.available = 100 * MB
        for _ in range(4):
            await gate._adapt_once()
        self.assertEqual(gate.limit, conc.MIN_CONCURRENCY)


class GateMemoryPressureTests(GateTestCase):
    """Backing off from memory, telling the memory the limit owns from the memory it does not.

    Nine rounds of measurement passed a green suite while this path walked one machine's runs
    to a limit of 1 and held them there for 86% of their wall clock, because every test above
    asserts on a single tick of a machine with 16 GB free. These are the sequences.
    """

    async def run_pressure(self, gate, ticks=1, drained=True):
        """Adapt ticks against a machine that is short of memory throughout.

        `drained` is whether the tasks holding the slots a cut took away have finished. The
        gate cannot evict them, so until they have, the cut it just made has not landed.
        """
        for _ in range(ticks):
            if drained:
                gate.in_flight = min(gate.in_flight, gate.limit)
            self.clock.advance(conc.ADAPT_INTERVAL_SECONDS)
            await gate._adapt_once()

    async def test_the_first_cut_is_still_made(self):
        # The response is not being switched off: a machine short of memory still gets fewer
        # tasks out of this run
        gate = self.make_gate(limit=64, max_limit=256)
        gate.in_flight = gate.limit
        self.memory.available = 100 * MB
        await self.run_pressure(gate)
        self.assertEqual(gate.limit, 32)

    async def test_a_cut_that_has_not_drained_yet_is_not_repeated(self):
        """The first cause of the sawtooth: eight halvings in fourteen seconds.

        Lowering the limit evicts nothing, it stops new acquires. Until the tasks holding the
        extra slots have finished, the machine has not been asked the question, let alone
        answered it.
        """
        gate = self.make_gate(limit=64, max_limit=256)
        gate.in_flight = 64
        self.memory.available = 100 * MB

        await self.run_pressure(gate)
        self.assertEqual(gate.limit, 32)
        # in_flight stays at 64 throughout: nothing has finished
        await self.run_pressure(gate, ticks=8, drained=False)
        self.assertEqual(gate.limit, 32)

    async def test_a_machine_whose_memory_the_run_does_not_own_is_cut_once_and_no_further(self):
        """Round 9 tablet problem, as a sequence.

        `available` sat under the reserve for the rest of the run whatever the limit did:
        nineteen minutes at a limit of 1 recovered 0.9 GB of a 7.0 GB RSS, and the ramp back
        out ran with RSS flat. The controller was reading a quantity its one actuator does not
        move, so it halved 32 times and reached a limit it then held for 86.4% of the run.
        """
        gate = self.make_gate(limit=261, max_limit=261)
        gate.in_flight = gate.limit
        self.memory.available = int(1.6 * GB)  # under the 3.2 GB reserve, and staying there

        await self.run_pressure(gate, ticks=60)

        # One cut, then held: the machine never answered, so it was never asked again
        self.assertEqual(gate.limit, 130)
        self.assertGreater(gate.limit, conc.MIN_AUTO_CONCURRENCY)

    async def test_a_cut_that_does_free_memory_is_repeated(self):
        # The other half of the same test: where lowering the limit is what frees the memory,
        # the loop closes and the response goes on until the machine is comfortable
        gate = self.make_gate(limit=64, max_limit=256)
        gate.in_flight = gate.limit
        self.memory.available = 100 * MB

        await self.run_pressure(gate)
        self.assertEqual(gate.limit, 32)
        for expected in (16, 8, 4):
            self.memory.available += 2 * conc.PRESSURE_RESPONSE_MIN_GAIN
            await self.run_pressure(gate)
            self.assertEqual(gate.limit, expected)

    async def test_the_machine_arm_stops_at_the_least_concurrency_worth_sizing_for(self):
        """MIN_AUTO_CONCURRENCY is the floor max_concurrency_for already refuses to go below.

        The pressure path floored at MIN_CONCURRENCY, so the controller was allowed to drive a
        run four times below the point the same module calls the minimum worth sizing an op
        for. On work that is mostly parked on a socket, a limit of 1 is not a memory saving,
        it is a stall.
        """
        gate = self.make_gate(limit=64, max_limit=256)
        gate.in_flight = gate.limit
        self.memory.available = 100 * MB
        for _ in range(10):
            # Every cut appears to work, so the response is never held back
            self.memory.available += 2 * conc.PRESSURE_RESPONSE_MIN_GAIN
            await self.run_pressure(gate)
        self.assertEqual(gate.limit, conc.MIN_AUTO_CONCURRENCY)

    async def test_the_run_own_memory_against_its_own_cap_still_goes_all_the_way_down(self):
        # The arm the limit does own. Here halving really is the remedy, so it keeps the lower
        # floor and is never held back for want of a response.
        gate = self.make_gate(config={"memory_limit": 900})
        gate.limit, gate.max_limit = 64, 256
        gate.in_flight = gate.limit
        self.memory.rss = 1000 * MB
        for _ in range(10):
            await self.run_pressure(gate)
        self.assertEqual(gate.limit, conc.MIN_CONCURRENCY)

    async def test_recovery_walks_back_to_the_limit_that_tripped_rather_than_sprinting(self):
        """Geometric recovery against geometric collapse is a stable and useless cycle.

        One measured run climbed 1 to 151 in forty seconds and fell back in twelve: forty
        useful seconds per cycle, then minutes of waiting for another process to free memory.
        """
        gate = self.make_gate(limit=64, max_limit=256)
        gate.in_flight = gate.limit
        self.memory.available = 100 * MB
        await self.run_pressure(gate)
        # Latched where the cut left the run, not at the limit that tripped: latching at the
        # latter lets geometric growth put the run back onto it in four ticks, which is the
        # same sawtooth with a smaller amplitude rather than no sawtooth.
        self.assertEqual(gate.pressure_ceiling, 32)

        self.memory.available = 16 * GB
        for _ in range(6):
            gate.in_flight = gate.limit
            self.clock.advance(conc.ADAPT_INTERVAL_SECONDS)
            await gate._adapt_once()
        # Six ticks is six slots, not six geometric steps. The round 9 ramp went 1 -> 151 in
        # forty seconds because nothing stood between the limit and max_limit.
        self.assertEqual(gate.limit, 38)

    async def test_a_fresh_episode_measures_its_own_response(self):
        # A hold is not permanent: memory that comes back and goes again is a new question
        gate = self.make_gate(limit=64, max_limit=256)
        gate.in_flight = gate.limit
        self.memory.available = 100 * MB
        await self.run_pressure(gate, ticks=5)
        self.assertEqual(gate.limit, 32)

        # Idle, so this tick clears the episode without also growing the limit
        self.memory.available = 16 * GB
        gate.in_flight = 0
        self.clock.advance(conc.ADAPT_INTERVAL_SECONDS)
        await gate._adapt_once()
        self.assertIsNone(gate._pressure_probe)

        self.memory.available = 100 * MB
        await self.run_pressure(gate)
        self.assertEqual(gate.limit, 16)

    async def test_the_progress_dialog_says_where_memory_capped_the_run(self):
        gate = self.make_gate(limit=64, max_limit=256)
        gate.in_flight = gate.limit
        self.memory.available = 100 * MB
        await self.run_pressure(gate)
        self.assertIn("Memory cap 32", gate.status_text())


class GateCeilingRefitTests(GateTestCase):
    """The ceiling tracks free memory for the whole run, not just the instant of construction.

    `max_limit` used to move only from `_apply_estimate`, which stops having anything to say
    once measuring stops at 30 seconds. One measured 72-minute run contains exactly one
    ceiling change, at 29 seconds; four runs of the same work on the same tablet were given
    ceilings of 4, 6, 20 and 69 purely from what was free when each one launched.
    """

    async def test_a_machine_that_frees_memory_gets_a_higher_ceiling(self):
        self.memory.total = 16 * GB
        self.memory.available = 3 * GB
        gate = self.make_gate()
        launched_with = gate.max_limit

        self.memory.available = 12 * GB
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertGreater(gate.max_limit, launched_with)

    async def test_a_machine_that_fills_up_gets_a_lower_ceiling_and_the_limit_follows(self):
        self.memory.total = 16 * GB
        self.memory.available = 12 * GB
        gate = self.make_gate(limit=200)

        # Still above the reserve, so this is not the pressure path, just less to spend
        self.memory.available = 4 * GB
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertLess(gate.max_limit, 200)
        self.assertLessEqual(gate.limit, gate.max_limit)

    async def test_a_reading_that_barely_moves_leaves_the_ceiling_alone(self):
        # Free memory jitters by a few megabytes between ticks; without hysteresis the ceiling
        # and the log line would move on nearly every one of them
        self.memory.total = 16 * GB
        self.memory.available = 12 * GB
        gate = self.make_gate()
        before = gate.max_limit

        self.memory.available += 8 * MB
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertEqual(gate.max_limit, before)

    async def test_a_probe_that_stops_answering_does_not_shrink_the_ceiling(self):
        # No reading is not the same as a machine that has filled up. The ceiling budgeted
        # from the last real reading stands.
        gate = self.make_gate(limit=16, max_limit=64)
        self.memory.probe_failed = True
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertEqual(gate.max_limit, 64)

    async def test_a_configured_maximum_still_caps_a_re_read_ceiling(self):
        self.memory.total = 16 * GB
        self.memory.available = 2 * GB
        gate = self.make_gate(config={"max_concurrent_requests": 10})
        self.memory.available = 15 * GB
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertEqual(gate.max_limit, 10)

    async def test_a_measurement_is_kept_as_soon_as_it_is_made(self):
        """A run that is killed used to throw its measurement away.

        `persist()` ran only from `finish()`, so the machine whose runs get killed - the slow
        one, the one that most needs to know what the op costs - was the one that never
        learned it.
        """
        saved: list = []
        conc.save_per_task_estimate = lambda op_key, value: saved.append((op_key, value))
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        gate = self.make_gate()

        self.measure_cost(gate, 4 * MB)

        self.assertEqual(saved, [("test op", 4 * MB)])

    async def test_the_gate_recovers_once_the_pressure_passes(self):
        """Backing off has to be temporary, which is why the probe must report current usage.

        The whole pressure response rests on process_memory() falling again when the run's
        memory is freed. Against a probe that reports a high-water mark the halving here would
        repeat every two seconds down to a limit of 1 and stay there for the rest of the
        session - which is what the macOS probe used to do. Whether a platform's probe can
        actually fall is checked by
        test_process_memory_reports_current_usage_not_a_high_water_mark.
        """
        gate = self.make_gate(config={"memory_limit": 900})
        gate.limit, gate.max_limit = 16, 256
        gate.in_flight = gate.limit

        self.memory.rss = 1000 * MB
        await gate._adapt_once()
        self.assertEqual(gate.limit, 8)

        self.memory.rss = 400 * MB
        gate.in_flight = gate.limit
        await gate._adapt_once()
        self.assertGreater(gate.limit, 8)

    async def test_a_measurement_can_raise_the_ceiling(self):
        # The op turned out cheaper than the default guess, so more of it fits
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB  # budget = 2GB, so 1MB/task gives a ceiling of 256
        gate = self.make_gate()
        gate.max_limit = 32

        self.measure_cost(gate, 1 * MB)

        self.assertEqual(gate.max_limit, conc.MAX_AUTO_CONCURRENCY)

    async def test_a_measurement_can_lower_the_ceiling_and_the_limit_with_it(self):
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB  # budget = 2GB
        gate = self.make_gate(limit=200, max_limit=256)

        # 128MB over 2 live tasks, so 64MB each and 256MB for a slot and its queue
        self.measure_cost(gate, 64 * MB)

        self.assertEqual(gate.max_limit, 2 * GB // (64 * MB * conc.TASK_QUEUE_DEPTH))
        self.assertLessEqual(gate.limit, gate.max_limit)

    async def test_the_queued_tasks_count_towards_what_one_costs(self):
        # The drivers report every task alive, not just the ones holding a slot: the queued
        # ones hold a note and a prompt too, and charging their memory to the running tasks
        # would put a task's cost at TASK_QUEUE_DEPTH times what it is.
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        gate = self.make_gate(limit=200, max_limit=256)

        self.measure_cost(gate, 4 * MB)

        self.assertEqual(gate.estimator.measured, 4 * MB)
        self.assertEqual(gate.max_limit, 2 * GB // (4 * MB * conc.TASK_QUEUE_DEPTH))

    async def test_a_configured_maximum_survives_re_measuring(self):
        gate = self.make_gate(config={"max_concurrent_requests": 10})
        self.measure_cost(gate, 20 * 1024)  # very cheap
        self.assertEqual(gate.max_limit, 10)

    async def test_a_run_that_measured_nothing_leaves_the_ceiling_alone(self):
        gate = self.make_gate(limit=16, max_limit=64)
        gate._apply_estimate()
        self.assertEqual(gate.max_limit, 64)

    async def test_probes_that_start_failing_mid_run_do_not_break_the_adapt_loop(self):
        """The probes can stop answering part way through - a WMI hiccup, a missing /proc.

        The run has to carry on. Without a reading there is no pressure signal, so the gate
        goes on growing towards the ceiling it was given; that ceiling was budgeted from a real
        reading at the start, which is what bounds the damage.
        """
        gate = self.make_gate(limit=16, max_limit=64)
        gate.in_flight = gate.limit
        self.memory.probe_failed = True

        for _ in range(10):
            await gate._adapt_once()
            gate.in_flight = gate.limit

        self.assertLessEqual(gate.limit, gate.max_limit)
        self.assertEqual(gate.available_memory, 0)

    async def test_memory_being_freed_again_is_measured_rather_than_discarded(self):
        """Tasks let go of their notes and response buffers as they finish, and the traced
        total falls with them. That is the fit's evidence, not a spoiled measurement: the old
        RSS difference read the same run as zero growth and threw it away.
        """
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        gate = self.make_gate()
        gate.estimator.start()

        for live in [0, 8, 16, 24, 32, 40, 32, 24, 16, 8, 0, 8]:
            self.tracing.current = 500 * MB + 64 * MB * live
            self.clock.advance(conc.SAMPLE_INTERVAL_SECONDS)
            gate.note_live_tasks(live)
        gate._apply_estimate()

        self.assertEqual(gate.estimator.measured, 64 * MB)


class BeginMeasuringTests(GateTestCase):
    """Tracing starts only when the run can act on what it would learn.

    start_adapting no longer starts it. The drivers know the run's task count only once they
    have planned the notes, and that planning pass is itself a long allocation-heavy stretch
    that would have been traced at ~10x while reporting no live task count at all.
    """

    def gate_on(self, total, available, stored=None, planned=5000, config=None):
        self.memory.total = total
        self.memory.available = available
        gate = self.make_gate(config=config)
        if stored is not None:
            gate.estimator.estimate = stored
        gate.start_adapting()
        gate.begin_measuring(planned)
        return gate

    async def test_start_adapting_alone_does_not_trace(self):
        self.memory.total = 4 * GB
        self.memory.available = 700 * MB
        gate = self.make_gate()

        gate.start_adapting()

        self.assertFalse(self.tracing.is_tracing())
        self.assertEqual(self.tracing.starts, 0)
        gate.stop_adapting()

    async def test_a_constrained_machine_measures(self):
        gate = self.gate_on(4 * GB, 700 * MB, stored=conc.MIN_PER_TASK_MEMORY)
        self.assertTrue(self.tracing.is_tracing())
        gate.stop_adapting()

    async def test_a_roomy_machine_with_a_measured_op_does_not(self):
        # The value the first run stored is what makes this one skip it
        gate = self.gate_on(16 * GB, 8 * GB, stored=conc.MIN_PER_TASK_MEMORY)
        self.assertFalse(self.tracing.is_tracing())
        gate.stop_adapting()

    async def test_a_roomy_machine_with_an_unmeasured_op_measures_once(self):
        gate = self.gate_on(16 * GB, 8 * GB, stored=conc.DEFAULT_PER_TASK_MEMORY)
        self.assertTrue(self.tracing.is_tracing())
        gate.stop_adapting()

    async def test_a_short_run_does_not_measure(self):
        gate = self.gate_on(4 * GB, 700 * MB, planned=conc.MIN_SLOPE_SPREAD)
        self.assertFalse(self.tracing.is_tracing())
        gate.stop_adapting()

    async def test_a_non_adaptive_gate_does_not_measure(self):
        self.memory.probe_failed = True
        gate = self.make_gate()
        self.assertFalse(gate.adaptive)

        gate.start_adapting()
        gate.begin_measuring(5000)

        self.assertFalse(self.tracing.is_tracing())

    async def test_a_skipped_run_still_adapts_and_still_backs_off(self):
        """Skipping the measurement gives up the ceiling, not the protection.

        The pressure response is what actually keeps a run off the machine's back, and it
        works off free memory rather than off anything the estimator produces.
        """
        gate = self.gate_on(16 * GB, 8 * GB, stored=conc.MIN_PER_TASK_MEMORY, planned=5000)
        self.assertFalse(self.tracing.is_tracing())
        gate.limit = 64
        gate.in_flight = 64

        self.memory.available = 100 * MB  # under the reserve
        await gate._adapt_once()

        self.assertEqual(gate.limit, 32)
        gate.stop_adapting()


class TraceTableExclusionTests(GateTestCase):
    """The trace table is in RSS, and both things that read an RSS have to see past it."""

    async def test_the_rss_series_excludes_the_table(self):
        gate = self.make_gate()
        gate.estimator.start()
        self.tracing.overhead = 30 * MB
        self.memory.rss = 500 * MB

        gate.estimator.sample()

        live, rss, _ = gate.estimator._rss._samples[-1]
        self.assertEqual(rss, 470 * MB)

    async def test_a_caller_supplied_rss_is_treated_the_same(self):
        # _adapt_once passes the reading it already took; it must not be exempt
        gate = self.make_gate()
        gate.estimator.start()
        self.tracing.overhead = 30 * MB

        gate.estimator.sample(rss=500 * MB)

        _, rss, _ = gate.estimator._rss._samples[-1]
        self.assertEqual(rss, 470 * MB)

    async def test_nothing_is_subtracted_when_not_tracing(self):
        gate = self.make_gate()
        gate.estimator.start()
        self.tracing.tracing = False
        self.tracing.overhead = 30 * MB

        gate.estimator.sample(rss=500 * MB)

        _, rss, _ = gate.estimator._rss._samples[-1]
        self.assertEqual(rss, 500 * MB)

    async def test_the_hard_cap_is_not_tripped_by_the_trace_table(self):
        """A configured memory_limit is spent on the run, not on the instrument.

        Halving the limit cannot shrink a trace table, so a controller that counted it would
        be backing off from a cost its one actuator does not touch.

        The reading is 620MB *including* a 40MB table, which is how a real RSS arrives - so
        the run itself is at 580MB, inside the 600MB the user asked for, and the only thing
        over the line is the measuring.
        """
        gate = self.make_gate(limit=64, config={"memory_limit": 600})
        gate.estimator.start()
        self.tracing.overhead = 40 * MB
        self.memory.rss = 620 * MB

        await gate._adapt_once()

        self.assertEqual(gate.limit, 64)

    async def test_the_hard_cap_still_bites_on_the_runs_own_memory(self):
        # 740MB with the same 40MB table in it, so the run is at 700MB and genuinely over
        gate = self.make_gate(limit=64, config={"memory_limit": 600})
        gate.estimator.start()
        self.tracing.overhead = 40 * MB
        self.memory.rss = 740 * MB

        await gate._adapt_once()

        self.assertEqual(gate.limit, 32)


class CeilingReportingTests(GateTestCase):
    """The connection pool is sized from the ceiling, so it has to hear the ceiling rise.

    set_connection_pool_size runs once at the start of a run, from a ceiling worked out from
    the starting guess at what a task costs. Fitting the real cost can raise that ceiling, and
    without this the pool keeps the size it was given: every request past it is a fresh TCP and
    TLS handshake plus a "connection pool is full" warning.

    Only upward, though. Reporting is not free - it drops every session and with it every idle
    keep-alive connection - so a ceiling coming down would pay the handshakes this exists to
    avoid, to buy a smaller pool, which is a pool that will simply not fill.
    """

    async def test_a_raised_ceiling_is_reported(self):
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        gate = self.make_gate()
        gate.max_limit = 32

        self.measure_cost(gate, 1 * MB)

        self.assertEqual(self.reported_ceilings, [conc.MAX_AUTO_CONCURRENCY])

    async def test_a_lowered_ceiling_is_applied_but_not_reported(self):
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB  # budget = 2GB
        gate = self.make_gate(limit=200, max_limit=256)

        # 64MB per live task, so 256MB per slot and its queue
        self.measure_cost(gate, 64 * MB)

        # The gate honours it: this is the case the measurement exists for
        self.assertEqual(gate.max_limit, 2 * GB // (64 * MB * conc.TASK_QUEUE_DEPTH))
        self.assertLessEqual(gate.limit, gate.max_limit)
        # ...but the pool is left alone. It is already larger than the new ceiling, so every
        # connection the run can now want is one it can already reuse.
        self.assertEqual(self.reported_ceilings, [])

    async def test_a_run_that_measured_nothing_reports_nothing(self):
        gate = self.make_gate(limit=16, max_limit=64)
        gate._apply_estimate()
        self.assertEqual(self.reported_ceilings, [])

    async def test_a_measurement_that_leaves_the_ceiling_where_it_was_reports_nothing(self):
        # Resizing the pool drops every session, so doing it once per window for a ceiling
        # that has not moved would rebuild connections all run long
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        gate = self.make_gate()
        self.assertEqual(gate.max_limit, conc.MAX_AUTO_CONCURRENCY)

        self.measure_cost(gate, 1 * MB)

        self.assertEqual(gate.max_limit, conc.MAX_AUTO_CONCURRENCY)
        self.assertEqual(self.reported_ceilings, [])

    async def test_a_non_adaptive_gate_never_reports(self):
        # Nothing to adapt against, so the ceiling it was given is the ceiling it keeps
        self.memory.probe_failed = True
        gate = self.make_gate()
        self.assertFalse(gate.adaptive)

        self.measure_cost(gate, 1 * MB)

        self.assertEqual(self.reported_ceilings, [])

    async def test_a_gate_with_nobody_listening_still_works(self):
        self.memory.total = 8 * GB
        self.memory.available = 8 * GB
        gate = conc.ConcurrencyGate({}, op_key="test op")
        gate.max_limit = 32
        self.measure_cost(gate, 1 * MB)
        self.assertEqual(gate.max_limit, conc.MAX_AUTO_CONCURRENCY)


class MemoryProbeContractTests(unittest.TestCase):
    """The one thing every platform's probe has to agree on, checked against the real probe.

    Deliberately not faked: a fake would only assert that the fake behaves, and the whole point
    is that the platform underneath is where these go wrong. This runs on whatever machine the
    suite runs on and fails on the one whose probe is broken.
    """

    def test_process_memory_reports_current_usage_not_a_high_water_mark(self):
        """Everything reading process_memory() assumes it tracks usage now, not the peak ever.

        The gate backs off while it is above the configured memory limit, and the estimator
        takes per-task cost from the growth over a window's baseline. A probe that cannot fall
        latches the first on forever - halving concurrency every two seconds down to 1 for the
        rest of the session - and makes the second measure zero growth.

        macOS is the platform this catches: process memory there came from
        resource.ru_maxrss, a high-water mark, before a `ps -o rss=` subprocess and now
        psutil's memory_info().rss. Neither replacement has been run on a Mac, so this test is
        what will say whether the current one works.
        """
        size = 256 * MB
        before = conc.process_memory()
        if before is None:
            self.skipTest("no process memory probe on this platform")

        blob = bytearray(size)
        blob[::4096] = bytes(len(blob[::4096]))  # touch every page so it is resident
        peak = conc.process_memory()
        del blob
        after = conc.process_memory()

        self.assertGreater(peak - before, size // 2, "allocating barely moved the probe")
        self.assertLess(
            after - before,
            size // 4,
            "the probe did not fall after the memory was freed, so it reports a peak",
        )


class NoPsutilTests(unittest.TestCase):
    """What happens when psutil is not there - lib/ unvendored, or built for another platform.

    The gate already copes with a probe that returns None: concurrency_limits gives it a static
    NO_PROBE_CONCURRENCY and turns adaptation off, and several tests above drive that through
    the stub. What none of them cover is the step before it, which is now the only place the
    whole scheme can fail - psutil missing has to become None rather than an ImportError
    escaping into the adapt loop.
    """

    def setUp(self):
        self.saved = (conc.psutil, conc._process, conc._probe_warning_logged)
        conc.psutil = None
        conc._process = None
        # Logged once per process, so a run of this class must not depend on test order
        conc._probe_warning_logged = False
        conc.load_per_task_estimates = lambda: {}

    def tearDown(self):
        conc.psutil, conc._process, conc._probe_warning_logged = self.saved
        conc.load_per_task_estimates = REAL_LOAD_PER_TASK_ESTIMATES

    def test_both_probes_return_none(self):
        self.assertIsNone(conc.system_memory())
        self.assertIsNone(conc.process_memory())

    def test_a_gate_falls_back_to_a_static_limit(self):
        gate = conc.ConcurrencyGate({}, op_key="test op")
        self.assertFalse(gate.adaptive)
        self.assertEqual(gate.limit, conc.NO_PROBE_CONCURRENCY)

    def test_a_probe_that_raises_is_reported_as_unavailable_rather_than_propagating(self):
        # psutil can raise where a container or a hardened kernel hides what it reads
        class Exploding:
            def virtual_memory(self):
                raise OSError("denied")

            def Process(self):
                raise OSError("denied")

        conc.psutil = Exploding()
        self.assertIsNone(conc.system_memory())
        self.assertIsNone(conc.process_memory())


class ProcessHandleTests(unittest.TestCase):
    def test_the_process_handle_is_made_once_and_reused(self):
        # psutil.Process() re-reads the process's creation time to prove the pid has not been
        # recycled. The adapt loop asks every two seconds about a process that is always ours.
        saved = (conc.psutil, conc._process)
        made = []

        class Counting:
            def Process(self):
                made.append(1)
                return self

            def memory_info(self):
                return type("Info", (), {"rss": 123})()

        conc.psutil = Counting()
        conc._process = None
        try:
            self.assertEqual(conc.process_memory(), 123)
            self.assertEqual(conc.process_memory(), 123)
        finally:
            conc.psutil, conc._process = saved
        self.assertEqual(len(made), 1)


if __name__ == "__main__":
    unittest.main()
