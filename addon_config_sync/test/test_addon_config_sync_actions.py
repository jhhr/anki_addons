"""What Addon Config Sync copies when a sync starts and when a media sync stops.

Every test here goes through the two functions Anki's hooks reach and looks at the files
afterwards, so none of them knows how the add-on decides. They are in three parts: what a
sync start does, what a media sync stopping does with a difference it copies over, and what
it does with a config that only this device changed. The last part is the one that used to
lose the change.
"""

from __future__ import annotations

import os
import shutil

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


def test_a_sync_start_sends_a_new_installs_config_over_the_copy_of_the_old_one(device):
    """Not what anyone wants of it. Pinned because what a sync start sends, and when, is
    what devices in use are set up around: it changes on purpose or not at all. Only a
    media sync that stops before the sync starts gives the new install the copy's config
    (below)."""
    device.install(ADDON, {"name": "An add-on", "config": {"speed": 1}})
    device.sync_starts()
    shutil.rmtree(device.addons / ADDON)
    device.install(ADDON, {"name": "An add-on"})

    result = device.sync_starts()

    assert device.media_copy(ADDON) == {"name": "An add-on"}
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


def test_what_anki_records_of_an_update_is_read_back_like_any_other_difference(device):
    """Anki installs an update over the folder and writes the meta.json back with the new
    `mod`, and when it rewrites one it fills in `disabled` and `update_enabled`. Kept, the
    new `mod` would go out with the next sync and tell the Anki of every other device that
    it has the update."""
    installed = {"name": "An add-on", "mod": 100, "config": {"speed": 1}}
    device.install(ADDON, installed)
    device.sync_starts()
    device.write_meta(
        ADDON,
        dict(installed, mod=200, human_version="2.0", disabled=False, update_enabled=True),
    )

    result = device.media_sync_stops()

    assert device.meta(ADDON) == installed
    assert result.loaded == [ADDON]


def test_an_addon_installed_again_gets_the_config_of_its_copy(device):
    """A new install has no config of its own, and the copy still holds the old one."""
    device.install(ADDON, {"name": "An add-on", "mod": 100, "config": {"speed": 1}})
    device.sync_starts()
    shutil.rmtree(device.addons / ADDON)
    device.install(ADDON, {"name": "An add-on", "mod": 200})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"name": "An add-on", "mod": 100, "config": {"speed": 1}}
    assert result.loaded == [ADDON]


def test_a_meta_json_anki_rebuilt_from_nothing_gets_the_config_of_its_copy(device):
    """Anki reads a meta.json it cannot parse as an empty one, and the next thing it writes
    about the add-on, such as enabling it, is then the whole file."""
    device.install(ADDON, {"name": "An add-on", "mod": 100, "config": {"speed": 1}})
    device.sync_starts()
    device.write_meta(ADDON, {"disabled": True, "mod": 0, "conflicts": [], "update_enabled": True})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"name": "An add-on", "mod": 100, "config": {"speed": 1}}
    assert result.loaded == [ADDON]


@pytest.mark.parametrize("broken", ["", '{"config": {"spe', "[]", "null"])
def test_a_config_anki_cannot_read_is_replaced_by_its_copy(device, broken):
    """A meta.json cut short by a crash. The copy repairs it for as long as no sync has
    started, which sends the broken file out in its place."""
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


def test_a_config_is_kept_over_a_copy_that_cannot_be_read(device):
    """Sent out broken at the last sync start and repaired here since: the copy would break
    it again."""
    device.install(ADDON)
    device.meta_path(ADDON).write_text('{"config": {"spe', encoding="utf-8")
    device.sync_starts()
    device.write_meta(ADDON, {"config": {"speed": 1}})

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 1}}
    assert result.loaded == []
