"""Memory-aware limiting of how much work is in flight at once.

The number of tasks waiting on API responses is what actually drives this addon's memory use,
and how many a device can afford differs a lot between a desktop and a tablet. So instead of a
fixed per-model request rate, a gate caps concurrent operations and resizes itself based on how
much memory is still free.

How much one operation costs also differs a lot between ops — a match-words task fans out per
word and can create notes and call several other ops, while translating a field is a single
request — so the cost is measured while running rather than assumed, and remembered per op for
next time.

Memory is read through psutil, vendored per platform because its wheels are abi3 and so
outlive Anki's Python upgrades - which the hand-written ctypes, /proc, vm_stat and ps probes
this replaced were meant to avoid needing, at the cost of four platform paths and a thread to
run the two macOS subprocesses off.

What psutil does not change is the requirement every probe has to meet: report what is in use
now, not a high-water mark. The pressure response and the per-task measurement both need the
number to be able to fall - a peak latches the first on and makes the second measure zero
growth. macOS is where that has actually bitten, when process memory came from
resource.ru_maxrss. memory_info().rss is current usage; the test named for it is the guard.

RSS turns out to fail that requirement too, just further down. Freed memory goes back to the
allocator rather than to the OS, so RSS ratchets, and a measurement built on it needed the run
to arrange a moment with nothing in flight to have any baseline to measure against. The
per-task cost is therefore measured from tracemalloc, whose total does come back down, and is
fitted against the live task count rather than differenced against a baseline - so it needs no
such moment, and the runs no longer serialise their first pass to provide one. RSS is still
measured alongside it, as the fallback and as the second opinion. See MemoryEstimator.

tracemalloc is also the one part of this that is not free, and it is not cheap: it charges
every allocation in the process - Anki's as well as this addon's - at roughly 4x on parsing a
response and 10x on ordinary object churn, and its trace table adds about 53 bytes per live
allocation, a third again on top of what it is measuring. Everything else here is noise beside
a request: an adapt tick is two psutil syscalls and two least-squares passes over 120 samples,
about 116us every two seconds, and the driver's live-count reports are a fifth of a
microsecond each.

So the tracing is bought rather than assumed. tracing_would_pay asks, before the estimator
starts, whether a fit could change what the gate does: a run of too few tasks cannot be fitted
at all, and on a machine with room to spare the budget divided by the backstop puts the
threshold well above what an op is believed to cost, so the ceiling would come out at the
backstop whatever the fit said. The asymmetry is the right way round - a constrained machine
has a small budget, so a low threshold, and is where tracing is switched on. An unmeasured op
clears the margin on its first run and the value that run stores is what makes the next one
skip it.

The same figure has to come off the RSS readings while tracing is on, for two different
reasons: the trace table grows with the live allocation count, so in the estimator it is the
instrument weighing itself and being charged to the tasks, and in the pressure check it is a
cost that lowering the limit cannot recover. See tracing_overhead.

Alongside all of that sits one limit that has nothing to do with memory: cpu_bound_section,
which rations cores rather than megabytes. The gate above may let hundreds of tasks be in
flight, which is right when a task in flight is waiting on a socket and wrong when it is
computing. See CPU_BOUND_HEADROOM.
"""

import asyncio
import json
import logging
import os
import threading
import time
import tracemalloc
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

try:
    import psutil  # type: ignore
except ImportError:  # the addon's lib/ was not vendored; see build.py vendor
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MB = 1024 * 1024

# Starting guess for what one live task costs: the note and prompt it holds from the moment it
# is created, and once it has a slot, a worker thread's committed stack, the connection and the
# request/response buffers. A limit of N keeps N * TASK_QUEUE_DEPTH tasks alive, which is why
# max_concurrency_for divides the budget by that many of these. Only used until the op has been
# measured once; see MemoryEstimator.
DEFAULT_PER_TASK_MEMORY = 2 * MB
# Measured values are clamped to this range. Anything outside it is measurement noise rather
# than a real per-task cost.
MIN_PER_TASK_MEMORY = 512 * 1024
MAX_PER_TASK_MEMORY = 128 * MB

MIN_CONCURRENCY = 1
MIN_AUTO_CONCURRENCY = 4
# Backstop only, and only while nothing is configured. Memory is meant to be what limits
# concurrency; this just stops a very cheap op on a very empty machine from opening an absurd
# number of connections at once. A max_concurrent_requests takes its place, higher or lower.
MAX_AUTO_CONCURRENCY = 256
# Where an adaptive run starts before it has grown into its ceiling
ADAPTIVE_START_CONCURRENCY = 16
# Used when memory can't be probed at all, so there is nothing to adapt against
NO_PROBE_CONCURRENCY = 8

# Fraction of the memory we're allowed to touch that goes to in-flight tasks. The rest is
# headroom, so a run doesn't drive the machine to the point where the adapt loop has to
# start backing off.
MEMORY_TARGET_FRACTION = 0.5
# ...but never more than this fraction of total RAM, so a machine that happens to be idle
# doesn't get us a limit it can't sustain once other apps want memory back
MEMORY_TOTAL_FRACTION = 0.25

# Memory to leave for everything else on the machine - the browser and editor the user has
# open alongside Anki. Both the budget and the runtime pressure check work off this, so the
# ceiling we plan for and the point we back off at agree with each other.
MIN_RESERVE_BYTES = 512 * MB
RESERVE_TOTAL_FRACTION = 0.1

ADAPT_INTERVAL_SECONDS = 2.0
# Fraction of the current limit to add per adapt tick while memory stays comfortable. Growing
# by a fixed step instead would take minutes to reach a high ceiling, so a run that is short
# or whose notes are quick would finish having never used the concurrency it could afford.
GROWTH_RATE = 0.25

# How many tasks to have queued behind the gate, as a multiple of the current limit. Some
# queue is needed: a slot must be claimed the instant one frees, and the gate can only tell
# it is the bottleneck (and so may grow) while tasks are waiting on it. Kept small because
# queued tasks are counted by both halves of the memory arithmetic, which have to agree with
# each other: see memory_per_slot and MemoryEstimator.
TASK_QUEUE_DEPTH = 4

# Share of the time the collection's one permit must be busy before the gate treats it as the
# run's bottleneck and stops growing. Not 1.0: the sample covers a couple of seconds and a few
# turns, so it is noisy, and a limit held one tick too long costs far less than a limit that
# keeps climbing past the point where climbing does anything. Because it is noisy, reading it
# once latches a ceiling rather than skipping one tick of growth - see _adapt_once.
COLLECTION_SATURATED = 0.85

# What the machine's free memory has to rise by before a cut to the limit counts as having
# worked. The pressure response is a control loop, and on a machine where the run's own RSS is
# not what crossed the reserve line its actuator does not move its input: one measured tablet
# run halved the limit 32 times, took it 261 -> 1 in fourteen seconds, and then held 1 for
# nineteen minutes while RSS fell 7.0 GB -> 6.1 GB entirely because other processes let go.
# So the second cut of an episode has to show that the first one bought something. 64MB is
# well above the noise in a `available` reading and well below what dropping half the limit
# would free if the limit were really what was holding the memory.
PRESSURE_RESPONSE_MIN_GAIN = 64 * MB
# How far the ceiling re-read off free memory has to move before it is worth applying. The
# budget is recomputed every tick now rather than once at construction, and free memory
# jitters by a few megabytes between two ticks; without this the ceiling - and with it the
# log - would move on nearly every one of them.
CEILING_HYSTERESIS = 0.10

# How many CPU-bound sections may run at once, over and above the core count. The gate's own
# limit is calibrated for work that waits - a collection turn, a request on a socket - where a
# thread costs nothing while it is parked, so an over-generous ceiling is free. MDX dictionary
# lookups are the opposite: regex and dict work over index files, which needs a core rather
# than a socket, and handing 300 runnable threads to 4 cores buys no throughput while
# multiplying every latency by the oversubscription factor. The headroom is for the part of a
# lookup that does wait - reading the index file off disk - so a core is not left idle.
CPU_BOUND_HEADROOM = 1


class CollectionPressure:
    """How much of the time the collection's single permit is occupied.

    The gate's own test for being the bottleneck - every slot taken - cannot tell work from
    waiting. A task blocked on the collection holds its slot and allocates nothing, so a run
    queueing for the collection looks to the gate exactly like one that could use more
    concurrency, and to the memory estimator like one whose tasks are free. Both then argue for
    growth that cannot help: Anki runs one caller at a time inside the collection whatever the
    limit says, so tasks added past that point queue rather than do anything.

    Hence the resource reporting on itself. Only time actually spent holding the collection is
    counted, because that is what caps the run's throughput - the queue behind it is the
    symptom, and measuring the symptom would make the reading depend on the limit it is meant
    to be choosing.

    Lives here rather than in collection_access so that concurrency.py keeps to the stdlib and
    stays loadable, and testable, outside a running Anki.

    Timed on perf_counter rather than monotonic. Windows resolves monotonic to about 15.6ms,
    which is coarser than a good many turns with the collection - a cached get_note among them
    - and every one of those reads as a hold of exactly zero. The error is one-directional:
    utilisation comes out lower than it is, so the gate sees headroom that is not there and
    grows into a collection that is already the bottleneck, which is the reading this class
    exists to prevent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held = 0.0
        self._turns = 0
        self._since = time.perf_counter()

    def reset(self) -> None:
        """Start a fresh window. Called when a run begins adapting."""
        with self._lock:
            self._held = 0.0
            self._turns = 0
            self._since = time.perf_counter()

    def record(self, held_seconds: float) -> None:
        """One completed turn holding the collection."""
        with self._lock:
            self._held += held_seconds
            self._turns += 1

    def sample(self) -> Optional[tuple[float, float]]:
        """(utilisation, mean seconds per turn) since the last sample, None if nothing ran.

        Consuming: a sample covers only the window since the previous one, so the reading
        follows the run instead of averaging over all of it.
        """
        with self._lock:
            now = time.perf_counter()
            elapsed = now - self._since
            held, turns = self._held, self._turns
            self._held, self._turns, self._since = 0.0, 0, now
        if turns == 0 or elapsed <= 0:
            return None
        # Capped at 1: a turn that started before this window lands its whole hold time in it
        return min(1.0, held / elapsed), held / turns


# One collection, so one of these. Reset by whichever gate starts adapting.
collection_pressure = CollectionPressure()


# --- Memory probes -----------------------------------------------------------------------

_probe_warning_logged = False


def _warn_probe_unavailable(what: str, error: Exception) -> None:
    global _probe_warning_logged
    if not _probe_warning_logged:
        _probe_warning_logged = True
        logger.warning(
            "Memory probing unavailable (%s: %s); concurrency will use a static limit",
            what,
            error,
        )


# Made once and reused: constructing a psutil.Process re-reads the process's creation time to
# prove the pid has not been recycled, and the adapt loop asks every two seconds about a
# process that is always this one.
_process = None


def system_memory() -> Optional[tuple[int, int]]:
    """Total and available physical memory in bytes, or None if it can't be determined."""
    if psutil is None:
        _warn_probe_unavailable("system memory", ImportError("psutil is not importable"))
        return None
    try:
        memory = psutil.virtual_memory()
    except Exception as e:
        _warn_probe_unavailable("system memory", e)
        return None
    return int(memory.total), int(memory.available)


def process_memory() -> Optional[int]:
    """This process's resident memory in bytes, or None if it can't be determined."""
    global _process
    if psutil is None:
        _warn_probe_unavailable("process memory", ImportError("psutil is not importable"))
        return None
    try:
        if _process is None:
            _process = psutil.Process()
        return int(_process.memory_info().rss)
    except Exception as e:
        _warn_probe_unavailable("process memory", e)
        return None


def traced_memory() -> Optional[int]:
    """Bytes of live Python allocations, or None when nothing is tracing them.

    Two counter reads, so it costs nothing to call. The expense of tracemalloc is not here but
    in the tracing itself, which charges every allocation in the process - which is why the
    estimator does not switch it on unless a fit could change the gate's ceiling
    (tracing_would_pay), and keeps it on only for the first stretch of a run when it does.
    """
    if not tracemalloc.is_tracing():
        return None
    try:
        return int(tracemalloc.get_traced_memory()[0])
    except Exception as e:
        _warn_probe_unavailable("traced memory", e)
        return None


def format_bytes(value: Optional[int]) -> str:
    if value is None:
        return "?"
    if value >= 1024 * MB:
        return f"{value / (1024 * MB):.1f} GB"
    if value >= MB:
        return f"{value / MB:.0f} MB"
    # Per-task costs live down here, and rounding them to whole megabytes showed the honest
    # answer for a cheap task as "0 MB/task" in the progress dialog - which reads as a broken
    # measurement rather than a small one.
    return f"{value / 1024:.0f} KB"


def configured_memory_limit_bytes(config: Optional[dict], total_memory: int) -> int:
    """Configured RSS hard cap in bytes.

    `memory_limit` accepts either an MB quantity (number) or a percentage string
    like "25%" of total RAM.
    """
    config = config or {}
    value = config.get("memory_limit", 0)

    def warn_invalid(reason: str) -> None:
        logger.warning(
            "Ignoring invalid memory_limit (%r): %s. Expected a positive MB number"
            " or a percentage string like '25%%'.",
            value,
            reason,
        )

    # bool is a subclass of int; treating true as 1 MB would be surprising.
    if isinstance(value, bool):
        warn_invalid("booleans are not supported")
        return 0

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if text.endswith("%"):
            if total_memory <= 0:
                warn_invalid("total RAM is unknown, so percentage limits cannot be resolved")
                return 0
            try:
                percent = float(text[:-1].strip())
            except ValueError:
                warn_invalid("could not parse percentage")
                return 0
            if percent <= 0:
                warn_invalid("percentage must be greater than 0")
                return 0
            return int(total_memory * (percent / 100.0))
        try:
            value = float(text)
        except ValueError:
            warn_invalid("could not parse MB value")
            return 0

    if isinstance(value, (int, float)) and value > 0:
        return int(value * MB)

    if value != 0:
        warn_invalid("MB value must be greater than 0")

    return 0


# --- The gate ----------------------------------------------------------------------------


def memory_reserve(total: int) -> int:
    """Memory to keep free for the rest of the machine."""
    return max(MIN_RESERVE_BYTES, int(total * RESERVE_TOTAL_FRACTION))


def memory_budget() -> Optional[float]:
    """Bytes we're willing to spend on in-flight tasks, or None if memory can't be probed.

    The reserve comes off the top before anything is budgeted, so the ceiling we size for and
    the level the adapt loop backs off at are consistent. Budgeting from raw available memory
    would happily plan a limit that immediately triggers the pressure response.
    """
    memory = system_memory()
    if not memory or not memory[0] or not memory[1]:
        return None
    total, available = memory
    spendable = max(0, available - memory_reserve(total))
    return min(spendable * MEMORY_TARGET_FRACTION, total * MEMORY_TOTAL_FRACTION)


def max_possible_concurrency(config: Optional[dict] = None) -> int:
    """The highest limit the gate could ever reach for this config.

    Used to size the shared thread pool, which is created before the gate and must still be
    big enough if a cheap op turns out to warrant a lot of concurrency. Threads are spawned
    lazily, so an over-generous ceiling costs nothing.
    """
    config = config or {}
    configured = int(config.get("max_concurrent_requests", 0) or 0)
    return max(configured, MAX_AUTO_CONCURRENCY)


def cpu_bound_concurrency() -> int:
    """How many CPU-bound sections may run at once.

    Sized off the machine, not off the gate: the gate's limit says how many tasks may be in
    flight, and a task in flight is nearly always waiting for something. This says how many of
    them may be *computing*, which no amount of memory makes bigger than the core count.
    """
    return max(1, os.cpu_count() or 1) + CPU_BOUND_HEADROOM


_cpu_bound_gate: Optional[threading.Semaphore] = None
_cpu_bound_gate_lock = threading.Lock()


def _cpu_bound_gate_instance() -> threading.Semaphore:
    """The process-wide CPU-bound gate, built on first use.

    Deliberately a threading primitive and deliberately process-wide. An asyncio.Semaphore
    binds to the loop it is first awaited on, and every bulk op builds a fresh loop, so an
    asyncio one would have to be rebuilt per run and would fail loudly if it were not. The
    thing being rationed - cores - is shared by every loop and every thread the addon runs, so
    one gate for the process is also the more accurate model.
    """
    global _cpu_bound_gate
    with _cpu_bound_gate_lock:
        if _cpu_bound_gate is None:
            _cpu_bound_gate = threading.Semaphore(cpu_bound_concurrency())
        return _cpu_bound_gate


@contextmanager
def cpu_bound_section():
    """Hold a CPU slot for the duration of the block.

    For work that computes rather than waits, called from a worker thread. Blocking that
    thread is the point: it is already off the event loop, and a thread blocked here is a
    thread the scheduler is not rotating through a core to no purpose.

    Wrap only the computing part. Anything that waits on a socket or on the collection must be
    outside the block, or the slot sits idle while it waits and the core goes unused.
    """
    gate = _cpu_bound_gate_instance()
    gate.acquire()
    try:
        yield
    finally:
        gate.release()


def _set_cpu_bound_limit(limit: Optional[int]) -> None:
    """Rebuild the gate at a given size, or at the default when limit is None. Tests only.

    Replaces the semaphore rather than resizing it, so it must not be called while any section
    is running - the slots held against the old one are simply forgotten.
    """
    global _cpu_bound_gate
    with _cpu_bound_gate_lock:
        size = limit if limit is not None else cpu_bound_concurrency()
        _cpu_bound_gate = threading.Semaphore(size)


def memory_per_slot(per_task_memory: float) -> float:
    """What one place in the limit really costs in memory.

    The multiplier is here because the estimate on the other side is divided by the same thing.
    What is measured is memory per *live* task, and the drivers keep TASK_QUEUE_DEPTH tasks
    alive for every slot - so if a slot's work costs C, the measurement comes out at C /
    TASK_QUEUE_DEPTH and this puts it back.

    It is not that a queued task costs what a running one does. It costs almost nothing: every
    task parks on `await gate.acquire()` as its first statement, so a queued one holds a
    reference to a note that was already loaded and nothing else - no prompt, no request, no
    response, all of which come after the slot is granted. The two factors have to keep
    matching each other, and both are TASK_QUEUE_DEPTH, which is what makes the ceiling right.
    """
    return max(per_task_memory, MIN_PER_TASK_MEMORY) * TASK_QUEUE_DEPTH


def max_concurrency_for(per_task_memory: float, configured_max: int = 0) -> int:
    """The ceiling memory allows for an op costing this much per task.

    A configured max replaces the automatic backstop rather than applying under it: the 256 is
    only there so an unconfigured run on a very empty machine doesn't open an absurd number of
    connections, and someone who names a number has decided that for themselves. What memory
    allows still applies on top, so a configured ceiling is what the run may grow to and not
    what it will get.
    """
    budget = memory_budget()
    if budget is None:
        return NO_PROBE_CONCURRENCY
    backstop = configured_max if configured_max > 0 else MAX_AUTO_CONCURRENCY
    return int(
        min(
            backstop,
            max(MIN_AUTO_CONCURRENCY, budget // memory_per_slot(per_task_memory)),
        )
    )


def concurrency_limits(
    config: Optional[dict] = None, per_task_memory: Optional[float] = None
) -> tuple[int, int, bool]:
    """Work out (starting limit, ceiling, adaptive) for this device.

    The ceiling comes from how much memory is free divided by what one task costs, so a tablet
    gets a lower one than a desktop, and a heavy op a lower one than a light op, without anyone
    having to configure it. A configured max_concurrent_requests replaces the automatic backstop
    and may be higher or lower than it, but does not switch adaptation off - the memory-pressure
    response still applies underneath it, which is what actually protects the machine.
    """
    config = config or {}
    configured = int(config.get("max_concurrent_requests", 0) or 0)

    if memory_budget() is None:
        # Nothing to adapt against: a conservative static limit
        static = configured if configured > 0 else NO_PROBE_CONCURRENCY
        return static, static, False

    max_limit = max_concurrency_for(per_task_memory or DEFAULT_PER_TASK_MEMORY, configured)
    return min(max_limit, ADAPTIVE_START_CONCURRENCY), max_limit, True


# --- Learning what an op actually costs ---------------------------------------------------

ESTIMATES_FILE = "memory_estimates.json"
# Bumped whenever what a stored number means changes. Version 1 was an unversioned mapping
# holding the cost of a place in the limit - a whole window slice of tasks - because the
# measurement divided a window's growth by the tasks in flight rather than the tasks alive.
ESTIMATES_VERSION = 2
# Weight given to a new run's measurement when blending it into the stored value. Low enough
# that one unusual run doesn't throw the estimate off, high enough to track real changes.
ESTIMATE_BLEND = 0.4
# One frame per traced allocation. The estimator only ever reads the total, so a deeper
# traceback would be detail nothing looks at - and it is not free detail: measured on this
# addon's own shape of work, parsing a 28 KB JSON response, tracing costs 6.1x at one frame
# and 23.6x at ten.
TRACE_FRAMES = 1
# How long into a run to keep tracing. Long enough for the limit to have moved and the fit to
# have settled, and no longer, because of that 6.1x. It buys the run more than it costs: the
# first pass used to be serialised outright to give the old measurement a clean window, and
# that 6.1x is on parsing, which is a millisecond or two beside the request it came from.
# What one task costs does not change halfway through a run.
MEASURE_SECONDS = 30.0
# Probing is throttled to this, because the driver reports a live-task count on every batch of
# completions - far more often than a fit needs, and each sample is a syscall for RSS.
SAMPLE_INTERVAL_SECONDS = 0.25
# Samples kept for the fit; at the interval above, the whole of the measuring window.
SLOPE_SAMPLE_CAPACITY = 120
# Below this there are not enough points to fit anything meaningful
MIN_SLOPE_SAMPLES = 8
# ...and the live-task count has to have actually moved across them, or the "slope" is only
# the noise in the memory readings divided by nearly nothing.
MIN_SLOPE_SPREAD = 4
# How far above what a task is already believed to cost the deciding threshold may sit before
# measuring stops being worth what it costs. Four means an op has to have quadrupled in cost
# since it was last measured for the fit to have anything to say; see tracing_would_pay.
MEASUREMENT_MARGIN = 4.0


def ceiling_threshold(configured_max: int = 0) -> Optional[float]:
    """The per-task cost at which a measurement starts changing the ceiling.

    max_concurrency_for is `min(backstop, budget // memory_per_slot)`. Below this figure the
    division comes out above the backstop and the backstop is the answer, whatever the fit
    says - so a fit that lands below it has been computed and then multiplied by zero.

    None when memory cannot be probed, which is also when there is no budget to divide.
    """
    budget = memory_budget()
    if budget is None:
        return None
    backstop = configured_max if configured_max > 0 else MAX_AUTO_CONCURRENCY
    return budget / (backstop * TASK_QUEUE_DEPTH)


def tracing_would_pay(
    per_task_memory: float, planned_tasks: int, configured_max: int = 0
) -> "tuple[bool, str]":
    """Whether measuring this run can change what the gate does. Returns (worth it, why).

    Tracing is the one part of this module that is not free: it charges every allocation in
    the process, not just this addon's, at roughly 4x on parsing a response and 10x on
    ordinary object churn, and its trace table adds about 53 bytes per live allocation. That
    is worth paying for a number the gate will act on and not otherwise, so this asks whether
    it can act on it before the estimator starts.

    Three ways the answer is no:

    * The run is too short for a fit. The live-task count has to span MIN_SLOPE_SPREAD for
      slope() to return anything at all, and a run of fewer tasks than that never can - so
      such a run traces for its whole length and provably produces nothing.
    * No fit could reach the threshold. Fitted values are clamped to MAX_PER_TASK_MEMORY, so
      a threshold above that clamp cannot be cleared by any measurement.
    * The threshold is far above what this op is already believed to cost. On a machine with
      room the budget is large, the threshold is correspondingly large, and an op measured at
      the floor would have to have grown several times over for the ceiling to move. The
      asymmetry here is the right way round: a constrained machine has a small budget and so
      a small threshold, and is exactly where this returns True.

    An op that has never been measured is believed to cost DEFAULT_PER_TASK_MEMORY, which on
    a typical desktop clears the margin - so a new op is measured once, and the value that
    measurement stores is then what makes later runs skip it.
    """
    if planned_tasks <= MIN_SLOPE_SPREAD:
        return False, (
            f"only {planned_tasks} tasks, too few for the live count to span"
            f" {MIN_SLOPE_SPREAD}"
        )
    threshold = ceiling_threshold(configured_max)
    if threshold is None:
        return False, "memory cannot be probed, so the limit is static"
    if threshold > MAX_PER_TASK_MEMORY:
        return False, (
            f"the ceiling only moves above {format_bytes(int(threshold))} per task, past the"
            f" {format_bytes(MAX_PER_TASK_MEMORY)} clamp on a fitted value"
        )
    believed = max(per_task_memory, MIN_PER_TASK_MEMORY)
    if threshold > believed * MEASUREMENT_MARGIN:
        return False, (
            f"the ceiling only moves above {format_bytes(int(threshold))} per task and this"
            f" op is believed to cost {format_bytes(int(believed))}"
        )
    return True, (
        f"the ceiling moves above {format_bytes(int(threshold))} per task, within reach of"
        f" the {format_bytes(int(believed))} this op is believed to cost"
    )


def tracing_overhead() -> int:
    """Bytes of RSS that tracemalloc's own trace table is occupying.

    Roughly 53 bytes per live traced allocation, which on this addon's shape of work is about
    a third again on top of what it is measuring. It has to come off an RSS reading before
    either of the two things that read one can use it - see MemoryEstimator.sample.
    """
    if not tracemalloc.is_tracing():
        return 0
    try:
        return int(tracemalloc.get_tracemalloc_memory())
    except Exception as e:
        logger.debug("Could not read tracemalloc's own memory use: %s", e)
        return 0


def _estimates_path() -> Path:
    # user_files is the add-on's own data directory: it survives add-on updates and is not
    # synced, which suits a per-device measurement.
    return Path(__file__).resolve().parent.parent / "user_files" / ESTIMATES_FILE


def _read_estimates_file() -> "tuple[Optional[dict], bool]":
    """What the file holds, and whether it was readable at all.

    The two empty answers have to stay apart. A file that isn't there yet is ours to write; one
    we couldn't parse may still hold every other op's measurements, and rewriting it with only
    this op's would be how they get lost.
    """
    path = _estimates_path()
    if not path.exists():
        return None, True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.debug("Could not read memory estimates: %s", e)
        return None, False
    if not isinstance(data, dict):
        # Valid JSON, but not a shape this ever wrote; treat it as someone else's file
        return None, False
    return data, True


def _written_by_a_newer_version(data: Optional[dict]) -> bool:
    version = (data or {}).get("version")
    return isinstance(version, int) and version > ESTIMATES_VERSION


def _estimates_in(data: Optional[dict]) -> dict:
    if data is None:
        return {}
    version = data.get("version")
    if version is None:
        # Version 1: a bare mapping, whose numbers were a slot's worth of tasks rather than
        # one task's. Converting keeps what those runs measured instead of throwing it away.
        return {
            key: value / TASK_QUEUE_DEPTH
            for key, value in data.items()
            if isinstance(value, (int, float)) and value > 0
        }
    if version != ESTIMATES_VERSION:
        # Written by a later version of the add-on; measure it again rather than guess at
        # what the numbers mean
        return {}
    estimates = data.get("estimates")
    return estimates if isinstance(estimates, dict) else {}


def load_per_task_estimates() -> dict:
    return _estimates_in(_read_estimates_file()[0])


def save_per_task_estimate(op_key: str, value: float) -> None:
    path = _estimates_path()
    temp = path.with_name(f"{path.name}.tmp")
    try:
        data, readable = _read_estimates_file()
        if not readable:
            logger.debug(
                "Memory estimates file could not be read; leaving it alone rather than"
                " replacing what other ops measured with just this one"
            )
            return
        if _written_by_a_newer_version(data):
            # Its numbers are unreadable here, but they are not ours to throw away: downgrading
            # for one session would otherwise destroy what the newer version had measured.
            logger.debug(
                "Memory estimates file is version %s, newer than this add-on writes;"
                " leaving it alone",
                (data or {}).get("version"),
            )
            return
        estimates = _estimates_in(data)
        previous = estimates.get(op_key)
        if isinstance(previous, (int, float)) and previous > 0:
            value = previous * (1 - ESTIMATE_BLEND) + value * ESTIMATE_BLEND
        estimates[op_key] = int(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the file and moved into place, so a crash or a force-quit partway
        # through leaves the previous file intact. Writing over it directly is how it came to
        # be half a file, which the read above then has to refuse to overwrite - at which point
        # nothing can be saved until the user deletes it.
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": ESTIMATES_VERSION,
                    "estimates": {key: int(item) for key, item in sorted(estimates.items())},
                },
                f,
                indent=2,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        logger.debug("Saved per-task memory estimate for %r: %s", op_key, format_bytes(int(value)))
    except Exception as e:
        logger.debug("Could not save memory estimate for %r: %s", op_key, e)
        try:
            temp.unlink()
        except Exception:
            # Never written, gone already, or not a path that can be written at all
            pass


class _Fit:
    """Least squares of a memory reading against the number of live tasks, and against time.

    Fitting is what removes the need for a quiet baseline. A run accumulates memory that has
    nothing to do with how many tasks are in flight - the results it has collected, the notes
    it has touched - and in a fit that accumulation is not the answer but a term to be
    separated out. The measurement this replaced could only separate it by arranging a moment
    when nothing was in flight, which is what the first pass of every run was made to be.

    Time is the second predictor rather than only the intercept, and it is doing real work.
    The accumulation grows with elapsed time, and so does the live task count, because the
    adapt loop raises the limit as a run settles in. Against task count alone the two cannot
    be told apart and the accumulation is charged to the tasks: on a workload built to have a
    known answer, that read 1.5x high. Regressing on both attributes the time trend to time.

    The sample window is bounded so the fit follows the run rather than averaging over all of
    it.
    """

    def __init__(self, capacity: int = SLOPE_SAMPLE_CAPACITY):
        self._samples: "deque[tuple[float, float, float]]" = deque(maxlen=capacity)

    def __len__(self) -> int:
        return len(self._samples)

    def add(self, live_tasks: float, memory: float, at: float) -> None:
        self._samples.append((float(live_tasks), float(memory), float(at)))

    def slope(self) -> Optional[float]:
        """Bytes per live task, or None if these samples cannot support an answer."""
        count = len(self._samples)
        if count < MIN_SLOPE_SAMPLES:
            return None
        live = [tasks for tasks, _, _ in self._samples]
        if max(live) - min(live) < MIN_SLOPE_SPREAD:
            # Every sample was taken at effectively the same task count, so there is no slope
            # here to find - only the noise in the memory readings, divided by nearly nothing.
            return None
        memory = [used for _, used, _ in self._samples]
        elapsed = [at for _, _, at in self._samples]
        mean_live = sum(live) / count
        mean_memory = sum(memory) / count
        mean_elapsed = sum(elapsed) / count
        d_live = [value - mean_live for value in live]
        d_memory = [value - mean_memory for value in memory]
        d_elapsed = [value - mean_elapsed for value in elapsed]

        live_live = sum(value * value for value in d_live)
        elapsed_elapsed = sum(value * value for value in d_elapsed)
        live_elapsed = sum(a * b for a, b in zip(d_live, d_elapsed))
        live_memory = sum(a * b for a, b in zip(d_live, d_memory))
        elapsed_memory = sum(a * b for a, b in zip(d_elapsed, d_memory))

        determinant = live_live * elapsed_elapsed - live_elapsed * live_elapsed
        if determinant <= 0:
            # The task count moved in lockstep with the clock and nothing here can say which of
            # them the memory followed. One predictor is better than a confident wrong answer.
            return live_memory / live_live if live_live > 0 else None
        return (elapsed_elapsed * live_memory - live_elapsed * elapsed_memory) / determinant


class MemoryEstimator:
    """Measures what one live task of a given op costs, and remembers it for next time.

    Two series are fitted against the same live-task counts:

    * **Traced Python allocations**, from tracemalloc. This is what drives the estimate.
    * **RSS**, from psutil, less whatever tracemalloc's own trace table weighs. The fallback
      for a runtime that will not trace, and otherwise a second opinion, logged beside the
      traced figure so the two can be compared on real runs. The subtraction is what keeps it
      a second opinion: the table grows with the live allocation count, so left in it would
      be the instrument's own weight, charged to the tasks it correlates with.

    RSS is no longer the primary because it cannot fall. Freed memory goes back to the
    allocator, not to the OS, so RSS ratchets: it rises over the first busy stretch and then
    stays there whatever the task count does afterwards. That is why the measurement this
    replaced needed a pass that both began and ended with nothing in flight, and why it threw
    away every window whose growth came out at or below zero - which, once a run is warm, is
    most of them. tracemalloc's total falls the moment a task's objects are freed, so the fit
    sees the task count come down as well as go up, and no barrier has to be arranged for it.

    What tracemalloc does not see is memory allocated outside Python's allocator: a C
    extension's own buffers, and anything allocated before tracing began. The part that scales
    with the task count is covered - urllib3 hands response bodies back as Python `bytes` and
    the prompts are Python strings. The notes are not, because they are loaded before the run
    starts; but they are a fixed cost of the run rather than the cost of one more live task,
    so a per-task figure is right to leave them out.

    Expect the number to be small, and to be a *quarter* of what a running task costs: three
    live tasks in four are parked on `gate.acquire()` and have allocated nothing at all. That
    is the convention memory_per_slot multiplies back out - see it - and not a fault, but it
    does mean a cheap op measures near MIN_PER_TASK_MEMORY, where the clamp starts hiding what
    was actually fitted. refit() logs both figures for that reason.

    Within a run the largest fit wins, because what has to fit in RAM is the peak rather than
    the average. Across runs the value is blended into the stored one, so a single odd run
    does not skew it.
    """

    def __init__(self, op_key: Optional[str], stored: Optional[float] = None):
        self.op_key = op_key
        self.measured: Optional[float] = None
        self.estimate: float = float(stored) if stored else DEFAULT_PER_TASK_MEMORY
        self.from_measurement = bool(stored)
        self._traced = _Fit()
        self._rss = _Fit()
        self._live_tasks = 0.0
        self._measuring = False
        self._owns_tracing = False
        self._started_at: Optional[float] = None
        self._sampled_at: Optional[float] = None
        self._fitted_at_samples = -1

    def start(self) -> None:
        """Begin measuring, tracing Python allocations unless something else already is.

        Whether this is worth its cost at all is the gate's decision, not this one's; see
        ConcurrencyGate.begin_measuring and tracing_would_pay.
        """
        self._measuring = True
        self._started_at = time.monotonic()
        if tracemalloc.is_tracing():
            # Somebody else's tracing - another addon, or a developer with a debugger open.
            # Reading the total is fine; stopping it is not ours to do.
            logger.debug("tracemalloc is already tracing; reading it without taking it over")
            return
        try:
            # One frame per allocation: only the total is ever read here, so deeper tracebacks
            # would be time and memory spent on detail nothing looks at.
            tracemalloc.start(TRACE_FRAMES)
            self._owns_tracing = True
        except Exception as e:
            _warn_probe_unavailable("traced memory", e)

    def stop(self) -> None:
        """Stop measuring, and stop tracing if this is what started it."""
        self._measuring = False
        if not self._owns_tracing:
            return
        self._owns_tracing = False
        try:
            tracemalloc.stop()
        except Exception as e:
            logger.debug("Could not stop tracemalloc: %s", e)

    def note_live_tasks(self, count: float) -> None:
        """How many of the op's API tasks are alive right now, and a sample at that count.

        Live, not in flight, because that is the count memory_per_slot divides back out again;
        see it for why the two have to agree. Most of them are parked on `gate.acquire()` and
        have allocated nothing yet, so the figure this produces is roughly what one task costs
        divided by TASK_QUEUE_DEPTH - small, and small enough to sit on MIN_PER_TASK_MEMORY.

        The driver reports it on every refill and every batch of completions, which is where
        the fit gets most of its samples and all of its spread.
        """
        self._live_tasks = max(0.0, float(count))
        self.sample()

    def sample(self, rss: Optional[int] = None, in_flight: Optional[int] = None) -> None:
        """Record memory at the current live-task count.

        `rss` saves a second probe for a caller that has just read it anyway, and is expected
        raw: the trace table comes off here so that every caller gets the same treatment.
        `in_flight` is a floor on the live count, for a caller driving the gate without
        reporting one of its own.
        """
        if not self._measuring:
            return
        now = time.monotonic()
        if (
            self._sampled_at is not None
            and now - self._sampled_at < SAMPLE_INTERVAL_SECONDS
        ):
            # The driver reports on every completion, which on a fast op is far more often than
            # a fit needs. Throttling bounds the probing rather than the reporting.
            return
        self._sampled_at = now
        at = now - (self._started_at if self._started_at is not None else now)
        live = max(self._live_tasks, float(in_flight or 0))
        traced = traced_memory()
        if traced is not None:
            self._traced.add(live, traced, at)
        if rss is None:
            rss = process_memory()
        if rss is not None:
            # The trace table is in RSS, and it grows with the number of live allocations -
            # which is to say with the live task count, the very thing this is being fitted
            # against. Left in, the instrument reading its own weight would be charged to the
            # tasks, and the series kept as a second opinion on the traced one would be partly
            # a copy of it. Taken off, RSS is again a reading the tracing did not affect.
            self._rss.add(live, max(0, rss - tracing_overhead()), at)

    def refit(self) -> Optional[float]:
        """Fit the samples collected so far. Returns the new estimate when it rises.

        A traced slope that comes out at or below zero is taken at face value rather than
        falling back to the RSS one: it means this stretch of the run does not show a per-task
        cost, and RSS - which only ever ratchets upward - would answer the question with the
        run's accumulation instead.
        """
        if (
            self._measuring
            and self._started_at is not None
            and time.monotonic() - self._started_at >= MEASURE_SECONDS
        ):
            # Tracing charges every allocation in the process, and what one task costs does not
            # change halfway through a run, so there is nothing left to buy by paying past here.
            # Checked before the short-circuit below, which would otherwise leave tracing on for
            # the rest of a run whose samples had stopped changing.
            logger.debug("Measured for %.0fs; stopping tracing", MEASURE_SECONDS)
            self.stop()

        samples = len(self._traced) + len(self._rss)
        if samples == self._fitted_at_samples:
            # Nothing new to fit. Worth checking because sampling stops well before the run
            # does, and the adapt loop goes on calling this every couple of seconds regardless.
            return None
        self._fitted_at_samples = samples

        traced_slope = self._traced.slope()
        rss_slope = self._rss.slope()
        if traced_slope is not None and rss_slope is not None:
            # The comparison the RSS series is kept for. On a warm run the RSS slope is
            # expected to read high, because the ratchet is in it.
            logger.debug(
                "Per-task fit for %r over %d samples: traced %s, rss %s",
                self.op_key,
                len(self._traced),
                format_bytes(int(traced_slope)),
                format_bytes(int(rss_slope)),
            )
        fitted = traced_slope if traced_slope is not None else rss_slope
        if fitted is None or fitted <= 0:
            return None
        per_task = min(MAX_PER_TASK_MEMORY, max(MIN_PER_TASK_MEMORY, fitted))
        if self.measured is not None and per_task <= self.measured:
            return None
        previous = self.estimate
        self.measured = per_task
        self.estimate = per_task
        self.from_measurement = True
        # The fitted figure is logged beside the one that is kept, because a value sitting on
        # a clamp is not a measurement of that value - it is the fit saying "smaller than this
        # floor" or "larger than this ceiling", and the two read identically otherwise.
        logger.debug(
            "Measured per-task memory for %r: %s from %s (fitted %s%s, was %s)",
            self.op_key,
            format_bytes(int(per_task)),
            "traced allocations" if traced_slope is not None else "rss",
            format_bytes(int(fitted)),
            " - clamped" if int(per_task) != int(fitted) else "",
            format_bytes(int(previous)),
        )
        return per_task

    def persist(self) -> None:
        if self.op_key and self.measured:
            save_per_task_estimate(self.op_key, self.measured)


class ConcurrencyGate:
    """Caps how many operations run at once, resizing itself as memory allows.

    Built on a counter plus a queue of waiters rather than an asyncio.Semaphore because the
    limit has to be able to shrink: lowering it simply makes new acquires wait until enough
    in-flight work has drained. Everything runs on the event loop thread, so no locking is
    needed, and `release` is synchronous so it stays safe to call from a `finally` while the
    task is being cancelled.

    One gate per bulk run; create it inside the running event loop.
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        op_key: Optional[str] = None,
        on_ceiling_changed: Optional[Callable[[int], None]] = None,
    ):
        config = config or {}
        total, available = system_memory() or (0, 0)
        self.total_memory = total
        self.memory_limit = configured_memory_limit_bytes(config, total)
        self.reserve = memory_reserve(total) if total else 0
        # A user-set ceiling the gate must never grow past, even after re-measuring
        self.configured_max = int(config.get("max_concurrent_requests", 0) or 0)

        # What this particular op cost last time it ran, if we've measured it before
        stored = load_per_task_estimates().get(op_key) if op_key else None
        self.estimator = MemoryEstimator(
            op_key, stored if isinstance(stored, (int, float)) else None
        )

        self.limit, self.max_limit, self.adaptive = concurrency_limits(
            config, self.estimator.estimate
        )
        # The ceiling as free memory last implied it. Kept beside max_limit so the adapt loop
        # can tell a ceiling that has genuinely moved from one that has not: it re-reads the
        # budget every tick, and comparing against what it last computed rather than against
        # max_limit itself means a re-read that agrees with the last one leaves the ceiling
        # exactly as it found it. This is what stops the run being decided by the single
        # instant the gate was constructed - four runs of the same work on the same tablet got
        # ceilings of 4, 6, 20 and 69 purely from what happened to be free at launch.
        self._ceiling_from_budget = self.max_limit

        # Told when the ceiling rises. The connection pool is sized from the ceiling and is
        # created before the op has been measured, so without this it keeps the size that came
        # from the starting guess: every request past it then pays a fresh TCP and TLS
        # handshake, and urllib3 logs a "connection pool is full" warning for each one.
        # Not told when it falls - see _apply_estimate. Resizing costs every idle connection
        # in the pool, and a pool larger than the ceiling costs nothing.
        self.on_ceiling_changed = on_ceiling_changed

        self.in_flight = 0
        self.available_memory = available
        self.collection_use = 0.0
        # The highest limit the collection has shown it can serve this run, once it has shown
        # anything. None until the collection first reads saturated; see _adapt_once.
        self.collection_ceiling: Optional[int] = None
        # The same shape for memory: the limit that was in force when the machine last crossed
        # a memory line, so recovery walks back one slot per tick instead of sprinting to a
        # ceiling that is known to trip it. None until pressure is first seen.
        self.pressure_ceiling: Optional[int] = None
        # (limit left by the last cut, free memory read at that moment). The evidence for
        # whether cutting the limit does anything to the reading that triggered it; None
        # between pressure episodes. See _respond_to_pressure.
        self._pressure_probe: Optional[tuple[int, int]] = None
        self._pressure_held = False
        self._waiters: deque[asyncio.Future] = deque()
        self._adapt_task: Optional[asyncio.Task] = None
        self._aborted = False

        logger.debug(
            "ConcurrencyGate(%r): limit=%d max=%d adaptive=%s per_task=%s (%s)"
            " total_mem=%s avail_mem=%s reserve=%s hard_cap=%s",
            op_key,
            self.limit,
            self.max_limit,
            self.adaptive,
            format_bytes(int(self.estimator.estimate)),
            "measured previously" if self.estimator.from_measurement else "default guess",
            format_bytes(total or None),
            format_bytes(available or None),
            format_bytes(self.reserve or None),
            format_bytes(self.memory_limit or None),
        )

    def abort(self) -> None:
        """Stop letting anything through, and release everything queued up.

        Used on cancellation: rather than relying on each waiting task being cancelled
        individually, the gate turns every pending and future acquire into a CancelledError at
        once, so a run with hundreds of queued tasks unwinds in one step.
        """
        self._aborted = True
        queued = len(self._waiters)
        while self._waiters:
            future = self._waiters.popleft()
            if not future.done():
                future.cancel()
        logger.debug(
            "Gate abort: released %d queued, %d still holding a slot",
            queued,
            self.in_flight,
        )

    async def acquire(self) -> None:
        """Wait for a free slot. Raises CancelledError if the task is cancelled while waiting."""
        if self._aborted:
            raise asyncio.CancelledError("concurrency gate aborted")
        while self.in_flight >= self.limit:
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters.append(future)
            try:
                await future
            except asyncio.CancelledError:
                try:
                    self._waiters.remove(future)
                except ValueError:
                    # Already popped: we were handed a slot just as we got cancelled, so pass
                    # the wakeup on rather than losing it
                    if future.done() and not future.cancelled():
                        self._wake_waiters(1)
                raise
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight -= 1
        self._wake_waiters(1)

    def _wake_waiters(self, count: int) -> None:
        """Wake up to `count` waiters that are still waiting."""
        woken = 0
        while self._waiters and woken < count:
            future = self._waiters.popleft()
            if not future.done():
                future.set_result(None)
                woken += 1

    def start_adapting(self) -> None:
        """Begin watching memory and resizing the limit. No-op when not adaptive.

        Measuring does not start here. It needs the run's task count, which the drivers only
        know once they have planned the notes - and the planning pass itself is a long
        allocation-heavy stretch that would be traced at ~10x while contributing no samples at
        all, because nothing reports a live task count until tasks exist. begin_measuring is
        the second half of this, called once planning is done.
        """
        # This run's collection cost, not the previous run's
        collection_pressure.reset()
        if not self.adaptive and not self.memory_limit:
            return
        if self._adapt_task and not self._adapt_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._adapt_task = loop.create_task(self._adapt_loop())

    def begin_measuring(self, planned_tasks: int) -> None:
        """Start measuring what a task costs, if this run can act on the answer.

        Called by the drivers after planning, with the number of tasks the run will create.
        Both halves of the question need that number: a run too short to move the live count
        cannot be fitted, and a run whose fit could not move the ceiling has nothing to learn.
        See tracing_would_pay, which is where the whole decision lives.

        Skipping costs the run nothing it was going to get. The stored estimate still sizes
        the ceiling, the adapt loop still grows and backs off against real memory, and the
        pressure response - which is what actually protects the machine - is untouched.
        """
        if not self.adaptive:
            # Without a memory budget the limit is static whatever a task turns out to cost,
            # so a figure nothing will read is a cost with no return.
            logger.debug("Not measuring %r: the limit is static", self.estimator.op_key)
            return
        worth_it, why = tracing_would_pay(
            self.estimator.estimate, planned_tasks, self.configured_max
        )
        if not worth_it:
            logger.debug("Not measuring %r: %s", self.estimator.op_key, why)
            return
        logger.debug("Measuring %r: %s", self.estimator.op_key, why)
        self.estimator.start()

    def stop_adapting(self) -> None:
        if self._adapt_task:
            self._adapt_task.cancel()
            self._adapt_task = None

    def finish(self) -> None:
        """End of the run: stop adapting and remember what this op cost."""
        self.stop_adapting()
        # One last fit while the samples are still the ones the run ended on. A run shorter
        # than an adapt tick would otherwise finish having never fitted at all.
        self._apply_estimate()
        self.estimator.stop()
        self.estimator.persist()

    def note_live_tasks(self, count: float) -> None:
        """Called by the drivers with how many of the op's API tasks are alive right now.

        The gate only ever sees the ones holding a slot, and they are a quarter of the tasks
        alive - the rest are queued on acquire(), holding their note and prompt meanwhile. The
        estimate is per live task, so it needs the whole count or it charges the queue's memory
        to the running tasks.

        Only the API tasks, though: an op that also creates bookkeeping tasks to write its
        results back would otherwise spread the memory over more tasks than are holding any.
        """
        self.estimator.note_live_tasks(count)

    def _apply_estimate(self) -> None:
        """Refit what a task costs, and move the ceiling if the answer changed."""
        if self.estimator.refit() is None:
            return
        # Written out as soon as it is known rather than only from finish(). A run that is
        # killed - which on a slow machine is how the long ones tend to end - used to throw
        # its measurement away and leave the next run starting from the stale stored value,
        # so the machine that most needs to know what this op costs was the one that never
        # learned it. Measuring stops at MEASURE_SECONDS, so this writes a handful of times
        # at the very start of a run and never again.
        self.estimator.persist()
        if not self.adaptive:
            # Nothing to adapt against; we still learn the cost for next time
            return
        new_max = max_concurrency_for(self.estimator.estimate, self.configured_max)
        self._set_max_limit(
            new_max, f"per-task memory now {format_bytes(int(self.estimator.estimate))}"
        )

    def _refresh_ceiling(self) -> None:
        """Re-read what free memory allows, and move the ceiling if it has really changed.

        `max_limit` used to be computed once, inside `__init__`, from the free memory of that
        instant, and then only ever revisited by `_apply_estimate` - which stops having
        anything to say once measuring stops at MEASURE_SECONDS. One measured 72-minute run
        contains exactly one ceiling change, at 29 seconds. That makes the whole run a
        function of what happened to be free at launch: four runs of the same work on the same
        tablet were given ceilings of 4, 6, 20 and 69.

        The budget is one syscall the adapt tick has already made, so re-reading it is free.
        Comparing the new figure against the last one this computed - rather than against
        `max_limit` - is deliberate: it makes this a detector of change in the machine rather
        than an overwriter of a ceiling something else set.
        """
        if not self.adaptive:
            return
        if memory_budget() is None:
            # No reading this tick. The ceiling budgeted from the last real one still stands;
            # a probe that has started failing must not be read as a machine that has filled.
            return
        new_max = max_concurrency_for(self.estimator.estimate, self.configured_max)
        moved = abs(new_max - self._ceiling_from_budget)
        if moved == 0 or moved < self._ceiling_from_budget * CEILING_HYSTERESIS:
            return
        self._ceiling_from_budget = new_max
        self._set_max_limit(new_max, f"free memory now {format_bytes(self.available_memory)}")

    def _set_max_limit(self, new_max: int, why: str) -> None:
        """Move the ceiling, and the limit with it when the ceiling has come down past it."""
        if new_max == self.max_limit:
            return
        logger.debug(
            "%s, adjusting concurrency ceiling %d -> %d",
            why[:1].upper() + why[1:],
            self.max_limit,
            new_max,
        )
        previous_max = self.max_limit
        self.max_limit = new_max
        self._ceiling_from_budget = new_max
        if self.limit > self.max_limit:
            self.limit = self.max_limit
        if self.on_ceiling_changed and new_max > previous_max:
            # Only upward. Resizing the pool drops every session and with it every idle
            # keep-alive connection, so the next wave of requests each pay a fresh TCP and TLS
            # handshake - which is the cost this callback exists to avoid, not to cause. It is
            # worth paying to make room the run is about to use; it buys nothing on the way
            # down, where a pool larger than the ceiling is simply a pool that will not fill.
            self.on_ceiling_changed(new_max)
        # A raised ceiling isn't applied at once: the adapt loop grows the limit towards it
        # only while memory stays comfortable.

    async def _adapt_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(ADAPT_INTERVAL_SECONDS)
                await self._adapt_once()
        except asyncio.CancelledError:
            pass

    async def _adapt_once(self) -> None:
        # Sampled every tick both for the hard cap and to learn what a task costs. Read on the
        # loop: psutil is one syscall each - GlobalMemoryStatusEx, host_statistics64 or
        # /proc/meminfo, then GetProcessMemoryInfo, task_info or /proc/self/statm - where the
        # probes this replaced spawned two subprocesses per tick on macOS and so needed a
        # thread of their own to keep off the loop that polls for cancellation.
        memory, rss = system_memory(), process_memory()
        available = memory[1] if memory else None
        self.available_memory = available or 0
        self.estimator.sample(rss, self.in_flight)
        self._apply_estimate()

        # The trace table comes off before the hard cap is judged, for the same reason it comes
        # off the estimator's series but a different argument. It is real memory the process is
        # really using; what it is not is memory the limit can do anything about. Halving
        # concurrency does not shrink a trace table, so leaving it in would make the controller
        # respond to a cost its one actuator cannot touch - and would spend a user's configured
        # memory_limit on the apparatus measuring it. tracing_would_pay keeps the table small
        # or absent in the first place; this is what makes the reading right regardless.
        if rss is not None:
            rss = max(0, rss - tracing_overhead())

        # Sampled every tick whether or not it is read below, so the window it covers is one
        # tick rather than however long it has been since the gate last considered growing
        collection = collection_pressure.sample()
        self.collection_use = collection[0] if collection else 0.0

        # One line per tick, unconditionally. Everything else here logs only when it acts, and
        # the two states worth measuring are both states in which nothing acts: a gate pinned
        # at its ceiling stops assessing the collection, and a gate pinned at MIN_CONCURRENCY
        # under pressure stops moving the limit. Both fall silent, and a silence in the
        # raising/lowering stream reads as calm - one measured run's worst stall was 23
        # minutes and appears in that stream as a gap between timestamps. So the collection
        # share, which is sampled every tick anyway and otherwise reaches only the progress
        # dialog, has never been in a log across nine rounds of reading them.
        logger.debug(
            "Gate tick: %d/%d in flight, ceiling %d, collection %.0f%% (%.3fs per turn),"
            " avail=%s rss=%s, caps collection=%s memory=%s",
            self.in_flight,
            self.limit,
            self.max_limit,
            100 * self.collection_use,
            collection[1] if collection else 0.0,
            format_bytes(available),
            format_bytes(rss),
            self.collection_ceiling,
            self.pressure_ceiling,
        )

        rss_over_limit = bool(rss is not None and self.memory_limit and rss > self.memory_limit)
        available_short = bool(
            available is not None and self.reserve and available < self.reserve
        )

        if rss_over_limit or available_short:
            self._respond_to_pressure(available, rss, owned=rss_over_limit)
            return

        # Out of the episode. The next one measures its own response from scratch rather than
        # against a reading from whenever the last one happened to end.
        self._pressure_probe = None
        self._pressure_held = False

        # Only once the machine is comfortable: under pressure the response below owns the
        # limit, and a budget of nearly nothing would otherwise drop the ceiling onto the
        # limit before the halving had been judged.
        self._refresh_ceiling()

        # Only grow when the gate itself is the bottleneck; if tasks aren't queueing up, a
        # bigger limit wouldn't be used anyway.
        if self.adaptive and self.in_flight >= self.limit:
            if collection is not None and collection[0] >= COLLECTION_SATURATED:
                # Every slot is taken, but by tasks queueing for a resource that serves one at
                # a time. Raising the limit here adds waiters, not work: throughput is already
                # pinned at one turn per `mean hold`, and the extra tasks only lengthen the
                # queue each of them has to wait through. Left where it is, the limit settles
                # at the point where the collection became the constraint, which is as far as
                # concurrency is worth taking for this op.
                #
                # So it *latches* rather than vetoing this one tick. The sample covers two
                # seconds and a few turns, so it is noisy, and a run where the collection is
                # the constraint throughout still reads below the threshold on some ticks -
                # sixteen of them in one measured run. Growth is geometric, and 1.25 ** 16 is
                # 35x: those sixteen ticks alone took the limit from 16 to its 512 backstop in
                # 86 seconds, past a collection that had been saturated for 80% of the run.
                # Vetoing four ticks in five caps nothing while the fifth compounds.
                if self.collection_ceiling != self.limit:
                    logger.debug(
                        "Collection busy %.0f%% of the last window (%.2fs per turn), holding"
                        " concurrency at %d rather than growing towards %d",
                        100 * collection[0],
                        collection[1],
                        self.limit,
                        self.max_limit,
                    )
                self.collection_ceiling = self.limit
                return
            if self.collection_ceiling is not None:
                # Recovering from a latch is one slot per tick where growing is geometric.
                # The collection reading idle for a tick is what the noise looks like, so it
                # cannot be allowed to buy back a quarter of the limit; a collection that has
                # genuinely stopped being the constraint - the run's searches replaced by an
                # index, say - keeps reading idle, and half a minute of that is 15 slots.
                self.collection_ceiling += 1
            if self.pressure_ceiling is not None:
                # The same shape, for the same reason. Geometric recovery from 1 against
                # geometric collapse from the ceiling is a stable and useless cycle: one
                # measured run climbed 1 -> 151 in forty seconds and fell back in twelve, so
                # it got forty useful seconds per cycle and then waited minutes for some other
                # process to free memory. Walking back one slot at a time means the ceiling
                # that tripped the machine is approached rather than jumped to.
                self.pressure_ceiling += 1
            ceiling = self.max_limit
            if self.collection_ceiling is not None:
                ceiling = min(ceiling, self.collection_ceiling)
            if self.pressure_ceiling is not None:
                ceiling = min(ceiling, self.pressure_ceiling)
            if self.limit >= ceiling:
                return
            previous = self.limit
            self.limit = min(ceiling, self.limit + max(1, int(self.limit * GROWTH_RATE)))
            self._wake_waiters(self.limit - self.in_flight)
            logger.debug(
                "Memory comfortable (avail=%s), raising concurrency %d -> %d (ceiling %d)",
                format_bytes(available),
                previous,
                self.limit,
                ceiling,
            )

    def _respond_to_pressure(
        self, available: Optional[int], rss: Optional[int], owned: bool
    ) -> None:
        """Back off from a memory reading, but only as far as backing off demonstrably helps.

        There are two pressure readings and the limit only owns one of them. `rss >
        memory_limit` is the run's own memory against a cap the user set, and halving the
        limit is exactly the right response: fewer tasks, less of the memory those tasks
        allocated. `available < reserve` is the *machine's* free memory, and on a machine
        where the run is not what is consuming it, halving is a control loop whose actuator
        does not move its input. One measured tablet run halved 32 times, walked 261 -> 1 in
        fourteen seconds, and then sat at a limit of 1 for 86.4% of its wall clock; the
        estimator, asked what a task cost, had answered by clamping up from a fitted 480 KB,
        which is the fit's way of saying it can find no per-task cost at all. Nineteen
        straight minutes at limit 1 recovered 0.9 GB of a 7.0 GB RSS, and the ramp back out
        took the limit 1 -> 151 with RSS flat. RSS was not a function of in_flight, so the one
        quantity the controller can move was not the one it was reading.

        So on that arm the first cut is made and then *measured*: the next cut waits until the
        previous one has actually drained, and then until free memory has actually risen by
        it. Neither test passing means the machine's memory is not the run's to give back, and
        the run is left where it is rather than being walked down to a limit of 1 - which on
        work that is mostly parked on a socket is not a memory saving, it is a stall.

        The floor differs for the same reason. `MIN_AUTO_CONCURRENCY` is what
        `max_concurrency_for` already treats as the least concurrency worth sizing an op for,
        and the machine arm has no business driving the run four times below it. The arm that
        the limit does own keeps `MIN_CONCURRENCY`: there, cutting to 1 is a real remedy.
        """
        held = None if owned else self._why_not_cut_again(available)
        if held is not None:
            if not self._pressure_held:
                # Once per episode: the reason does not change from tick to tick, and this is
                # the stretch that can last for tens of minutes.
                self._pressure_held = True
                logger.debug(
                    "Memory pressure persists (avail=%s rss=%s) but %s; holding concurrency"
                    " at %d rather than cutting again",
                    format_bytes(available),
                    format_bytes(rss),
                    held,
                    self.limit,
                )
            return

        floor = MIN_CONCURRENCY if owned else MIN_AUTO_CONCURRENCY
        # min() with the current limit because the floor is a floor and not a target: a gate
        # already below it must not be raised by the pressure response of all things.
        new_limit = min(self.limit, max(floor, self.limit // 2))
        if new_limit != self.limit:
            logger.debug(
                "Memory pressure (avail=%s rss=%s, %s), lowering concurrency %d -> %d",
                format_bytes(available),
                format_bytes(rss),
                "the run's own" if owned else "the machine's",
                self.limit,
                new_limit,
            )
            # Tasks already running keep their slot; the lower limit takes effect as they
            # finish and waiting tasks stay blocked until enough have drained.
            self.limit = new_limit
        # Recovery starts from where the cut left the run, not from the limit that tripped the
        # machine: latching at the latter lets geometric growth put the run straight back onto
        # it in three ticks, which is the sawtooth with a smaller amplitude rather than no
        # sawtooth. Walked up one slot per comfortable tick in _adapt_once, so the tripping
        # limit is approached over half a minute and the next episode re-latches below it.
        self.pressure_ceiling = (
            new_limit if self.pressure_ceiling is None else min(self.pressure_ceiling, new_limit)
        )
        self._pressure_probe = (self.limit, available if available is not None else 0)
        self._pressure_held = False

    def _why_not_cut_again(self, available: Optional[int]) -> Optional[str]:
        """Why this tick must not halve the limit again, or None if it may.

        Two gates, in the order a control loop has to apply them: the actuator has to have
        moved before its effect can be read, and then the effect has to be there.
        """
        if self._pressure_probe is None:
            return None
        _, available_at_cut = self._pressure_probe
        if self.in_flight > self.limit:
            # Lowering the limit does not evict anything; it stops new acquires. Until the
            # tasks holding the extra slots have finished, the machine has not yet been asked
            # the question, let alone answered it. This alone is most of the sawtooth: eight
            # halvings in fourteen seconds is eight cuts made before the first one landed.
            return f"{self.in_flight} tasks are still draining to a limit of {self.limit}"
        if available is None:
            return None
        gained = available - available_at_cut
        if gained >= PRESSURE_RESPONSE_MIN_GAIN:
            return None
        return (
            f"the last cut freed nothing the run owns (avail"
            f" {format_bytes(available_at_cut)} -> {format_bytes(available)})"
        )

    def status_text(self) -> str:
        """Short description of the gate's state, for the progress dialog."""
        text = f"{self.in_flight}/{self.limit}"
        if self.available_memory:
            text += f" | Free memory: {format_bytes(self.available_memory)}"
        if self.estimator.measured:
            text += f" | {format_bytes(int(self.estimator.measured))}/task"
        if self.collection_use:
            text += f" | Collection {self.collection_use:.0%}"
        if self.collection_ceiling is not None:
            text += f" (cap {self.collection_ceiling})"
        if self.pressure_ceiling is not None:
            text += f" | Memory cap {self.pressure_ceiling}"
        return text
