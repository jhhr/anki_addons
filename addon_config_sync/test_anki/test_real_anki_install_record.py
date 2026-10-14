"""What Anki records about an installed add-on stays on each device.

Beside what the user set (`config`, `disabled`, and `update_enabled`, the add-on's tick in
Anki's list of updates), Anki keeps in an add-on's meta.json a record of the package it
installed (`sync_actions.INSTALL_KEYS`): above all `mod`, AnkiWeb's time for the installed
version, which Anki's update check compares with AnkiWeb's latest, and the range of Anki
versions that version is for. Released versions of the add-on copy the whole file between
devices, record and all: an update installed on one device was hidden from the others, an
older record had Anki offer an update already installed, and a range written for one Anki
version stopped the add-on loading in another. Now a media copy read keeps this device's
record, a copy sent keeps its own, and only the settings are compared.

These tests have a real Anki write the record, with its own download, install, update check,
add-on loading and sync wiring, and follow it to a second device, which runs this add-on or
the released one. Only the network is left out: AnkiWeb is `FakeAnkiWeb`, and a media sync
is `Server` and `AnkiDevice.media_sync`, which keep Anki's rule that a file changed on the
server is downloaded even over one changed here (rslib's `determine_required_change`:
"differs from server, favour server"). A device is an add-ons folder and a profile folder
that the running Anki's profile manager is pointed at.
"""

from __future__ import annotations

import filecmp
import importlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, Iterator, cast
from unittest import mock
from urllib.parse import parse_qs, urlparse

import aqt
import aqt.addons
import pytest
from anki.collection import AddonInfo
from anki.httpclient import HttpClient
from anki.sync import SyncOutput
from anki.sync_pb2 import SyncAuth
from aqt import gui_hooks
from aqt.qt import QLabel, Qt

# An AnkiWeb id: Anki checks for updates only of add-ons whose folder is one
ADDON_ID = 1234567890
FOLDER = str(ADDON_ID)
HOMEPAGE = "https://example.org/tiny"
T1 = 1_700_000_000
T2 = 1_700_500_000
# What Anki writes into a meta.json from a package's manifest and from AnkiWeb
ANKIS_RECORD = {
    key
    for key, spec in aqt.addons.AddonManager._manifest_schema["properties"].items()
    if spec["meta"]
}

REPO = Path(__file__).resolve().parents[2]
# Addon Config Sync as released (1.0.0): its last state on origin/main before matches were
# kept, which copies the whole file both ways
RELEASED = "8d1d24f2104d8251651b7e3bdbd0c8e17cc66707"
RELEASED_AS = "addon_config_sync_released"

# An add-on whose code puts a handler on a hook when Anki loads it, as Addon Config Sync does
REGISTERS_HANDLER = """
from aqt import gui_hooks


def on_sync_will_start():
    on_sync_will_start.ran = VERSION


gui_hooks.sync_will_start.append(on_sync_will_start)
"""


class FakeAnkiWeb:
    """AnkiWeb for the add-ons published on it here: the answer to an update check, and the
    download.

    Each branch of an add-on is for a range of Anki versions, and AnkiWeb answers an Anki with
    the last branch whose range has its version. Asked for add-on 3918629684, it answered
    Anki 2.1.30 with time 1562914481 and range 0 to 49, 2.1.66 with 1647228727 and 50 to 66,
    and 26.09 with 1697685221 and 231000 to 231000. A positive maximum is the version the
    branch was tested on, which Anki ignores (`AddonMeta.compatible`)."""

    def __init__(self) -> None:
        self.branches: dict[int, list[dict[str, Any]]] = {}

    def publish(
        self,
        version: str,
        mod: int,
        *,
        branch: int = 0,
        min_version: int = 0,
        max_version: int = 0,
        code: str = "",
    ) -> None:
        branches = self.branches.setdefault(ADDON_ID, [])
        while len(branches) <= branch:
            branches.append({})
        branches[branch] = {
            "version": version,
            "mod": mod,
            "min_version": min_version,
            "max_version": max_version,
            "code": code,
        }

    def branch_for(self, addon_id: int, client_version: int) -> tuple[int, dict[str, Any]] | None:
        chosen = None
        for index, branch in enumerate(self.branches.get(addon_id, [])):
            if not branch or branch["min_version"] > client_version:
                continue
            if branch["max_version"] < 0 and client_version > -branch["max_version"]:
                continue
            chosen = (index, branch)
        return chosen

    def addon_info(self, addon_ids: list[int], client_version: int) -> list[AddonInfo]:
        """What Anki's backend gets from ankiweb.net/svc/desktop/addon-info and passes on as
        it is. Nothing for an add-on with no branch for that version."""
        answer = []
        for addon_id in addon_ids:
            chosen = self.branch_for(addon_id, client_version)
            if chosen is not None:
                branch = chosen[1]
                answer.append(
                    AddonInfo(
                        id=addon_id,
                        modified=branch["mod"],
                        min_version=branch["min_version"],
                        max_version=branch["max_version"],
                    )
                )
        return answer

    def http_client(self) -> FakeHttpClient:
        return FakeHttpClient(self)


class FakeHttpClient:
    """What `aqt.addons.download_addon` asks of Anki's `HttpClient`. AnkiWeb redirects a
    download to a URL that carries the branch's time, range and index, which Anki reads back
    into the record (`extract_meta_from_download_url`)."""

    def __init__(self, ankiweb: FakeAnkiWeb) -> None:
        self.ankiweb = ankiweb
        self.progress_hook: Any = None

    def get(self, url: str) -> SimpleNamespace:
        parsed = urlparse(url)
        addon_id = int(parsed.path.rsplit("/", 1)[-1])
        chosen = self.ankiweb.branch_for(addon_id, int(parse_qs(parsed.query)["p"][0]))
        if chosen is None:
            return SimpleNamespace(status_code=404)
        index, branch = chosen
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as z:
            z.writestr("__init__.py", f'VERSION = "{branch["version"]}"\n{branch["code"]}')
            # As build.py writes it
            manifest = {
                "package": "tiny-addon",
                "name": "Tiny Addon",
                "human_version": branch["version"],
                "homepage": HOMEPAGE,
            }
            z.writestr("manifest.json", json.dumps(manifest))
        return SimpleNamespace(
            status_code=200,
            url=(
                f"https://ankiweb.net/shared/download/{addon_id}?t={branch['mod']}"
                f"&minpt={branch['min_version']}&maxpt={branch['max_version']}&bidx={index}"
            ),
            headers={"content-disposition": "attachment; filename=Tiny_Addon.ankiaddon"},
            content=package.getvalue(),
        )

    def stream_content(self, response: SimpleNamespace) -> bytes:
        return response.content


class Server:
    """AnkiWeb's side of a media sync: each file and when it last changed."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.changed_at: dict[str, int] = {}
        self.usn = 0

    def put(self, name: str, data: bytes) -> None:
        self.usn += 1
        self.files[name] = data
        self.changed_at[name] = self.usn


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    filecmp.clear_cache()


class AnkiDevice:
    """One device: its add-ons folder and profile folder, the Anki version it runs, and the
    Addon Config Sync it runs with what that remembers while Anki runs. While it is active,
    the running Anki's profile manager answers with its folders, so Anki's add-on manager and
    the add-on both work on them."""

    def __init__(
        self,
        root: Path,
        mw: Any,
        qtbot: Any,
        acs: ModuleType,
        ankiweb: FakeAnkiWeb,
        anki_version: int | None,
    ) -> None:
        self.mw = mw
        self.qtbot = qtbot
        self.acs = acs
        self.actions = sys.modules[f"{acs.__name__}.sync_actions"]
        self.ankiweb = ankiweb
        self.anki_version = anki_version
        self.addons = root / "addons21"
        self.addons.mkdir(parents=True)
        self.profile = root / "profile"
        self.media = self.profile / "collection.media"
        self.media.mkdir(parents=True)
        self.matches = root / "matched_copies.json"
        self.session: set[str] = set()
        self.updated: dict[str, bool] = {}
        self.mode: dict[str, bool] = {}
        self.seen_usn = 0
        self.known: dict[str, bytes] = {}

    @contextmanager
    def active(self) -> Iterator[None]:
        pm = self.mw.pm
        with ExitStack() as stack:

            def patch(target: Any, name: str, value: Any) -> None:
                stack.enter_context(mock.patch.object(target, name, value))

            patch(pm, "addonFolder", lambda: str(self.addons))
            patch(pm, "profileFolder", lambda create=True: str(self.profile))
            # Where loadAddons imports from: Anki puts its add-ons folder on sys.path
            patch(sys, "path", [str(self.addons), *sys.path])
            if self.anki_version is not None:
                patch(aqt.addons, "_current_version", self.anki_version)
            matched = sys.modules.get(f"{self.acs.__name__}.matched_copies")
            if matched is not None:
                patch(matched, "state_file", lambda: self.matches)
            if hasattr(self.actions, "OVERWRITTEN_THIS_SESSION"):
                patch(self.actions, "OVERWRITTEN_THIS_SESSION", self.session)
            if self.mode:
                config = {
                    "addon_settings": {},
                    "run_on_sync": True,
                    "show_summary_on_sync": False,
                    "ask_on_sync": False,
                    **self.mode,
                }
                patch(self.acs, "get_main_config", lambda: dict(config))
                dialog = sys.modules[f"{self.acs.__name__}.config_manager_dialog"]
                patch(dialog, "get_main_config", lambda: dict(config))
            # The dialog holds this dict itself, so its contents are what is swapped
            state = self.actions.UPDATED_STATE
            outside = dict(state)
            state.clear()
            state.update(self.updated)
            filecmp.clear_cache()
            try:
                yield
            finally:
                self.updated = dict(state)
                state.clear()
                state.update(outside)

    # The files

    def meta(self) -> dict[str, Any]:
        return json.loads((self.addons / FOLDER / "meta.json").read_text(encoding="utf-8"))

    def code_version(self) -> str:
        """The VERSION in the add-on's code on disk."""
        code = (self.addons / FOLDER / "__init__.py").read_text(encoding="utf-8")
        found = re.search(r'^VERSION = "(.*)"$', code, re.M)
        assert found is not None
        return found.group(1)

    def set_config(self, **values: Any) -> None:
        """The user changes the config: Anki writes the whole meta.json again."""
        with self.active():
            manager = self.mw.addonManager
            manager.writeConfig(FOLDER, {**manager.addonMeta(FOLDER).get("config", {}), **values})

    # What the add-on does on Anki's sync hooks, called as the hooks call it

    def sync_starts(self) -> list[str]:
        """`sync_will_start`, which only a collection sync fires. What the add-on sent."""
        with self.active():
            self.acs.sync_on_save()
        return list(self.acs.sync_saved_addons)

    def media_sync(self, server: Server) -> None:
        """What the server changed since this device last synced is downloaded first, also
        over a file changed here, and what is then still changed here is uploaded."""
        for name, usn in sorted(server.changed_at.items()):
            if usn > self.seen_usn:
                _write(self.media / name, server.files[name])
                self.known[name] = server.files[name]
        for path in sorted(self.media.glob("_*_meta.json")):
            data = path.read_bytes()
            if self.known.get(path.name) != data:
                server.put(path.name, data)
                self.known[path.name] = data
        self.seen_usn = server.usn

    def media_sync_stops(self) -> list[str]:
        """`media_sync_did_start_or_stop(False)`. What the add-on read, or listed in ask mode."""
        with self.active():
            self.acs.read_on_sync(False)
        return list(self.acs.sync_loaded_addons)

    def syncs(self, server: Server) -> list[str]:
        """The sync on opening or closing a profile, or from the sync button."""
        self.sync_starts()
        self.media_sync(server)
        return self.media_sync_stops()

    def restarts_on(self, acs: ModuleType) -> None:
        """Anki started again with another version of the add-on: what it remembered is
        gone, the files stay."""
        self.acs = acs
        self.actions = sys.modules[f"{acs.__name__}.sync_actions"]
        self.session = set()
        self.updated = {}

    # What Anki does

    def anki_installs(self) -> None:
        """Get Add-ons with the add-on's code: Anki downloads the branch AnkiWeb has for its
        version and installs it."""
        with self.active():
            _, result = aqt.addons.download_and_install_addon(
                self.mw.addonManager, cast(HttpClient, self.ankiweb.http_client()), ADDON_ID
            )
        assert isinstance(result, aqt.addons.InstallOk), result

    def anki_checks_for_updates(
        self, accept: bool = True, by_user: bool = False, untick: bool = False
    ) -> list[tuple[str, bool]]:
        """Anki's update check: the one it runs by itself once a day as a profile opens, or
        with `by_user` the add-ons dialog's Check for Updates. What its list of updates shows:
        each add-on's name and whether it is ticked, and nothing when it shows no list. With
        `accept` the user clicks OK, after unticking every add-on with `untick`, and Anki
        downloads and installs what is ticked."""
        shown: list[tuple[str, bool]] = []
        state: dict[str, Any] = {"handled": False, "downloading": False, "log": []}
        handle_update_info = aqt.addons.handle_update_info

        def handle(*args: Any, **kwargs: Any) -> None:
            try:
                handle_update_info(*args, **kwargs)
            finally:
                state["handled"] = True

        def ask(dialog: Any, on_done: Callable[[list[int]], None]) -> None:
            listed = dialog.addons_list_widget
            for row in range(1, listed.count()):
                item = listed.item(row)
                shown.append((item.text().split("   ", 1)[1], listed.checked(item)))
                if untick:
                    listed.check_item(item, Qt.CheckState.Unchecked)
            if accept:
                dialog._on_done = on_done
                state["downloading"] = bool(listed.get_selected_addon_ids())
                dialog.accept()
            else:
                dialog.reject()
            dialog.deleteLater()

        def done(log: list[Any]) -> None:
            state["log"] = log
            state["downloading"] = False

        with self.active(), ExitStack() as stack:
            for name, value in (
                (
                    "_fetch_update_info_batch",
                    lambda chunk: self.ankiweb.addon_info(list(chunk), aqt.addons._current_version),
                ),
                ("HttpClient", self.ankiweb.http_client),
                ("handle_update_info", handle),
            ):
                stack.enter_context(mock.patch.object(aqt.addons, name, value))
            stack.enter_context(
                mock.patch.object(aqt.addons.ChooseAddonsToUpdateDialog, "ask", ask)
            )
            aqt.addons.check_and_prompt_for_updates(
                self.mw, self.mw.addonManager, done, requested_by_user=by_user
            )
            self.qtbot.waitUntil(
                lambda: state["handled"] and not state["downloading"], timeout=15000
            )
        for _, entry in state["log"]:
            assert isinstance(entry, aqt.addons.InstallOk), entry
        return shown

    def anki_loads_addons(self) -> bool:
        """Anki starting: `loadAddons` imports every add-on that is enabled and `compatible()`
        with its version, and passes over the others without a word. Whether it imported
        this one."""
        with self.active():
            sys.modules.pop(FOLDER, None)
            importlib.invalidate_caches()
            self.mw.addonManager.loadAddons()
            return FOLDER in sys.modules

    @contextmanager
    def _sync_hooks(self, fired: list[str]) -> Iterator[None]:
        """Anki's sync hooks with only this device's add-on on them, and a note of each firing.
        The hooks also hold the handlers of the main windows of earlier tests, which are gone."""

        def will_start() -> None:
            fired.append("sync will start")

        def media(running: bool) -> None:
            fired.append("media sync started" if running else "media sync stopped")

        with ExitStack() as stack:
            for hook, handlers in (
                (gui_hooks.sync_will_start, [will_start, self.acs.sync_on_save]),
                (gui_hooks.media_sync_did_start_or_stop, [media, self.acs.read_on_sync]),
                (gui_hooks.sync_did_finish, []),
                (gui_hooks.media_sync_did_progress, []),
                # Fired by the refresh after a collection sync (`mw.reset`)
                (gui_hooks.operation_did_execute, []),
                (gui_hooks.state_did_reset, []),
            ):
                stack.enter_context(mock.patch.object(hook, "_hooks", handlers))
            yield

    def anki_syncs_media_by_itself(self, server: Server) -> list[str]:
        """Anki's five-minute timer once its periodic media sync is due (15 minutes by
        default), through its own MediaSyncer with the transfer replaced. What fired."""
        fired: list[str] = []
        synced = threading.Event()

        def sync_media(auth: Any) -> None:
            self.media_sync(server)
            synced.set()

        with self.active(), self._sync_hooks(fired), ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(self.mw.pm, "sync_auth", lambda: SyncAuth(hkey="test"))
            )
            stack.enter_context(mock.patch.object(self.mw.col, "sync_media", sync_media))
            stack.enter_context(
                mock.patch.object(
                    self.mw.col,
                    "media_sync_status",
                    lambda: SimpleNamespace(active=not synced.is_set(), progress=None),
                )
            )
            self.mw.media_syncer._last_progress_at = 0
            self.mw.on_periodic_sync_timer()
            self.qtbot.waitUntil(
                lambda: fired[-1:] == ["media sync stopped"] and self.mw._background_op_count == 0,
                timeout=15000,
            )
        return fired

    def anki_opens_profile(
        self, server: Server, while_media_syncs: Callable[[], None]
    ) -> list[str]:
        """Anki opening a profile with sync on open, through its own sync wiring with the
        network left out. `loadProfile` starts its add-on update check when the collection
        sync is done (`_onsuccess`); the media sync that sync started is then still running,
        and `while_media_syncs` is what happens before it ends. What fired."""
        fired: list[str] = []
        media_synced = threading.Event()

        with self.active(), self._sync_hooks(fired), ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(self.mw.pm, "sync_auth", lambda: SyncAuth(hkey="test"))
            )
            stack.enter_context(
                mock.patch.object(
                    self.mw.col,
                    "sync_collection",
                    lambda auth, sync_media: SyncOutput(required=SyncOutput.NO_CHANGES),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.mw.col,
                    "media_sync_status",
                    lambda: SimpleNamespace(active=not media_synced.is_set(), progress=None),
                )
            )
            self.mw.maybe_auto_sync_on_open_close(
                lambda synced: fired.append("collection sync done")
            )
            self.qtbot.waitUntil(lambda: "collection sync done" in fired, timeout=15000)
            assert self.mw.media_syncer.is_syncing()
            while_media_syncs()
            self.media_sync(server)
            media_synced.set()
            self.qtbot.waitUntil(lambda: fired[-1:] == ["media sync stopped"], timeout=15000)
        return fired


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


def shown_status(acs: ModuleType) -> str:
    """The status the add-on's dialog, opened by a sync, shows for the test's add-on. The
    dialog is closed after."""
    dialogs = sys.modules[f"{acs.__name__}.config_manager_dialog"].ACTIVE_MANAGER_DIALOGS
    assert len(dialogs) == 1
    dialog = dialogs[0]
    try:
        for row in range(dialog.table.rowCount()):
            if dialog.table.cellWidget(row, 0).findChild(QLabel).text().endswith(f"({FOLDER})"):
                return dialog.table.cellWidget(row, 1).text()
        raise AssertionError("the add-on is not listed")
    finally:
        dialog.close()


def both_installed_and_synced(
    make_device: Callable[..., AnkiDevice],
    ankiweb: FakeAnkiWeb,
    a_runs: str = "today",
    b_runs: str = "today",
) -> tuple[AnkiDevice, AnkiDevice, Server]:
    """Version 1.0 installed on two devices, set up on the first and synced to the second."""
    ankiweb.publish("1.0", T1)
    server = Server()
    a, b = make_device("a", a_runs), make_device("b", b_runs)
    a.anki_installs()
    a.set_config(speed=1)
    a.syncs(server)
    b.anki_installs()
    b.syncs(server)
    assert a.meta() == b.meta()
    assert b.meta()["config"] == {"speed": 1} and b.meta()["mod"] == T1
    return a, b, server


# Each device keeps its own record


@pytest.mark.parametrize("b_runs", ["today", "released"])
def test_an_update_on_one_device_is_still_offered_on_the_other(make_device, ankiweb, b_runs):
    """With the config changed beside the update, so that the copy is written. A device on
    the released version reads the copy whole, and the copy has kept its record."""
    a, b, server = both_installed_and_synced(make_device, ankiweb, b_runs=b_runs)
    ankiweb.publish("2.0", T2)
    assert a.anki_checks_for_updates() == [("Tiny Addon", True)]
    a.set_config(speed=2)

    a.syncs(server)
    assert a.acs.sync_saved_addons == [FOLDER]
    assert a.meta()["mod"] == T2
    assert b.syncs(server) == [FOLDER]
    assert b.meta()["config"] == {"speed": 2}
    assert b.meta()["mod"] == T1 and b.meta()["human_version"] == "1.0"
    assert b.anki_checks_for_updates() == [("Tiny Addon", True)]
    assert b.code_version() == "2.0" and b.meta()["mod"] == T2


@pytest.mark.parametrize("b_runs", ["today", "released"])
def test_a_device_that_updated_keeps_its_record_when_it_reads_another_devices_config(
    make_device, ankiweb, b_runs
):
    a, b, server = both_installed_and_synced(make_device, ankiweb, b_runs=b_runs)
    ankiweb.publish("2.0", T2)
    a.anki_checks_for_updates()
    # The device that has not updated changes the config, and syncs first
    b.set_config(speed=2)
    b.syncs(server)

    assert a.syncs(server) == [FOLDER]
    assert a.meta()["config"] == {"speed": 2}
    assert a.meta()["mod"] == T2 and a.meta()["human_version"] == "2.0"
    assert a.anki_checks_for_updates(accept=False) == []


def test_the_media_sync_anki_runs_by_itself_leaves_an_update_alone(make_device, ankiweb):
    a, _, server = both_installed_and_synced(make_device, ankiweb)
    ankiweb.publish("2.0", T2)
    a.anki_checks_for_updates()

    assert a.anki_syncs_media_by_itself(server) == ["media sync started", "media sync stopped"]
    assert a.acs.sync_loaded_addons == []
    assert a.meta()["mod"] == T2 and a.meta()["human_version"] == "2.0"
    assert a.anki_checks_for_updates(accept=False) == []


def test_an_update_taken_while_the_media_sync_of_opening_a_profile_runs_stays(make_device, ankiweb):
    a, _, server = both_installed_and_synced(make_device, ankiweb)
    ankiweb.publish("2.0", T2)
    taken: list[list[tuple[str, bool]]] = []

    fired = a.anki_opens_profile(server, lambda: taken.append(a.anki_checks_for_updates()))
    assert fired == [
        "sync will start",
        "media sync started",
        "collection sync done",
        "media sync stopped",
    ]
    assert taken == [[("Tiny Addon", True)]]
    assert a.meta()["mod"] == T2 and a.code_version() == "2.0"


def test_a_new_install_on_anki_2_1_66_keeps_its_own_range(make_device, ankiweb):
    """Two branches with the times and ranges AnkiWeb gave add-on 3918629684 (FakeAnkiWeb).
    Only Anki 2.1.66's install and loading are run here, which are 26.09's but for names.
    Its update check is not: it takes the branch from the record's `branch_index` as well,
    which is now its own too."""
    ankiweb.publish("2.1.66 branch", 1647228727, branch=1, min_version=50, max_version=66)
    ankiweb.publish("23.10 branch", 1697685221, branch=2, min_version=231000, max_version=231000)
    server = Server()
    new, old = make_device("new"), make_device("old", anki_version=66)
    new.anki_installs()
    new.set_config(speed=1)
    new.syncs(server)
    old.anki_installs()

    # A new install is given the config of the copy
    assert old.syncs(server) == [FOLDER]
    assert old.meta()["config"] == {"speed": 1}
    assert old.meta()["min_point_version"] == 50 and old.meta()["branch_index"] == 1
    assert old.anki_loads_addons() is True


def test_two_anki_versions_each_keep_the_range_of_their_own_update_checks(make_device, ankiweb):
    """Anki 23.10 to 26.09 run the same update check. A branch for Anki 25.07 and later,
    which ship Python 3.13, beside one for the versions before."""
    ankiweb.publish("before 25.07", 1_750_000_000, min_version=50, max_version=250200)
    ankiweb.publish("25.07 on", 1_760_000_000, branch=1, min_version=250700, max_version=260900)
    server = Server()
    new, old = make_device("new"), make_device("old", anki_version=250200)
    new.anki_installs()
    new.set_config(speed=1)
    new.syncs(server)
    old.anki_installs()
    assert old.syncs(server) == [FOLDER]
    assert old.meta()["min_point_version"] == 50

    for device in (old, new, old, new):
        assert device.anki_checks_for_updates() == []
        device.syncs(server)
    assert old.meta()["min_point_version"] == 50 and old.anki_loads_addons() is True
    assert new.meta()["min_point_version"] == 250700 and new.anki_loads_addons() is True


def test_a_new_install_on_anki_26_keeps_its_own_record_beside_one_from_2_1_66(make_device, ankiweb):
    ankiweb.publish("2.1.66 branch", 1647228727, branch=1, min_version=50, max_version=66)
    ankiweb.publish("23.10 branch", 1697685221, branch=2, min_version=231000, max_version=231000)
    server = Server()
    new, old = make_device("new"), make_device("old", anki_version=66)
    old.anki_installs()
    old.set_config(speed=1)
    old.syncs(server)
    new.anki_installs()

    assert new.syncs(server) == [FOLDER]
    assert new.meta()["config"] == {"speed": 1} and new.meta()["mod"] == 1697685221
    assert new.anki_checks_for_updates() == []


@pytest.mark.parametrize("mode, status", [("summary", "Updated"), ("ask", "Changed")])
def test_only_a_change_of_config_made_on_another_device_is_listed(
    make_device, ankiweb, mode, status
):
    a, b, server = both_installed_and_synced(make_device, ankiweb)
    ankiweb.publish("2.0", T2)
    a.anki_checks_for_updates()
    a.syncs(server)
    if mode == "summary":
        b.mode = {"show_summary_on_sync": True}
    else:
        b.mode = {"run_on_sync": False, "ask_on_sync": True}

    assert b.syncs(server) == []
    assert sys.modules[f"{b.acs.__name__}.config_manager_dialog"].ACTIVE_MANAGER_DIALOGS == []

    a.set_config(speed=2)
    a.syncs(server)
    assert b.syncs(server) == [FOLDER]
    assert shown_status(b.acs) == status


def test_the_dialog_shows_an_update_installed_here_as_no_change(make_device, ankiweb):
    """The copy keeps the record of 1.0 while this device has 2.0, and Anki filled in
    `disabled` and `update_enabled` when the update was taken from its list."""
    _, b, server = both_installed_and_synced(make_device, ankiweb)
    ankiweb.publish("2.0", T2)
    b.anki_checks_for_updates()
    assert b.syncs(server) == []
    assert b.meta()["mod"] == T2 and b.meta()["update_enabled"] is True

    with b.active():
        manager = sys.modules[f"{b.acs.__name__}.config_manager_dialog"]
        dialog = manager.open_addon_config_manager(blocking=False)
        assert dialog.read_json_lines(b.addons / FOLDER / "meta.json") == dialog.read_json_lines(
            b.media / f"_{FOLDER}_meta.json"
        )
    assert shown_status(b.acs) == "Up to date"


def test_a_device_on_1_0_0_told_that_it_has_this_version_is_told_no_longer(make_device, addon):
    """The first sync after Anki installs this version still runs 1.0.0, which sends this
    add-on's meta.json whole, new `mod` and all. A device on 1.0.0 that reads it is told it
    has this version, and its Anki never offers it. Once the updated device syncs on this
    version, the copy loses its `mod`, and so does the meta.json of a device on 1.0.0 that
    reads it; Anki then offers that device the update, as
    test_without_a_record_anki_offers_the_addon_as_an_update_under_its_folder_name shows."""
    server = Server()
    a, b = make_device("a", "released"), make_device("b", "released")
    own = addon.__name__
    released = {"name": "Addon Config Sync", "mod": T1, "config": {"run_on_sync": True}}
    for device in (a, b):
        (device.addons / own).mkdir()
        _write(device.addons / own / "meta.json", json.dumps(released).encode("utf-8"))
        device.syncs(server)

    # Anki installs this version on a, whose sync as it closes still runs 1.0.0
    updated = json.dumps(dict(released, mod=T2)).encode("utf-8")
    _write(a.addons / own / "meta.json", updated)
    a.syncs(server)
    b.syncs(server)
    assert json.loads((b.addons / own / "meta.json").read_bytes())["mod"] == T2

    a.restarts_on(addon)
    a.syncs(server)
    b.syncs(server)
    assert "mod" not in json.loads((b.addons / own / "meta.json").read_bytes())
    assert (a.addons / own / "meta.json").read_bytes() == updated
    assert a.sync_starts() == [] and b.syncs(server) == []


# What Anki does that the rule rests on


def test_an_update_keeps_what_the_user_set_and_rewrites_the_record(make_device, ankiweb):
    ankiweb.publish("1.0", T1, min_version=50)
    device = make_device("device")
    device.anki_installs()
    record = {
        "name": "Tiny Addon",
        "mod": T1,
        "min_point_version": 50,
        "max_point_version": 0,
        "branch_index": 0,
        "human_version": "1.0",
        "homepage": HOMEPAGE,
    }
    assert device.meta() == record
    device.set_config(speed=1)
    with device.active():
        device.mw.addonManager.toggleEnabled(FOLDER, enable=False)

    ankiweb.publish("2.0", T2, min_version=231000)
    assert device.anki_checks_for_updates() == [("Tiny Addon", True)]
    assert device.meta() == {
        **record,
        "mod": T2,
        "min_point_version": 231000,
        "human_version": "2.0",
        "conflicts": [],
        "config": {"speed": 1},
        "disabled": True,
        "update_enabled": True,
    }
    assert set(device.meta()) - {"config", "disabled", "update_enabled"} == ANKIS_RECORD
    assert device.actions.INSTALL_KEYS == ANKIS_RECORD


def test_without_a_record_anki_offers_the_addon_as_an_update_under_its_folder_name(
    make_device, ankiweb
):
    ankiweb.publish("1.0", T1)
    device = make_device("device")
    (device.addons / FOLDER).mkdir()
    (device.addons / FOLDER / "__init__.py").write_text('VERSION = "0.9"\n', encoding="utf-8")
    with device.active():
        device.mw.addonManager.writeConfig(FOLDER, {"speed": 1})
    assert device.meta() == {"config": {"speed": 1}}

    assert device.anki_checks_for_updates() == [(FOLDER, True)]
    assert device.code_version() == "1.0"
    assert device.meta()["mod"] == T1 and device.meta()["config"] == {"speed": 1}


def test_an_addon_unticked_in_the_list_of_updates_is_not_offered_when_a_profile_opens(
    make_device, ankiweb
):
    ankiweb.publish("1.0", T1)
    device = make_device("device")
    device.anki_installs()
    ankiweb.publish("2.0", T2)

    assert device.anki_checks_for_updates(untick=True) == [("Tiny Addon", True)]
    assert device.meta()["update_enabled"] is False and device.code_version() == "1.0"
    assert device.anki_checks_for_updates() == []
    assert device.anki_checks_for_updates(by_user=True, accept=False) == [("Tiny Addon", False)]


def test_an_update_leaves_the_code_that_was_loaded_running_until_anki_restarts(
    make_device, ankiweb
):
    ankiweb.publish("1.0", T1, code=REGISTERS_HANDLER)
    device = make_device("device")
    device.anki_installs()
    assert device.anki_loads_addons() is True
    handler = gui_hooks.sync_will_start._hooks[-1]

    ankiweb.publish("2.0", T2, code=REGISTERS_HANDLER)
    assert device.anki_checks_for_updates() == [("Tiny Addon", True)]
    assert device.code_version() == "2.0" and device.meta()["mod"] == T2
    assert sys.modules[FOLDER].VERSION == "1.0"
    assert gui_hooks.sync_will_start._hooks[-1] is handler
    handler()
    assert handler.ran == "1.0"


@pytest.mark.parametrize("runs", ["released", "today"])
def test_the_addon_sends_its_own_meta_json_as_it_sends_any_other(make_device, runs):
    device = make_device("device", runs)
    (device.addons / "addon_config_sync").mkdir()
    with device.active():
        device.mw.addonManager.writeConfig("addon_config_sync", {"run_on_sync": True})

    assert "addon_config_sync" in device.sync_starts()
    assert (device.media / "_addon_config_sync_meta.json").is_file()
