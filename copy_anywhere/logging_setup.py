"""Where CopyAnywhere's log records go: one file per triggered operation.

A copy is triggered as a whole -- the browser's picker dialog running four definitions over
a note selection, a note being added, a card being answered, a field losing focus -- and the
story of what happened is the story of that whole trigger. So one trigger writes one file,
and everything logged underneath it lands there: this addon's own lines and the ones
`jp_text_processing` emits from inside `kana_highlight`/`word_highlight`, which are the ones
worth reading when a furigana process produces the wrong thing.

Two loggers, then, and one handler across both. `jp_text_processing` logs under a fixed name
of its own rather than under this addon's -- it is vendored into several addons, so its
records cannot sit in any one addon's tree -- and the helpers here are the single place that
attaches to both, so their level and destination cannot drift apart.

**The shared logger is not this addon's.** Every addon that vendors `jp_text_processing`
logs through the same `jp_text_processing` logger object, often from threads of its own. So
the operation's handler takes only the records logged while one of this addon's definitions
runs (`running_a_definition`), and the operation only ever lowers that logger's level, and
puts it back only if nobody changed it meanwhile. Before, a debug-level run wrote another
addon's lines into this addon's file, and an error-level run silenced its warnings.

**Why the file is reference counted rather than scoped.** `copy_fields` hands its work to a
`CollectionOp` and returns immediately; the records are written on the worker thread and the
operation ends in `on_success`/`on_failure` on the main thread, long after the hook that
called it has returned. An "am I already inside an operation" flag would let the unfocus
hook, which runs its own definitions *and* calls `copy_fields`, close the file before the
operation that owns it had written a line. A count does not: the hook takes a reference, the
operation takes a second, and the file closes when the last one goes.
"""

import logging
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Iterator, Optional, Tuple

from .shared.utils.logger import LogLevel, log_level_to_int

ADDON_MODULE = __name__.split(".")[0]

# The package logs under this name whoever embeds it, so it is addressed by name here rather
# than reached through an import -- nothing in this module needs the package itself.
SHARED_LOGGER_NAME = "jp_text_processing"

# Diagnostics are worth keeping for a while and worth not keeping forever.
LOGS_TO_KEEP = 200

LOG_FORMAT = "%(asctime)s %(levelname)s %(prefix)s%(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

logger = logging.getLogger(__name__)


def addon_logger() -> logging.Logger:
    """The root logger for this addon -- the one every module's logger sits under."""
    return logging.getLogger(ADDON_MODULE)


def _operation_loggers() -> list[logging.Logger]:
    """Every logger an operation's handler is attached to."""
    return [addon_logger(), logging.getLogger(SHARED_LOGGER_NAME)]


# Nothing here prints on its own: a library that logs leaves the destination to whoever runs
# it, and until an operation opens a file there is no destination.
addon_logger().addHandler(logging.NullHandler())


# The prefix ------------------------------------------------------------------------------

# Which definition, and which note, the records being written just now belong to. A
# ContextVar rather than an attribute on a logger: a `CollectionOp` runs on a worker thread
# while the editor's hooks keep firing on the main one, and each thread starts from the
# default, so the operation's prefix cannot leak into a hook's lines.
log_context: ContextVar[Tuple[Optional[str], Optional[int]]] = ContextVar(
    "copy_anywhere_log_context", default=(None, None)
)


def set_log_definition(definition_name: Optional[str]) -> None:
    """Name the definition the records from here on belong to, and forget the previous note."""
    log_context.set((definition_name, None))


def set_log_nid(nid: Optional[int]) -> None:
    """Name the note the records from here on are about."""
    definition_name, _ = log_context.get()
    log_context.set((definition_name, nid))


def reset_log_context() -> None:
    log_context.set((None, None))


# Whether the thread logging right now is running one of this addon's definitions. A
# ContextVar for the same reason `log_context` is one: each thread starts from the default.
_running: ContextVar[bool] = ContextVar("copy_anywhere_running", default=False)


@contextmanager
def running_a_definition() -> Iterator[None]:
    """Mark the block as this addon's work, so `jp_text_processing`'s lines in it are its."""
    token = _running.set(True)
    try:
        yield
    finally:
        _running.reset(token)


class OwnSharedRecordsFilter(logging.Filter):
    """Lets a `jp_text_processing` record through only from inside one of this addon's runs.

    Everything logged under the addon's own name is the addon's, whatever thread it came
    from. The shared logger's records are only this addon's when one of its definitions is
    what called the package; the rest belong to another addon using the same logger.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == SHARED_LOGGER_NAME or record.name.startswith(SHARED_LOGGER_NAME + "."):
            return _running.get()
        return True


class LogContextFilter(logging.Filter):
    """Puts `[definition][NID:n]` on every record the operation's handler writes.

    On the handler rather than on a logger, so that `jp_text_processing`'s records get the
    same prefix as this addon's -- they are about the same note, and a reader sorting out
    which definition produced a bad reading needs to see that on the line reporting it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        definition_name, nid = log_context.get()
        prefix = ""
        if definition_name:
            prefix += f"[{definition_name}]"
        if nid:
            prefix += f"[NID:{nid}]"
        record.prefix = prefix
        return True


# The operation's log file ----------------------------------------------------------------


def logs_dir() -> str:
    """The directory the operation logs are written to.

    Under `user_files/` because that is the only directory Anki carries across an addon
    update; everything else is sent to the trash and re-extracted.
    """
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(addon_dir, "user_files", "logs")


_UNSAFE_IN_A_FILENAME_RE = re.compile(r"[^\w.-]+", re.UNICODE)


def _log_file_name(name: str) -> str:
    """A filename for this operation: what triggered it, and when."""
    stem = _UNSAFE_IN_A_FILENAME_RE.sub("_", name).strip("_")[:80] or "operation"
    return f"{stem}_{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.log"


def prune_old_logs(directory: str, keep: int = LOGS_TO_KEEP) -> None:
    """Delete all but the newest `keep` log files, so the directory cannot grow forever."""
    try:
        files = [
            entry
            for entry in os.scandir(directory)
            if entry.is_file() and entry.name.endswith(".log")
        ]
    except OSError:
        return
    files.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        try:
            os.remove(stale.path)
        except OSError:
            # A file something else is holding open is not worth failing an operation over.
            pass


class _OperationLog:
    """The one file an operation writes to, and how many callers still hold it open."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.depth = 0
        self.handler: Optional[logging.Handler] = None
        self.path: Optional[str] = None
        #: (logger, its level before, the level the operation set), for each one it set.
        self.previous_levels: list[Tuple[logging.Logger, int, int]] = []


_operation = _OperationLog()


def start_operation_log(name: str, level: LogLevel = "error") -> Optional[str]:
    """Open this operation's log file, or take a reference on the one already open.

    Returns the path records are being written to, or None if no file could be opened -- a
    log file is diagnostics, and failing to create one must not take the operation with it.
    Every caller must pair this with exactly one `finish_operation_log()`.
    """
    with _operation.lock:
        if _operation.depth > 0:
            _operation.depth += 1
            return _operation.path

        directory = logs_dir()
        try:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, _log_file_name(name))
            # delay=True: the file is not created until something is actually written, so a
            # clean run at the default `error` level leaves nothing behind.
            handler: logging.Handler = logging.FileHandler(path, encoding="utf-8", delay=True)
        except OSError as error:
            logger.warning("Could not open a log file for %r (%s); logging on", name, error)
            return None

        log_level = log_level_to_int(level)
        handler.setLevel(log_level)
        handler.addFilter(OwnSharedRecordsFilter())
        handler.addFilter(LogContextFilter())
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))

        _operation.previous_levels = []
        for target in _operation_loggers():
            # The addon's own logger is set to the level asked for. The shared one is only
            # ever lowered, far enough for the records this run asked for to be made: raised,
            # it silenced every other addon's lines below the level for the whole run.
            if target is addon_logger() or log_level < target.getEffectiveLevel():
                _operation.previous_levels.append((target, target.level, log_level))
                target.setLevel(log_level)
            target.addHandler(handler)
        _operation.handler = handler
        _operation.path = path
        _operation.depth = 1
        reset_log_context()
        return path


def _close_operation_log() -> Optional[str]:
    """Detach and close the operation's handler, and put the loggers back as they were.

    Called with `_operation.lock` held.
    """
    handler = _operation.handler
    path = _operation.path
    for target, previous_level, set_level in _operation.previous_levels:
        # Left alone if something else set it while the operation ran; putting the old
        # level back would undo that.
        if target.level == set_level:
            target.setLevel(previous_level)
    if handler is not None:
        for target in _operation_loggers():
            target.removeHandler(handler)
    _operation.previous_levels = []
    _operation.handler = None
    _operation.path = None
    _operation.depth = 0
    if handler is not None:
        handler.close()
    reset_log_context()

    if path is not None and os.path.exists(path):
        # Only when a file was written: with `delay=True` most operations write none, and
        # scanning the folder for them would be work at every field unfocus for nothing.
        prune_old_logs(os.path.dirname(path))
        return path
    return None


def finish_operation_log() -> Optional[str]:
    """Drop one reference on the operation's log file, closing it when the last one goes.

    Returns the path only to the caller whose release closed the file, and only if anything
    was written to it -- `delay=True` means a run that reported nothing never created one.
    That is the signal a caller needs to decide whether there is a log worth showing.
    """
    with _operation.lock:
        if _operation.depth == 0:
            return None
        _operation.depth -= 1
        if _operation.depth > 0:
            return None
        return _close_operation_log()


def reset_operation_log() -> Optional[str]:
    """Close whatever an unfinished operation left attached, whatever its reference count.

    Nothing in the addon needs this: `CollectionOp` always reaches one of its callbacks, and
    the note hooks release theirs in a `finally`. Tests do -- several drive `copy_fields`'s
    `op` closure without its callbacks, and a reference left standing would attach the next
    test's records to a file in a directory that has since been deleted.
    """
    with _operation.lock:
        if _operation.depth == 0:
            return None
        return _close_operation_log()


@contextmanager
def operation_logging(name: str, level: LogLevel = "error") -> Iterator[Optional[str]]:
    """Give everything logged inside the block one file, named after what triggered it.

    For the synchronous triggers -- the note hooks. `copy_fields` cannot use this: its work
    outlives the call, so it pairs `start_operation_log` with a `finish_operation_log` in the
    operation's own callbacks.
    """
    path = start_operation_log(name, level)
    try:
        yield path
    finally:
        finish_operation_log()
