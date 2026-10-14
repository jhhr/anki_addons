"""What Addon Config Sync holds on to between syncs, and what happens when it cannot.

A config changed only on this device is told from one another device sent by the media copy
still being what the config last matched (`matched_copies.py`). These tests are about that
record: where it comes from, what makes it worthless, and that without it the add-on copies
as it did before there was one.
"""

from __future__ import annotations

import filecmp
import json
import shutil
from unittest import mock

import pytest

from addon_config_sync import matched_copies, sync_actions
from addon_config_sync.matched_copies import MatchedCopies

ADDON = "1234567890"
OTHER = "another_addon"


def _digest_of_copy(device, addon_id=ADDON):
    return matched_copies.digest(device.media_path(addon_id).read_bytes())


def _a_change_made_here_is_kept(device) -> bool:
    """Whether a media sync stopping now leaves a config alone that only differs here."""
    device.set_config(ADDON, changed_here=True)
    device.media_sync_stops()
    return "changed_here" in device.meta(ADDON).get("config", {})


# The file


def test_a_sync_start_writes_down_what_each_copy_is(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.install(OTHER, {"config": {"colour": "red"}})

    device.sync_starts(ignored=[OTHER])

    assert json.loads(device.matches.read_text(encoding="utf-8")) == {
        "version": 1,
        "profiles": {str(device.media.parent): {ADDON: _digest_of_copy(device)}},
    }
    assert not device.matches.with_name(device.matches.name + ".tmp").exists()


def test_the_file_is_not_written_again_when_nothing_changed(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.matches.write_text(device.matches.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    marked = device.matches.read_bytes()

    device.sync_starts()
    device.media_sync_stops()

    assert device.matches.read_bytes() == marked


def _as_written(state: dict) -> str:
    return json.dumps(state)


def _another_version(state: dict) -> str:
    return json.dumps(dict(state, version=state["version"] + 1))


def _profiles_as_a_list(state: dict) -> str:
    return json.dumps(dict(state, profiles=list(state["profiles"].values())))


def _a_profile_as_text(state: dict) -> str:
    return json.dumps(dict(state, profiles={key: "x" for key in state["profiles"]}))


def _digests_as_lists(state: dict) -> str:
    profiles = {
        profile: {addon_id: [held] for addon_id, held in copies.items()}
        for profile, copies in state["profiles"].items()
    }
    return json.dumps(dict(state, profiles=profiles))


@pytest.mark.parametrize(
    "rewritten, kept",
    [
        (_as_written, True),
        (lambda state: "not json at all", False),
        (lambda state: "", False),
        (lambda state: "[]", False),
        (lambda state: "[" * 200_000, False),
        (_another_version, False),
        (_profiles_as_a_list, False),
        (_a_profile_as_text, False),
        (_digests_as_lists, False),
    ],
)
def test_a_file_that_is_not_what_was_written_means_copying_as_before(device, rewritten, kept):
    """Each made from the file a sync start wrote, digests and all, so that what makes it
    worthless is the one thing changed. The first is that file again, to show the rest
    could have kept the config."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    state = json.loads(device.matches.read_text(encoding="utf-8"))
    device.set_config(ADDON, speed=333)
    device.matches.write_text(rewritten(state), encoding="utf-8")

    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 333 if kept else 1}}
    assert result.loaded == ([] if kept else [ADDON])
    # ...and the file is whole again, with the match the copy made
    assert device.held() == {ADDON: _digest_of_copy(device)}


def test_a_file_that_cannot_be_written_stops_nothing_and_means_copying_as_before(device):
    device.matches.mkdir()
    device.install(ADDON, {"config": {"speed": 1}})

    assert device.sync_starts().saved == [ADDON]
    device.set_config(ADDON, speed=333)
    result = device.media_sync_stops()

    assert device.meta(ADDON) == {"config": {"speed": 1}}
    assert result == ([ADDON], [], [], 1)


def test_a_copy_is_unchanged_only_while_its_bytes_are_the_ones_held(tmp_path):
    """The careful side: a copy rewritten to the same JSON reads as downloaded, and one that
    cannot be read as changed."""
    matched = MatchedCopies("a profile's folder")
    copy = tmp_path / "_1_meta.json"
    copy.write_text('{"a": 1, "b": 2}', encoding="utf-8")
    assert not matched.unchanged("1", copy)
    matched.record("1", copy.read_bytes())
    matched.save()
    assert matched.unchanged("1", copy)
    assert MatchedCopies("a profile's folder").unchanged("1", copy)
    assert not MatchedCopies("another profile's folder").unchanged("1", copy)

    copy.write_text('{"b": 2, "a": 1}', encoding="utf-8")
    assert not matched.unchanged("1", copy)

    copy.unlink()
    assert not matched.unchanged("1", copy)


# What makes a match


def test_a_pair_that_is_the_same_already_when_a_sync_starts_is_a_match(device):
    """The first sync after the add-on is updated to a version that keeps matches: nothing
    is copied, and every config is in step with its copy."""
    device.install(ADDON, {"name": "An add-on", "config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 1}, "name": "An add-on"})

    assert device.sync_starts().saved == []

    assert device.held() == {ADDON: _digest_of_copy(device)}
    assert _a_change_made_here_is_kept(device)


def test_a_pair_found_the_same_when_a_media_sync_stops_is_a_match(device):
    """A device that has only ever synced media since the add-on was updated."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 1}})

    device.media_sync_stops()

    assert device.held() == {ADDON: _digest_of_copy(device)}
    assert _a_change_made_here_is_kept(device)


def test_ask_mode_makes_matches_as_well(device):
    """So that a device switched from asking to copying knows where it stands."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 1}})

    device.media_sync_stops(apply=False)

    assert device.held() == {ADDON: _digest_of_copy(device)}
    assert _a_change_made_here_is_kept(device)


def test_a_config_read_from_its_copy_matches_it(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 22}})

    device.media_sync_stops()

    assert device.held() == {ADDON: _digest_of_copy(device)}


def test_a_config_read_from_its_copy_is_read_again_for_as_long_as_anki_runs(device):
    """An add-on may hold the config from before in memory and write it back
    (`sync_actions.OVERWRITTEN_THIS_SESSION`); restarted, it holds the one that was read."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 22}})
    device.media_sync_stops()

    assert not _a_change_made_here_is_kept(device)
    device.restarts()
    assert _a_change_made_here_is_kept(device)


def test_saving_to_media_in_the_dialog_is_a_match(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.set_config(ADDON, speed=22)

    assert device.calls(sync_actions.save_addon_to_media, ADDON)

    assert device.media_copy(ADDON) == {"config": {"speed": 22}}
    assert device.held() == {ADDON: _digest_of_copy(device)}
    assert _a_change_made_here_is_kept(device)


def test_overwriting_from_media_in_the_dialog_is_a_match_and_a_read(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.write_media(ADDON, {"config": {"speed": 22}})

    assert device.calls(sync_actions.overwrite_addon_from_media, ADDON)

    assert device.held() == {ADDON: _digest_of_copy(device)}
    assert not _a_change_made_here_is_kept(device)
    device.restarts()
    assert _a_change_made_here_is_kept(device)


def test_a_copy_replaced_while_a_sync_starts_is_not_held_as_a_match(device):
    """A media sync can be under way when a sync starts. What is held is what was compared,
    so a download that lands between the comparing and the holding is read when that media
    sync stops, as it always was."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    compare = filecmp.cmp

    def compare_and_then_a_download_lands(*args, **kwargs):
        same = compare(*args, **kwargs)
        device.write_media(ADDON, {"config": {"speed": 22}})
        return same

    with mock.patch.object(filecmp, "cmp", compare_and_then_a_download_lands):
        assert device.sync_starts().saved == []

    assert device.held() == {}
    assert device.media_sync_stops().loaded == [ADDON]
    assert device.meta(ADDON) == {"config": {"speed": 22}}


# What ends one


def test_removing_a_copy_in_the_dialog_ends_its_match(device):
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    copy = device.media_path(ADDON).read_bytes()

    assert device.calls(sync_actions.remove_addon_from_media, ADDON)

    assert device.held() == {}
    device.media_path(ADDON).write_bytes(copy)
    assert not _a_change_made_here_is_kept(device)


@pytest.mark.parametrize("ignored_at", ["sync_starts", "media_sync_stops"])
def test_an_ignored_addon_has_no_match(device, ignored_at):
    """Neither file is looked at while it is ignored, so nothing is known of the pair."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.install(OTHER, {"config": {"colour": "red"}})
    device.sync_starts()

    getattr(device, ignored_at)(ignored=[ADDON])

    assert device.held() == {OTHER: _digest_of_copy(device, OTHER)}


@pytest.mark.parametrize("sync", ["sync_starts", "media_sync_stops"])
def test_an_addon_without_a_meta_json_has_no_match_in_any_profile(device, sync):
    device.install(ADDON, {"config": {"speed": 1}})
    device.install(OTHER, {"config": {"colour": "red"}})
    device.sync_starts()
    device.open_profile("Second")
    device.sync_starts()
    shutil.rmtree(device.addons / ADDON)

    getattr(device, sync)()

    profiles = json.loads(device.matches.read_text(encoding="utf-8"))["profiles"]
    assert len(profiles) == 2
    assert all(set(copies) == {OTHER} for copies in profiles.values())


# Whose match it is


def test_a_match_is_one_profiles(device):
    """A media folder is a profile's; an add-on's config is every profile's."""
    device.install(ADDON, {"config": {"speed": 1}})
    device.sync_starts()
    device.set_config(ADDON, speed=333)

    # In another profile the same config is news to that profile's media folder
    device.open_profile("Second")
    device.write_media(ADDON, {"config": {"speed": 1}})
    assert device.media_sync_stops().loaded == [ADDON]
    assert device.meta(ADDON) == {"config": {"speed": 1}}

    # ...and what the first profile's copy matched is still what it was
    device.restarts()
    device.open_profile("User 1")
    assert _a_change_made_here_is_kept(device)


def test_two_anki_folders_with_the_addon_in_one_place_do_not_share_a_match(make_device, server):
    """An add-on's folder linked into two Anki data folders is one matches file for both,
    and each has a profile of the default name. What one of them sent must not pass, in
    the other, for a copy nothing was downloaded over."""
    first, second = make_device("first"), make_device("second")
    second.matches = first.matches
    for each in (first, second):
        each.install(ADDON, {"config": {"speed": 1}})
        each.syncs(server)

    second.restarts()
    second.set_config(ADDON, colour="blue")
    first.set_config(ADDON, speed=22)
    first.syncs(server)
    result = second.syncs_media_only(server)

    assert result.loaded == [ADDON]
    assert second.meta(ADDON) == {"config": {"speed": 22}}
