"""Fixtures for Addon Config Sync in a real, running Anki.

`addon_config_sync/test` covers what the add-on copies, over made-up folders behind a stub
`mw`. What that cannot reach is the add-on's own `__init__.py`, which no suite imports: the
handlers it puts on Anki's hooks, the real profile manager answering for the folders, and
the meta.json files Anki's own add-on manager writes when it installs, updates or disables
an add-on, which the tests there only imitate. The machinery that makes a real Anki safe to
run in the same process as the stub suites is `anki_shared/testing/running_anki.py`'s.

The add-ons folder and the profile are pytest-anki's temporary ones. The one thing the
add-on keeps outside them is its matches file, beside its own code and so in this checkout,
where it is the file of the Anki this checkout is installed in; `addon` points it into the
test's directory before any handler can run.

`make_device` gives a test the devices of `devices.py`, each with folders of its own, running
this add-on or the released one, on the running Anki or as another Anki version; `ankiweb`
is the AnkiWeb they download from.
"""

from __future__ import annotations

import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator

import pytest
from aqt import gui_hooks

from anki_shared.testing import running_anki

from .devices import FOLDER, RELEASED, RELEASED_AS, REPO, AnkiDevice, FakeAnkiWeb

ADDON_DIR = Path(__file__).resolve().parents[1]
# The name `__init__.py` is executed under. Not the directory's: that name is the stub
# package the other suites imported their modules of the add-on through.
LOADED_AS = "addon_config_sync_in_anki"
REBOUND_PACKAGES = [LOADED_AS, ADDON_DIR.name, "anki_shared"]
# What `__init__.py` appends to; process-global, so put back after
HOOKS = [gui_hooks.sync_will_start, gui_hooks.media_sync_did_start_or_stop]


@pytest.fixture(autouse=True)
def restore_stub_mw() -> Iterator[Any]:
    """Autouse and taking no other fixture, so it is torn down after `anki_session`."""
    with running_anki.stub_mw_restored(REBOUND_PACKAGES, HOOKS) as stub:
        yield stub


@pytest.fixture
def real_mw(anki_session, restore_stub_mw) -> Iterator[Any]:
    """A running Anki with a fresh profile loaded."""
    with running_anki.main_window(anki_session, REBOUND_PACKAGES) as mw:
        yield mw


@pytest.fixture
def addon(real_mw, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """The add-on as Anki loads it: its `__init__.py` run against the real `mw`. A package
    of its own, so what its modules remember for a session starts empty."""
    spec = importlib.util.spec_from_file_location(
        LOADED_AS, ADDON_DIR / "__init__.py", submodule_search_locations=[str(ADDON_DIR)]
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[LOADED_AS] = package
    try:
        spec.loader.exec_module(package)
        monkeypatch.setattr(
            sys.modules[f"{LOADED_AS}.matched_copies"],
            "state_file",
            lambda: tmp_path / "matched_copies.json",
        )
        yield package
    finally:
        for name in [m for m in sys.modules if m == LOADED_AS or m.startswith(LOADED_AS + ".")]:
            del sys.modules[name]


@pytest.fixture
def ankiweb() -> FakeAnkiWeb:
    return FakeAnkiWeb()


@pytest.fixture
def manager(real_mw: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Anki's add-on manager, installing over a folder without moving the old one to the
    system's trash first, which is no place for a test's."""
    manager = real_mw.addonManager
    monkeypatch.setattr(
        manager, "deleteAddon", lambda module: shutil.rmtree(manager.addonsFolder(module))
    )
    return manager


@pytest.fixture(scope="module")
def released_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    archive = subprocess.run(
        ["git", "-C", str(REPO), "archive", RELEASED, "addon_config_sync"], capture_output=True
    )
    if archive.returncode != 0:
        pytest.skip(f"this checkout has no revision {RELEASED[:8]} to load the release from")
    out = tmp_path_factory.mktemp("released")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(out, filter="data")
    return out / "addon_config_sync"


@pytest.fixture
def released_addon(real_mw: Any, released_source: Path) -> Iterator[ModuleType]:
    """The released add-on, loaded as Anki loads it, beside today's."""
    spec = importlib.util.spec_from_file_location(
        RELEASED_AS,
        released_source / "__init__.py",
        submodule_search_locations=[str(released_source)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[RELEASED_AS] = package
    try:
        spec.loader.exec_module(package)
        yield package
    finally:
        for name in [m for m in sys.modules if m == RELEASED_AS or m.startswith(RELEASED_AS + ".")]:
            del sys.modules[name]


@pytest.fixture
def make_device(
    request: pytest.FixtureRequest,
    real_mw: Any,
    addon: ModuleType,
    manager: Any,
    ankiweb: FakeAnkiWeb,
    anki_session: Any,
    tmp_path: Path,
) -> Callable[..., AnkiDevice]:
    """`make_device("a", "released", anki_version=66)`: a device running today's add-on or the
    released one, on the Anki that runs or on another version."""

    def make(name: str, runs: str = "today", anki_version: int | None = None) -> AnkiDevice:
        acs = addon if runs == "today" else request.getfixturevalue("released_addon")
        return AnkiDevice(tmp_path / name, real_mw, anki_session.qtbot, acs, ankiweb, anki_version)

    return make


@pytest.fixture(autouse=True)
def no_addon_module_left() -> Iterator[None]:
    yield
    sys.modules.pop(FOLDER, None)
