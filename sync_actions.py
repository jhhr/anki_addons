import filecmp
import os
import shutil
from pathlib import Path

from aqt import mw

from .utils import (
    is_addon_disabled,
    get_existing_addons,
    get_configs_in_media,
)


def save_configs_on_sync(
    saved_addons: list[str],
    disabled_addons: list[str],
    skipped_addons: list[str],
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
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), "collection.media")

    for addon_dir in anki_addons_path.iterdir():
        if not addon_dir.is_dir():
            continue

        meta_json = addon_dir / "meta.json"
        dest_file = media_path / f"_{addon_dir.name}_meta.json"

        if meta_json.is_file():
            # If the destination media file doesn't exist, or the meta.json file has changed,
            # copy the meta.json file to the media folder
            saved_addon = False
            if not dest_file.is_file():
                shutil.copy(meta_json, dest_file)
                saved_addon = True
            elif not filecmp.cmp(meta_json, dest_file, False):
                # To trigger Anki to sync the file, remove the old one and copy the new one
                os.remove(dest_file)
                shutil.copy(meta_json, dest_file)
                saved_addon = True

            # Update feedback lists
            if saved_addon:
                saved_addons.append(addon_dir.name)
                if is_addon_disabled(meta_json):
                    disabled_addons.append(addon_dir.name)
        else:
            # No meta.json file to save, skip
            skipped_addons.append(addon_dir.name)


def read_configs_on_sync(
    loaded_addons: list[str],
    disabled_addons: list[str],
    missing_addons: list[str],
    on_finish_callback: callable,
    media_sync_status: bool,
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

    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), "collection.media")

    existing_addon_ids = get_existing_addons(anki_addons_path)
    synced_addon_ids = get_configs_in_media(media_path)

    for addon_id in synced_addon_ids:
        if addon_id in existing_addon_ids:
            meta_json = media_path / f"_{addon_id}_meta.json"
            dest_file = anki_addons_path / addon_id / "meta.json"

            # do we have a dest file that differs from the current meta.json file?
            if dest_file.is_file():
                if not meta_json.is_file() or not filecmp.cmp(meta_json, dest_file, False):
                    # The files don't match, so copy the dest file to the meta.json
                    shutil.copy(dest_file, meta_json)
                    # Update feedback lists
                    loaded_addons.append(addon_id)
                    if is_addon_disabled(dest_file):
                        disabled_addons.append(addon_id)
        else:
            missing_addons.append(addon_id)

    on_finish_callback()
