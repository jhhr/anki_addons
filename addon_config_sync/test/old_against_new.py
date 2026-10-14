"""The sync handlers from before matches were kept, run beside today's over random histories.

Not collected with the suite (its name does not start with `test_`), because it takes
minutes and needs the repository's history. Run it when `read_configs_on_sync`, or anything
it decides by, is about to change, from the repo root:

    python -m pytest addon_config_sync/test/old_against_new.py -s

That is 150 histories of 60 steps, about two minutes. ACS_HISTORIES (first:last) and
ACS_STEPS in the environment make it another run: 0:600 and 80 took eleven minutes.

Until `matched_copies.py`, a sync start sent every meta.json that differed from its media
copy, and a media sync stopping copied every media copy over a meta.json that differed from
it. That is the reference. Whatever the add-on holds back or leaves alone today, it must
send nothing the old code did not, copy nothing the old code did not, and list in ask mode
what the old code listed. Since it stopped carrying what Anki records about the installed
package (INSTALL_KEYS), a difference in nothing else is neither sent, read nor listed, and
what is sent or read is the settings: a media copy written keeps its record, unless it had
none to keep, and a meta.json read over keeps its own. The old code also took `{}` and `[]`
for the same file, which the new code tells apart (`empty_object_and_list`): it sends,
reads or lists such a pair as any other where one file is no JSON object.

Each history is two devices with two profiles each and two add-ons, doing at random what
users and Anki do: configs edited, add-ons updated, their version range rewritten, switched
off, installed again, files cut short, syncs, media syncs with none before them, restarts,
and devices on an older version sending a new install's file, a broken one, or their whole
meta.json, record and all.
Before every call of a handler the device's folders are copied and the old handler is run
on the copy, so both start from the same files. Checked at each call:

  SAVE    a sync start sends what the old code sends and fills the same lists, except that
          it holds back a meta.json that is no JSON object, or has no config where the copy
          has one, and sends none that differs from its copy only in the record. It always
          holds those back, leaves their copy as it was, and lists the first two as
          skipped. What it sends is the meta.json's settings with the record the copy had,
          or with the meta.json's own where the copy was missing or no JSON object.
  ASK     in ask mode a media sync stopping lists what the old code lists, but for copies
          that differ only in the record, and changes no file
  SUBSET  what the new code copies when a media sync stops, the old code copies too, and
          never a copy that is no JSON object, nor one without a config over a meta.json
          that has one, nor one that differs only in the record. The meta.json then has the
          copy's settings and the record it had, none if it was missing or no JSON object.
  SKIP    a copy the old code makes and the new one does not, as an oracle that does not
          read the add-on's state has it, is unfit to read (above), differs only in the
          record, or is of a config that only this device changed: the pair matched at some
          sync, the media copy is still those bytes, no media copy was copied over that
          config since Anki started, and the meta.json is a JSON object with a config if the
          copy has one
  DIALOG  Save and Overwrite copy no file that is no JSON object, and carry the settings as
          the handlers do, each side keeping its record

The oracle restates the rule, so what it can show is that the code keeps to the rule, not
that the rule is right: what each device ends up with across a history is not judged.
Devices on an older version are modelled only as the files they send, never as a device
that also reads them.

The oracle is the rule of `read_configs_on_sync` written a second time, from what happened
and not from what the add-on kept. A change to the rule is a change to `Oracle`, made first
and on purpose. `ACS_BREAK=session` (or `install`, `config`, `object`, `bytes`, `ask`, `save`,
`held`, `refused`, `dialog`, `record_read`, `record_send`, `record_compare`) switches one
part of the add-on's rule off in memory for a run, to see the check for it fail; do that
after touching either.

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


def users_part(meta: dict[str, Any]) -> dict[str, Any]:
    part = settings(meta)
    part["disabled"] = bool(meta.get("disabled"))
    part["update_enabled"] = meta.get("update_enabled", True)
    return part


def settings(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in meta.items() if key not in INSTALL_KEYS}


def record(meta: dict[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (meta or {}).items() if key in INSTALL_KEYS}


def same(meta: Path, media: Path) -> bool:
    """The two files matching, as the add-on means it: by bytes, or as the same settings
    whatever the order of the keys and whatever each records of its install."""
    a, b = read(meta), read(media)
    if a is None or b is None:
        return False
    if a == b:
        return True
    config, copy = as_object(meta), as_object(media)
    if config is None or copy is None:
        return False
    return bool(canonical(users_part(config)) == canonical(users_part(copy)))


def only_the_record_differs(meta: Path, media: Path) -> bool:
    a, b = read(meta), read(media)
    return a is not None and b is not None and a != b and same(meta, media)


def parsed(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None


def empty_object_and_list(meta: Path, media: Path) -> bool:
    """A pair the old code took for the same file: its `ordered()` gives `{}` and `[]` alike.
    The new code compares settings, finds one of the two no JSON object, and so sends a
    `{}` over a `[]` copy, reads a `{}` copy over a `[]` meta.json, and lists either in ask
    mode, as it would for any other file that is no JSON object."""
    a, b = parsed(meta), parsed(media)
    return (a == {} and b == []) or (a == [] and b == {})


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
        if copy is not None and "config" in copy and "config" not in config:
            return "skipped though the meta.json has lost its config"
        return ""

    @staticmethod
    def unfit_to_send(meta: Path, media: Path) -> bool:
        config, copy = as_object(meta), as_object(media)
        return config is None or (copy is not None and "config" in copy and "config" not in config)

    @staticmethod
    def unfit_to_read(meta: Path, media: Path) -> bool:
        config, copy = as_object(meta), as_object(media)
        if copy is None:
            return True
        return config is not None and "config" in config and "config" not in copy


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
        self.files: list[str] = []
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

    def note_files(self, machine: Machine) -> None:
        """Both files of each add-on as a handler is called, printed with a Violation."""
        self.files = [
            f"{machine.name}[{machine.profile}] {addon}: meta.json {read(machine.meta(addon))!r},"
            f" copy {read(machine.media(addon))!r}"
            for addon in ADDONS
        ]

    def sync_start(self, machine: Machine) -> None:
        self.note_files(machine)
        ignored = set(machine.ignored)
        before = {addon: read(machine.media(addon)) for addon in ADDONS}
        copies_before = {addon: as_object(machine.media(addon)) for addon in ADDONS}
        unfit = {
            addon
            for addon in ADDONS
            if addon not in ignored
            and machine.meta(addon).is_file()
            and Oracle.unfit_to_send(machine.meta(addon), machine.media(addon))
        }
        record_only = {
            addon
            for addon in ADDONS
            if addon not in ignored
            and only_the_record_differs(machine.meta(addon), machine.media(addon))
        }
        empty_mixup = {
            addon
            for addon in ADDONS
            if addon not in ignored
            and empty_object_and_list(machine.meta(addon), machine.media(addon))
        }
        lists: tuple[list[str], list[str], list[str]] = ([], [], [])
        old_addons, _ = self.old_call(
            machine, self.old.save_configs_on_sync, *lists, ignored_addons=set(ignored)
        )
        result = machine.device.sync_starts(ignored=sorted(ignored))
        old_saved, old_disabled, old_skipped = (sorted(each) for each in lists)
        self.stats["sync starts"] += 1
        self.stats["configs sent"] += len(result.saved)
        held = set(old_saved) - set(result.saved)
        self.stats["held back by the new code where the old one sent"] += len(held - record_only)
        self.stats["not sent by the new code, as only the record differs"] += len(
            held & record_only
        )
        if set(result.saved) - set(old_saved) - empty_mixup:
            raise Violation("SAVE: sent what the old code does not")
        if set(result.saved) & unfit:
            raise Violation("SAVE: sent a meta.json that is broken or takes a config away")
        if set(result.saved) & record_only:
            raise Violation("SAVE: sent a meta.json that differs from its copy only in the record")
        if not held <= unfit | record_only:
            raise Violation("SAVE: held back a meta.json that takes no config away")
        if set(result.skipped) - set(old_skipped) - unfit:
            raise Violation("SAVE: skipped what the old code does not and is fit to send")
        if set(result.disabled) != set(old_disabled) - held:
            raise Violation("SAVE: another disabled list than the old code")
        for addon in unfit:
            if read(machine.media(addon)) != before[addon]:
                raise Violation("SAVE: wrote over the copy of a meta.json it holds back")
            if addon not in result.skipped:
                raise Violation("SAVE: held back a meta.json without listing it as skipped")
        for addon in ADDONS:
            if addon in result.saved:
                sent, config = as_object(machine.media(addon)), as_object(machine.meta(addon))
                if sent is None or config is None or settings(sent) != settings(config):
                    raise Violation("SAVE: sent other settings than the meta.json's")
                kept = copies_before[addon]
                if record(sent) != record(config if kept is None else kept):
                    raise Violation("SAVE: the media copy did not keep its record")
            elif read(machine.media(addon)) != before[addon]:
                raise Violation("SAVE: wrote over a copy it does not list as sent")
            if read(machine.meta(addon)) != read(old_addons / addon / "meta.json"):
                raise Violation("SAVE: touched a meta.json")
        machine.seen_by_a_handler(ignored)

    def media_sync_stops(self, machine: Machine, apply: bool) -> None:
        self.note_files(machine)
        ignored = set(machine.ignored)
        before = {addon: read(machine.meta(addon)) for addon in ADDONS}
        metas_before = {addon: as_object(machine.meta(addon)) for addon in ADDONS}
        copies = {addon: as_object(machine.media(addon)) for addon in ADDONS}
        record_only = {
            addon
            for addon in ADDONS
            if only_the_record_differs(machine.meta(addon), machine.media(addon))
        }
        empty_mixup = {
            addon
            for addon in ADDONS
            if empty_object_and_list(machine.meta(addon), machine.media(addon))
        }
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
        refused = {
            addon: Oracle.unfit_to_read(machine.meta(addon), machine.media(addon))
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
            if result.disabled != old_disabled:
                raise Violation("ASK: another disabled list than the old code")
            if set(result.loaded) - set(old_loaded) - empty_mixup:
                raise Violation("ASK: listed what the old code does not")
            if set(result.loaded) & record_only:
                raise Violation("ASK: listed a copy that differs only in the record")
            if set(old_loaded) - set(result.loaded) - record_only:
                raise Violation("ASK: left out a difference in the settings")
            if after != before or old_after != before:
                raise Violation("ASK: a file changed")
        else:
            self.stats["copied by the old code"] += len(old_loaded)
            self.stats["copied by the new code"] += len(result.loaded)
            if not set(result.loaded) <= set(old_loaded) | empty_mixup:
                raise Violation("SUBSET: copied what the old code does not")
            if set(result.loaded) & record_only:
                raise Violation("SUBSET: read a copy that differs only in the record")
            for addon in ADDONS:
                if addon in result.loaded:
                    if refused[addon]:
                        raise Violation("SUBSET: copied a media copy that is unfit to read")
                    now, copy = as_object(machine.meta(addon)), copies[addon]
                    if now is None or copy is None or settings(now) != settings(copy):
                        raise Violation("SUBSET: read other settings than the copy's")
                    if record(now) != record(metas_before[addon]):
                        raise Violation("SUBSET: the meta.json did not keep its record")
                    machine.oracle.overwritten.add(addon)
                elif after[addon] != before[addon]:
                    raise Violation("READ: changed a meta.json it does not list")
                elif addon in old_loaded and refused[addon]:
                    self.stats["not read by the new code, as the copy is unfit to read"] += 1
                elif addon in old_loaded and addon in record_only:
                    self.stats["not read by the new code, as only the record differs"] += 1
                elif addon in old_loaded and against[addon]:
                    raise Violation(f"SKIP: {against[addon]}")
        machine.seen_by_a_handler(ignored)

    def dialog(self, machine: Machine, action: str, addon: str) -> None:
        function = {
            "save": new.save_addon_to_media,
            "overwrite": new.overwrite_addon_from_media,
            "remove": new.remove_addon_from_media,
        }[action]
        self.note_files(machine)
        before = read(machine.meta(addon))
        media_before = read(machine.media(addon))
        meta_before, copy_before = as_object(machine.meta(addon)), as_object(machine.media(addon))
        unfit = (action == "save" and meta_before is None) or (
            action == "overwrite" and copy_before is None
        )
        done = machine.device.calls(function, addon)
        filecmp.clear_cache()
        if unfit and (read(machine.meta(addon)), read(machine.media(addon))) != (
            before,
            media_before,
        ):
            raise Violation(f"DIALOG: {action} copied a file that is no JSON object")
        if action == "save" and meta_before is not None:
            sent = as_object(machine.media(addon))
            if copy_before is not None and canonical(users_part(meta_before)) == canonical(
                users_part(copy_before)
            ):
                if read(machine.media(addon)) != media_before:
                    raise Violation("DIALOG: save wrote over a copy with the same settings")
            elif (
                sent is None
                or settings(sent) != settings(meta_before)
                or record(sent) != record(meta_before if copy_before is None else copy_before)
            ):
                raise Violation("DIALOG: save did not send the settings beside the copy's record")
        if action == "overwrite" and done:
            now = as_object(machine.meta(addon))
            if (
                now is None
                or copy_before is None
                or settings(now) != settings(copy_before)
                or record(now) != record(meta_before)
            ):
                raise Violation("DIALOG: overwrite did not read the settings beside the record")
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
            "range rewritten by an update check": 3,
            "rewritten by anki": 4,
            "switched off or on": 4,
            "ticked or unticked for updates": 3,
            "installed again": 4,
            "cut short": 2,
            "meta.json deleted": 2,
            "rebuilt from nothing": 3,
            "copy dropped in": 3,
            "an older device sends a new install's file": 2,
            "an older device sends a file cut short": 1,
            "an older device sends a file that is no object": 1,
            "an older device sends its update": 2,
            "an older device sends a config and its record": 2,
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
        elif kind == "range rewritten by an update check" and meta is not None:
            meta["min_point_version"] = rng.choice([0, 50, 231000, 250700])
            meta["max_point_version"] = rng.choice([0, 66, -250200, 260900])
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
        elif kind == "ticked or unticked for updates" and meta is not None:
            meta["update_enabled"] = not meta.get("update_enabled", True)
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
        elif kind == "an older device sends a new install's file":
            name = machine.media(addon).name
            self.servers[machine.profile].put(name, json.dumps({"name": addon}).encode("utf-8"))
        elif kind == "an older device sends a file cut short":
            name = machine.media(addon).name
            self.servers[machine.profile].put(name, b'{"name": "' + addon.encode("utf-8"))
        elif kind == "an older device sends a file that is no object":
            self.servers[machine.profile].put(machine.media(addon).name, b"[]")
        elif kind == "an older device sends its update":
            # Its whole meta.json, which only an update changed
            sent = as_object(machine.media(addon))
            if sent is not None:
                sent["mod"] = sent.get("mod", 0) + self.value()
                sent["human_version"] = f"v{self.counter}"
                name = machine.media(addon).name
                self.servers[machine.profile].put(name, json.dumps(sent).encode("utf-8"))
        elif kind == "an older device sends a config and its record":
            sent = {"name": addon, "mod": 100 + self.value(), "config": {"v": self.value()}}
            name = machine.media(addon).name
            self.servers[machine.profile].put(name, json.dumps(sent).encode("utf-8"))
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
    elif part == "held":
        monkeypatch.setattr(new, "fit_to_send", lambda meta, media: True)
    elif part == "refused":
        monkeypatch.setattr(new, "fit_to_read", lambda meta, media: True)
    elif part == "dialog":
        # Both buttons as they were before they refused anything

        def save_anything(addon_id: str) -> bool:
            addons, media = new.get_paths()
            meta = addons / addon_id / "meta.json"
            if not meta.is_file():
                return False
            shutil.copy(meta, media / f"_{addon_id}_meta.json")
            return True

        def overwrite_with_anything(addon_id: str) -> bool:
            addons, media = new.get_paths()
            copy = media / f"_{addon_id}_meta.json"
            if not copy.is_file() or not (addons / addon_id).is_dir():
                return False
            shutil.copy(copy, addons / addon_id / "meta.json")
            return True

        monkeypatch.setattr(new, "save_addon_to_media", save_anything)
        monkeypatch.setattr(new, "overwrite_addon_from_media", overwrite_with_anything)
    elif part == "save":
        # Nothing is ever found to differ from its copy
        monkeypatch.setattr(new, "send_settings", lambda meta, media: False)
    elif part == "record_read":
        # A copy read whole, its record over this device's

        def read_whole(meta: Path, media: Path) -> bool:
            if new.read_json_object(media) is None:
                return False
            shutil.copy(media, meta)
            return True

        monkeypatch.setattr(new, "read_settings", read_whole)
    elif part == "record_send":
        # A copy sent with this device's record
        send = new.send_settings

        def send_with_this_record(meta: Path, media: Path) -> bool:
            if not send(meta, media):
                return False
            config, copy = new.read_json_object(meta), new.read_json_object(media)
            assert config is not None and copy is not None
            write(media, json.dumps(dict(new.synced_settings(copy), **new.install_record(config))))
            return True

        monkeypatch.setattr(new, "send_settings", send_with_this_record)
    elif part == "record_compare":
        # Files compared whole, the record with the settings

        def compared_whole(meta: dict[str, Any]) -> object:
            filled = dict(meta, disabled=bool(meta.get("disabled")))
            filled["update_enabled"] = meta.get("update_enabled", True)
            return new.ordered(filled)

        monkeypatch.setattr(new, "_as_compared", compared_whole)
    elif part:
        raise ValueError(f"ACS_BREAK={part}: not a part of the rule this file knows how to break")
    yield part


def test_old_against_new(tmp_path: Path, old: ModuleType, broken_on_purpose: str) -> None:
    first, _, last = os.environ.get("ACS_HISTORIES", "0:150").partition(":")
    steps = int(os.environ.get("ACS_STEPS", "60"))
    found: Counter[str] = Counter()
    shortest: dict[str, tuple[int, list[str], list[str]]] = {}
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
                shortest[what] = (seed, list(history.log), list(history.files))
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
        seed, log, files = shortest[what]
        print(f"\n{count:5d} x {what}\n        shortest: history {seed}, {len(log)} steps")
        for line in log[-16:]:
            print("          " + line)
        print("        the files as the handler was called:")
        for line in files:
            print("          " + line)
    assert not found, "see the histories printed above"
    # A run in which nothing was ever left alone has not looked at the rule at all
    assert kept > 0 or int(last) - int(first) < 20
