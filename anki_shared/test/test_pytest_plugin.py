"""The repo-wide pytest plugin's own decisions, made without running pytest twice.

Only the parts that are ordinary functions are covered here. The shutdown guard is not: it
ends the process on purpose, so the only honest test of it is a run that exits with pytest's
status, which every real-Anki run in this repo already is.
"""

from types import SimpleNamespace

from anki_shared.testing.pytest_plugin import (
    DEFAULT_XDIST_DISTRIBUTION,
    choose_xdist_distribution,
)


def config(numprocesses=None, dist="no", args=()):
    return SimpleNamespace(
        option=SimpleNamespace(numprocesses=numprocesses, dist=dist),
        invocation_params=SimpleNamespace(args=tuple(args)),
    )


def test_a_parallel_run_is_distributed_by_file():
    # `-n 4` on its own used to leave xdist's `load`, which splits the running-Anki file
    # across workers; a worker running a real Anki that way segfaults mid-run and the whole
    # parallel run fails on a suite that is green serially.
    cfg = config(numprocesses=4, dist="load", args=("-n", "4"))

    assert choose_xdist_distribution(cfg) is True
    assert cfg.option.dist == DEFAULT_XDIST_DISTRIBUTION


def test_a_serial_run_is_left_alone():
    cfg = config(args=())

    assert choose_xdist_distribution(cfg) is False
    assert cfg.option.dist == "no"


def test_an_explicit_distribution_wins():
    cfg = config(numprocesses=4, dist="load", args=("-n", "4", "--dist", "load"))

    assert choose_xdist_distribution(cfg) is False
    assert cfg.option.dist == "load"


def test_an_explicit_distribution_written_with_an_equals_sign_wins():
    cfg = config(numprocesses=4, dist="load", args=("-n4", "--dist=load"))

    assert choose_xdist_distribution(cfg) is False
    assert cfg.option.dist == "load"


def test_a_distribution_that_is_not_the_default_is_left_alone():
    cfg = config(numprocesses=4, dist="loadscope", args=("-n", "4"))

    assert choose_xdist_distribution(cfg) is False
    assert cfg.option.dist == "loadscope"


class TestTheGuardWhenPyQtsHandlerCannotBeFound:
    """What the guard does with the `atexit` registry when its `gc` scan comes back empty.

    `_pyqt_exit_handler` reaches PyQt's `_qtcore_cleanup` the only way there is: PyQt hands
    it straight to `atexit.register` and keeps no name for it, so the guard scans
    `gc.get_objects()` for a builtin function with that name. A rename, a C-level change, or
    any PyQt that registers a bound method or a `functools.partial` instead makes the scan
    return `None`.

    `pytest_unconfigure` treats `None` as "nothing to unregister" and carries on to
    `_exit_without_interpreter_shutdown`, which runs the registry by hand with
    `atexit._run_exitfuncs()`. The handler is still in that registry, so the guard invokes
    the application teardown it exists to prevent -- at the one moment nothing can recover
    from it, since `os._exit` is never reached.

    Forcing the scan to report `None` against this repo's own real-Anki suite reproduces it:
    three of eight runs exit 139 on "1 passed", against zero of eight with the scan working
    and four of eight with the guard switched off entirely. The crash is timing-dependent,
    which is the worst shape for CI -- an intermittent 139 on a green suite reads as a flaky
    test, not as a shutdown problem.

    Running the registry is worth doing when it is safe: pytest's tmpdir lock and pruning
    live there, and skipping them leaves ~90 MB behind per run for three days. It is not
    worth the exit status it exists to protect. So the decision has to be conditional on the
    disarm having actually happened, which means `pytest_unconfigure` has to tell the exit
    what it found rather than the exit assuming it.
    """

    def exit_spy(self, monkeypatch):
        from anki_shared.testing import pytest_plugin

        calls = {"ran_handlers": False, "exited_with": None}

        def fake_run_exitfuncs():
            calls["ran_handlers"] = True

        def fake_exit(status):
            calls["exited_with"] = status

        monkeypatch.setattr("atexit._run_exitfuncs", fake_run_exitfuncs)
        monkeypatch.setattr(pytest_plugin.os, "_exit", fake_exit)
        return pytest_plugin, calls

    def test_the_registry_is_not_run_when_the_handler_is_still_in_it(self, monkeypatch):
        plugin, calls = self.exit_spy(monkeypatch)

        plugin._exit_without_interpreter_shutdown(0, run_handlers=False)

        assert calls["ran_handlers"] is False
        assert calls["exited_with"] == 0

    def test_the_registry_is_run_once_the_handler_is_out_of_it(self, monkeypatch):
        # The common case, and the reason the hand-run is there at all.
        plugin, calls = self.exit_spy(monkeypatch)

        plugin._exit_without_interpreter_shutdown(3, run_handlers=True)

        assert calls["ran_handlers"] is True
        assert calls["exited_with"] == 3

    def test_disarming_reports_whether_it_found_anything(self, monkeypatch):
        from anki_shared.testing import pytest_plugin

        monkeypatch.setattr(pytest_plugin, "_pyqt_exit_handler", lambda: None)
        assert pytest_plugin._disarm_qt_teardown() is False

        handler = pytest_plugin.atexit.register(lambda: None)
        monkeypatch.setattr(pytest_plugin, "_pyqt_exit_handler", lambda: handler)
        try:
            assert pytest_plugin._disarm_qt_teardown() is True
        finally:
            pytest_plugin.atexit.unregister(handler)
