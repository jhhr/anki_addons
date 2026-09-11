"""Repo-wide pytest plugin, registered by the root conftest through `pytest_plugins`.

It holds what every running-Anki suite in the repo needs and no single suite owns:

* **Skipping them when pytest-anki is missing.** Their conftests sit in `testpaths`, which
  makes them *initial* conftests, loaded while pytest is still parsing its arguments; a
  `pytest.importorskip` there does not skip anything but aborts the whole run. So a test
  that needs `anki_session` is marked skipped here instead, and the rest of the run goes on.

* **One media server readiness per `AnkiQt`.** Each such test runs inside
  `running_anki.media_servers_waited_for()`, whose docstring has the race it closes. It is
  applied here rather than by fixture because it has to be in place before `anki_session`
  builds the main window, and an addon's fixtures cannot promise to come first. It is
  imported only for those tests, so a run without them never loads `aqt.mediasrv`.

* **The shutdown guard**, which lets a run that started a real Anki exit with pytest's own
  status instead of a segfault. The rest of this docstring is about that.

Why a guard is needed at all. PyQt registers an `atexit` handler, `_qtcore_cleanup`, that
destroys the `QApplication` and the Python-owned widgets still alive when the interpreter
exits. After a pytest-anki session that application has had QtWebEngine running inside it,
and destroying it at that point dies with an access violation -- every test passed, the
summary is already printed, and the process exits 139. It is not any addon's doing -- it
is pytest-anki's application being torn down by PyQt -- and a test process has no use for
that teardown, since the OS reclaims everything a moment later anyway.

Why it unregisters one handler rather than calling `os._exit`. `os._exit` would also skip
every other `atexit` handler, and pytest's own temporary-directory housekeeping lives there:
the lock on this run's `pytest-of-<user>/pytest-N` and the pruning of old runs are both
`atexit` callbacks. Skip them and every run leaves a ~90 MB directory behind that pytest
refuses to delete for three days. Removing only PyQt's handler keeps all of that, keeps
normal interpreter shutdown, and keeps the exit status exactly what pytest returned -- for
`python -m pytest`, for `pytest.main()` callers such as VS Code's runner, and for xdist
workers alike, because nothing about exiting changes except that one callback not running.

`os._exit` is still here, as the fallback for a PyQt that no longer names its handler
`_qtcore_cleanup`. It runs only in the main process (an xdist worker still has results
queued for the controller at this point) and after pytest has written its report, cache and
junitxml, which all happen in `pytest_sessionfinish`, before this plugin's
`pytest_unconfigure`.

The guard only engages when a `QApplication` exists, which is to say when a real Anki was
started in this process; a stub-only run never creates one and keeps the ordinary teardown
untouched. Set `ANKI_TEST_SHUTDOWN_GUARD=0` to switch it off -- to see the crash, or to debug
Qt's own shutdown -- and `ANKI_TEST_SHUTDOWN_GUARD=exit` to force the `os._exit` fallback.
"""

import atexit
import gc
import logging
import os
import sys
import types
from typing import Any, Optional

import pytest

GUARD_ENV = "ANKI_TEST_SHUTDOWN_GUARD"

_EXIT_STATUS = pytest.StashKey[int]()

# The name pytest-anki2's entry point registers its plugin under.
_PYTEST_ANKI_PLUGIN = "anki"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Checked by plugin rather than by import, so `-p no:anki` skips these tests too
    # instead of erroring on a missing fixture.
    if config.pluginmanager.has_plugin(_PYTEST_ANKI_PLUGIN):
        return
    skip = pytest.mark.skip(
        reason="needs pytest-anki2: python -m pip install --no-deps -r requirements-dev-nodeps.txt"
    )
    for item in items:
        if _uses_running_anki(item):
            item.add_marker(skip)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: Optional[pytest.Item]):
    # Wrapping the whole protocol puts the patch in place before fixture setup, where
    # `anki_session` builds the main window and its media server.
    if not (_uses_running_anki(item) and item.config.pluginmanager.has_plugin(_PYTEST_ANKI_PLUGIN)):
        return (yield)

    from anki_shared.testing import running_anki

    with running_anki.media_servers_waited_for():
        return (yield)


def _uses_running_anki(item: pytest.Item) -> bool:
    fixturenames = getattr(item, "fixturenames", ())
    return "anki_session" in fixturenames or "anki_session_module" in fixturenames


@pytest.hookimpl(wrapper=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int):
    # Read after every other implementation has run: a plugin may still change it here.
    try:
        return (yield)
    finally:
        session.config.stash[_EXIT_STATUS] = int(session.exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    mode = os.environ.get(GUARD_ENV, "1").strip().lower()
    if mode in ("0", "off", "false", "no"):
        return
    if _running_qt_application() is None:
        return

    handler = None if mode == "exit" else _pyqt_exit_handler()
    if handler is not None:
        atexit.unregister(handler)
        return

    exit_status = config.stash.get(_EXIT_STATUS, None)
    if exit_status is None or _is_xdist_worker(config):
        return
    _exit_without_interpreter_shutdown(exit_status)


def _running_qt_application() -> Optional[Any]:
    """The live QApplication, looked up without importing Qt into a run that never used it."""
    for binding in ("PyQt6.QtCore", "PyQt5.QtCore"):
        qtcore = sys.modules.get(binding)
        if qtcore is not None:
            return qtcore.QCoreApplication.instance()
    return None


def _pyqt_exit_handler() -> Optional[types.BuiltinFunctionType]:
    """PyQt's `_qtcore_cleanup`, which nothing but the `atexit` registry holds a reference to.

    PyQt hands it straight to `atexit.register` and keeps no name for it, so the garbage
    collector's list of live objects is the only way to reach it. Over a full run's heap
    the scan takes a few tens of milliseconds.
    """
    for candidate in gc.get_objects():
        if (
            type(candidate) is types.BuiltinFunctionType
            and candidate.__name__ == "_qtcore_cleanup"
        ):
            return candidate
    return None


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput") or "PYTEST_XDIST_WORKER" in os.environ


def _exit_without_interpreter_shutdown(exit_status: int) -> None:
    """Leave now with `exit_status`, running no `atexit` handler and no finalizer.

    Everything buffered has to be pushed out first, because nothing will flush it later.
    """
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(exit_status)
