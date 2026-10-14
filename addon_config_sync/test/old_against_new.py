"""The sync handlers from before matches were kept, run beside today's over random histories.

Not collected with the suite (its name does not start with `test_`), because it takes
minutes and needs the repository's history. Run it when `read_configs_on_sync`, or anything
it decides by, is about to change, from the repo root:

    python -m pytest addon_config_sync/test/old_against_new.py -s

That is 150 histories of 60 steps, about two minutes. ACS_HISTORIES (first:last) and
ACS_STEPS in the environment make it another run: 0:600 and 80 took eleven minutes.

Until `matched_copies.py`, a media sync stopping copied every media copy over a meta.json
that differed from it. That is the reference: whatever the add-on leaves alone today, it
must copy nothing the old code did not, send what the old code sent, and list in ask mode
what the old code listed. Each history is two devices with two profiles each and two
add-ons, doing at random what users and Anki do: configs edited, add-ons updated, switched
off, installed again, files cut short, syncs, media syncs with none before them, restarts.
Before every call of a handler the device's folders are copied and the old handler is run
on the copy, so both start from the same files. Checked at each call:

  SAVE    a sync start leaves the same media copies and fills the same lists as the old code
  ASK     in ask mode a media sync stopping fills the same lists and changes no file
  SUBSET  what the new code copies when a media sync stops, the old code copies too, to the
          same bytes
  SKIP    a copy the old code makes and the new one does not is one of a config that only
          this device changed, as an oracle that does not read the add-on's state has it:
          the pair matched at some sync, the media copy is still those bytes, no media copy
          was copied over that config since Anki started, the meta.json is a JSON object
          with a config if the copy has one, and the two differ in more than what Anki
          records of an install

The oracle is the rule of `read_configs_on_sync` written a second time, from what happened
and not from what the add-on kept. A change to the rule is a change to `Oracle`, made first
and on purpose. `ACS_BREAK=session` (or `install`, `config`, `object`, `bytes`, `ask`, `save`)
switches one part of the add-on's rule off in memory for a run, to see the check for it
fail; do that after touching either.

The old handlers are `sync_actions.py` as of BEFORE_MATCHES, loaded beside today's modules,
so they run on today's `utils.py`: they ask it only for helpers that have not changed since.
"""

from __future__ import annotations

import filecmp
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator
from unittest import mock

import pytest

from addon_config_sync import matched_copies, sync_actions as new

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# The last revision of sync_actions.py whose read step copied every difference
BEFORE_MATCHES = "3ed59d46fc6d4066944d988056980ba62aec5512"

ADDONS = ["111", "222"]
PROFILES = ["User 1", "Second"]
# Written out again here rather than read from sync_actions: the oracle is a second opinion
INSTALL_KEYS = {
    "name",
    "mod",
    "conflicts",
    "min_point_version",
    "max_point_version",
    "branch_index",
    "human_version",
    "homepage",
}


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The suite's Device and Server, under a name of this file's own
kit = _load("_old_against_new_kit", HERE / "conftest.py")


@pytest.fixture(scope="module")
def old(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    """`sync_actions.py` from before matches, as a module of the add-on's package."""
    shown = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{BEFORE_MATCHES}:addon_config_sync/sync_actions.py"],
        capture_output=True,
    )
    if shown.returncode != 0:
        pytest.skip(f"this checkout has no revision {BEFORE_MATCHES[:8]} to compare with")
    source = tmp_path_factory.mktemp("before_matches") / "sync_actions.py"
    source.write_bytes(shown.stdout)
    return _load("addon_config_sync._sync_actions_before_matches", source)


def write(path: Path, content: str | bytes) -> None:
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    # filecmp remembers its answer per pair of paths, sizes and modification times
    filecmp.clear_cache()


def read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def as_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return sorted((key, canonical(item)) for key, item in value.items())
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def same(meta: Path, media: Path) -> bool:
    """The two files matching, as the add-on means it: by bytes, or as JSON whatever the
    order of the keys."""
    a, b = read(meta), read(media)
    if a is None or b is None:
        return False
    if a == b:
        return True
    try:
        return bool(
            canonical(json.loads(a.decode("utf-8"))) == canonical(json.loads(b.decode("utf-8")))
        )
    except ValueError:
        return False


def users_part(meta: dict[str, Any]) -> dict[str, Any]:
    part = {key: value for key, value in meta.items() if key not in INSTALL_KEYS}
    part["disabled"] = bool(meta.get("disabled"))
    part["update_enabled"] = meta.get("update_enabled", True)
    return part


class Oracle:
    """What happened on one device, kept apart from anything the add-on keeps."""

    def __init__(self) -> None:
        # Per profile and add-on, the media copy's bytes when a handler last saw the pair match
        self.matched: dict[tuple[str, str], bytes] = {}
        # The add-ons a media copy was copied over the meta.json of, since Anki started
        self.overwritten: set[str] = set()

    def why_not_a_change_made_here(
        self, profile: str, addon: str, meta: Path, media: Path, overwritten: set[str]
    ) -> str:
        """Empty for a difference that may be left alone; the reason against, otherwise."""
        held = self.matched.get((profile, addon))
        if held is None:
            return "skipped a pair no sync ever saw matching"
        if held != read(media):
            return "skipped though the media copy was written over since the last match"
        if addon in overwritten:
            return "skipped though a media copy was copied over this config since Anki started"
        config, copy = as_object(meta), as_object(media)
        if config is None:
            return "skipped though the meta.json is no JSON object"
        if copy is not None:
            if "config" in copy and "config" not in config:
                return "skipped though the meta.json has lost its config"
            if canonical(users_part(config)) == canonical(users_part(copy)):
                return "skipped though only what Anki records of the install differs"
        return ""


class Machine:
    """One of the suite's devices, with a media-sync memory per profile and its oracle."""

    def __init__(self, root: Path, name: str) -> None:
        self.name = name
        self.device = kit.Device(root / name, PROFILES[0])
        self.sync_state: dict[str, tuple[int, dict[str, bytes]]] = {p: (0, {}) for p in PROFILES}
        self.ignored: set[str] = set()
        # What each add-on read as its config when Anki started, and may write back
        self.memory: dict[str, dict[str, Any]] = {}
        self.oracle = Oracle()

    @property
    def profile(self) -> str:
        return str(self.device.profile)

    def meta(self, addon: str) -> Path:
        return Path(self.device.meta_path(addon))

    def media(self, addon: str) -> Path:
        return Path(self.device.media_path(addon))

    def switch(self, profile: str) -> None:
        self.sync_state[self.profile] = (self.device.seen_usn, self.device.known)
        self.device.open_profile(profile)
        self.device.seen_usn, self.device.known = self.sync_state[profile]

    def restart(self) -> None:
        self.device.restarts()
        self.oracle.overwritten.clear()
        self.memory = {a: (as_object(self.meta(a)) or {}).get("config", {}) for a in ADDONS}

    def seen_by_a_handler(self, ignored: set[str]) -> None:
        """A handler has looked at every pair it does not ignore."""
        for addon in ADDONS:
            if addon not in ignored and same(self.meta(addon), self.media(addon)):
                self.oracle.matched[(self.profile, addon)] = self.media(addon).read_bytes()


class Violation(Exception):
    pass


class History:
    def __init__(self, root: Path, seed: int, old: ModuleType) -> None:
        self.root = root
        self.old = old
        self.rng = random.Random(seed)
        self.servers = {profile: kit.Server() for profile in PROFILES}
        self.machines = [Machine(root, "A"), Machine(root, "B")]
        self.log: list[str] = []
        self.copies = 0
        self.counter = 0
        self.stats: Counter[str] = Counter()
        for machine in self.machines:
            for addon in ADDONS:
                machine.device.install(addon, {"name": addon, "mod": 100, "config": {"v": 0}})
            machine.restart()

    # The old code, on a copy of the folders

    def old_call(
        self, machine: Machine, function: Any, *args: Any, **kwargs: Any
    ) -> tuple[Path, Path]:
        self.copies += 1
        base = self.root / "copies" / str(self.copies)
        addons, profile = base / "addons21", base / "profile"
        shutil.copytree(machine.device.addons, addons)
        shutil.copytree(machine.device.media.parent, profile)
        pm = self.old.mw.pm
        with mock.patch.object(pm, "addonFolder", lambda: str(addons), create=True):
            with mock.patch.object(pm, "profileFolder", lambda: str(profile)):
                function(*args, **kwargs)
        filecmp.clear_cache()
        return addons, profile / "collection.media"

    # The handlers, new beside old

    def sync_start(self, machine: Machine) -> None:
        ignored = set(machine.ignored)
        lists: tuple[list[str], list[str], list[str]] = ([], [], [])
        old_addons, old_media = self.old_call(
            machine, self.old.save_configs_on_sync, *lists, ignored_addons=set(ignored)
        )
        result = machine.device.sync_starts(ignored=sorted(ignored))
        self.stats["sync starts"] += 1
        self.stats["configs sent"] += len(result.saved)
        if tuple(sorted(each) for each in lists) != tuple(result):
            raise Violation("SAVE: other lists than the old code")
        for addon in ADDONS:
            if read(machine.media(addon)) != read(old_media / machine.media(addon).name):
                raise Violation("SAVE: another media copy than the old code")
            if read(machine.meta(addon)) != read(old_addons / addon / "meta.json"):
                raise Violation("SAVE: touched a meta.json")
        machine.seen_by_a_handler(ignored)

    def media_sync_stops(self, machine: Machine, apply: bool) -> None:
        ignored = set(machine.ignored)
        before = {addon: read(machine.meta(addon)) for addon in ADDONS}
        overwritten_before = set(machine.oracle.overwritten)
        against = {
            addon: machine.oracle.why_not_a_change_made_here(
                machine.profile,
                addon,
                machine.meta(addon),
                machine.media(addon),
                overwritten_before,
            )
            for addon in ADDONS
        }
        old_lists: tuple[list[str], list[str], list[str]] = ([], [], [])
        old_addons, _ = self.old_call(
            machine,
            self.old.read_configs_on_sync,
            *old_lists,
            lambda: None,
            False,
            ignored_addons=set(ignored),
            apply_to_addons=apply,
        )
        result = machine.device.media_sync_stops(ignored=sorted(ignored), apply=apply)
        old_loaded, old_disabled, old_missing = (sorted(each) for each in old_lists)
        after = {addon: read(machine.meta(addon)) for addon in ADDONS}
        old_after = {addon: read(old_addons / addon / "meta.json") for addon in ADDONS}
        self.stats["media syncs stopping" if apply else "media syncs stopping, ask mode"] += 1
        if result.missing != old_missing:
            raise Violation("READ: another missing list than the old code")
        if not apply:
            if (result.loaded, result.disabled) != (old_loaded, old_disabled):
                raise Violation("ASK: other lists than the old code")
            if after != before or old_after != before:
                raise Violation("ASK: a file changed")
        else:
            self.stats["copied by the old code"] += len(old_loaded)
            self.stats["copied by the new code"] += len(result.loaded)
            if not set(result.loaded) <= set(old_loaded):
                raise Violation("SUBSET: copied what the old code does not")
            for addon in ADDONS:
                if addon in result.loaded:
                    if after[addon] != old_after[addon]:
                        raise Violation("SUBSET: copied to other bytes than the old code")
                    machine.oracle.overwritten.add(addon)
                elif after[addon] != before[addon]:
                    raise Violation("READ: changed a meta.json it does not list")
                elif addon in old_loaded and against[addon]:
                    raise Violation(f"SKIP: {against[addon]}")
        machine.seen_by_a_handler(ignored)

    def dialog(self, machine: Machine, action: str, addon: str) -> None:
        function = {
            "save": new.save_addon_to_media,
            "overwrite": new.overwrite_addon_from_media,
            "remove": new.remove_addon_from_media,
        }[action]
        before = read(machine.meta(addon))
        machine.device.calls(function, addon)
        filecmp.clear_cache()
        if machine.meta(addon).is_file() and read(machine.meta(addon)) != before:
            machine.oracle.overwritten.add(addon)
        if action == "remove":
            machine.oracle.matched.pop((machine.profile, addon), None)
        elif same(machine.meta(addon), machine.media(addon)):
            machine.oracle.matched[(machine.profile, addon)] = machine.media(addon).read_bytes()

    # One thing happening

    def value(self) -> int:
        self.counter += 1
        return self.counter

    def step(self) -> None:
        rng = self.rng
        machine = rng.choice(self.machines)
        addon = rng.choice(ADDONS)
        meta_path = machine.meta(addon)
        meta = as_object(meta_path)
        steps = {
            "edit": 14,
            "sync": 10,
            "media sync alone": 12,
            "sync start alone": 4,
            "media sync, not read": 3,
            "sync, ask mode": 3,
            "media sync alone, ask mode": 3,
            "restart": 5,
            "switch profile": 5,
            "written back from memory": 6,
            "update": 5,
            "rewritten by anki": 4,
            "switched off or on": 4,
            "installed again": 4,
            "cut short": 2,
            "meta.json deleted": 2,
            "rebuilt from nothing": 3,
            "copy dropped in": 3,
            "state file deleted": 1,
            "state file broken": 1,
            "ignore or not": 3,
            "dialog": 5,
            "keys reordered": 2,
        }
        kind = rng.choices(list(steps), list(steps.values()))[0]
        self.log.append(f"{machine.name}[{machine.profile}] {kind} {addon}")
        if kind == "edit" and meta is not None:
            meta.setdefault("config", {})["v"] = self.value()
            write(meta_path, json.dumps(meta))
        elif kind == "keys reordered" and meta is not None:
            write(meta_path, json.dumps(dict(reversed(list(meta.items())))))
        elif kind == "written back from memory" and meta is not None:
            meta["config"] = dict(machine.memory.get(addon, {}), w=self.value())
            write(meta_path, json.dumps(meta))
        elif kind == "update" and meta is not None:
            meta["mod"] = meta.get("mod", 0) + self.value()
            meta["human_version"] = f"v{self.counter}"
            write(meta_path, json.dumps(meta))
        elif kind == "rewritten by anki" and meta is not None:
            for key, default in (
                ("disabled", False),
                ("update_enabled", True),
                ("conflicts", []),
                ("min_point_version", 0),
                ("max_point_version", 0),
                ("branch_index", 0),
            ):
                meta.setdefault(key, default)
            write(meta_path, json.dumps(meta))
        elif kind == "switched off or on" and meta is not None:
            meta["disabled"] = not meta.get("disabled", False)
            write(meta_path, json.dumps(meta))
        elif kind == "installed again":
            shutil.rmtree(machine.device.addons / addon, ignore_errors=True)
            machine.device.install(addon, {"name": addon, "mod": 100 + self.value()})
        elif kind == "cut short" and meta_path.is_file():
            write(meta_path, meta_path.read_bytes()[: rng.randint(0, 12)])
        elif kind == "meta.json deleted" and meta_path.is_file():
            meta_path.unlink()
            filecmp.clear_cache()
        elif kind == "rebuilt from nothing" and (machine.device.addons / addon).is_dir():
            write(
                meta_path, json.dumps({"disabled": rng.random() < 0.5, "mod": 0, "conflicts": []})
            )
        elif kind == "copy dropped in":
            write(machine.media(addon), json.dumps({"name": addon, "config": {"v": self.value()}}))
        elif kind == "state file deleted" and machine.device.matches.exists():
            machine.device.matches.unlink()
        elif kind == "state file broken":
            write(
                machine.device.matches, rng.choice(["", "[]", "{", '{"version": 1, "profiles": 3}'])
            )
        elif kind == "ignore or not":
            machine.ignored ^= {addon}
        elif kind == "restart":
            machine.restart()
        elif kind == "switch profile":
            machine.switch(rng.choice([p for p in PROFILES if p != machine.profile]))
        elif kind == "dialog":
            self.dialog(machine, rng.choice(["save", "overwrite", "remove"]), addon)
        elif kind in ("sync", "sync, ask mode"):
            self.sync_start(machine)
            machine.device.media_sync(self.servers[machine.profile])
            self.media_sync_stops(machine, apply=kind == "sync")
        elif kind in ("media sync alone", "media sync alone, ask mode"):
            machine.device.media_sync(self.servers[machine.profile])
            self.media_sync_stops(machine, apply=kind == "media sync alone")
        elif kind == "sync start alone":
            self.sync_start(machine)
        elif kind == "media sync, not read":
            # The dialog's "Sync media only now", which holds the reading back
            machine.device.media_sync(self.servers[machine.profile])


@pytest.fixture
def broken_on_purpose(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """ACS_BREAK switches one part of the add-on's rule off, in memory, for this run."""
    part = os.environ.get("ACS_BREAK", "")
    if part == "session":
        # No config is ever remembered as copied over
        monkeypatch.setattr(
            new, "mark_addon_updated", lambda addon_id: new.UPDATED_STATE.update({addon_id: True})
        )
    elif part == "install":
        monkeypatch.setattr(new, "INSTALL_KEYS", frozenset())
    elif part == "config":
        whole = new.read_json_object

        def with_a_config(path: Path) -> dict[str, Any] | None:
            data = whole(path)
            return None if data is None else dict({"config": {}}, **data)

        monkeypatch.setattr(new, "read_json_object", with_a_config)
    elif part == "object":
        monkeypatch.setattr(new, "is_change_made_here", lambda meta, media: True)
    elif part == "bytes":
        monkeypatch.setattr(
            matched_copies.MatchedCopies,
            "unchanged",
            lambda self, addon_id, media: addon_id in self._profiles.get(self.profile, {}),
        )
    elif part == "ask":
        read_configs = new.read_configs_on_sync

        def copying(*args: Any, **kwargs: Any) -> Any:
            kwargs["apply_to_addons"] = True
            return read_configs(*args, **kwargs)

        monkeypatch.setattr(new, "read_configs_on_sync", copying)
    elif part == "save":
        monkeypatch.setattr(new, "json_files_deep_equal", lambda a, b: True)
    elif part:
        raise ValueError(f"ACS_BREAK={part}: not a part of the rule this file knows how to break")
    yield part


def test_old_against_new(tmp_path: Path, old: ModuleType, broken_on_purpose: str) -> None:
    first, _, last = os.environ.get("ACS_HISTORIES", "0:150").partition(":")
    steps = int(os.environ.get("ACS_STEPS", "60"))
    found: Counter[str] = Counter()
    shortest: dict[str, tuple[int, list[str]]] = {}
    stats: Counter[str] = Counter()
    for seed in range(int(first), int(last)):
        history = History(tmp_path / f"history{seed}", seed, old)
        try:
            for _step in range(steps):
                history.step()
        except Violation as violation:
            what = str(violation)
            found[what] += 1
            if what not in shortest or len(history.log) < len(shortest[what][1]):
                shortest[what] = (seed, list(history.log))
        stats.update(history.stats)
        shutil.rmtree(tmp_path / f"history{seed}", ignore_errors=True)

    print(
        f"\n{int(last) - int(first)} histories of {steps} steps"
        + (f", ACS_BREAK={broken_on_purpose}" if broken_on_purpose else "")
    )
    for name, count in sorted(stats.items()):
        print(f"{count:7d}  {name}")
    kept = stats["copied by the old code"] - stats["copied by the new code"]
    print(f"{kept:7d}  left alone by the new code where the old one copied")
    for what, count in found.most_common():
        seed, log = shortest[what]
        print(f"\n{count:5d} x {what}\n        shortest: history {seed}, {len(log)} steps")
        for line in log[-16:]:
            print("          " + line)
    assert not found, "see the histories printed above"
    # A run in which nothing was ever left alone has not looked at the rule at all
    assert kept > 0 or int(last) - int(first) < 20
