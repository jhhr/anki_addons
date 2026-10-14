"""What Addon Config Sync copies when a sync starts and when a media sync stops.

Every test here goes through the two functions Anki's hooks reach and looks at the files
afterwards, so none of them knows how the add-on decides. They are in five parts: what a
sync start does, what a media sync stopping does with a difference it copies over, what it
does with a config that only this device changed, how a broken file or one without a config
is kept from replacing a config either way, and how what Anki records about the installed
package stays on each device. The third and fourth parts are what used to lose configs; the
fifth is what used to hide updates.
"""

from __future__ import annotations

import builtins
import errno
import io
import json
import os
import shutil
from pathlib import Path

import pytest

from addon_config_sync import sync_actions

ADDON = "1234567890"
OTHER = "another_addon"


# A collection sync starts: configs go out to the media folder


def test_a_sync_start_copies_a_config_that_has_no_media_copy(device):
    device.install(ADDON, {"config": {"speed": 1}})

    result = device.sync_starts()

    assert device.media_copy(ADDON) == {"config": {"speed": 1}}
    assert result.saved == [ADDON]


def test_a_sync_start_replaces_a_changed_configs_copy_by_removing_it_first(device, monkeypatch):
    """Anki only uploads a media file it sees removed and added; one overwritten in place
    stays as it is on the server."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    removed = []
    remove = os.remove
    monkeypatch.setattr(os, "remove", lambda path: (removed.append(path), remove(path))[1])

    device.set_config(ADDON, speed=22)
    result = device.sync_starts()

    assert device.media_copy(ADDON) == {"config": {"speed": 22}}
    assert result.saved == [ADDON]
    assert removed == [device.media_path(ADDON)]


def test_the_dialogs_save_replaces_the_copy_by_removing_it_first(device, monkeypatch):
    """As a sync start does. Written over in place, the copy stayed out of the next media
    sync, which looks for changed files only when the folder's modification time changed."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    removed = []
    remove = os.remove
    monkeypatch.setattr(os, "remove", lambda path: (removed.append(path), remove(path))[1])

    device.set_config(ADDON, speed=22)
    assert device.calls(sync_actions.save_addon_to_media, ADDON)

    assert device.media_copy(ADDON) == {"config": {"speed": 22}}
    assert removed == [device.media_path(ADDON)]


def test_a_sync_start_leaves_a_copy_that_only_orders_its_keys_differently(device):
    """Anki writes a meta.json's keys in whatever order it holds them."""
    device.install(ADDON, {"name": "An add-on", "config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 1}, "name": "An add-on"})
    before = device.media_path(ADDON).read_bytes()

    result = device.sync_starts()

    assert device.media_path(ADDON).read_bytes() == before
    assert result.saved == []


def test_a_sync_start_passes_over_ignored_addons_and_reports_folders_without_a_config(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.install(OTHER, {"config": {"speed": 2}})
    device.install("no_config_yet")
    (device.addons / "a_file.txt").write_text("not an add-on", encoding="utf-8")

    result = device.sync_starts(ignored=[OTHER])

    assert result.saved == [ADDON]
    assert result.skipped == ["no_config_yet"]
    assert not device.media_path(OTHER).exists()


def test_a_sync_start_reports_a_saved_addon_that_is_disabled(device):
    device.install(ADDON, {"disabled": True})

    result = device.sync_starts()

    assert result.saved == [ADDON]
    assert result.disabled == [ADDON]


def test_a_sync_start_holds_back_a_new_installs_meta_json_from_a_copy_with_a_config(device):
    """Anki writes no `config` into a new install's meta.json. Sent, it would wipe the
    config the copy holds on every device; held back, the media sync's end reads the copy
    over it (below)."""
    device.install(ADDON, {"name": "An add-on", "config": {"speed": 1}})
    device.sync_starts()
    shutil.rmtree(device.addons / ADDON)
    device.install(ADDON, {"name": "An add-on"})

    result = device.sync_starts()

    assert device.media_copy(ADDON) == {"name": "An add-on", "config": {"speed": 1}}
    assert result.saved == []
    assert result.skipped == [ADDON]


@pytest.mark.parametrize("broken", ["", '{"config": {"spe', "[]", "null"])
def test_a_sync_start_holds_back_a_meta_json_anki_cannot_read(device, broken):
    """A meta.json cut short by a crash. Sent, it would break the config on every device."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.write_text(device.meta_path(ADDON), broken)

    result = device.sync_starts()

    assert device.media_copy(ADDON) == {"config": {"speed": 1}}
    assert result.saved == []


def test_a_sync_start_makes_no_copy_of_a_meta_json_anki_cannot_read(device):
    device.install(ADDON)
    device.write_text(device.meta_path(ADDON), '{"config": {"spe')

    result = device.sync_starts()

    assert not device.media_path(ADDON).exists()
    assert result.saved == []


@pytest.mark.parametrize(
    "here, copy, sent",
    [
        (
            {"config": {"speed": 1}},
            {"name": "An add-on"},
            {"config": {"speed": 1}, "name": "An add-on"},
        ),
        (
            {"name": "An add-on", "disabled": True},
            {"name": "An add-on"},
            {"name": "An add-on", "disabled": True},
        ),
        ({"name": "An add-on"}, {"name": "An add-on", "disabled": True}, {"name": "An add-on"}),
    ],
)
def test_a_sync_start_still_sends_what_takes_no_config_away(device, here, copy, sent):
    """A config over a copy without one, or a meta.json without a config over a copy that
    has none either: as before. The copy keeps its record of the package, here its name."""
    device.install(ADDON, here)
    device.write_media(ADDON, copy)

    result = device.sync_starts()

    assert device.media_copy(ADDON) == sent
    assert result.saved == [ADDON]


# A media sync stops: the differences that are copied over


def test_nothing_is_read_while_the_media_sync_is_still_running(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 22}})

    result = device.media_sync_stops(running=True)

    assert device.meta(ADDON) == {"config": {"speed": 1}}
    assert result == ([], [], [], 0)


def test_a_config_another_device_sent_replaces_this_devices(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.write_media(ADDON, {"config": {"speed": 22}})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 22}}
    assert result.loaded == [ADDON]
    assert result.finished == 1
    assert sync_actions.UPDATED_STATE == {ADDON: True}


def test_a_downloaded_config_that_disables_its_addon_is_reported(device):
    device.install(ADDON, {"disabled": False})
    device.sync_starts()
    device.write_media(ADDON, {"disabled": True})

    result = device.media_sync_stops()

    assert result.loaded == [ADDON]
    assert result.disabled == [ADDON]


def test_an_addon_folder_without_a_config_gets_the_media_copy(device):
    device.install(ADDON)
    device.write_media(ADDON, {"config": {"speed": 22}})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 22}}
    assert result.loaded == [ADDON]


def test_a_copy_for_an_addon_that_is_not_installed_is_reported_missing(device):
    device.write_media(ADDON, {"config": {"speed": 22}})

    result = device.media_sync_stops()

    assert result.missing == [ADDON]
    assert result.loaded == []
    assert not (device.addons / ADDON).exists()


def test_an_ignored_addon_is_left_alone_when_the_media_sync_stops(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 22}})
    device.write_media("not_installed", {"config": {}})

    result = device.media_sync_stops(ignored=[ADDON, "not_installed"])

    assert device.meta(ADDON) == {"config": {"speed": 1}}
    assert result == ([], [], [], 1)


def test_a_copy_that_only_orders_its_keys_differently_is_not_read(device):
    device.install(ADDON, {"name": "An add-on", "config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 1}, "name": "An add-on"})
    before = device.meta_path(ADDON).read_bytes()

    result = device.media_sync_stops()

    assert device.meta_path(ADDON).read_bytes() == before
    assert result.loaded == []


def test_a_differing_copy_is_read_on_a_device_that_has_not_started_a_sync_yet(device):
    """A device where the add-on is new, or has not been through a sync start with it yet:
    whatever the copy holds is taken."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 22}})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 22}}
    assert result.loaded == [ADDON]


def test_a_config_changed_here_and_on_another_device_ends_as_the_other_devices(device):
    """The conflict rule: what came down wins over what was not sent yet."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.set_config(ADDON, speed=333)
    device.write_media(ADDON, {"config": {"speed": 22}})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 22}}
    assert result.loaded == [ADDON]


def test_in_ask_mode_a_downloaded_config_is_listed_and_not_copied(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.write_media(ADDON, {"config": {"speed": 22}})

    result = device.media_sync_stops(apply=False)

    assert device.meta(ADDON) == {"config": {"speed": 1}}
    assert result.loaded == [ADDON]
    assert sync_actions.UPDATED_STATE == {}


def test_in_ask_mode_a_config_changed_only_here_is_listed_as_well(device):
    """Ask mode copies nothing by itself, so it loses nothing either; what it lists is every
    difference, as it always has."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.set_config(ADDON, speed=333)

    result = device.media_sync_stops(apply=False)

    assert device.meta(ADDON) == {"config": {"speed": 333}}
    assert result.loaded == [ADDON]


def test_an_edit_synced_on_one_device_reaches_the_other(make_device, server):
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"config": {"speed": 1}})
        each.syncs(server)

    desktop.set_config(ADDON, speed=22)
    desktop.syncs(server)
    result = laptop.syncs(server)

    assert laptop.meta(ADDON) == {"config": {"speed": 22}}
    assert result.loaded == [ADDON]


def test_the_first_device_to_sync_wins_a_conflict(make_device, server):
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"config": {"speed": 1}})
        each.syncs(server)

    desktop.set_config(ADDON, speed=22)
    laptop.set_config(ADDON, speed=333)
    desktop.syncs(server)
    laptop.syncs(server)
    desktop.syncs(server)

    assert laptop.meta(ADDON) == {"config": {"speed": 22}}
    assert desktop.meta(ADDON) == {"config": {"speed": 22}}


# ...also where nothing was downloaded, when the difference is not a change to keep


def test_a_config_an_addon_writes_back_after_a_download_was_applied_is_replaced_again(
    make_device, server
):
    """An add-on that read its config when Anki started still holds that one after a
    download replaced the file, and writes all of it back with whatever it changes. Kept,
    that would undo on both devices what the other one had just sent."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"config": {"speed": 1, "last_opened": 0}})
        each.syncs(server)

    desktop.set_config(ADDON, speed=22)
    desktop.syncs(server)
    laptop.syncs(server)  # 22 reaches the laptop's file; its add-on still holds 1
    laptop.write_meta(ADDON, {"config": {"speed": 1, "last_opened": 5}})
    laptop.syncs_media_only(server)
    laptop.syncs(server)
    desktop.syncs(server)

    assert laptop.meta(ADDON) == {"config": {"speed": 22, "last_opened": 0}}
    assert desktop.meta(ADDON) == {"config": {"speed": 22, "last_opened": 0}}


def test_an_addon_installed_again_gets_the_config_of_its_copy(device):
    """A new install has no config of its own, and the copy still holds the old one. What
    Anki recorded of the new install stays."""
    device.install(ADDON, {"name": "An add-on", "mod": 100, "config": {"speed": 1}})
    device.sync_starts()
    shutil.rmtree(device.addons / ADDON)
    device.install(ADDON, {"name": "An add-on", "mod": 200})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"name": "An add-on", "mod": 200, "config": {"speed": 1}}
    assert result.loaded == [ADDON]


def test_a_meta_json_anki_rebuilt_from_nothing_gets_the_config_of_its_copy(device):
    """Anki reads a meta.json it cannot parse as an empty one, and the next thing it writes
    about the add-on, such as enabling it, is then the whole file. The record in it knows
    no version, so Anki offers the add-on as an update until it is installed again; the
    copy's record is of another install."""
    device.install(ADDON, {"name": "An add-on", "mod": 100, "config": {"speed": 1}})
    device.sync_starts()
    device.write_meta(ADDON, {"disabled": True, "mod": 0, "conflicts": [], "update_enabled": True})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 1}, "mod": 0, "conflicts": []}
    assert result.loaded == [ADDON]


@pytest.mark.parametrize("broken", ["", '{"config": {"spe', "[]", "null"])
def test_a_config_anki_cannot_read_is_replaced_by_its_copy(device, broken):
    """A meta.json cut short by a crash: the copy repairs it. A sync start in between holds
    the broken file back."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.meta_path(ADDON).write_text(broken, encoding="utf-8")

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 1}}
    assert result.loaded == [ADDON]


def test_a_deleted_config_is_given_back_by_its_copy(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.meta_path(ADDON).unlink()

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 1}}
    assert result.loaded == [ADDON]


@pytest.mark.parametrize("ignored_at", ["sync_starts", "media_sync_stops"])
def test_an_addon_that_was_ignored_is_read_from_its_copy_once_it_no_longer_is(device, ignored_at):
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.set_config(ADDON, speed=333)
    getattr(device, ignored_at)(ignored=[ADDON])

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 1}}
    assert result.loaded == [ADDON]


# A media sync stops: a config only this device changed is left alone


def test_a_config_changed_after_the_sync_started_survives_its_media_sync_stopping(device):
    """An add-on that writes its config once the collection sync has finished, or the user
    saving one while a long media sync still runs."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.set_config(ADDON, speed=333)

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 333}}
    assert result.loaded == []
    assert result.finished == 1
    assert sync_actions.UPDATED_STATE == {}


def test_a_config_changed_here_survives_media_syncs_until_a_sync_sends_it(make_device, server):
    """Anki syncs media by itself every 15 minutes, with no `sync_will_start` before it."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"config": {"speed": 1}})
        each.syncs(server)

    laptop.set_config(ADDON, speed=333)
    for _ in range(3):
        assert laptop.syncs_media_only(server).loaded == []
        assert laptop.meta(ADDON) == {"config": {"speed": 333}}
    laptop.syncs(server)
    desktop.syncs(server)

    assert laptop.meta(ADDON) == {"config": {"speed": 333}}
    assert desktop.meta(ADDON) == {"config": {"speed": 333}}


def test_a_download_for_one_addon_leaves_another_addons_unsent_change_alone(make_device, server):
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"config": {"speed": 1}})
        each.install(OTHER, {"config": {"colour": "red"}})
        each.syncs(server)

    desktop.set_config(OTHER, colour="blue")
    desktop.syncs(server)
    laptop.set_config(ADDON, speed=333)
    result = laptop.syncs_media_only(server)

    assert result.loaded == [OTHER]
    assert laptop.meta(OTHER) == {"config": {"colour": "blue"}}
    assert laptop.meta(ADDON) == {"config": {"speed": 333}}


def test_a_newer_download_still_wins_over_a_change_that_survived_media_syncs(make_device, server):
    """Keeping an unsent change is not keeping it for good: the conflict rule is unchanged."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"config": {"speed": 1}})
        each.syncs(server)

    laptop.set_config(ADDON, speed=333)
    laptop.syncs_media_only(server)
    assert laptop.meta(ADDON) == {"config": {"speed": 333}}
    desktop.set_config(ADDON, speed=22)
    desktop.syncs(server)
    result = laptop.syncs_media_only(server)

    assert result.loaded == [ADDON]
    assert laptop.meta(ADDON) == {"config": {"speed": 22}}


def test_once_anki_was_restarted_a_change_is_kept_for_an_addon_that_had_a_download(
    make_device, server
):
    """Restarted, the add-on holds the config that was downloaded, and what is written
    after that is a change like any other."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"config": {"speed": 1}})
        each.syncs(server)
    desktop.set_config(ADDON, speed=22)
    desktop.syncs(server)
    laptop.syncs(server)

    laptop.restarts()
    laptop.set_config(ADDON, colour="blue")
    assert laptop.syncs_media_only(server).loaded == []
    laptop.syncs(server)
    desktop.syncs(server)

    assert desktop.meta(ADDON) == {"config": {"speed": 22, "colour": "blue"}}


def test_a_config_changed_along_with_what_anki_records_of_an_update_is_kept(device):
    device.install(ADDON, {"name": "An add-on", "mod": 100, "config": {"speed": 1}})
    device.sync_starts()
    changed = {"name": "An add-on", "mod": 200, "config": {"speed": 333}}
    device.write_meta(ADDON, changed)

    result = device.media_sync_stops()

    assert device.meta(ADDON) == changed
    assert result.loaded == []


@pytest.mark.parametrize("key, value", [("disabled", True), ("update_enabled", False)])
def test_an_addon_disabled_or_kept_from_updating_here_stays_so(device, key, value):
    """The two things in a meta.json besides the config that are the user's to set."""
    device.install(ADDON, {"name": "An add-on", "mod": 100})
    device.sync_starts()
    device.write_meta(ADDON, {"name": "An add-on", "mod": 100, key: value})

    result = device.media_sync_stops()

    assert device.meta(ADDON)[key] is value
    assert result.loaded == []


# A broken file or one without a config replaces no config, either way


def test_an_addon_installed_on_another_device_gets_the_config_this_one_has(make_device, server):
    """The laptop has had the copy since an earlier sync, without the add-on. It is
    installed there, and Anki restarted, which syncs."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    desktop.install(ADDON, {"name": "An add-on", "mod": 100, "config": {"speed": 22}})
    desktop.syncs(server)
    assert laptop.syncs(server).missing == [ADDON]

    laptop.install(ADDON, {"name": "An add-on", "mod": 100})
    result = laptop.syncs(server)
    desktop.syncs(server)

    assert result.loaded == [ADDON]
    assert laptop.meta(ADDON) == {"name": "An add-on", "mod": 100, "config": {"speed": 22}}
    assert desktop.meta(ADDON) == {"name": "An add-on", "mod": 100, "config": {"speed": 22}}


def test_a_meta_json_cut_short_on_one_device_reaches_no_other(make_device, server):
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"config": {"speed": 1}})
        each.syncs(server)

    laptop.write_text(laptop.meta_path(ADDON), '{"config": {"spe')
    laptop.syncs(server)
    desktop.syncs(server)

    assert laptop.meta(ADDON) == {"config": {"speed": 1}}
    assert desktop.meta(ADDON) == {"config": {"speed": 1}}


@pytest.mark.parametrize("sent", ['{"name": "An add-on"}', '{"config": {"spe', "[]"])
def test_what_an_older_version_sends_takes_no_config_away_and_is_repaired(
    make_device, server, sent
):
    """A device running a version from before this rule still sends a new install's
    meta.json, or one cut short. Here it is not read over the config, and the next sync
    start sends the config in its place, for every device."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, {"name": "An add-on", "config": {"speed": 1}})
        each.syncs(server)

    server.put(laptop.media_path(ADDON).name, sent.encode("utf-8"))
    for each in (desktop, laptop):
        assert each.syncs_media_only(server).loaded == []
        assert each.meta(ADDON) == {"name": "An add-on", "config": {"speed": 1}}
    laptop.syncs(server)
    desktop.syncs(server)

    assert desktop.meta(ADDON) == {"name": "An add-on", "config": {"speed": 1}}
    assert json.loads(server.files[laptop.media_path(ADDON).name]) == {
        "name": "An add-on",
        "config": {"speed": 1},
    }


def test_a_copy_without_a_config_is_still_read_over_a_meta_json_without_one(device):
    device.install(ADDON, {"name": "An add-on", "mod": 100})
    device.sync_starts()
    device.write_media(ADDON, {"name": "An add-on", "mod": 100, "disabled": True})

    result = device.media_sync_stops()

    assert device.meta(ADDON)["disabled"] is True
    assert result.loaded == [ADDON]


def test_in_ask_mode_a_copy_that_would_take_the_config_away_is_still_listed(device):
    """Ask mode copies nothing by itself; it shows every difference and the user judges."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.write_text(device.media_path(ADDON), '{"config": {"spe')

    result = device.media_sync_stops(apply=False)

    assert result.loaded == [ADDON]
    assert device.meta(ADDON) == {"config": {"speed": 1}}


@pytest.mark.parametrize("broken", ['{"config": {"spe', "[]"])
def test_the_dialogs_buttons_copy_no_file_that_is_no_json_object(device, broken):
    device.install(ADDON, {"name": "An add-on", "config": {"speed": 1}})
    device.sync_starts()
    copy = device.media_path(ADDON).read_bytes()

    device.write_text(device.meta_path(ADDON), broken)
    assert not device.calls(sync_actions.save_addon_to_media, ADDON)
    assert device.media_path(ADDON).read_bytes() == copy

    device.write_meta(ADDON, {"name": "An add-on", "config": {"speed": 333}})
    device.write_text(device.media_path(ADDON), broken)
    assert not device.calls(sync_actions.overwrite_addon_from_media, ADDON)
    assert device.meta(ADDON) == {"name": "An add-on", "config": {"speed": 333}}


def test_the_dialogs_buttons_do_copy_a_file_without_a_config_when_told_to(device):
    """They are the user's own decision, made with the diff in front of them. In ask mode
    they are the only way to settle a new install's file that sync starts hold back."""
    device.install(ADDON, {"name": "An add-on", "config": {"speed": 1}})
    device.sync_starts()

    device.write_meta(ADDON, {"name": "An add-on"})
    assert device.calls(sync_actions.save_addon_to_media, ADDON)
    assert device.media_copy(ADDON) == {"name": "An add-on"}

    device.write_meta(ADDON, {"name": "An add-on", "config": {"speed": 333}})
    assert device.calls(sync_actions.overwrite_addon_from_media, ADDON)
    assert device.meta(ADDON) == {"name": "An add-on"}


@pytest.mark.parametrize("copy", ["[]", "null", '{"config": {"spe'])
@pytest.mark.parametrize("here", [None, '{"config": {"spe'])
def test_a_copy_that_is_no_json_object_is_read_over_nothing(device, here, copy):
    """Not even over a meta.json that is missing or broken: Anki reads one cut short as no
    config, and a valid one that is no object stops it loading add-ons at all."""
    device.install(ADDON)
    if here is not None:
        device.write_text(device.meta_path(ADDON), here)
    device.write_text(device.media_path(ADDON), copy)

    result = device.media_sync_stops()

    assert result.loaded == []
    if here is None:
        assert not device.meta_path(ADDON).exists()
    else:
        assert device.meta_path(ADDON).read_text(encoding="utf-8") == here


def test_a_copy_that_is_an_empty_list_is_replaced_by_an_empty_meta_json(device):
    """Versions up to 1.0.0 took the two for the same file, as utils.ordered gives both as
    [], and so left the copy as it was. Read whole by such a version, a copy that is no JSON
    object stops Anki loading add-ons."""
    device.install(ADDON)
    device.write_text(device.meta_path(ADDON), "{}")
    device.write_text(device.media_path(ADDON), "[]")

    assert device.media_sync_stops(apply=False).loaded == [ADDON]
    assert device.sync_starts().saved == [ADDON]
    assert device.media_copy(ADDON) == {}


def test_a_meta_json_that_is_an_empty_list_is_repaired_from_an_empty_copy(device):
    """As above, the other way: Anki stops loading add-ons over such a meta.json."""
    device.install(ADDON)
    device.write_text(device.meta_path(ADDON), "[]")
    device.write_text(device.media_path(ADDON), "{}")

    assert device.media_sync_stops().loaded == [ADDON]
    assert device.meta(ADDON) == {}


@pytest.fixture
def held_open(monkeypatch):
    """Files that cannot be opened, as on Windows while another program holds them."""
    held: list[Path] = []
    real_open = io.open

    def opening(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and Path(file).resolve() in held:
            raise PermissionError(errno.EACCES, "held open by another program", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", opening)
    monkeypatch.setattr(builtins, "open", opening)
    return held


@pytest.mark.parametrize("which", ["meta_path", "media_path"])
def test_an_addon_whose_file_is_held_open_is_passed_over_until_the_next_sync(
    device, held_open, which
):
    """Taken for a file that is missing or broken, the media copy would be written anew
    over the record it keeps, or the meta.json read over and a change made here lost."""
    device.install(ADDON, {"name": "An add-on", "mod": 200, "config": {"speed": 1}})
    device.write_media(ADDON, {"name": "An add-on", "mod": 100, "config": {"speed": 1}})
    device.sync_starts()
    device.set_config(ADDON, speed=333)
    files = (device.meta_path(ADDON), device.media_path(ADDON))
    before = [path.read_bytes() for path in files]

    held_open.append(getattr(device, which)(ADDON).resolve())
    assert device.media_sync_stops().loaded == []
    assert device.sync_starts().skipped == [ADDON]
    held_open.clear()

    assert [path.read_bytes() for path in files] == before
    assert device.sync_starts().saved == [ADDON]
    assert device.media_copy(ADDON) == {"name": "An add-on", "mod": 100, "config": {"speed": 333}}


# What Anki records about the installed package stays on each device

INSTALLED = {"name": "An add-on", "mod": 100, "human_version": "1.0"}
UPDATED = {"name": "An add-on", "mod": 200, "human_version": "2.0"}


def test_what_anki_records_of_an_update_stays_and_is_not_sent(device):
    """Anki installs an update over the folder and writes the meta.json back with the new
    `mod`, and when it rewrites one it fills in `disabled` and `update_enabled`. Read back
    from the copy, the update just installed would be offered again; sent, every other
    device's Anki would be told that it has the update."""
    device.install(ADDON, dict(INSTALLED, config={"speed": 1}))
    device.sync_starts()
    updated = dict(UPDATED, config={"speed": 1}, disabled=False, update_enabled=True)
    device.write_meta(ADDON, updated)

    assert device.media_sync_stops().loaded == []
    assert device.meta(ADDON) == updated
    assert device.sync_starts().saved == []
    assert device.media_copy(ADDON) == dict(INSTALLED, config={"speed": 1})


def test_a_config_read_from_its_copy_keeps_the_record_of_this_devices_install(device):
    device.install(ADDON, dict(UPDATED, config={"speed": 1}))
    device.sync_starts()
    device.write_media(ADDON, dict(INSTALLED, config={"speed": 22}))

    result = device.media_sync_stops()

    assert device.meta(ADDON) == dict(UPDATED, config={"speed": 22})
    assert result.loaded == [ADDON]


def test_a_config_sent_to_its_copy_leaves_the_copy_its_record(device):
    """A device on an older version of this add-on reads the copy whole, and is so told the
    version the copy had, never that it has the one installed here."""
    device.install(ADDON, dict(UPDATED, config={"speed": 1}))
    device.write_media(ADDON, dict(INSTALLED, config={"speed": 1}))
    device.set_config(ADDON, speed=22)

    assert device.sync_starts().saved == [ADDON]
    assert device.media_copy(ADDON) == dict(INSTALLED, config={"speed": 22})


@pytest.mark.parametrize("copy", [None, '{"config": {"spe', "[]"])
def test_a_copy_made_anew_takes_the_record_of_the_device_that_makes_it(device, copy):
    """There is no record to keep: the first copy of an add-on's config, or one that repairs
    a copy that is no JSON object."""
    device.install(ADDON, dict(UPDATED, config={"speed": 1}))
    if copy is not None:
        device.write_text(device.media_path(ADDON), copy)

    assert device.sync_starts().saved == [ADDON]
    assert device.media_copy(ADDON) == dict(UPDATED, config={"speed": 1})


def test_a_difference_only_in_what_anki_records_is_neither_read_nor_listed(device):
    here = dict(UPDATED, config={"speed": 1})
    device.install(ADDON, here)
    device.write_media(
        ADDON, dict(INSTALLED, branch_index=1, min_point_version=231000, config={"speed": 1})
    )

    assert device.media_sync_stops(apply=False).loaded == []
    assert device.media_sync_stops().loaded == []
    assert device.meta(ADDON) == here


def test_a_change_made_here_survives_a_media_sync_while_the_records_differ(device):
    """A match is held for the same settings. Held only for files the same in everything, it
    would never be made while the copy keeps another device's record, and a change made
    here would be read over at every media sync stopping."""
    device.install(ADDON, dict(UPDATED, config={"speed": 1}))
    device.write_media(ADDON, dict(INSTALLED, config={"speed": 1}))
    device.sync_starts()
    device.set_config(ADDON, speed=333)

    result = device.media_sync_stops()

    assert device.meta(ADDON) == dict(UPDATED, config={"speed": 333})
    assert result.loaded == []


def test_a_meta_json_that_is_gone_gets_the_settings_of_its_copy_and_no_record(device):
    """There is no record of this device's install to keep, and the copy's is of another
    install: Anki names the add-on by its folder and offers it as an update until it is
    installed again."""
    device.install(ADDON)
    device.write_media(ADDON, dict(INSTALLED, config={"speed": 22}, disabled=True))

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 22}, "disabled": True}
    assert result.loaded == [ADDON]


def test_the_dialogs_buttons_copy_the_settings_and_leave_each_side_its_record(device):
    device.install(ADDON, dict(UPDATED, config={"speed": 1}))
    device.write_media(ADDON, dict(INSTALLED, config={"speed": 22}))

    assert device.calls(sync_actions.overwrite_addon_from_media, ADDON)
    assert device.meta(ADDON) == dict(UPDATED, config={"speed": 22})

    device.set_config(ADDON, speed=333)
    assert device.calls(sync_actions.save_addon_to_media, ADDON)
    assert device.media_copy(ADDON) == dict(INSTALLED, config={"speed": 333})


def test_an_update_on_one_device_leaves_the_other_its_record(make_device, server):
    """The other device's Anki goes on offering the update until it is installed there."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, dict(INSTALLED, config={"speed": 1}))
        each.syncs(server)

    desktop.write_meta(ADDON, dict(UPDATED, config={"speed": 22}))
    desktop.syncs(server)
    result = laptop.syncs(server)

    assert result.loaded == [ADDON]
    assert laptop.meta(ADDON) == dict(INSTALLED, config={"speed": 22})
    assert desktop.meta(ADDON) == dict(UPDATED, config={"speed": 22})
    assert json.loads(server.files[laptop.media_path(ADDON).name])["mod"] == 100


def test_the_tick_in_ankis_list_of_updates_goes_to_the_other_device(make_device, server):
    """`update_enabled` is the user's, though Anki writes it beside its record."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, dict(INSTALLED, config={"speed": 1}))
        each.syncs(server)

    desktop.write_meta(ADDON, dict(desktop.meta(ADDON), update_enabled=False))
    desktop.syncs(server)

    assert laptop.syncs(server).loaded == [ADDON]
    assert laptop.meta(ADDON) == dict(INSTALLED, config={"speed": 1}, update_enabled=False)


def test_the_range_one_anki_version_writes_stays_on_its_device(make_device, server):
    """After every update check, Anki writes the range of Anki versions that AnkiWeb answered
    for its own version, and it does not load an add-on outside that range."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, dict(INSTALLED, min_point_version=50, config={"speed": 1}))
        each.syncs(server)

    desktop.write_meta(ADDON, dict(desktop.meta(ADDON), min_point_version=231000))
    assert desktop.sync_starts().saved == []
    desktop.media_sync(server)
    desktop.media_sync_stops()

    assert laptop.syncs(server).loaded == []
    assert laptop.meta(ADDON)["min_point_version"] == 50


def test_the_update_a_device_on_an_older_version_sends_is_not_read(make_device, server):
    """Such a device sends its whole meta.json when an update changed what Anki records."""
    desktop, laptop = make_device("desktop"), make_device("laptop")
    for each in (desktop, laptop):
        each.install(ADDON, dict(INSTALLED, config={"speed": 1}))
        each.syncs(server)

    sent = dict(UPDATED, config={"speed": 1})
    server.put(laptop.media_path(ADDON).name, json.dumps(sent).encode("utf-8"))
    result = laptop.syncs_media_only(server)

    assert result.loaded == []
    assert laptop.meta(ADDON) == dict(INSTALLED, config={"speed": 1})


# The folder of this add-on itself, under the name the suite loads it by
OWN = "addon_config_sync"


@pytest.mark.parametrize("told, untold", [(200, True), (300, True), (100, False), (None, False)])
def test_this_addons_own_copy_tells_no_device_on_1_0_0_that_it_has_this_version(
    device, told, untold
):
    """The first sync after Anki installed this version ran 1.0.0, still in memory, which
    sends this add-on's meta.json whole. A device on 1.0.0 that read the `mod` of this
    version, or of a later one, would never be offered it; an older one has it offered."""
    here = {"name": "Addon Config Sync", "mod": 200, "config": {"run_on_sync": True}}
    copy = {key: value for key, value in here.items() if key != "mod"}
    if told is not None:
        copy["mod"] = told
    device.install(OWN, here)
    device.write_media(OWN, copy)

    assert device.sync_starts().saved == ([OWN] if untold else [])
    assert device.media_copy(OWN) == {
        key: value for key, value in copy.items() if not (untold and key == "mod")
    }
    assert device.sync_starts().saved == []


def test_an_install_of_this_addon_with_no_mod_of_its_own_leaves_its_copy_as_it_is(device):
    """Copied in, as this repository's build does, it has no version to tell devices."""
    device.install(OWN, {"config": {"run_on_sync": True}})
    copy = {"name": "Addon Config Sync", "mod": 200, "config": {"run_on_sync": True}}
    device.write_media(OWN, copy)

    assert device.sync_starts().saved == []
    assert device.media_copy(OWN) == copy
