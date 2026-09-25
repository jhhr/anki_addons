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

Why a guard is needed at all. Once a pytest-anki session has had QtWebEngine running inside
its `QApplication`, tearing that application down at interpreter exit dies with an access
violation: every test passed, the summary is already printed, and the process still exits
139. It is not any addon's doing, and a test process has no use for that teardown, since the
OS reclaims everything a moment later anyway. But the exit status is the only thing CI
reads, so an unguarded run reports every green result as a failure.

What it does. At `pytest_unconfigure` -- after pytest has written its report, cache and
junitxml, all of which happen in `pytest_sessionfinish` -- it runs the `atexit` handlers by
hand and then leaves through `os._exit` with the status pytest returned, so the interpreter
never reaches the teardown that crashes.

Why the handlers are run by hand rather than skipped. `os._exit` on its own would skip every
`atexit` handler, and pytest's own temporary-directory housekeeping lives there: the lock on
this run's `pytest-of-<user>/pytest-N` and the pruning of old runs are both `atexit`
callbacks. Skip them and every run leaves a ~90 MB directory behind that pytest refuses to
delete for three days. Running them first keeps all of that.

PyQt's own `atexit` handler, `_qtcore_cleanup`, is unregistered before that hand-run, since
destroying the application is the very thing being avoided. PyQt keeps no reference to it, so
the only way to reach it is a scan of live objects for a builtin function of that name, and
the guard checks whether that scan actually found it: if it did not, the handler is still in
the registry and the hand-run is skipped entirely, because running it would invoke the
teardown rather than avoid it. That costs the tmpdir housekeeping and keeps the exit status. Unregistering it *used* to be
the whole guard, on the theory that normal interpreter shutdown was then safe. It is not,
and has not been since PyQt6 6.11 / QtWebEngine 6.11: with that handler gone the process
still segfaults, now on the way through QtWebEngine's own teardown ("Release of profile
requested but WebEnginePage still not deleted"). So the exit is no longer a fallback for a
PyQt that renamed its handler -- it is the guard, and unregistering is one step inside it.

The exit runs only in the main process: an xdist worker still has results queued for the
controller at this point, so it is left to shut down normally. Parallel runs have their own
requirement, which is nothing to do with the guard: `choose_xdist_distribution` says what it
is and why.

The guard only engages when a `QApplication` exists, which is to say when a real Anki was
started in this process; a stub-only run never creates one and keeps the ordinary teardown
untouched. Set `ANKI_TEST_SHUTDOWN_GUARD=0` to switch it off, to see the crash or to debug
Qt's own shutdown. There is no longer a separate `=exit` mode: forcing the `os._exit`
fallback is what the guard now does in every case.
"""

import atexit
import gc
import logging
import os
import shlex
import sys
import types
from typing import Any, Optional

import pytest

GUARD_ENV = "ANKI_TEST_SHUTDOWN_GUARD"

#: What a parallel run is distributed by when the user asked for workers and not for a
#: distribution. `loadfile` keeps every test of one file on one worker.
DEFAULT_XDIST_DISTRIBUTION = "loadfile"

_EXIT_STATUS = pytest.StashKey[int]()

# The name pytest-anki2's entry point registers its plugin under.
_PYTEST_ANKI_PLUGIN = "anki"


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    # Before xdist builds its scheduler, which reads this option in its own `pytest_configure`.
    choose_xdist_distribution(config)


def choose_xdist_distribution(config: Any) -> bool:
    """Send a parallel run to `--dist loadfile`, unless the user picked a distribution.

    xdist's default, `load`, hands tests out one at a time from a single queue, which splits
    the running-Anki file across workers and interleaves each worker's share with tests from
    every other file. A worker that runs a real Anki that way segfaults partway through --
    at every worker count from two up, with the shutdown guard on or off -- and xdist reports
    `node down: Not properly terminated` for a suite that is green run serially. Keeping each
    file on one worker is enough to avoid it, and costs nothing here: the suites are spread
    over enough files to keep four workers busy either way.

    Only the default is moved: an explicit `--dist` (or `-d`) is left alone, whether it is
    on the command line, in an ini file's `addopts` or in `PYTEST_ADDOPTS`. Each is the user
    saying what they want, including `--dist load` to see the crash.
    """
    if not getattr(config.option, "numprocesses", None):
        return False
    if getattr(config.option, "dist", "no") != "load":
        return False
    args = list(getattr(getattr(config, "invocation_params", None), "args", ()))
    # pytest parses `addopts` and `PYTEST_ADDOPTS` along with the command line, but
    # `invocation_params.args` holds only what was typed, so a `--dist` written there looked
    # like no choice at all and was overridden.
    args += config.getini("addopts")
    args += shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))
    if any(arg == "-d" or arg == "--dist" or arg.startswith("--dist=") for arg in args):
        return False
    config.option.dist = DEFAULT_XDIST_DISTRIBUTION
    return True


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

    # Removed before the handlers are run by hand below: destroying the application is
    # exactly what must not happen.
    disarmed = _disarm_qt_teardown()

    exit_status = config.stash.get(_EXIT_STATUS, None)
    if exit_status is None or _is_xdist_worker(config):
        return
    _exit_without_interpreter_shutdown(exit_status, run_handlers=disarmed)


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


def _disarm_qt_teardown() -> bool:
    """Unregister PyQt's `atexit` handler, and say whether it is really out of the registry.

    The answer decides whether the registry can be run by hand afterwards, so it has to be
    the truth rather than an assumption. `_pyqt_exit_handler` reaches `_qtcore_cleanup` by
    scanning live objects for a builtin function of that name, which is the only way there
    is -- and which a rename, a C-level change, or a PyQt that registers a bound method or a
    `functools.partial` would defeat.
    """
    handler = _pyqt_exit_handler()
    if handler is None:
        return False
    atexit.unregister(handler)
    return True


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput") or "PYTEST_XDIST_WORKER" in os.environ


def _exit_without_interpreter_shutdown(exit_status: int, run_handlers: bool = True) -> None:
    """Leave now with `exit_status`, before the interpreter can tear Qt down.

    The `atexit` handlers are run here rather than skipped when it is safe to run them --
    pytest's temporary-directory housekeeping is one of them -- and everything buffered is
    pushed out afterwards, because no finalizer will flush it later.

    `run_handlers` is false when PyQt's handler could not be found and so is still in the
    registry. Running it then would invoke the application teardown this whole function
    exists to avoid, at the one point nothing can recover from it: the process segfaults and
    `os._exit` below is never reached, so a green suite exits 139. Forcing the scan to fail
    against this repo's own real-Anki suite reproduces that in three runs out of eight, which
    is the worst shape for CI -- an intermittent 139 on "1 passed" reads as a flaky test.

    Skipping the hand-run costs pytest's tmpdir lock and pruning, so a run that takes this
    path leaves its `pytest-of-<user>/pytest-N` behind for three days. That is the right
    trade: the exit status is the thing the guard is for, and a stale directory is visible
    and harmless where a segfault is neither.
    """
    if run_handlers:
        try:
            # Private, but stable since 2.x and the only way to run these without exiting. It
            # also clears the registry, so nothing can run twice.
            atexit._run_exitfuncs()
        except Exception:  # noqa: BLE001 -- housekeeping is best effort
            # A handler that raises must not stop the exit: carrying on into normal
            # interpreter shutdown is the crash this exists to avoid.
            pass
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(exit_status)
