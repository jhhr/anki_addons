"""Addon Config Sync loaded in a running Anki: its handlers on Anki's hooks, the real profile
manager, and the meta.json files Anki's own add-on manager writes.

Which differences the add-on leaves alone when a media sync stops depends on what is in a
meta.json after Anki has installed an add-on, updated it or switched it off
(`sync_actions.is_change_made_here`). The tests in `addon_config_sync/test` write such files
by hand; these have Anki write them.

The two sync handlers are called as the hooks call them, not through the hooks. In this
process `media_sync_did_start_or_stop` also holds the handlers of main windows that earlier
tests ran and that are gone, and firing it would run those.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from aqt import gui_hooks

TINY = "tiny_addon"


def _media_copy(mw) -> Path:
    return Path(mw.pm.profileFolder(), "collection.media", f"_{TINY}_meta.json")


def _package(tmp_path: Path, **manifest) -> str:
    """An .ankiaddon of one empty module, with what Anki's install reads out of it."""
    path = tmp_path / f"tiny_{len(list(tmp_path.glob('*.ankiaddon')))}.ankiaddon"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("__init__.py", "")
        package.writestr("manifest.json", json.dumps({"package": TINY, "name": "Tiny", **manifest}))
    return str(path)


@pytest.fixture
def manager(real_mw, monkeypatch):
    """The add-on manager, installing over a folder without Anki's way of clearing it first,
    which moves the folder to the system's trash: no place for a test's."""
    manager = real_mw.addonManager
    monkeypatch.setattr(
        manager, "deleteAddon", lambda module: shutil.rmtree(manager.addonsFolder(module))
    )
    return manager


def test_the_addon_loads_and_puts_its_handlers_on_ankis_hooks(real_mw, addon):
    assert gui_hooks.sync_will_start._hooks[-1] is addon.sync_on_save
    assert gui_hooks.media_sync_did_start_or_stop._hooks[-1] is addon.read_on_sync


def test_a_config_an_addon_writes_after_a_sync_started_survives_the_media_sync(
    real_mw, addon, manager, tmp_path
):
    Path(manager.addonsFolder(TINY)).mkdir()
    manager.writeConfig(TINY, {"speed": 1})

    addon.sync_on_save()
    assert json.loads(_media_copy(real_mw).read_text(encoding="utf-8")) == {"config": {"speed": 1}}
    held = json.loads((tmp_path / "matched_copies.json").read_text(encoding="utf-8"))
    assert list(held["profiles"]) == [str(Path(real_mw.pm.profileFolder()))]
    assert list(held["profiles"][str(Path(real_mw.pm.profileFolder()))]) == [TINY]

    # What an add-on does once the collection sync is over, or the user in a config dialog
    manager.writeConfig(TINY, {"speed": 333})
    addon.read_on_sync(True)
    addon.read_on_sync(False)
    assert manager.addonMeta(TINY) == {"config": {"speed": 333}}
    assert addon.sync_loaded_addons == []

    # The next sync sends it
    addon.sync_on_save()
    assert TINY in addon.sync_saved_addons
    assert json.loads(_media_copy(real_mw).read_text(encoding="utf-8")) == {
        "config": {"speed": 333}
    }

    # ...and what another device sends after that is still read
    _media_copy(real_mw).write_text('{"config": {"speed": 22}}', encoding="utf-8")
    addon.read_on_sync(False)
    assert manager.addonMeta(TINY) == {"config": {"speed": 22}}
    assert addon.sync_loaded_addons == [TINY]

    # ...and read again over what is written here before Anki is restarted
    manager.writeConfig(TINY, {"speed": 4444})
    addon.read_on_sync(False)
    assert manager.addonMeta(TINY) == {"config": {"speed": 22}}


def test_what_an_update_by_anki_writes_is_read_back_as_before(real_mw, addon, manager, tmp_path):
    """Anki installs an update over the folder and writes the meta.json back with the new
    version in it. That is no change made here: kept, it would go out with the next sync
    and tell the other devices' Anki that they have the update."""
    manager.install(_package(tmp_path, human_version="1.0", mod=1))
    manager.writeConfig(TINY, {"speed": 1})
    addon.sync_on_save()
    sent = manager.addonMeta(TINY)

    manager.install(_package(tmp_path, human_version="2.0", mod=22, min_point_version=45))
    assert manager.addonMeta(TINY) == dict(sent, human_version="2.0", mod=22, min_point_version=45)
    addon.read_on_sync(False)

    assert manager.addonMeta(TINY) == sent
    assert addon.sync_loaded_addons == [TINY]


def test_what_anki_fills_in_when_it_rewrites_a_meta_json_is_read_back_as_before(
    real_mw, addon, manager, tmp_path
):
    """Switching an add-on off and on again leaves it as it was, in a file that now has
    every key Anki knows."""
    manager.install(_package(tmp_path, mod=1))
    manager.writeConfig(TINY, {"speed": 1})
    addon.sync_on_save()
    sent = manager.addonMeta(TINY)

    manager.toggleEnabled(TINY, enable=False)
    manager.toggleEnabled(TINY, enable=True)
    assert set(manager.addonMeta(TINY)) > set(sent)
    addon.read_on_sync(False)

    assert manager.addonMeta(TINY) == sent
    assert addon.sync_loaded_addons == [TINY]


def test_an_addon_switched_off_in_anki_stays_off(real_mw, addon, manager, tmp_path):
    manager.install(_package(tmp_path, mod=1))
    manager.writeConfig(TINY, {"speed": 1})
    addon.sync_on_save()

    manager.toggleEnabled(TINY, enable=False)
    addon.read_on_sync(False)

    assert manager.addonMeta(TINY)["disabled"] is True
    assert addon.sync_loaded_addons == []


def test_an_addon_anki_installs_again_gets_the_config_of_its_copy(
    real_mw, addon, manager, tmp_path
):
    """As it always did: the copy in the media folder is the config a new install of the
    add-on is given, and Anki's own new install is one without a config."""
    manager.install(_package(tmp_path, mod=1))
    manager.writeConfig(TINY, {"speed": 1})
    addon.sync_on_save()

    shutil.rmtree(manager.addonsFolder(TINY))
    manager.install(_package(tmp_path, mod=22))
    assert "config" not in manager.addonMeta(TINY)
    addon.read_on_sync(False)

    assert manager.addonMeta(TINY)["config"] == {"speed": 1}
    assert addon.sync_loaded_addons == [TINY]
