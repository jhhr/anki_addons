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
