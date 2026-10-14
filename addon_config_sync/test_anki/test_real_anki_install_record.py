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

The devices, AnkiWeb and media syncs are `devices.py`'s.
"""

from __future__ import annotations

import json
import sys
from typing import Callable

import aqt
import aqt.addons
import pytest
from aqt import gui_hooks

from .devices import (
    FOLDER,
    HOMEPAGE,
    T1,
    T2,
    AnkiDevice,
    FakeAnkiWeb,
    Server,
    shown_status,
    write_file,
)

# What Anki writes into a meta.json from a package's manifest and from AnkiWeb
ANKIS_RECORD = {
    key
    for key, spec in aqt.addons.AddonManager._manifest_schema["properties"].items()
    if spec["meta"]
}

# An add-on whose code puts a handler on a hook when Anki loads it, as Addon Config Sync does
REGISTERS_HANDLER = """
from aqt import gui_hooks


def on_sync_will_start():
    on_sync_will_start.ran = VERSION


gui_hooks.sync_will_start.append(on_sync_will_start)
"""


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


@pytest.mark.parametrize("mode", ["summary", "ask"])
def test_only_a_change_of_config_made_on_another_device_is_listed(
    make_device, ankiweb, mode, monkeypatch
):
    a, b, server = both_installed_and_synced(make_device, ankiweb)
    # A summary is a post to the shared notification stack, beside the other add-ons' sync
    # results, and lists the add-ons whose config it loaded.
    posts: list[dict] = []
    monkeypatch.setattr(b.acs, "post", lambda **kwargs: posts.append(kwargs))
    dialogs = sys.modules[f"{b.acs.__name__}.config_manager_dialog"].ACTIVE_MANAGER_DIALOGS
    ankiweb.publish("2.0", T2)
    a.anki_checks_for_updates()
    a.syncs(server)
    if mode == "summary":
        b.mode = {"show_summary_on_sync": True}
    else:
        b.mode = {"run_on_sync": False, "ask_on_sync": True}

    assert b.syncs(server) == []
    assert (dialogs, posts) == ([], [])

    a.set_config(speed=2)
    a.syncs(server)
    assert b.syncs(server) == [FOLDER]
    if mode == "summary":
        assert [post["body"] for post in posts] == ["Tiny Addon"]
        assert b.meta()["config"] == {"speed": 2}
        assert dialogs == []
    else:
        # A request for input, so still the dialog
        assert shown_status(b.acs) == "Changed"
        assert posts == []


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
        write_file(device.addons / own / "meta.json", json.dumps(released).encode("utf-8"))
        device.syncs(server)

    # Anki installs this version on a, whose sync as it closes still runs 1.0.0
    updated = json.dumps(dict(released, mod=T2)).encode("utf-8")
    write_file(a.addons / own / "meta.json", updated)
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
