"""Addon Config Sync's file logic over folders made up for each test.

The add-on's two sync handlers come down to `save_configs_on_sync` and `read_configs_on_sync`
copying files between an add-ons folder and a profile's media folder. A `Device` here is one
such pair of folders, with the file the add-on keeps its matches in and what it remembers
for as long as Anki runs, and its methods call those functions with the arguments Anki's
hooks give them. A `Server` is the other end of a media sync, as far as these files go, for
the tests that need two devices.

Nothing of the machine's own Anki is read or written. The stub `mw.pm` answers with the
test's folders, and the matches file, which the add-on keeps beside its own code and so in
this checkout, is pointed into them as well: for every test, whether or not it uses a
`Device`.
"""

from __future__ import annotations

import filecmp
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Sequence
from unittest import mock

import pytest

from addon_config_sync import matched_copies, sync_actions


class Saved(NamedTuple):
    """The lists `save_configs_on_sync` fills."""

    saved: list[str]
    disabled: list[str]
    skipped: list[str]


class Read(NamedTuple):
    """The lists `read_configs_on_sync` fills, and how often it called its finish callback."""

    loaded: list[str]
    disabled: list[str]
    missing: list[str]
    finished: int


class Server:
    """The server's side of a media sync: each file's bytes and when they last changed."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.changed_at: dict[str, int] = {}
        self.usn = 0

    def put(self, name: str, data: bytes) -> None:
        self.usn += 1
        self.files[name] = data
        self.changed_at[name] = self.usn


class Device:
    """An add-ons folder, a profile's media folder, the add-on's matches file, and an Anki
    running on them."""

    def __init__(self, root: Path, profile: str = "User 1") -> None:
        self.root = root
        self.addons = root / "addons21"
        self.addons.mkdir(parents=True)
        self.matches = root / "matched_copies.json"
        # sync_actions.OVERWRITTEN_THIS_SESSION, as this device's Anki has it
        self.session: set[str] = set()
        self.open_profile(profile)
        # What the media sync of this device last saw: the server's counter, and each file
        self.seen_usn = 0
        self.known: dict[str, bytes] = {}

    def open_profile(self, profile: str) -> None:
        """Another profile of the same Anki: its own media folder, the same add-ons."""
        self.profile = profile
        self.media = self.root / profile / "collection.media"
        self.media.mkdir(parents=True, exist_ok=True)

    # The files

    def meta_path(self, addon_id: str) -> Path:
        return self.addons / addon_id / "meta.json"

    def media_path(self, addon_id: str) -> Path:
        return self.media / f"_{addon_id}_meta.json"

    def install(self, addon_id: str, meta: dict[str, Any] | None = None) -> None:
        """The add-on's folder, with a meta.json when `meta` is given."""
        (self.addons / addon_id).mkdir(exist_ok=True)
        if meta is not None:
            self.write_meta(addon_id, meta)

    def write_meta(self, addon_id: str, meta: dict[str, Any]) -> None:
        _write(self.meta_path(addon_id), json.dumps(meta))

    def write_media(self, addon_id: str, meta: dict[str, Any]) -> None:
        """A media copy as a download leaves one: the file replaced, nothing else told."""
        _write(self.media_path(addon_id), json.dumps(meta))

    def write_text(self, path: Path, text: str) -> None:
        """Any of the files, with what JSON cannot hold: a file cut short, say."""
        _write(path, text)

    def meta(self, addon_id: str) -> Any:
        return json.loads(self.meta_path(addon_id).read_text(encoding="utf-8"))

    def media_copy(self, addon_id: str) -> Any:
        return json.loads(self.media_path(addon_id).read_text(encoding="utf-8"))

    def set_config(self, addon_id: str, **values: Any) -> None:
        """Change the add-on's config as Anki does: the whole meta.json written again."""
        meta = self.meta(addon_id)
        meta.setdefault("config", {}).update(values)
        self.write_meta(addon_id, meta)

    # What Anki's hooks call

    @contextmanager
    def active(self) -> Iterator[None]:
        """This device as the one `mw.pm`, the matches file and the session belong to."""
        pm = sync_actions.mw.pm
        with ExitStack() as stack:
            for name, value in (
                ("addonFolder", lambda: str(self.addons)),
                ("profileFolder", lambda: str(self.media.parent)),
            ):
                stack.enter_context(mock.patch.object(pm, name, value, create=True))
            stack.enter_context(
                mock.patch.object(matched_copies, "state_file", lambda: self.matches)
            )
            stack.enter_context(
                mock.patch.object(sync_actions, "OVERWRITTEN_THIS_SESSION", self.session)
            )
            yield

    def sync_starts(self, ignored: Sequence[str] = ()) -> Saved:
        """`sync_will_start`, which only a collection sync fires."""
        result = Saved([], [], [])
        with self.active():
            sync_actions.save_configs_on_sync(*result, ignored_addons=set(ignored))
        return Saved(sorted(result.saved), sorted(result.disabled), sorted(result.skipped))

    def media_sync_stops(
        self, ignored: Sequence[str] = (), apply: bool = True, running: bool = False
    ) -> Read:
        """`media_sync_did_start_or_stop`, with False unless `running`. `apply` is False in
        the mode that asks instead of copying."""
        loaded: list[str] = []
        disabled: list[str] = []
        missing: list[str] = []
        finished: list[int] = []
        with self.active():
            sync_actions.read_configs_on_sync(
                loaded,
                disabled,
                missing,
                lambda: finished.append(1),
                running,
                ignored_addons=set(ignored),
                apply_to_addons=apply,
            )
        return Read(sorted(loaded), sorted(disabled), sorted(missing), len(finished))

    def calls(self, function: Callable[..., Any], *args: Any) -> Any:
        """Any other function of `sync_actions`, as this device: the three behind the manager
        dialog's buttons."""
        with self.active():
            return function(*args)

    def restarts(self) -> None:
        """Anki is closed and started again: the files stay, what it remembered does not."""
        self.session = set()

    def held(self) -> dict[str, str]:
        """What the matches file holds for the open profile, by add-on id."""
        state = json.loads(self.matches.read_text(encoding="utf-8"))
        return state["profiles"].get(str(self.media.parent), {})

    # A media sync, and the two kinds of sync Anki runs

    def media_sync(self, server: Server) -> None:
        """One media sync by Anki's rule: what the server changed since this device last
        synced is downloaded first, also over a file changed here, and what is then still
        changed here is uploaded."""
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

    def syncs(self, server: Server, apply: bool = True) -> Read:
        """A sync from the sync button, or the one on opening or closing a profile."""
        self.sync_starts()
        self.media_sync(server)
        return self.media_sync_stops(apply=apply)

    def syncs_media_only(self, server: Server, apply: bool = True) -> Read:
        """The media sync Anki starts by itself every few minutes: no `sync_will_start`."""
        self.media_sync(server)
        return self.media_sync_stops(apply=apply)


def _write(path: Path, content: str | bytes) -> None:
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    # `filecmp.cmp` remembers its answer per pair of paths, sizes and modification times, and
    # two writes of the same length can land within one tick of the file system's clock
    filecmp.clear_cache()


@pytest.fixture(autouse=True)
def module_state() -> Iterator[None]:
    """`filecmp`'s cache and the add-on's `UPDATED_STATE` are the process's, not a test's."""
    filecmp.clear_cache()
    sync_actions.UPDATED_STATE.clear()
    yield
    sync_actions.UPDATED_STATE.clear()


@pytest.fixture(autouse=True)
def nothing_of_this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse: no test may reach the checkout's own `user_files/`, which is the matches
    file of the Anki this checkout is installed in, nor find the session of a test before
    it. Both are a `Device`'s own while it is active."""
    monkeypatch.setattr(matched_copies, "state_file", lambda: tmp_path / "no_device.json")
    monkeypatch.setattr(sync_actions, "OVERWRITTEN_THIS_SESSION", set())


@pytest.fixture
def make_device(tmp_path: Path) -> Callable[..., Device]:
    """`make_device("a")`: a device of its own under the test's directory."""

    def make(name: str = "device", profile: str = "User 1") -> Device:
        return Device(tmp_path / name, profile)

    return make


@pytest.fixture
def device(make_device: Callable[..., Device]) -> Device:
    return make_device()


@pytest.fixture
def server() -> Server:
    return Server()
