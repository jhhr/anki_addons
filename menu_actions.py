import shutil
from pathlib import Path

from aqt import mw
from aqt.qt import QMessageBox
from aqt.utils import showInfo
from .utils import (
    is_addon_disabled,
    get_existing_addons,
    get_configs_in_media,
)

from .messages import get_read_configs_message, get_save_configs_message
from .show_missing_addons_dialog import show_missing_addons_dialog


def save_configs_menu_action() -> None:
    """
    Save all addon configs from the addon folder to the media folder.
    Shows a summary dialog of what was saved."""
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), "collection.media")

    saved_addons = []
    disabled_addons = []
    skipped_addons = []

    for addon_dir in anki_addons_path.iterdir():
        if not addon_dir.is_dir():
            continue

        addon_meta_json = addon_dir / "meta.json"
        if addon_meta_json.is_file():
            media_meta_json = media_path / f"_{addon_dir.name}_meta.json"
            shutil.copy(addon_meta_json, media_meta_json)
            saved_addons.append(addon_dir.name)

            if is_addon_disabled(addon_meta_json):
                disabled_addons.append(addon_dir.name)
        else:
            skipped_addons.append(addon_dir.name)

    # Prepare feedback message
    message = "<b>Save Configs Complete!</b><br><br>"
    message += get_save_configs_message(
        saved_addons,
        disabled_addons,
        skipped_addons,
        is_menu_action=True,
    )
    showInfo(message, title="Addon Config Sync", textFormat="rich")


def read_configs_menu_action():
    """Read all addon configs from the media folder to the addon folder.
    Shows a summary dialog of what was loaded and which addons are missing.
    Will overwrite existing configs in the addon folder, possibly destroying local changes
    that have not saved into the media folder yet."""

    # Show confirmation dialog before proceeding
    reply = QMessageBox.question(
        mw,
        "Confirm Read Configs",
        "<b>Read all synced addon configs?</b><br><br>This will overwrite existing configs in the"
        " addon folder with synced versions from AnkiWeb.<br><br><i>Any local changes that haven't"
        " been saved will be lost.</i>",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )

    if reply != QMessageBox.StandardButton.Yes:
        return

    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), "collection.media")

    existing_addon_ids = get_existing_addons(anki_addons_path)
    synced_addon_ids = get_configs_in_media(media_path)

    loaded_addons = []
    disabled_addons = []
    missing_addons = []

    for addon_id in synced_addon_ids:
        if addon_id in existing_addon_ids:
            media_meta_json = media_path / f"_{addon_id}_meta.json"
            addon_meta_json = anki_addons_path / addon_id / "meta.json"
            shutil.copy(media_meta_json, addon_meta_json)
            loaded_addons.append(addon_id)

            # Check if addon will be disabled
            if is_addon_disabled(media_meta_json):
                disabled_addons.append(addon_id)
        else:
            missing_addons.append(addon_id)

    # Prepare feedback message
    message = "<b>Read Configs Complete!</b><br><br>"
    message += get_read_configs_message(
        loaded_addons,
        disabled_addons,
        missing_addons,
    )
    showInfo(message, title="Addon Config Sync", textFormat="rich")


def show_missing_addons() -> None:
    """Show a list of addon codes that have synced configs but are not installed"""
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), "collection.media")

    # Get all addon directories that exist
    existing_addon_ids = get_existing_addons(anki_addons_path)

    # Get all synced addon IDs from media folder
    synced_addon_ids = get_configs_in_media(media_path)

    missing_addons = [
        addon_id for addon_id in synced_addon_ids if addon_id not in existing_addon_ids
    ]

    if not missing_addons:
        message = "<b>No Missing Addons!</b><br><br>"
        message += "All synced addon configs have their addons installed.<br><br>"
        message += "<i>Run 'Read Configs' to load the configurations.</i>"
        showInfo(message, title="Missing Addons", textFormat="rich")
    else:
        # Show custom dialog with copy button
        show_missing_addons_dialog(missing_addons)
