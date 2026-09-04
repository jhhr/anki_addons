"""The parts of the local rebuild that can be tested without running pip.

The install itself is a network call and is not exercised here; what is, is everything that
decides whether it may run at all, and everything that happens to its output afterwards.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from anki_shared.utils import vendor_path, vendor_rebuild


class TestCanRebuild:
    def test_refuses_when_the_executable_is_not_a_python(self, monkeypatch):
        """PyInstaller-packaged Anki reports anki.exe here, and `-m pip` would start Anki."""
        monkeypatch.setattr(sys, "executable", r"C:\Program Files\Anki\anki.exe")
        reason = vendor_rebuild.can_rebuild() or ""
        assert "anki.exe" in reason

    def test_refuses_when_there_is_no_executable_at_all(self, monkeypatch):
        monkeypatch.setattr(sys, "executable", "")
        assert vendor_rebuild.can_rebuild() is not None

    def test_refuses_when_pip_is_missing(self, monkeypatch):
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
        monkeypatch.setattr(
            vendor_rebuild, "_run",
            lambda *a, **k: subprocess.CompletedProcess([], 1, "", "No module named pip"),
        )
        assert vendor_rebuild.can_rebuild() == "this Anki's Python has no working pip"

    def test_refuses_rather_than_raising_when_the_probe_will_not_run(self, monkeypatch):
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

        def explode(*_a, **_k):
            raise OSError("nope")

        monkeypatch.setattr(vendor_rebuild, "_run", explode)
        assert "could not run pip" in (vendor_rebuild.can_rebuild() or "")

    def test_allows_a_real_python_with_pip(self, monkeypatch):
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13")
        monkeypatch.setattr(
            vendor_rebuild, "_run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, "pip 25.1.1", ""),
        )
        assert vendor_rebuild.can_rebuild() is None


class TestRebuildLibs:
    def test_refuses_without_a_requirements_file(self, tmp_path):
        with pytest.raises(RuntimeError, match="requirements.txt"):
            vendor_rebuild.rebuild_libs(str(tmp_path))

    def test_a_failed_install_leaves_no_tree_behind(self, tmp_path, monkeypatch):
        """A half-installed tree would be worse than none: it goes first on sys.path."""
        (tmp_path / "requirements.txt").write_text("psutil==7.2.2\n", "utf-8")
        monkeypatch.setattr(
            vendor_rebuild, "_run",
            lambda *a, **k: subprocess.CompletedProcess([], 1, "", "could not resolve host"),
        )
        with pytest.raises(RuntimeError, match="could not resolve host"):
            vendor_rebuild.rebuild_libs(str(tmp_path))
        assert not os.path.exists(vendor_path.user_lib(str(tmp_path)))
        assert os.listdir(tmp_path / "user_files") == []

    def test_a_failed_install_leaves_an_existing_tree_alone(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("psutil==7.2.2\n", "utf-8")
        existing = tmp_path / "user_files" / "lib"
        existing.mkdir(parents=True)
        (existing / "keepme.py").write_text("# still here\n", "utf-8")
        monkeypatch.setattr(
            vendor_rebuild, "_run",
            lambda *a, **k: subprocess.CompletedProcess([], 1, "", "boom"),
        )
        with pytest.raises(RuntimeError):
            vendor_rebuild.rebuild_libs(str(tmp_path))
        assert (existing / "keepme.py").is_file()

    def test_a_successful_install_is_pruned_manifested_and_swapped_in(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("psutil==7.2.2\n", "utf-8")
        stale = tmp_path / "user_files" / "lib"
        stale.mkdir(parents=True)
        (stale / "from_the_old_tree.py").write_text("# gone\n", "utf-8")

        def fake_pip(command, timeout):
            target = command[command.index("--target") + 1]
            Path(target, "psutil").mkdir()
            Path(target, "psutil", "__init__.py").write_text("", "utf-8")
            Path(target, "psutil", "__pycache__").mkdir()
            Path(target, "psutil", "__pycache__", "x.pyc").write_text("", "utf-8")
            Path(target, "bin").mkdir()
            Path(target, "bin", "console-script").write_text("", "utf-8")
            return subprocess.CompletedProcess(command, 0, "", "warning about anki-release")

        monkeypatch.setattr(vendor_rebuild, "_run", fake_pip)
        steps = []
        vendor_rebuild.rebuild_libs(str(tmp_path), steps.append)

        lib = tmp_path / "user_files" / "lib"
        assert steps  # the caller gets something to put in the progress dialog
        assert not (lib / "from_the_old_tree.py").exists()
        assert not (lib / "bin").exists()
        assert not (lib / "psutil" / "__pycache__").exists()
        # Nothing left in user_files but the tree itself
        assert os.listdir(tmp_path / "user_files") == ["lib"]

        manifest = json.loads((lib / vendor_path.VENDOR_MANIFEST).read_text("utf-8"))
        assert manifest["python_version"] == vendor_path.runtime_python_version()
        assert manifest["platforms"] == [vendor_path.platform_tag()]
        assert manifest["flat"] == ["psutil"]
        # The manifest it just wrote is the one vendor_health reads
        monkeypatch.setattr(vendor_path, "_smoke_test", lambda: None)
        assert vendor_path.vendor_health(str(tmp_path)) is None


class TestNotAskingTwice:
    def test_a_fresh_install_is_due(self, tmp_path):
        assert vendor_rebuild.prompt_is_due(str(tmp_path))

    def test_a_recorded_attempt_for_this_runtime_is_not_due(self, tmp_path):
        vendor_rebuild.record_attempt(str(tmp_path), "declined")
        assert not vendor_rebuild.prompt_is_due(str(tmp_path))

    def test_a_new_python_makes_it_due_again(self, tmp_path):
        vendor_rebuild.record_attempt(str(tmp_path), "failed")
        state = tmp_path / "user_files" / vendor_rebuild.REBUILD_STATE
        state.write_text(json.dumps({"python": "3.12.0", "addon_version": ""}), "utf-8")
        assert vendor_rebuild.prompt_is_due(str(tmp_path))

    def test_a_new_addon_version_makes_it_due_again(self, tmp_path):
        (tmp_path / "manifest.json").write_text(json.dumps({"human_version": "1.0.0"}), "utf-8")
        vendor_rebuild.record_attempt(str(tmp_path), "failed")
        assert not vendor_rebuild.prompt_is_due(str(tmp_path))
        (tmp_path / "manifest.json").write_text(json.dumps({"human_version": "1.1.0"}), "utf-8")
        assert vendor_rebuild.prompt_is_due(str(tmp_path))

    def test_a_corrupt_state_file_is_due(self, tmp_path):
        (tmp_path / "user_files").mkdir()
        (tmp_path / "user_files" / vendor_rebuild.REBUILD_STATE).write_text("{oops", "utf-8")
        assert vendor_rebuild.prompt_is_due(str(tmp_path))

    def test_clearing_makes_it_due_again(self, tmp_path):
        vendor_rebuild.record_attempt(str(tmp_path), "declined")
        vendor_rebuild.clear_attempts(str(tmp_path))
        assert vendor_rebuild.prompt_is_due(str(tmp_path))

    def test_clearing_nothing_is_not_an_error(self, tmp_path):
        vendor_rebuild.clear_attempts(str(tmp_path))
