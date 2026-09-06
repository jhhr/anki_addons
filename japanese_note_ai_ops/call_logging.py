"""Where the addon's log records go, and how long a log file stays open.

Two things decide that, and until now only one of them was written down.

The first is the *destination*: one file per call, created by the UI hook that starts the
call. That is right for a single-note op - a story generated on field unfocus, a translation -
where the call is the unit of work and its log is the record of it.

The second is *granularity*, and per-call was quietly the wrong answer for a bulk run. The
note-adding phase of `match_words_to_notes` is synchronous and follows the async plan phase,
so it is a phase rather than 1,512 independent events - but `note_will_be_added` fires per
note, and the hook behind it built a fresh `FileHandler` and tore down the previous one every
time. One measured run produced 1,453 log files for a phase that has one story to tell, and
spawned a `sap_log_closer` thread per note to close them, each polling `threading.enumerate()`
every two seconds for up to five minutes over a thread list those very pollers were
lengthening.

So a phase installs its own handler once, for its whole duration, and the hooks that fire
inside it write to whatever is already attached. `bulk_op_logging` marks the run so a hook can
tell it is inside one; `phase_log` swaps in the file for a phase of it. A hook that fires with
neither in force - the user adding a note by hand, which is the case the per-call handler was
originally for - still creates one of its own, and that is the only remaining path that does.
"""

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from aqt import mw

from .async_api_ops.diagnostics import WORKER_THREAD_PREFIX

ADDON_MODULE = __name__.split(".")[0]

logger = logging.getLogger(__name__)

# Marks the handlers this addon attaches, so they can be found and closed again
_ADDON_HANDLER_FLAG = "_simple_anki_ai_prompts_handler"


# How long to keep waiting for a run's threads before closing its log file anyway. Long enough
# for a cancelled run to finish unwinding, short enough that the file doesn't stay open for the
# session if something never exits.
_LOG_CLOSE_TIMEOUT_SECONDS = 300
_LOG_CLOSE_POLL_SECONDS = 2.0

# How deep in bulk operations this thread currently is. A bulk op owns its own log file for its
# whole length, so any hook firing underneath it must leave the handlers alone rather than
# replacing them per note. Thread-local because Anki runs a CollectionOp on a background
# thread while the editor's hooks keep firing on the main one.
_bulk_state = threading.local()


def addon_logger() -> logging.Logger:
    """The root logger for this addon - the one every module's logger sits under."""
    return logging.getLogger(ADDON_MODULE)


def _addon_threads_alive() -> bool:
    """Whether any of this addon's worker threads is still running.

    A run's threads outlive the operation on purpose: a cancelled run cannot interrupt a
    request already in flight, so its threads unwind on their own afterwards - and what they
    log while doing it is the whole point of the cancellation diagnostics.
    """
    return any(
        thread.name.startswith(WORKER_THREAD_PREFIX) and thread.is_alive()
        for thread in threading.enumerate()
    )


def _close_handler_when_idle(handler: logging.Handler) -> None:
    """Close a detached handler, once nothing is still writing through it.

    Closing it straight away closed the file out from under a cancelled run's threads. They
    keep logging as they unwind, and a handler whose stream has been closed re-opens the file
    on the next record - so the descriptor this function exists to release was leaked after
    all, and the run's last diagnostics ended up somewhere nothing was looking.

    The waiting costs a thread, which is why the callers are now phases rather than notes: at
    one note a second, with the run's own workers alive for the whole loop, every one of these
    ran its full timeout and ~300 of them were polling at any moment.
    """

    def close_quietly() -> None:
        try:
            handler.close()
        except Exception:
            pass

    if not _addon_threads_alive():
        close_quietly()
        return

    def wait_and_close() -> None:
        deadline = time.monotonic() + _LOG_CLOSE_TIMEOUT_SECONDS
        while time.monotonic() < deadline and _addon_threads_alive():
            time.sleep(_LOG_CLOSE_POLL_SECONDS)
        close_quietly()

    threading.Thread(target=wait_and_close, name="sap_log_closer", daemon=True).start()


def close_previous_log_handlers(logger_instance: logging.Logger) -> None:
    """Detach any log handler this addon attached earlier, and close it once it is idle.

    A handler was added every time the browser context menu was built or a field lost focus,
    and none were ever removed. They accumulate for the lifetime of the session, so every log
    record gets written once per handler - and with several worker threads logging at once
    that turns into a great deal of redundant file I/O. It also keeps every previous log file
    open, which is why they can't be deleted until Anki is closed.

    Detaching is immediate; the close waits for the threads that may still be writing.

    Nothing detached here can belong to an operation still running. Every caller is a UI hook -
    building the browser context menu, unfocusing a field - and Anki's progress dialog owns the
    UI while an operation is in progress, so none of them can fire until it has finished.
    Cancelling is the only thing the user can do meanwhile.
    """
    for handler in list(logger_instance.handlers):
        if getattr(handler, _ADDON_HANDLER_FLAG, False):
            logger_instance.removeHandler(handler)
            _close_handler_when_idle(handler)


def create_call_log_handler(function_name: str) -> logging.Handler:
    """Create a new file handler for a specific function call"""
    config = mw.addonManager.getConfig(ADDON_MODULE) or {}

    # Get log level from config
    log_level_str = config.get("log_level", "ERROR")
    log_level = getattr(logging, log_level_str.upper(), logging.ERROR)

    # Update the root addon logger's level to match config
    addon_logger().setLevel(log_level)

    # Check if console logging is enabled
    log_to_console = config.get("log_to_console", False)

    if log_to_console:
        # Create console handler
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(log_level)
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        setattr(handler, _ADDON_HANDLER_FLAG, True)
        return handler

    # Create logs directory
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(addon_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # Create unique log file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(logs_dir, f"{function_name}_{timestamp}.log")

    # Create handler. delay=True so the file isn't opened (or created) until something is
    # actually logged - building the context menu shouldn't leave an empty log file behind.
    handler = logging.FileHandler(log_file, encoding="utf-8", delay=True)
    handler.setLevel(log_level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    setattr(handler, _ADDON_HANDLER_FLAG, True)

    return handler


def start_call_log(function_name: str) -> None:
    """Give this call its own log file, replacing whatever the previous one left attached."""
    handler = create_call_log_handler(function_name)
    if not handler:
        return
    logger_instance = addon_logger()
    # Replace the previous run's handler rather than stacking another one on top
    close_previous_log_handlers(logger_instance)
    logger_instance.addHandler(handler)


def in_bulk_op() -> bool:
    """Whether this thread is running inside a bulk operation.

    The one question a per-note hook has to ask before touching a log handler. A bulk op has
    already installed the file its whole run belongs in; a hook that replaced it per note would
    close the run's log out from under it - which is exactly what used to happen, and why the
    phase table for a 36-minute run ended up in the last of 1,453 files.
    """
    return getattr(_bulk_state, "depth", 0) > 0


@contextmanager
def bulk_op_logging() -> Iterator[None]:
    """Mark this thread as running a bulk op, for as long as the block lasts.

    Installs nothing: the run's log file is the one the UI hook that started it attached. This
    only says that a run owns it, so `in_bulk_op` can answer.
    """
    _bulk_state.depth = getattr(_bulk_state, "depth", 0) + 1
    try:
        yield
    finally:
        _bulk_state.depth = max(0, getattr(_bulk_state, "depth", 1) - 1)


@contextmanager
def phase_log(function_name: str) -> Iterator[None]:
    """Send one phase of a run to its own log file, and put the run's own file back after.

    The run's handlers are detached rather than closed, and re-attached when the block ends, so
    the phases either side of this one stay in one file and the phase table stays readable in
    a single grep. Only the phase's own handler is closed here, and only once nothing is still
    writing through it.

    A phase that cannot open a file for itself simply keeps the run's handler: a log file is
    diagnostics, and failing to create one must not take the operation down with it.
    """
    logger_instance = addon_logger()
    try:
        handler: Optional[logging.Handler] = create_call_log_handler(function_name)
    except Exception as e:
        logger.warning(
            "Could not open a log file for phase %r (%s); logging on", function_name, e
        )
        handler = None
    if handler is None:
        yield
        return

    detached = [h for h in logger_instance.handlers if getattr(h, _ADDON_HANDLER_FLAG, False)]
    for previous in detached:
        logger_instance.removeHandler(previous)
    logger_instance.addHandler(handler)
    try:
        yield
    finally:
        logger_instance.removeHandler(handler)
        for previous in detached:
            logger_instance.addHandler(previous)
        _close_handler_when_idle(handler)
