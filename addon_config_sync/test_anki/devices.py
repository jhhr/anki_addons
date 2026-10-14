"""Two devices on one running Anki, with AnkiWeb and media syncs faked: where the real-Anki
tests follow an add-on, or what Anki records about it, from one device to another.

A real Anki does what it does, with its own download, install, update check, add-on loading
and sync wiring, and a test follows it to a second device, which runs this add-on or the
released one. Only the network is left out: AnkiWeb is `FakeAnkiWeb`, and a media sync
is `Server` and `AnkiDevice.media_sync`, which keep Anki's rule that a file changed on the
server is downloaded even over one changed here (rslib's `determine_required_change`:
"differs from server, favour server"). A device is an add-ons folder and a profile folder
that the running Anki's profile manager is pointed at.
"""

from __future__ import annotations

import filecmp
import importlib
import io
import json
import re
import sys
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

REPO = Path(__file__).resolve().parents[2]
# Addon Config Sync as released (1.0.0): its last state on origin/main before matches were
# kept, which copies the whole file both ways
RELEASED = "8d1d24f2104d8251651b7e3bdbd0c8e17cc66707"
RELEASED_AS = "addon_config_sync_released"


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
        # The add-on of each download asked for, in order
        self.downloads: list[int] = []

    def publish(
        self,
        version: str,
        mod: int,
        *,
        branch: int = 0,
        min_version: int = 0,
        max_version: int = 0,
        code: str = "",
        addon_id: int = ADDON_ID,
    ) -> None:
        branches = self.branches.setdefault(addon_id, [])
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
        self.ankiweb.downloads.append(addon_id)
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


def write_file(path: Path, data: bytes) -> None:
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
                write_file(self.media / name, server.files[name])
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


def row_of(dialog: Any, addon_id: str) -> int:
    """The row of the add-on's dialog that lists this add-on."""
    for row in range(dialog.table.rowCount()):
        if dialog.table.cellWidget(row, 0).findChild(QLabel).text().endswith(f"({addon_id})"):
            return row
    raise AssertionError(f"{addon_id} is not listed")


def row_status(dialog: Any, addon_id: str) -> str:
    return dialog.table.cellWidget(row_of(dialog, addon_id), 1).text()


def shown_status(acs: ModuleType) -> str:
    """The status the add-on's dialog, opened by a sync, shows for the test's add-on. The
    dialog is closed after."""
    dialogs = sys.modules[f"{acs.__name__}.config_manager_dialog"].ACTIVE_MANAGER_DIALOGS
    assert len(dialogs) == 1
    dialog = dialogs[0]
    try:
        return row_status(dialog, FOLDER)
    finally:
        dialog.close()
