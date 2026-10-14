import filecmp
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from aqt import mw

from .matched_copies import MatchedCopies
from .utils import (
    is_addon_disabled,
    get_existing_addons,
    get_configs_in_media,
    is_addon_ignored,
    json_bytes_deep_equal,
    json_files_deep_equal,
    ordered,
    read_json_object,
)

UPDATED_STATE: dict[str, bool] = {}
# The add-ons whose meta.json a media copy was copied over since Anki started. An add-on
# reads its config when it loads, and many keep it and later write all of it back. Written
# back after the file was replaced under it, that undoes what another device sent, and
# nothing tells it from a change the user made. So for the rest of the session such an
# add-on's config is read back from the media folder as it always was (read_configs_on_sync).
OVERWRITTEN_THIS_SESSION: set[str] = set()
SUPPRESS_AUTO_SYNC_ACTIONS = False
SUPPRESS_SYNC_FINISH_CALLBACKS: list[Callable[[], None]] = []

# What Anki writes into a meta.json about the installed package, from the package and from
# AnkiWeb, as against what the user set (`config`, `disabled`, `update_enabled`)
INSTALL_KEYS = frozenset({
    "name",
    "mod",
    "conflicts",
    "min_point_version",
    "max_point_version",
    "branch_index",
    "human_version",
    "homepage",
})


def get_paths() -> tuple[Path, Path]:
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), "collection.media")
    return anki_addons_path, media_path


def get_matched_copies() -> MatchedCopies:
    """What the open profile's media copies last matched; see matched_copies.py."""
    return MatchedCopies(str(Path(mw.pm.profileFolder())))


def record_if_same(
    matched: MatchedCopies, addon_id: str, addon_meta_json: Path, media_meta_json: Path
) -> None:
    """Hold the media copy as the one this config matches, going by one read of each file.

    The caller has just copied one over the other or found them the same, but not from
    these bytes, and when a sync starts, a media sync that writes the copy can be under way.
    So what is held is the digest of bytes that were compared here, never of a file read
    afterwards; and a pair that is not the same after all is held as nothing."""
    try:
        media_copy = media_meta_json.read_bytes()
        config = addon_meta_json.read_bytes()
    except OSError:
        matched.forget(addon_id)
        return
    if media_copy == config or json_bytes_deep_equal(media_copy, config):
        matched.record(addon_id, media_copy)
    else:
        matched.forget(addon_id)


def drop_matches_of_addons_without_config(matched: MatchedCopies, anki_addons_path: Path) -> None:
    try:
        with_config = {
            addon_dir.name
            for addon_dir in anki_addons_path.iterdir()
            if (addon_dir / "meta.json").is_file()
        }
    except OSError:
        # This looks into every add-on's folder, the ignored ones too, which the sync
        # itself does not. One that cannot be looked into is no reason to fail a sync.
        return
    matched.keep_only(with_config)


def _users_part(meta: dict[str, Any]) -> dict[str, Any]:
    part = {key: value for key, value in meta.items() if key not in INSTALL_KEYS}
    # As Anki reads the two (AddonMeta.from_json_meta). It writes both out, with these
    # values where the file had neither, whenever it rewrites the file for another reason.
    part["disabled"] = bool(meta.get("disabled"))
    part["update_enabled"] = meta.get("update_enabled", True)
    return part


def is_change_made_here(addon_meta_json: Path, media_meta_json: Path) -> bool:
    """Whether a meta.json that differs from its media copy holds a change of the user's or
    of the add-on's: what there is to lose by copying the media copy over it, when nothing
    was downloaded over that copy. Three differences are not that, and are copied over as
    they always were."""
    config = read_json_object(addon_meta_json)
    if config is None:
        # Missing, cut short, or no JSON object: nothing to keep, and the copy can repair it
        return False
    media_copy = read_json_object(media_meta_json)
    if media_copy is None:
        # It is the copy that is broken, and this file that would repair it
        return True
    if "config" in media_copy and "config" not in config:
        # A new install of the add-on, or a file Anki rebuilt from nothing after it failed
        # to read one: Anki writes no `config` until one is set. The copy holds the config
        # this add-on had, and a new install has always been given it.
        return False
    # An update of the add-on changes only what Anki records about the package. Kept, that
    # would go out with the next sync every time and mark the add-on as updated on devices
    # that have not updated it, which Anki then does not offer the update to.
    return ordered(_users_part(config)) != ordered(_users_part(media_copy))


def mark_addon_updated(addon_id: str) -> None:
    UPDATED_STATE[addon_id] = True
    OVERWRITTEN_THIS_SESSION.add(addon_id)


def mark_sync_cycle(updated_addon_ids: list[str]) -> None:
    updated_set = set(updated_addon_ids)
    for addon_id in list(UPDATED_STATE.keys()):
        if addon_id not in updated_set:
            del UPDATED_STATE[addon_id]
    for addon_id in updated_set:
        UPDATED_STATE[addon_id] = True


def save_addon_to_media(addon_id: str) -> bool:
    anki_addons_path, media_path = get_paths()
    addon_meta_json = anki_addons_path / addon_id / "meta.json"
    media_meta_json = media_path / f"_{addon_id}_meta.json"
    if not addon_meta_json.is_file():
        return False
    shutil.copy(addon_meta_json, media_meta_json)
    record_match(addon_id, addon_meta_json, media_meta_json)
    return True


def overwrite_addon_from_media(addon_id: str) -> bool:
    anki_addons_path, media_path = get_paths()
    media_meta_json = media_path / f"_{addon_id}_meta.json"
    addon_meta_json = anki_addons_path / addon_id / "meta.json"
    if not media_meta_json.is_file() or not addon_meta_json.parent.is_dir():
        return False
    shutil.copy(media_meta_json, addon_meta_json)
    mark_addon_updated(addon_id)
    record_match(addon_id, addon_meta_json, media_meta_json)
    return True


def remove_addon_from_media(addon_id: str) -> bool:
    _, media_path = get_paths()
    media_meta_json = media_path / f"_{addon_id}_meta.json"
    if not media_meta_json.exists():
        return False
    media_meta_json.unlink()
    matched = get_matched_copies()
    matched.forget(addon_id)
    matched.save()
    return True


def record_match(addon_id: str, addon_meta_json: Path, media_meta_json: Path) -> None:
    """One of the dialog's copies has made the add-on's config and its media copy the same."""
    matched = get_matched_copies()
    record_if_same(matched, addon_id, addon_meta_json, media_meta_json)
    matched.save()


def save_configs_on_sync(
    saved_addons: list[str],
    disabled_addons: list[str],
    skipped_addons: list[str],
    ignored_addons: set[str] | None = None,
):
    """
    Saves the configs from the addon folder to the media folder, if they have changed or
    don't exist in the media folder yet.

    The file actions done here are what will trigger Anki to upload the files to AnkiWeb.
    This is run before media sync starts, so the changes will be immediately uploaded.
    However, if the file has been modified in AnkiWeb, it will not be overwritten.
    Thus, the first device to sync will have its changes uploaded, and the other devices will
    download those.

    saved_addons: List of addon IDs to mutate, for feedback purposes
    disabled_addons: List of addon IDs that are disabled, for feedback purposes
    skipped_addons: List of addon IDs that were skipped, for feedback purposes
    :return:
    """
    anki_addons_path, media_path = get_paths()
    ignored_addons = ignored_addons or set()
    matched = get_matched_copies()

    for addon_dir in anki_addons_path.iterdir():
        if not addon_dir.is_dir():
            continue
        if addon_dir.name in ignored_addons:
            # Whatever happens to either file while it is ignored is not looked at, so
            # nothing is known of the pair when it is no longer ignored
            matched.forget(addon_dir.name)
            continue

        addon_meta_json = addon_dir / "meta.json"
        media_meta_json = media_path / f"_{addon_dir.name}_meta.json"

        if addon_meta_json.is_file():
            # If the destination media file doesn't exist, or the meta.json file has changed,
            # copy the meta.json file to the media folder
            saved_addon = False
            if not media_meta_json.is_file():
                shutil.copy(addon_meta_json, media_meta_json)
                saved_addon = True
            elif not filecmp.cmp(
                addon_meta_json, media_meta_json, False
            ) and not json_files_deep_equal(addon_meta_json, media_meta_json):
                # To trigger Anki to sync the file, remove the old one and copy the new one
                os.remove(media_meta_json)
                shutil.copy(addon_meta_json, media_meta_json)
                saved_addon = True

            # Copied just now or the same already: the media copy is this config. What the
            # media sync that follows downloads over it is what read_configs_on_sync is for.
            record_if_same(matched, addon_dir.name, addon_meta_json, media_meta_json)

            # Update feedback lists
            if saved_addon:
                saved_addons.append(addon_dir.name)
                if is_addon_disabled(addon_meta_json):
                    disabled_addons.append(addon_dir.name)
        else:
            # No meta.json file to save, skip
            skipped_addons.append(addon_dir.name)

    drop_matches_of_addons_without_config(matched, anki_addons_path)
    matched.save()


def read_configs_on_sync(
    loaded_addons: list[str],
    disabled_addons: list[str],
    missing_addons: list[str],
    on_finish_callback: Callable[[], None],
    media_sync_status: bool,
    ignored_addons: set[str] | None = None,
    apply_to_addons: bool = True,
):
    """
    Read the configs from the media folder and copy them to the addon folder.
    This is run after every media sync that finishes: the one a sync starts, before which
    save_configs_on_sync has run, and the ones Anki starts by itself, before which it has not.
    Changes made in AnkiWeb will have been downloaded to the media folder,
    and those are then copied to the addon folder.

    A config that differs from its media copy is left as it is when the difference is a
    change made on this device that is waiting for the next save_configs_on_sync to send
    it. That takes all of: nothing was downloaded over the media copy since the two last
    matched, no media copy was copied over this add-on's config since Anki started, and
    is_change_made_here. Every other difference is copied over, as all of them used to be.
    Asking instead of copying (apply_to_addons=False) lists every difference, as before:
    it loses nothing, and what it shows is the user's to judge.

    loaded_addons: List of addon IDs to mutate, for feedback purposes
    disabled_addons: List of addon IDs that are disabled, for feedback purposes
    missing_addons: List of addon IDs that were missing, for feedback purposes
    media_sync_status: Arg from Anki, whether media sync is still in progress
    """
    # If media_sync_status is True, then media sync is still in progress, and we should not read
    # the configs yet
    if media_sync_status is True:
        return

    anki_addons_path, media_path = get_paths()
    ignored_addons = ignored_addons or set()
    matched = get_matched_copies()

    existing_addon_ids = get_existing_addons(anki_addons_path)
    synced_addon_ids = get_configs_in_media(media_path)

    for addon_id in synced_addon_ids:
        if addon_id in ignored_addons:
            matched.forget(addon_id)  # as in save_configs_on_sync
            continue
        if addon_id in existing_addon_ids:
            media_meta_json = media_path / f"_{addon_id}_meta.json"
            addon_meta_json = anki_addons_path / addon_id / "meta.json"

            # Copy when destination config is missing, or when it differs from media.
            if media_meta_json.is_file() and (
                not addon_meta_json.is_file()
                or (
                    not filecmp.cmp(media_meta_json, addon_meta_json, False)
                    and not json_files_deep_equal(media_meta_json, addon_meta_json)
                )
            ):
                # ...unless it is the config that changed, on this device, and the copy
                # would undo that
                if (
                    apply_to_addons
                    and addon_id not in OVERWRITTEN_THIS_SESSION
                    and matched.unchanged(addon_id, media_meta_json)
                    and is_change_made_here(addon_meta_json, media_meta_json)
                ):
                    continue
                loaded_addons.append(addon_id)
                if apply_to_addons:
                    shutil.copy(media_meta_json, addon_meta_json)
                    mark_addon_updated(addon_id)
                    record_if_same(matched, addon_id, addon_meta_json, media_meta_json)
                    if is_addon_disabled(addon_meta_json):
                        disabled_addons.append(addon_id)
            elif media_meta_json.is_file():
                record_if_same(matched, addon_id, addon_meta_json, media_meta_json)
        else:
            missing_addons.append(addon_id)

    drop_matches_of_addons_without_config(matched, anki_addons_path)
    matched.save()

    if apply_to_addons:
        mark_sync_cycle(loaded_addons)
    else:
        mark_sync_cycle([])

    on_finish_callback()


def get_ignored_addons_from_config(config: dict) -> set[str]:
    anki_addons_path, media_path = get_paths()
    all_addons = get_existing_addons(anki_addons_path) | set(get_configs_in_media(media_path))
    return {addon_id for addon_id in all_addons if is_addon_ignored(config, addon_id)}
