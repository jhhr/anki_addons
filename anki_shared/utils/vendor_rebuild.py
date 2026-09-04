"""Rebuild an addon's vendored packages on the machine that is running it.

`build.py vendor` resolves requirements.txt for all five platforms Anki runs on and ships the
result, which stays the fast path. What it cannot do is follow Anki's Python: the launcher
updates the interpreter independently of any addon, and a tree built for 3.13 is dead weight
on 3.14 - silently, since most packages carrying a compiled half fall back to pure Python
rather than raising. `vendor_path.vendor_health` notices; this puts it right.

The result lands in `<addon>/user_files/lib`, because Anki sends the whole addon directory to
the trash on every update and restores only `user_files` from a backup.

It is also an *upgrade* rather than only a repair. build.json's `vendor_no_binaries` keeps
rapidfuzz's ~6 MB of extensions out of the shipped tree - five platforms of them is more than
the package can afford - but build.json is not in the zip, so a rebuild on one machine
installs the C extensions for that one platform and gets the real matching speed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Optional

from .vendor_path import (
    VENDOR_MANIFEST,
    platform_tag,
    runtime_python_version,
    user_lib,
)

REQUIREMENTS = "requirements.txt"

# Not inside user_files/lib: vendor_health treats that directory existing without a manifest
# as an interrupted rebuild, so a bare record of a *failed* attempt there would ask for the
# rebuild it exists to suppress. Beside it instead, where it also survives the tree swap.
REBUILD_STATE = ".lib_rebuild_state.json"

# pip writes console scripts an addon can never run, and cached bytecode is dead weight in a
# tree that is only ever imported.
_DROP_ENTRIES = ("bin", "Scripts", "__pycache__")

# Generous: the verified run took 12.7 s cold, but a slow connection resolving eight
# distributions is a different matter. This is only here so a wedged pip cannot hold the
# progress dialog open for the rest of the session.
_TIMEOUT_SECONDS = 900


def can_rebuild() -> Optional[str]:
    """None if pip can be driven here, else a short reason it cannot.

    The guard is not paranoia. On PyInstaller-packaged Anki (24.x and earlier) sys.executable
    is `anki.exe`, and `[sys.executable, "-m", "pip", ...]` would **launch a second Anki**
    rather than install anything. Refuse rather than guess: this whole feature is optional and
    the addon runs, more slowly, without it.
    """
    executable = sys.executable
    if not executable:
        return "Anki did not report which Python it is running"
    stem = os.path.splitext(os.path.basename(executable))[0].lower()
    if not stem.startswith("python"):
        return (
            f"this Anki runs from {os.path.basename(executable)} rather than a Python"
            " executable, so there is no pip to install with"
        )
    try:
        probe = _run([executable, "-m", "pip", "--version"], timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not run pip: {error}"
    if probe.returncode != 0:
        return "this Anki's Python has no working pip"
    return None


def rebuild_libs(addon_dir: str, on_progress: Callable[[str], None] = lambda _: None) -> None:
    """Install requirements.txt into <addon_dir>/user_files/lib. Raises on failure.

    Builds into a sibling temporary directory and swaps it in only once pip has succeeded, so
    an interrupted or failed rebuild cannot leave a half-installed tree behind - which would be
    worse than no tree at all, since it goes first on sys.path.
    """
    requirements = os.path.join(addon_dir, REQUIREMENTS)
    if not os.path.isfile(requirements):
        raise RuntimeError(f"{REQUIREMENTS} is not in the addon, so there is nothing to install")

    target = user_lib(addon_dir)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    staging = tempfile.mkdtemp(prefix="lib.building-", dir=os.path.dirname(target))
    try:
        on_progress("Downloading and installing packages...")
        result = _run(
            [
                sys.executable, "-m", "pip", "install",
                # --target only. Never --upgrade, and nothing that could reach Anki's own venv:
                # Anki pins requests and its chain, and this must not move them.
                "--target", staging,
                "--requirement", requirements,
                "--disable-pip-version-check",
                # There is no console attached to ask on.
                "--no-input",
                "--quiet",
            ],
            timeout=_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            # pip warns on stderr that these versions do not match anki-release's own pins.
            # That is expected and harmless - --target never touches the venv - so only the
            # exit code decides.
            raise RuntimeError(_failure_message(result))

        on_progress("Tidying up...")
        _prune(staging)
        _write_manifest(staging)

        on_progress("Installing...")
        _swap_in(staging, target)
        staging = ""
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------
# not asking twice
# --------------------------------------------------------------------------

def record_attempt(addon_dir: str, outcome: str) -> None:
    """Remember that this runtime and addon version already had their chance.

    A user who says no, or a machine that is offline behind a proxy pip cannot use, must not be
    asked again at every single startup. Recording what the attempt was *for* is what lets the
    question come back when the situation actually changes - a new Anki Python, or an addon
    update carrying a new lib.
    """
    state = {
        "outcome": outcome,
        "python": sys.version.split()[0],
        "addon_version": _addon_version(addon_dir),
    }
    path = os.path.join(addon_dir, "user_files", REBUILD_STATE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError:
        # Losing the record only means asking again next time, which is not worth a traceback.
        pass


def prompt_is_due(addon_dir: str) -> bool:
    """False while the last unsuccessful attempt was for this same runtime and addon."""
    path = os.path.join(addon_dir, "user_files", REBUILD_STATE)
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return True
    if not isinstance(state, dict):
        return True
    return state.get("python") != sys.version.split()[0] or state.get(
        "addon_version"
    ) != _addon_version(addon_dir)


def clear_attempts(addon_dir: str) -> None:
    """Forget the record, so a later refusal or failure can prompt again."""
    try:
        os.remove(os.path.join(addon_dir, "user_files", REBUILD_STATE))
    except OSError:
        pass


def _addon_version(addon_dir: str) -> str:
    """The human_version Anki extracted with the addon, or "" for a dev checkout."""
    try:
        with open(os.path.join(addon_dir, "manifest.json"), encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return ""
    return str(loaded.get("human_version", "")) if isinstance(loaded, dict) else ""


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------

def _run(command: list[str], timeout: int) -> "subprocess.CompletedProcess[str]":
    """Run a child process without flashing a console window over Anki."""
    kwargs = {}
    if sys.platform == "win32":
        # Anki is a GUI process, so a child that wants a console gets a window of its own.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **kwargs,
    )


def _failure_message(result: "subprocess.CompletedProcess[str]") -> str:
    tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
    return f"pip exited with {result.returncode}:\n" + ("\n".join(tail) or "no output")


def _prune(tree: str) -> None:
    for name in _DROP_ENTRIES:
        shutil.rmtree(os.path.join(tree, name), ignore_errors=True)
    for dirpath, dirnames, _ in os.walk(tree):
        for name in list(dirnames):
            if name == "__pycache__":
                dirnames.remove(name)
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)


def _write_manifest(tree: str) -> None:
    """The same shape build.py writes, so vendor_health is one code path for both trees.

    Written last, after pip has succeeded and before the swap: it is the marker that says this
    tree is complete, and vendor_health reads its absence as an interrupted rebuild.
    """
    flat = sorted(
        name
        for name in os.listdir(tree)
        if name != "__pycache__" and not name.startswith(".")
    )
    manifest = {
        "python_version": runtime_python_version(),
        # One machine, one platform - unlike the shipped tree, which carries all five.
        "platforms": [platform_tag()],
        "flat": flat,
        "per_platform": [],
        "rebuilt_locally": True,
    }
    with open(os.path.join(tree, VENDOR_MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def _swap_in(staging: str, target: str) -> None:
    """Move staging into place, keeping the old tree until the new one is there.

    os.replace cannot overwrite a non-empty directory on Windows, so the old one steps aside
    first. If the rename then fails the old tree goes back: a machine with no lib at all is
    worse off than one with a stale lib.
    """
    retired = ""
    if os.path.isdir(target):
        retired = target + ".old"
        shutil.rmtree(retired, ignore_errors=True)
        os.rename(target, retired)
    try:
        os.rename(staging, target)
    except OSError:
        if retired:
            os.rename(retired, target)
        raise
    if retired:
        # A file still open from this session - an imported .pyd on Windows - can refuse to go.
        # Not fatal: the new tree is in place already and a restart is required regardless.
        shutil.rmtree(retired, ignore_errors=True)
