import filecmp
import os
import shutil
from pathlib import Path
from typing import Callable

from aqt import mw

from .utils import (
    is_addon_disabled,
    get_existing_addons,
    get_configs_in_media,
    is_addon_ignored,
    json_files_deep_equal,
)

UPDATED_STATE: dict[str, bool] = {}
SUPPRESS_AUTO_SYNC_ACTIONS = False
SUPPRESS_SYNC_FINISH_CALLBACKS: list[Callable[[], None]] = []


def get_paths() -> tuple[Path, Path]:
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), "collection.media")
    return anki_addons_path, media_path


def mark_addon_updated(addon_id: str) -> None:
    UPDATED_STATE[addon_id] = True


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
    return True


def overwrite_addon_from_media(addon_id: str) -> bool:
    anki_addons_path, media_path = get_paths()
    media_meta_json = media_path / f"_{addon_id}_meta.json"
    addon_meta_json = anki_addons_path / addon_id / "meta.json"
    if not media_meta_json.is_file() or not addon_meta_json.parent.is_dir():
        return False
    shutil.copy(media_meta_json, addon_meta_json)
    mark_addon_updated(addon_id)
    return True


def remove_addon_from_media(addon_id: str) -> bool:
    _, media_path = get_paths()
    media_meta_json = media_path / f"_{addon_id}_meta.json"
    if not media_meta_json.exists():
        return False
    media_meta_json.unlink()
    return True


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

    for addon_dir in anki_addons_path.iterdir():
        if not addon_dir.is_dir():
            continue
        if addon_dir.name in ignored_addons:
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

            # Update feedback lists
            if saved_addon:
                saved_addons.append(addon_dir.name)
                if is_addon_disabled(addon_meta_json):
                    disabled_addons.append(addon_dir.name)
        else:
            # No meta.json file to save, skip
            skipped_addons.append(addon_dir.name)


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
    This is run after media sync has finished and save_configs_on_sync has run.
    Changes made in AnkiWeb will have been downloaded to the media folder,
    and those are then copied to the addon folder.

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

    existing_addon_ids = get_existing_addons(anki_addons_path)
    synced_addon_ids = get_configs_in_media(media_path)

    for addon_id in synced_addon_ids:
        if addon_id in ignored_addons:
            continue
        if addon_id in existing_addon_ids:
            media_meta_json = media_path / f"_{addon_id}_meta.json"
            addon_meta_json = anki_addons_path / addon_id / "meta.json"

            # do we have a dest file that differs from the current meta.json file?
            if addon_meta_json.is_file():
                if not media_meta_json.is_file() or (
                    not filecmp.cmp(media_meta_json, addon_meta_json, False)
                    and not json_files_deep_equal(media_meta_json, addon_meta_json)
                ):
                    loaded_addons.append(addon_id)
                    if apply_to_addons:
                        shutil.copy(media_meta_json, addon_meta_json)
                        mark_addon_updated(addon_id)
                        if is_addon_disabled(addon_meta_json):
                            disabled_addons.append(addon_id)
        else:
            missing_addons.append(addon_id)

    if apply_to_addons:
        mark_sync_cycle(loaded_addons)
    else:
        mark_sync_cycle([])

    on_finish_callback()


def get_ignored_addons_from_config(config: dict) -> set[str]:
    anki_addons_path, media_path = get_paths()
    all_addons = get_existing_addons(anki_addons_path) | set(get_configs_in_media(media_path))
    return {addon_id for addon_id in all_addons if is_addon_ignored(config, addon_id)}
