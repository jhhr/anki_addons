"""The process-wide capture API: which store records, which run, note and call a line of work
belongs to, and the record of each AI call.

`capture_store` is the file and its writer; this module is what the rest of the addon calls.
One store is installed per profile (`install` at profile open, `shutdown` at close). Until
then, and after the store turns itself off, every function here is a cheap no-op: `call`
yields None, `begin_run` returns None, the `note_*` functions do nothing. Nothing is hashed or
built before that check, because `get_response` runs in hundreds of pool threads at once.

Where a call happens is carried in ContextVars rather than passed down: the run, the note, the
task within the note and the call itself. A bulk run sets the run and each note's scope once,
around the asyncio task that works on it; `asyncio.create_task` and `asyncio.to_thread` copy
the context, so the pool thread that runs `get_response` sees both without any op passing them
(this is how `run_errors.error_subject` reaches its errors too). A plain `threading.Thread`
starts with an empty context and sees none of it.

Capture is diagnostics and never changes what an op does: every recording step inside `call`
catches its own failure and logs it, and the caller's result and exception pass through
untouched.

Free of aqt and anki, and imports nothing of the addon's but `capture_store`, so `api_client`
and `terminal_client` can note their attempts here.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any, Callable, Optional

from .capture_store import (
    WARNING_INTERVAL_SECONDS,
    CaptureStore,
    canonical_json,
    prompt_key,
    request_key,
)

logger = logging.getLogger(__name__)

# How long a re-install waits for the previous store's writer. The usual path has none to
# wait for: profile_will_close has shut it down before the next profile opens
REPLACE_TIMEOUT_SECONDS = 2.0

# Matched against a config key lowercased with "_", "-" and " " taken out, so that "apiKey" and
# "api-key" go too
_SECRET_KEY_PARTS = ("apikey", "secret", "password")
# "token" only as a word of the key ("auth_token", "accessToken"): as a substring it also took
# "max_output_tokens", a setting a replay of the run needs
_SECRET_KEY_WORDS = ("token",)
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_WORD_SEPARATORS = re.compile(r"[^a-z0-9]+")


# --- install ------------------------------------------------------------------------------


class _Installed:
    """The store and what install() was told to put on every run, swapped as one reference so
    a reader never sees one store with another install's versions."""

    __slots__ = ("store", "versions_json", "log_path")

    def __init__(
        self,
        store: CaptureStore,
        versions_json: Optional[str],
        log_path: Optional[Callable[[], Optional[str]]],
    ) -> None:
        self.store = store
        self.versions_json = versions_json
        self.log_path = log_path


# Read without the lock on every call (one attribute read is atomic); the lock only keeps two
# install/shutdown calls from interleaving their close and open
_installed: Optional[_Installed] = None
_install_lock = threading.Lock()


def install(
    path: Any,
    *,
    keep_days: Optional[float] = 90.0,
    versions: Optional[Mapping[str, Any]] = None,
    log_path: Optional[Callable[[], Optional[str]]] = None,
) -> bool:
    """Record from now on into the store at `path`; True when it opened and is recording.

    `versions` is written on every run; `log_path()` is asked at each run's start for the
    text log it writes to. A store already installed is closed first. If its writer is still
    busy after that, nothing is opened: the new store's ids are seeded from the rows already in
    the file, and the old writer may still hold rows with the same ids. Never raises.
    """
    global _installed
    with _install_lock:
        previous = _installed
        if previous is not None:
            _installed = None
            if not _close(previous.store, REPLACE_TIMEOUT_SECONDS):
                logger.warning(
                    "Capture store %s was still writing when %s was to replace it; nothing is"
                    " recorded this session",
                    previous.store.path,
                    path,
                )
                return False
        try:
            store = CaptureStore(path, keep_days=keep_days)
        except Exception as e:
            logger.warning(
                "Capture store %s could not be created (%s: %s); nothing is recorded this"
                " session",
                path,
                type(e).__name__,
                e,
            )
            return False
        if not store.enabled:
            # The store has logged why
            _close(store, REPLACE_TIMEOUT_SECONDS)
            logger.warning("Capture store %s is not recording; capture is off", store.path)
            return False
        _installed = _Installed(store, _json(versions, "the versions"), log_path)
    logger.info("Capture store %s: recording AI calls", store.path)
    return True


def shutdown(timeout: float = 2.0) -> bool:
    """Close the installed store and forget it; True when its writer stopped (or none was
    installed). Calls still in flight keep their reference and find the store closed."""
    global _installed
    with _install_lock:
        state = _installed
        _installed = None
        if state is None:
            return True
        return _close(state.store, timeout)


def installed() -> bool:
    """Whether a store is installed and still recording (it turns itself off on a failure)."""
    return _active() is not None


def current_store() -> Optional[CaptureStore]:
    """The installed store, recording or not; None when none is installed."""
    state = _installed
    return state.store if state is not None else None


def _active() -> Optional[_Installed]:
    state = _installed
    if state is None or not state.store.enabled:
        return None
    return state


def _close(store: CaptureStore, timeout: float) -> bool:
    try:
        return store.close(timeout)
    except Exception:
        logger.warning("Capture store %s could not be closed", store.path, exc_info=True)
        return False


# --- where the work is --------------------------------------------------------------------

# The run's id and its time.monotonic() at the scope's start, the origin of calls.started
_run: ContextVar[Optional[tuple[int, float]]] = ContextVar("capture_run", default=None)
_note: ContextVar[Optional[int]] = ContextVar("capture_note", default=None)
# Innermost last
_tasks: ContextVar[tuple[str, ...]] = ContextVar("capture_tasks", default=())
_call: ContextVar[Optional[CallTrace]] = ContextVar("capture_call", default=None)


@contextmanager
def run_scope(run_id: Optional[int]) -> Iterator[None]:
    """Make `run_id` the current run in this context and the tasks created inside it.

    None (what `begin_run` returns with capture off) makes it a scope that sets nothing. The
    calls' `started` counts from the scope's start, so enter it right after `begin_run`.
    """
    if run_id is None:
        yield
        return
    token = _run.set((run_id, time.monotonic()))
    try:
        yield
    finally:
        _run.reset(token)


def current_run_id() -> Optional[int]:
    run = _run.get()
    return run[0] if run is not None else None


@contextmanager
def note_scope(note_id: Optional[int]) -> Iterator[None]:
    """Make `note_id` the note the calls in this context are for.

    Set whether or not a store is installed: it costs a ContextVar set, and the log lines then
    name their note with capture off too. Recorded as given, so a note not added yet is 0.
    """
    token = _note.set(note_id)
    try:
        yield
    finally:
        _note.reset(token)


def current_note_id() -> Optional[int]:
    return _note.get()


@contextmanager
def task_scope(label: Optional[str]) -> Iterator[None]:
    """A finer unit of work than the note (the match op's word targets): calls inside record
    it as `task_id` and the scope around it, if any, as `parent_task_id`. None sets nothing."""
    if label is None:
        yield
        return
    token = _tasks.set(_tasks.get() + (label,))
    try:
        yield
    finally:
        _tasks.reset(token)


def current_call_id() -> Optional[int]:
    trace = _call.get()
    return trace.call_id if trace is not None else None


# --- runs ---------------------------------------------------------------------------------


def begin_run(
    label: str,
    *,
    implicit: bool = False,
    ops: Optional[list[str]] = None,
    chain_step: Optional[str] = None,
    note_count: Optional[int] = None,
    config: Optional[Mapping[str, Any]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """Record the start of a run and return its id, None with capture off.

    `config` is recorded through `scrub_config`; the install's versions and the current log
    path are added. Enter `run_scope(run_id)` for the work that belongs to it.
    """
    state = _active()
    if state is None:
        return None
    return _begin_run(
        state,
        label,
        implicit=implicit,
        ops=ops,
        chain_step=chain_step,
        note_count=note_count,
        config=config,
        extra=extra,
    )


def end_run(
    run_id: Optional[int], outcome: Optional[str], *, extra: Optional[Mapping[str, Any]] = None
) -> None:
    """Record a run's end and outcome. `extra`, when given, replaces the one `begin_run` had."""
    if run_id is None:
        return
    state = _active()
    if state is not None:
        _end_run(state.store, run_id, outcome, extra)


def _begin_run(
    state: _Installed,
    label: str,
    *,
    implicit: bool = False,
    ops: Optional[list[str]] = None,
    chain_step: Optional[str] = None,
    note_count: Optional[int] = None,
    config: Optional[Mapping[str, Any]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    try:
        store = state.store
        run_id = store.new_run_id()
        store.insert_run(
            {
                "run_id": run_id,
                "started": time.time(),
                "label": label,
                "implicit": 1 if implicit else 0,
                "ops_json": _json(ops, "a run's ops"),
                "chain_step": chain_step,
                "note_count": note_count,
                "config_json": _scrubbed_json(config),
                "versions_json": state.versions_json,
                "log_path": _current_log_path(state),
                "extra_json": _json(extra, "a run's extra"),
            }
        )
        return run_id
    except Exception:
        _warn_limited("begin run", "Capture: a run could not be recorded", exc_info=True)
        return None


def _end_run(
    store: CaptureStore,
    run_id: int,
    outcome: Optional[str],
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    try:
        columns: dict[str, Any] = {"ended": time.time(), "outcome": outcome}
        # Left out rather than None, which would wipe what begin_run recorded
        if extra is not None:
            columns["extra_json"] = _json(extra, "a run's extra")
        store.update_run(run_id, **columns)
    except Exception:
        _warn_limited("end run", "Capture: a run's end could not be recorded", exc_info=True)


def _scrubbed_json(config: Optional[Mapping[str, Any]]) -> Optional[str]:
    # Its own guard: a config that cannot be scrubbed is left out, never written unscrubbed
    if config is None:
        return None
    try:
        return canonical_json(scrub_config(config))
    except Exception:
        _warn_limited("config", "Capture: a run's config could not be recorded", exc_info=True)
        return None


def _current_log_path(state: _Installed) -> Optional[str]:
    if state.log_path is None:
        return None
    try:
        path = state.log_path()
    except Exception:
        _warn_limited("log path", "Capture: the log path could not be read", exc_info=True)
        return None
    return None if path is None else str(path)


# --- calls --------------------------------------------------------------------------------


class CallTrace:
    """What one call learns while it runs, noted by its provider and read when it ends.

    Written without a lock: the provider runs synchronously inside `get_response`, on the
    call's own thread, and nothing else writes here.

    The outcome: the last one noted (`note_outcome`) wins, since a provider notes it where it
    knows it and the helpers that note it are each the one point for theirs; with none noted,
    `finish(result, cancelled)` gives `ok` for a result, else `cancelled` for a cancelled run,
    else `error`. A block left by an exception records `error` whatever was noted, and one left
    without `finish` or a noted outcome records none: a call site that forgot `finish` shows as
    NULL rather than as a guess.
    """

    __slots__ = (
        "call_id",
        "run_id",
        "attempts",
        "response_raw",
        "usage_json",
        "corrected",
        "response_json",
        "finished",
        "_started",
        "_noted_outcome",
        "_noted_error",
        "_default_outcome",
    )

    def __init__(self, call_id: int, run_id: Optional[int], started: float) -> None:
        self.call_id = call_id
        self.run_id = run_id
        self.attempts = 0
        self.response_raw: Optional[str] = None
        self.usage_json: Optional[str] = None
        self.corrected = False
        self.response_json: Optional[str] = None
        self.finished = False
        self._started = started
        self._noted_outcome: Optional[str] = None
        self._noted_error: Optional[str] = None
        self._default_outcome: Optional[str] = None

    @property
    def outcome(self) -> Optional[str]:
        if self._noted_outcome is not None:
            return self._noted_outcome
        return self._default_outcome

    @property
    def error(self) -> Optional[str]:
        """The error given with the noted outcome."""
        return self._noted_error

    def elapsed(self) -> float:
        """Seconds since the call started, for its log line."""
        return time.monotonic() - self._started

    def note_attempt(self) -> None:
        self.attempts += 1

    def note_response(
        self, raw: Any = None, usage: Any = None, corrected: Optional[bool] = None
    ) -> None:
        """Set what is given; None leaves a value noted earlier as it is."""
        if raw is not None:
            self.response_raw = raw if isinstance(raw, str) else _json(raw, "a raw response")
        if usage is not None:
            # Now rather than in the writer: a usage object the writer could not serialise
            # would cost the whole row
            self.usage_json = _json(usage, "a call's usage")
        if corrected is not None:
            self.corrected = bool(corrected)

    def note_outcome(self, outcome: str, error: Optional[str] = None) -> None:
        self._noted_outcome = outcome
        self._noted_error = error

    def finish(self, result: Any, cancelled: bool = False) -> None:
        """Record what `get_response` returns, as it is now: the op may change it after."""
        self.response_json = _json(result, "a call's result")
        self.finished = True
        if result is not None:
            self._default_outcome = "ok"
        elif cancelled:
            self._default_outcome = "cancelled"
        else:
            self._default_outcome = "error"


class _Call:
    """The context manager `call` returns when a store is recording.

    A class rather than a generator so that the caller's exception is only looked at, never
    thrown into capture code and re-raised from it: `__exit__` returns None and Python raises
    the original, traceback and all.
    """

    __slots__ = (
        "_state",
        "_args",
        "_trace",
        "_row",
        "_implicit_run",
        "_run_token",
        "_call_token",
    )

    def __init__(self, state: _Installed, args: tuple[Any, ...]) -> None:
        self._state = state
        self._args = args
        self._trace: Optional[CallTrace] = None
        self._row: dict[str, Any] = {}
        self._implicit_run: Optional[int] = None
        self._run_token: Optional[Token[Optional[tuple[int, float]]]] = None
        self._call_token: Optional[Token[Optional[CallTrace]]] = None

    def __enter__(self) -> Optional[CallTrace]:
        try:
            return self._start()
        except Exception:
            _warn_limited("call start", "Capture: a call could not be recorded", exc_info=True)
            self._trace = None
            self._reset()
            return None

    def _start(self) -> CallTrace:
        started = time.monotonic()
        kind, inputs, model, prompt, instructions, schema, params, context = self._args
        store = self._state.store
        call_id = store.new_call_id()
        tasks = _tasks.get()
        row: dict[str, Any] = {
            "call_id": call_id,
            "note_id": _note.get(),
            "task_id": tasks[-1] if tasks else None,
            "parent_task_id": tasks[-2] if len(tasks) > 1 else None,
            "kind": kind,
            "request_key": request_key(kind, inputs),
            "prompt_key": prompt_key(model, instructions, prompt, schema, params),
            "model": model,
            # Serialised now, not by the writer: the op may change a dict or list it passed
            # after the call, and these must match the keys computed from them here
            "params_json": _json(params, "a call's params"),
            "inputs_json": _json(inputs, "a call's inputs"),
            "context_json": _json(context, "a call's context"),
            # put_blob(None) would store the text "null"
            "instructions_hash": None if instructions is None else store.put_blob(instructions),
            "prompt": prompt,
            "schema_hash": None if schema is None else store.put_blob(schema),
        }
        run = _run.get()
        new_run: Optional[tuple[int, float]] = None
        if run is None:
            # Last, so nothing after it can fail and leave the run without its end
            run_id = _begin_run(self._state, kind or "call", implicit=True)
            self._implicit_run = run_id
            row["started"] = 0.0
            if run_id is not None:
                new_run = (run_id, started)
        else:
            run_id = run[0]
            row["started"] = started - run[1]
        row["run_id"] = run_id
        trace = CallTrace(call_id, run_id, started)
        self._row = row
        self._trace = trace
        if new_run is not None:
            # Log lines inside the call then name the run it is recorded in
            self._run_token = _run.set(new_run)
        self._call_token = _call.set(trace)
        return trace

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        trace = self._trace
        if trace is None:
            return None
        try:
            # KeyboardInterrupt, SystemExit, GeneratorExit and CancelledError say nothing about
            # the call; the process or the task is going away, and they pass unrecorded
            if exc is None or isinstance(exc, Exception):
                self._record(trace, exc)
        except Exception:
            _warn_limited("call end", "Capture: a call could not be recorded", exc_info=True)
        finally:
            self._reset()
        return None

    def _record(self, trace: CallTrace, exc: Optional[BaseException]) -> None:
        latency_ms = (time.monotonic() - trace._started) * 1000.0
        if exc is not None:
            outcome: Optional[str] = "error"
            error: Optional[str] = _safe_repr(exc)
        else:
            outcome = trace.outcome
            error = trace.error
        row = self._row
        row["response_raw"] = trace.response_raw
        row["response_json"] = trace.response_json
        row["outcome"] = outcome
        row["error"] = error
        row["latency_ms"] = latency_ms
        row["attempts"] = trace.attempts
        row["usage_json"] = trace.usage_json
        row["extra_json"] = '{"corrected":true}' if trace.corrected else None
        store = self._state.store
        store.insert_call(row)
        if self._implicit_run is not None:
            _end_run(store, self._implicit_run, outcome)

    def _reset(self) -> None:
        call_token, self._call_token = self._call_token, None
        run_token, self._run_token = self._run_token, None
        try:
            if call_token is not None:
                _call.reset(call_token)
        except Exception:
            _warn_limited("call reset", "Capture: a call could not be reset", exc_info=True)
        try:
            if run_token is not None:
                _run.reset(run_token)
        except Exception:
            _warn_limited("call reset", "Capture: a call could not be reset", exc_info=True)


# Reusable and reentrant; returned when nothing records, so a call then costs one global read
_NOT_RECORDED: AbstractContextManager[None] = nullcontext()


def call(
    kind: str,
    inputs: Any,
    *,
    model: str,
    prompt: str,
    instructions: Optional[str],
    schema: Any,
    params: Any,
    context: Any = None,
) -> AbstractContextManager[Optional[CallTrace]]:
    """Record one AI call: `with capture.call(...) as trace:` around the request.

    Yields the call's `CallTrace`, or None when nothing records. At entry it takes the call's
    id and makes it current (`current_call_id`, the log lines' `c<id>`), computes both keys,
    stores the instructions and schema once as blobs, and, with no run current, opens an
    implicit run for this call alone. At exit it queues the `calls` row and ends the implicit
    run with the call's outcome. `instructions` is the text actually sent; `params` the
    sampling parameters as passed. An exception leaving the block is recorded as `error` with
    its repr and goes on unchanged.

    `context` is what reading or applying the answer needs that the prompt does not show:
    which note each numbered item of the prompt came from, the note the answer is written to.
    Plain JSON, note ids as ints; serialised at entry, like `inputs`. It is not part of either
    key, being about this collection rather than about what was asked, and it holds nothing
    the prompt is built from: that is `inputs`.
    """
    state = _installed
    if state is None or not state.store.enabled:
        return _NOT_RECORDED
    return _Call(
        state, (kind or "", inputs, model, prompt, instructions, schema, params, context)
    )


def note_attempt() -> None:
    """Count one send of the current call's request (a retry is another)."""
    trace = _call.get()
    if trace is not None:
        trace.note_attempt()


def note_response(raw: Any = None, usage: Any = None, corrected: Optional[bool] = None) -> None:
    """Note the current call's answer text before parsing, its provider's usage object, and
    whether the corrector produced its result; each only when given."""
    trace = _call.get()
    if trace is None:
        return
    try:
        trace.note_response(raw, usage, corrected)
    except Exception:
        _warn_limited("note response", "Capture: a response could not be noted", exc_info=True)


def note_outcome(outcome: str, error: Optional[str] = None) -> None:
    """Note the current call's outcome; the last noted wins (see `CallTrace`)."""
    trace = _call.get()
    if trace is not None:
        trace.note_outcome(outcome, error)


# --- config and logs ----------------------------------------------------------------------


def scrub_config(config: Any) -> Any:
    """A deep copy of `config` without any key that names a secret (an API key, a token, a
    secret, a password), at any depth of dicts and lists. The input is left as it is."""
    if isinstance(config, Mapping):
        return {
            key: scrub_config(value) for key, value in config.items() if not _is_secret(key)
        }
    if isinstance(config, list):
        return [scrub_config(value) for value in config]
    if isinstance(config, tuple):
        return tuple(scrub_config(value) for value in config)
    return copy.deepcopy(config)


def _is_secret(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    folded = key.lower().replace("_", "").replace("-", "").replace(" ", "")
    if any(part in folded for part in _SECRET_KEY_PARTS):
        return True
    words = _WORD_SEPARATORS.split(_CAMEL_BOUNDARY.sub(r"\1_\2", key).lower())
    return any(word in _SECRET_KEY_WORDS for word in words)


class CaptureContextFilter(logging.Filter):
    """Sets `record.capture_ids` to the ids current where the line was logged, e.g.
    `r12 n1712345678901 c4567`, only those there are, or `-`.

    Belongs on handlers, not loggers: a logger's filters do not see its children's records.
    It reads the context of the thread that logs, which is the handler's own for the addon's
    synchronous handlers. Always lets the record through and never raises: a log line must not
    be lost to capture.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = _ids_text()
        except Exception:
            text = "-"
        try:
            setattr(record, "capture_ids", text)
        except Exception:
            pass
        return True


def _ids_text() -> str:
    parts = []
    run = _run.get()
    if run is not None:
        parts.append(f"r{run[0]}")
    note_id = _note.get()
    if note_id is not None:
        parts.append(f"n{note_id}")
    trace = _call.get()
    if trace is not None:
        parts.append(f"c{trace.call_id}")
    return " ".join(parts) or "-"


# --- helpers ------------------------------------------------------------------------------

_warn_lock = threading.Lock()
_quiet_until: dict[str, float] = {}


def _warn_limited(key: str, message: str, *args: Any, exc_info: bool = False) -> None:
    """A capture failure tends to repeat on every call once it starts; one line a minute."""
    now = time.monotonic()
    with _warn_lock:
        if now < _quiet_until.get(key, float("-inf")):
            return
        _quiet_until[key] = now + WARNING_INTERVAL_SECONDS
    logger.warning(message, *args, exc_info=exc_info)


def _json(value: Any, what: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return canonical_json(value)
    except Exception:
        _warn_limited(
            f"json {what}", "Capture: %s has no JSON text; not recorded", what, exc_info=True
        )
        return None


def _safe_repr(error: BaseException) -> str:
    try:
        return repr(error)
    except Exception:
        return type(error).__name__
