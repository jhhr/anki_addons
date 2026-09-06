"""Run the Qt host harness, when a Qt is available to run it against.

Out of process on purpose: the harness installs a fake `aqt` built on real
PyQt6, while conftest stubs aqt with MagicMock for everything else. Two
incompatible fakes in one interpreter would make the suite order-dependent.
"""

import os
import subprocess
import sys

import pytest

pytest.importorskip("PyQt6", reason="Qt host coverage needs PyQt6 installed")

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qt_host_harness.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_qt_host_behaviour():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=REPO_ROOT)
    result = subprocess.run(
        [sys.executable, HARNESS], capture_output=True, text=True, env=env, timeout=180
    )
    if result.returncode != 0:
        pytest.fail(f"harness failed:\n{result.stdout}\n{result.stderr}")
