"""The capture store: a record of every AI call, in one SQLite file written by one thread.

`get_response` runs in hundreds of pool threads at once during a bulk run, and on the main
thread from the editor's hooks, so recording a call must cost a caller no more than hashing, a
dict copy and a `put_nowait`. Every public method but the constructor, `flush` and `close` is
that and nothing else: no sqlite call, no lock held across I/O, no waiting, no raising. One
daemon thread owns the connection that writes, drains the queue and commits in batches. The
constructor's short connection on the caller's thread, closed before the writer starts, is
the only other one.

Run and call ids are handed out here, from counters seeded with `max(...)+1` when the store
opens, so a caller has its id at once (for its log lines and its calls' rows) instead of
waiting on the writer. That is sound only because one process writes the file, the Anki
profile that opened it; readers (research scripts, a DB browser) are fine under WAL.

The store is diagnostics and never fails an op. A file it cannot open, a schema newer than
this code, a batch that cannot be written: each is logged with the path and turns the store
off for the rest of the session, and every later record is dropped. A full queue drops the
record. None of it reaches the caller. A file of an older schema is brought up to this one
when it opens (`_MIGRATIONS`); its old rows keep NULL in the columns added since.

Free of aqt and anki, and imports nothing of the addon's, so tests and research scripts load
it on its own.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any, Callable, Optional, Union

logger = logging.getLogger(__name__)

# 2: calls.context_json
SCHEMA_VERSION = 2

# Not diagnostics.WORKER_THREAD_PREFIX: call_logging waits for every thread with that prefix
# to end before it closes a run's log file, and this one lives as long as the profile
WRITER_THREAD_NAME = "jnaio_capture_writer"

# The constructor runs on Anki's main thread at profile open, so it waits only briefly for a
# lock another connection holds; the writer's thread can afford sqlite's usual wait
OPEN_TIMEOUT_SECONDS = 1.0
WRITE_TIMEOUT_SECONDS = 5.0

# A full queue or a refused row tends to repeat for every record once it starts; one line a
# minute says so without burying the log
WARNING_INTERVAL_SECONDS = 60.0

# How often a flush waiting on the writer checks that it is still alive, so a writer that
# stopped on a failed batch does not hold the caller for the whole timeout
_POLL_SECONDS = 0.05

DAY_SECONDS = 86400.0


# --- keys --------------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """The one JSON text of a value: keys sorted, no spaces, non-ASCII as is, others as str().

    The keys and blob hashes are sha1s of it, so equal values must give equal text whatever
    their dict order. A dict whose keys are of mixed types cannot be sorted and raises
    TypeError; the store's own callers of it catch that.
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def text_hash(text: str) -> str:
    """sha1 hex of the text's UTF-8.

    `surrogatepass` lets a lone surrogate (a decoded answer can hold one) hash instead of
    raising; valid text hashes exactly as a plain `.encode("utf-8")` does, which is what the
    research scripts' keys use.
    """
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()


def request_key(kind: str, inputs: Any) -> str:
    """What was asked: the call's purpose and the values its prompt was built from.

    A reworded prompt leaves it unchanged, which is what lets one request be followed across
    prompt versions.
    """
    return text_hash(kind + "\n" + canonical_json(inputs))


def prompt_key(
    model: str, instructions: Optional[str], prompt: str, schema: Any, params: Any
) -> str:
    """What was sent: equal only for the same model given the same request, byte for byte."""
    return text_hash(canonical_json([model, instructions, prompt, schema, params]))


def research_cache_key(model: str, prompt: str) -> str:
    """The research scripts' answer cache key (`judge_eval.prompt_key` and its siblings)."""
    return text_hash(f"{model}\n{prompt}")


# --- schema ------------------------------------------------------------------------------

RUN_COLUMNS: dict[str, str] = {
    "run_id": "INTEGER PRIMARY KEY",
    "v": "INTEGER NOT NULL",
    "started": "REAL",
    "ended": "REAL",
    "label": "TEXT",
    "implicit": "INTEGER DEFAULT 0",
    "ops_json": "TEXT",
    "chain_step": "TEXT",
    "note_count": "INTEGER",
    "config_json": "TEXT",
    "versions_json": "TEXT",
    "log_path": "TEXT",
    "outcome": "TEXT",
    "extra_json": "TEXT",
}

CALL_COLUMNS: dict[str, str] = {
    "call_id": "INTEGER PRIMARY KEY",
    "v": "INTEGER NOT NULL",
    "run_id": "INTEGER",
    "note_id": "INTEGER",
    "task_id": "TEXT",
    "parent_task_id": "TEXT",
    "kind": "TEXT",
    "request_key": "TEXT",
    "prompt_key": "TEXT",
    "model": "TEXT",
    "params_json": "TEXT",
    "inputs_json": "TEXT",
    "instructions_hash": "TEXT",
    "prompt": "TEXT",
    "schema_hash": "TEXT",
    "response_raw": "TEXT",
    "response_json": "TEXT",
    "outcome": "TEXT",
    "error": "TEXT",
    "started": "REAL",
    "latency_ms": "REAL",
    "attempts": "INTEGER",
    "usage_json": "TEXT",
    "extra_json": "TEXT",
    # Version 2. Last, where the migration's ALTER TABLE puts it, so that a migrated file and a
    # new one have their columns in the same order
    "context_json": "TEXT",
}

BLOB_COLUMNS: dict[str, str] = {"hash": "TEXT PRIMARY KEY", "text": "TEXT"}

_TABLES: dict[str, dict[str, str]] = {
    "runs": RUN_COLUMNS,
    "calls": CALL_COLUMNS,
    "blobs": BLOB_COLUMNS,
}

_INDEXES = (
    ("runs_started", "runs", "started"),
    ("calls_run_id", "calls", "run_id"),
    ("calls_note_id", "calls", "note_id"),
    ("calls_kind", "calls", "kind"),
    ("calls_request_key", "calls", "request_key"),
    ("calls_prompt_key", "calls", "prompt_key"),
    # These two are for the prune alone: finding the blobs no call references without them
    # reads every call row, prompts and answers included, at every profile open
    ("calls_instructions_hash", "calls", "instructions_hash"),
    ("calls_schema_hash", "calls", "schema_hash"),
)


# What brings a file of schema version N to N+1, keyed by N. A new file gets the current schema
# whole from _schema_statements instead
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: ("ALTER TABLE calls ADD COLUMN context_json TEXT",),
}


def _schema_statements() -> list[str]:
    statements = [
        f"CREATE TABLE IF NOT EXISTS {table} ("
        + ", ".join(f"{name} {declaration}" for name, declaration in columns.items())
        + ")"
        for table, columns in _TABLES.items()
    ]
    statements += [
        f"CREATE INDEX IF NOT EXISTS {name} ON {table}({column})"
        for name, table, column in _INDEXES
    ]
    return statements


# --- pruning -----------------------------------------------------------------------------

_PRUNE_RUNS = "DELETE FROM runs WHERE started < ?"
# Through the call_id subquery so that sqlite scans the narrow calls(run_id) index; the plain
# `DELETE ... WHERE run_id NOT IN` scans the table, whose rows carry whole prompts and answers
_PRUNE_CALLS = (
    "DELETE FROM calls WHERE call_id IN (SELECT call_id FROM calls"
    " WHERE run_id NOT IN (SELECT run_id FROM runs))"
)
# NOT EXISTS rather than NOT IN: most calls have no schema, and one NULL in a NOT IN list
# makes it match nothing
_PRUNE_BLOBS = (
    "DELETE FROM blobs"
    " WHERE NOT EXISTS (SELECT 1 FROM calls WHERE calls.instructions_hash = blobs.hash)"
    " AND NOT EXISTS (SELECT 1 FROM calls WHERE calls.schema_hash = blobs.hash)"
)


def prune(
    connection: sqlite3.Connection, *, keep_days: Optional[float], now: float
) -> tuple[int, int, int]:
    """Delete the runs started more than `keep_days` before `now`, the calls whose run is
    gone, then the blobs no call references; return how many of each went.

    Opens no transaction of its own. The store runs it in one on the writer's connection
    before it writes anything, so nothing this session records can be taken, whatever its
    `started`. `keep_days` None, 0 or less deletes nothing: keeping everything is the safe
    reading of a 0 in the config. A call with no `run_id` has no age to go by and stays.
    """
    if keep_days is None or keep_days <= 0:
        return (0, 0, 0)
    runs = connection.execute(_PRUNE_RUNS, (now - keep_days * DAY_SECONDS,)).rowcount
    calls = connection.execute(_PRUNE_CALLS).rowcount
    blobs = connection.execute(_PRUNE_BLOBS).rowcount
    return (runs, calls, blobs)


# --- the store ---------------------------------------------------------------------------

# A row the database refuses on its own (a duplicate id, an int past 64 bits, text with a lone
# surrogate) is that row's fault: skipping it keeps the rest of the session's capture. Any
# other error is the file's or the connection's, and it will not mend mid-session.
_ROW_ERRORS = (
    sqlite3.IntegrityError,
    sqlite3.InterfaceError,
    sqlite3.DataError,
    ValueError,
    TypeError,
    OverflowError,
)


class _Flush:
    """A marker in the queue; the writer sets `done` once the batch holding it is committed
    (`ok` True) or dropped."""

    __slots__ = ("done", "ok")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.ok = False


# Put by close() behind everything queued; the writer writes up to it, then exits
_STOP = object()


def _sql_value(value: Any) -> Any:
    # A dict or list for a *_json column is the expected case; anything else sqlite cannot
    # bind (a tuple, a Path) would otherwise cost the whole row
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    return canonical_json(value)


class CaptureStore:
    """One capture file and its writer thread.

    Records are queued as dicts keyed by column name. The store fills in `v`; a column the
    table does not have is left out, with one warning per column name. `insert_run` and
    `insert_call` assign the row an id from the counter when it has none, and return the id;
    an id the caller gives is used as it is, so it must have come from `new_run_id` or
    `new_call_id`, the counters being all that keeps ids unique. When the store is off every
    recording method still returns what it would have (ids, hashes), so a caller needs no
    second path, but ids from a store that could not open are not unique across sessions.
    """

    def __init__(
        self,
        path: Union[str, os.PathLike[str]],
        *,
        keep_days: Optional[float] = 90.0,
        batch_seconds: float = 0.5,
        batch_size: int = 200,
        max_queue: int = 5000,
    ) -> None:
        self.path = os.fspath(path)
        self._keep_days = keep_days
        self._batch_seconds = max(0.0, batch_seconds)
        self._batch_size = max(1, batch_size)
        # A maxsize of 0 is unbounded, the one thing the bound is there to prevent
        self._max_queue = max(1, max_queue)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._max_queue)
        self._id_lock = threading.Lock()
        self._blob_lock = threading.Lock()
        self._warn_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._known_blobs: set[str] = set()
        self._quiet_until: dict[str, float] = {}
        self._dropped = 0
        # Touched by the writer thread only
        self._unknown_columns: set[tuple[str, str]] = set()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._accepting = False

        first_ids: Optional[tuple[int, int]] = None
        try:
            first_ids = self._open()
        except Exception as e:
            logger.warning(
                "Capture store %s could not be opened (%s: %s); nothing is recorded this session",
                self.path,
                type(e).__name__,
                e,
            )
        first_run, first_call = first_ids or (1, 1)
        self._run_ids: Iterator[int] = itertools.count(first_run)
        self._call_ids: Iterator[int] = itertools.count(first_call)
        if first_ids is not None:
            self._accepting = True
            self._start_writer()

    @property
    def enabled(self) -> bool:
        """Whether records are still taken: False after a failure and after close()."""
        return self._accepting

    # --- recording: callable from any thread, never blocking, never raising ---------------

    def new_run_id(self) -> int:
        with self._id_lock:
            return next(self._run_ids)

    def new_call_id(self) -> int:
        with self._id_lock:
            return next(self._call_ids)

    def insert_run(self, row: Mapping[str, Any]) -> int:
        return self._insert("runs", "run_id", self.new_run_id, row)

    def update_run(self, run_id: Optional[int], **cols: Any) -> None:
        """Set columns of a run inserted earlier; one queue and one writer keep the order."""
        if self._accepting and cols:
            self._offer(("update", "runs", (run_id, cols)))

    def insert_call(self, row: Mapping[str, Any]) -> int:
        return self._insert("calls", "call_id", self.new_call_id, row)

    def put_blob(self, text: Any) -> Optional[str]:
        """Store a text once and return its hash; anything but a str is stored as its
        canonical JSON (a response schema). None only for a value that has no JSON text."""
        try:
            if not isinstance(text, str):
                text = canonical_json(text)
            digest = text_hash(text)
        except Exception as e:
            self._caller_error("put_blob", e)
            return None
        if not self._accepting:
            return digest
        # The set is updated under the same lock as the put, and only when the put succeeds:
        # a blob a full queue dropped must not be taken as stored by the next caller
        with self._blob_lock:
            if digest in self._known_blobs:
                return digest
            try:
                self._queue.put_nowait(("blob", "blobs", {"hash": digest, "text": text}))
            except queue.Full:
                queued = False
            else:
                queued = True
                self._known_blobs.add(digest)
        if not queued:
            self._count_dropped()
        return digest

    def _insert(
        self, table: str, key: str, new_id: Callable[[], int], row: Mapping[str, Any]
    ) -> int:
        try:
            record = dict(row)
        except Exception as e:
            self._caller_error(f"insert into {table}", e)
            return new_id()
        if record.get(key) is None:
            record[key] = new_id()
        record["v"] = SCHEMA_VERSION
        if self._accepting:
            self._offer(("insert", table, record))
        return record[key]

    def _offer(self, item: tuple[str, str, Any]) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._count_dropped()

    def _count_dropped(self) -> None:
        with self._warn_lock:
            self._dropped += 1
            dropped = self._dropped
        self._warn_limited(
            "queue full",
            "Capture store %s: its queue is full (%d records); %d records dropped so far",
            self.path,
            self._max_queue,
            dropped,
        )

    def _caller_error(self, what: str, error: Exception) -> None:
        self._warn_limited(
            what,
            "Capture store %s: %s was given something it cannot record (%s: %s)",
            self.path,
            what,
            type(error).__name__,
            error,
        )

    def _warn_limited(self, key: str, message: str, *args: Any) -> None:
        now = time.monotonic()
        with self._warn_lock:
            if now < self._quiet_until.get(key, float("-inf")):
                return
            self._quiet_until[key] = now + WARNING_INTERVAL_SECONDS
        logger.warning(message, *args)

    # --- waiting on the writer ------------------------------------------------------------

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued before this call is committed.

        False on timeout, when the store is off, or when the batch was dropped. A single row
        the database refused was logged and skipped, and does not make it False.
        """
        if not self._accepting:
            return False
        deadline = time.monotonic() + timeout
        marker = _Flush()
        try:
            self._queue.put(marker, timeout=max(0.0, timeout))
        except queue.Full:
            return False
        while True:
            remaining = deadline - time.monotonic()
            if marker.done.wait(max(0.0, min(remaining, _POLL_SECONDS))):
                return marker.ok
            if remaining <= 0 or not self._writer_alive():
                return marker.done.is_set() and marker.ok

    def close(self, timeout: float = 2.0) -> bool:
        """Stop taking records, let the writer write what is queued, and stop it.

        Returns whether the writer has stopped; if it is still writing after `timeout`, it
        finishes on its own (a daemon thread). Idempotent, and safe from any thread,
        including after the writer died.
        """
        with self._close_lock:
            first = not self._closed
            self._closed = True
            self._accepting = False
        thread = self._thread
        if thread is None:
            return True
        if not first or thread is threading.current_thread():
            return not thread.is_alive()
        deadline = time.monotonic() + timeout
        if thread.is_alive():
            try:
                self._queue.put(_STOP, timeout=max(0.0, timeout))
            except queue.Full:
                pass
            thread.join(max(0.0, deadline - time.monotonic()))
        stopped = not thread.is_alive()
        if not stopped:
            logger.warning(
                "Capture store %s: the writer was still busy after %.1fs; what it holds may be"
                " lost",
                self.path,
                timeout,
            )
        if self._dropped:
            logger.warning(
                "Capture store %s: %d records were dropped at a full queue this session",
                self.path,
                self._dropped,
            )
        return stopped

    def _writer_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- opening (caller's thread) -----------------------------------------------------------

    def _open(self) -> Optional[tuple[int, int]]:
        """Create the directory and schema, or migrate an older file's, turn on WAL, return the
        first free run and call ids; None when the file belongs to a newer version of this code."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(
            self.path, timeout=OPEN_TIMEOUT_SECONDS, isolation_level=None
        )
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                # Read before anything writes: WAL alone would change the file's header
                logger.warning(
                    "Capture store %s has schema version %d, newer than this code's %d; it is"
                    " left untouched and nothing is recorded this session",
                    self.path,
                    version,
                    SCHEMA_VERSION,
                )
                return None
            if version < SCHEMA_VERSION:
                # One transaction: a migration that fails leaves the file as it was, at its old
                # version, and the store off for this session
                connection.execute("BEGIN IMMEDIATE")
                # 0 is a new file, which the statements below create at this version whole;
                # an older file's tables exist, and those statements add nothing to them
                if version:
                    for step in range(version, SCHEMA_VERSION):
                        for statement in _MIGRATIONS[step]:
                            connection.execute(statement)
                for statement in _schema_statements():
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.execute("COMMIT")
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                # Still usable; a reader then holds the writer back while it reads
                logger.warning(
                    "Capture store %s: journal mode is %s, not wal", self.path, mode
                )
            last_run = connection.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
            last_call = connection.execute("SELECT MAX(call_id) FROM calls").fetchone()[0]
            return ((last_run or 0) + 1, (last_call or 0) + 1)
        finally:
            connection.close()

    def _start_writer(self) -> None:
        thread = threading.Thread(
            target=self._write_loop, name=WRITER_THREAD_NAME, daemon=True
        )
        self._thread = thread
        try:
            thread.start()
        except Exception as e:
            self._disable("could not start its writer thread", e)

    def _disable(self, what: str, error: BaseException) -> None:
        self._accepting = False
        logger.warning(
            "Capture store %s %s (%s: %s); nothing more is recorded this session",
            self.path,
            what,
            type(error).__name__,
            error,
        )

    # --- the writer thread ---------------------------------------------------------------

    def _write_loop(self) -> None:
        connection: Optional[sqlite3.Connection] = None
        try:
            # check_same_thread stays on: this connection is the writer's alone
            connection = sqlite3.connect(
                self.path, timeout=WRITE_TIMEOUT_SECONDS, isolation_level=None
            )
            # Under WAL, NORMAL can lose the last commits in a power cut, never the file
            connection.execute("PRAGMA synchronous=NORMAL")
            self._prune_on_start(connection)
            while True:
                batch = self._next_batch()
                if not self._write_batch(connection, batch) or batch[-1] is _STOP:
                    break
        except Exception as e:
            self._disable("stopped writing", e)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._release_leftovers()

    def _prune_on_start(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN")
            counts = prune(connection, keep_days=self._keep_days, now=time.time())
            connection.execute("COMMIT")
        except Exception as e:
            _rollback_quietly(connection)
            # Old rows staying another session is harmless; the writes may still work
            logger.warning(
                "Capture store %s: pruning old records failed (%s: %s)",
                self.path,
                type(e).__name__,
                e,
            )
            return
        if any(counts):
            logger.info(
                "Capture store %s: pruned %d runs, %d calls and %d blobs (kept %s days)",
                self.path,
                *counts,
                self._keep_days,
            )

    def _next_batch(self) -> list[Any]:
        """Block for the first item, then take more until the batch is full, its time is up,
        or a flush or stop ends it early."""
        first = self._queue.get()
        batch = [first]
        if not isinstance(first, tuple):
            return batch
        deadline = time.monotonic() + self._batch_seconds
        while len(batch) < self._batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            batch.append(item)
            if not isinstance(item, tuple):
                break
        return batch

    def _write_batch(self, connection: sqlite3.Connection, batch: list[Any]) -> bool:
        records = [item for item in batch if isinstance(item, tuple)]
        ok = True
        if records:
            try:
                connection.execute("BEGIN")
                for record in records:
                    self._write_record(connection, record)
                connection.execute("COMMIT")
            except Exception as e:
                ok = False
                _rollback_quietly(connection)
                self._disable(f"could not write a batch of {len(records)} records", e)
        for item in batch:
            if isinstance(item, _Flush):
                item.ok = ok
                item.done.set()
        return ok

    def _write_record(
        self, connection: sqlite3.Connection, record: tuple[str, str, Any]
    ) -> None:
        action, table, payload = record
        try:
            if action == "update":
                run_id, values = payload
                columns = self._columns(table, values)
                if columns:
                    connection.execute(
                        f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in columns)}"
                        " WHERE run_id = ?",
                        [_sql_value(values[c]) for c in columns] + [run_id],
                    )
                return
            columns = self._columns(table, payload)
            # A blob another session stored, or one queued twice by racing callers
            verb = "INSERT OR IGNORE" if action == "blob" else "INSERT"
            connection.execute(
                f"{verb} INTO {table} ({', '.join(columns)})"
                f" VALUES ({', '.join(['?'] * len(columns))})",
                [_sql_value(payload[c]) for c in columns],
            )
        except _ROW_ERRORS as e:
            self._warn_limited(
                "refused row",
                "Capture store %s: a %s row was refused and skipped, the rest of its batch"
                " written (%s: %s)",
                self.path,
                table,
                type(e).__name__,
                e,
            )

    def _columns(self, table: str, values: Mapping[str, Any]) -> list[str]:
        known = _TABLES[table]
        columns = []
        for name in values:
            if name in known:
                columns.append(name)
            elif (table, name) not in self._unknown_columns:
                self._unknown_columns.add((table, name))
                logger.warning(
                    "Capture store %s: %s has no column %r; its value is not recorded",
                    self.path,
                    table,
                    name,
                )
        return columns

    def _release_leftovers(self) -> None:
        """Answer the flushes still queued once the writer stops, instead of letting them
        wait out their timeout; the records beside them are dropped."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _Flush):
                item.done.set()


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except Exception:
        pass
