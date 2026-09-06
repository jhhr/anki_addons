"""Reading the collection, from worker threads and from the event loop.

Ops run on a pool of worker threads and several of them search the collection per note. Two
things about that turned out to matter a lot when a run is cancelled.

The first is that Anki's backend serialises collection access behind one lock, so sixty threads
calling find_notes do not run sixty searches at once - they queue, and each waits out all the
ones ahead of it. Running them concurrently buys nothing.

The second is what that does to cancelling. A search already inside the backend cannot be
interrupted by anything: not cancelling the task, not raising in the thread, not any signal
Python offers. So whatever has been handed to the backend when the cancel arrives has to be
waited out - and the operation's own final write has to queue up behind all of it. Measured on
a cancelled make-meanings run, that was 46 whole-collection regex searches ahead of a one-note
write, and the write took 121 seconds to happen.

So collection access goes through here. Only one call is inside the backend at a time, and the
rest wait in a queue that cancelling empties instantly. That leaves exactly one search to wait
out instead of all of them, and it costs nothing during a normal run because those searches
were already taking turns.

That queue used to be a semaphore every caller blocked on, which made waiting cost a thread.
It does not any more: one worker thread owns the collection and everyone submits work to it.
The difference is what waiting costs the rest of the run.

- A thread waiting on a semaphore is a thread. Callers reach here through asyncio.to_thread,
  so a run with hundreds of tasks queueing for the collection had hundreds of pool threads
  parked in `acquire`, doing nothing but holding stacks. A cancelled run measured 301 addon
  worker threads, 201 of them exactly there.
- Work that needs the collection and nothing else no longer needs a thread at all. Such a
  caller can await `find_notes_async` from the event loop and hold a coroutine while it waits
  instead of a thread, without blocking the loop - which is what the to_thread around those
  calls was there to prevent, at the cost of the thread.

Both paths queue on the same worker, so the serialisation the module exists for is unchanged,
and so is what a caller sees: one call inside the backend at a time, abandoned instantly on
cancellation except for the one already running. Cancelling is in fact cleaner - the worker
fails queued jobs as it reaches them rather than each caller noticing on its next 50ms poll.

Ops should use these instead of calling mw.col directly, so that cancellation keeps working
without every op having to think about it.
"""

import asyncio
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Sequence, TypeVar

from aqt import mw

from .api_client import Run, current_run, run_is_cancelled
from .concurrency import collection_pressure

if TYPE_CHECKING:
    from anki.notes import Note, NoteId

logger = logging.getLogger(__name__)

T = TypeVar("T")

# One at a time. The backend enforces this anyway; the point of doing it here is that our
# queue can be abandoned and the backend's cannot. It is now the count of worker threads, so
# the serialisation and the thread that provides it are the same fact.
COLLECTION_CONCURRENCY = 1

# Refusing on cancellation is right for the ops and wrong for the cleanup phase, which runs
# after a cancel precisely so the work already done gets saved. Some ops still have collection
# work to do there - resolving the ids of notes they added, for one - so the thread running
# cleanup is exempt. Read on the submitting thread and carried on the job, because the thread
# that ends up running it is the shared worker, which is nobody's cleanup thread.
_exempt = threading.local()

# The run is carried on the job for the same reason, and it is the more important of the two.
# Which run a thread belongs to is also thread-local, and the worker belongs to none: it is
# started once and outlives every run, so it is never enrolled in one. Asking it whether "the
# run" was cancelled therefore always answered no, and a cancelled run's queue was worked
# through to the end instead of being dropped - the exact thing this module exists to prevent.
# So the submitting thread's run rides along with the job and the worker judges the job by it.


def begin_cleanup_phase() -> None:
    """Let this thread keep reading the collection even though the run was cancelled.

    Paired with end_cleanup_phase in a finally: the thread running this is reused for later
    operations, so leaving it exempt would quietly disarm cancellation for the next one.
    """
    _exempt.on = True


def end_cleanup_phase() -> None:
    _exempt.on = False


def _giving_up(run: "Optional[Run]", exempt: bool) -> bool:
    return run_is_cancelled(run) and not exempt


class RunCancelled(Exception):
    """Raised in a caller that gave up because the run was cancelled.

    Ops do not need to catch this. It unwinds the op, the task treats it as an unsuccessful
    result, and the run carries on to save what it already had.
    """


class _Job:
    """One turn with the collection, waiting to be taken.

    Carries its own answer back rather than the caller polling for it, and carries the
    submitting thread's run and cleanup exemption, neither of which the worker can know.
    """

    __slots__ = ("what", "fn", "run", "exempt", "loop", "future", "done", "value", "error")

    def __init__(
        self,
        what: str,
        fn: "Callable[[], Any]",
        loop: "Optional[asyncio.AbstractEventLoop]" = None,
        future: "Optional[asyncio.Future]" = None,
    ):
        self.what = what
        self.fn = fn
        self.run = current_run()
        self.exempt = bool(getattr(_exempt, "on", False))
        self.loop = loop
        self.future = future
        # Only the blocking path waits on this, and an Event is a Condition and an RLock, so
        # the awaiting path - which resolves through its future and never touches it - does not
        # allocate one. Decided here rather than lazily on first use: the worker settles a job
        # the instant it is queued, so anything built on demand would be built in a race.
        self.done = threading.Event() if future is None else None
        self.value: Any = None
        self.error: "Optional[BaseException]" = None

    def settle(self, value: Any, error: "Optional[BaseException]") -> None:
        if self.future is not None and self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self._settle_future, value, error)
            except RuntimeError:
                # The loop this was awaited on is closed. An op's teardown closes its loop
                # while jobs it submitted can still be queued here, and by then there is
                # nothing left awaiting this one - the task went with the loop. Dropping the
                # answer is the whole of the handling. What matters is that this does not
                # escape into the worker, which is shared and not restarted while it is still
                # alive: killing it strands every job queued behind this one, and their
                # callers block in result() forever.
                logger.debug("Dropped the answer to %s, its event loop is closed", self.what)
            return
        self.value, self.error = value, error
        if self.done is not None:
            self.done.set()

    def _settle_future(self, value: Any, error: "Optional[BaseException]") -> None:
        # The awaiting task may have been cancelled while this was in flight
        if self.future is None or self.future.done():
            return
        if error is not None:
            self.future.set_exception(error)
        else:
            self.future.set_result(value)

    def result(self) -> Any:
        """Block until the worker has run this. Only for callers not on the event loop."""
        if self.done is None:
            raise RuntimeError("result() on a job that was submitted to be awaited")
        self.done.wait()
        if self.error is not None:
            raise self.error
        return self.value


_jobs: "queue.SimpleQueue[_Job]" = queue.SimpleQueue()
_worker: "Optional[threading.Thread]" = None
_worker_lock = threading.Lock()


def _run_jobs() -> None:
    while True:
        try:
            _run_one_job(_jobs.get())
        except BaseException:  # noqa: BLE001 - this thread must outlive any one job
            # Nothing should reach here: a job's own failure is handed to whoever is waiting
            # for it. But this thread is the collection for the whole process and nothing
            # restarts it while it is alive, so anything that did escape would strand every
            # job queued after it. A log is a better outcome than a dead worker.
            logger.exception("Collection worker survived an unexpected failure")


def _run_one_job(job: _Job) -> None:
    if _giving_up(job.run, job.exempt):
        # Drained rather than run. Nothing here has touched the backend yet, so a
        # cancelled run empties its whole queue in the time it takes to pop it, and only
        # the job already running has to be waited out.
        job.settle(None, RunCancelled(job.what))
        return
    if job.loop is not None and job.loop.is_closed():
        # Nobody is waiting for this any more, so spending a turn with the collection on it
        # would only delay the jobs that still have callers.
        logger.debug("Skipped %s, its event loop is closed", job.what)
        return
    started = time.perf_counter()
    value: Any = None
    error: "Optional[BaseException]" = None
    try:
        value = job.fn()
    except BaseException as raised:  # noqa: BLE001 - handed to whoever is waiting
        error = raised
    # Recorded before the caller is woken, so a turn is always accounted for by the time the
    # caller can observe anything. Only the holding is counted, and only here: this thread is
    # the collection, so the time it spends running jobs is exactly the time the collection is
    # occupied. What the queue behind it costs is the thing the gate uses this to avoid
    # causing. Nothing waits on this - the next job cannot start until the worker comes round
    # again regardless - so it is off the critical path either way.
    collection_pressure.record(time.perf_counter() - started)
    job.settle(value, error)


def _worker_thread() -> threading.Thread:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_run_jobs, name="collection-access", daemon=True
            )
            _worker.start()
        return _worker


def _on_worker() -> bool:
    """Whether this is the collection thread itself.

    A job that asks for the collection from inside another job must not queue behind itself:
    with one worker that is a deadlock rather than a slowdown, and it is an easy thing for an
    op to do by accident - fetching notes inside a loop that already holds a turn. Being here
    already means holding it, so the nested call just runs.
    """
    return threading.current_thread() is _worker


def _run_on_collection(what: str, fn: "Callable[[], T]") -> T:
    """Take the collection for one call, or give up if the run was cancelled.

    Blocks the calling thread. Callers on the event loop want run_on_collection_async instead.
    """
    if _on_worker():
        return fn()
    if _giving_up(current_run(), bool(getattr(_exempt, "on", False))):
        raise RunCancelled(what)
    job = _Job(what, fn)
    _worker_thread()
    _jobs.put(job)
    return job.result()


async def run_on_collection_async(what: str, fn: "Callable[[], T]") -> T:
    """Take the collection for one call, from the event loop, without blocking it.

    The wait costs a coroutine rather than a pool thread, which is the whole point: work that
    needs the collection and nothing else no longer has to be handed to a thread purely so
    that waiting for a turn happens somewhere the loop is not.
    """
    if _on_worker():
        return fn()
    if _giving_up(current_run(), bool(getattr(_exempt, "on", False))):
        raise RunCancelled(what)
    loop = asyncio.get_running_loop()
    future: "asyncio.Future" = loop.create_future()
    _worker_thread()
    _jobs.put(_Job(what, fn, loop=loop, future=future))
    return await future


def find_notes(query: str) -> "Sequence[NoteId]":
    """mw.col.find_notes, serialised and abandonable."""
    return _run_on_collection(f"find_notes: {query}", lambda: mw.col.find_notes(query))


async def find_notes_async(query: str) -> "Sequence[NoteId]":
    """find_notes for a caller on the event loop."""
    return await run_on_collection_async(
        f"find_notes: {query}", lambda: mw.col.find_notes(query)
    )


def get_note(note_id: "NoteId") -> "Note":
    """mw.col.get_note, serialised and abandonable."""
    return _run_on_collection("get_note", lambda: mw.col.get_note(note_id))


def _fetch_notes(ids: "list[NoteId]", run: "Optional[Run]", exempt: bool) -> "list[Note]":
    notes: "list[Note]" = []
    for note_id in ids:
        # One turn with the collection, but a bounded one. A broad search hands this thousands
        # of ids, and a batch that runs to the end no matter what is exactly the stretch of
        # uninterruptible backend work this module exists to avoid: only the fetch already
        # inside the backend has to be waited out, not all the rest.
        if _giving_up(run, exempt):
            raise RunCancelled(f"get_notes: gave up after {len(notes)} of {len(ids)} notes")
        notes.append(mw.col.get_note(note_id))
    return notes


def get_notes(note_ids: "Iterable[NoteId]") -> "list[Note]":
    """Fetch several notes under a single turn with the collection.

    Taking and releasing per note would let every other waiting caller in between, turning one
    stretch of work into a long interleaved one.
    """
    ids = list(note_ids)
    if not ids:
        return []
    run, exempt = current_run(), bool(getattr(_exempt, "on", False))
    return _run_on_collection(
        f"get_notes: {len(ids)} notes", lambda: _fetch_notes(ids, run, exempt)
    )


async def get_notes_async(note_ids: "Iterable[NoteId]") -> "list[Note]":
    """get_notes for a caller on the event loop."""
    ids = list(note_ids)
    if not ids:
        return []
    run, exempt = current_run(), bool(getattr(_exempt, "on", False))
    return await run_on_collection_async(
        f"get_notes: {len(ids)} notes", lambda: _fetch_notes(ids, run, exempt)
    )
