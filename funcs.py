import filecmp
import os
import shutil
import json
from pathlib import Path

from aqt import mw
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QMessageBox,
    QApplication,
    QScrollArea,
    QWidget,
    QFrame,
)
from aqt.utils import showInfo, tooltip


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

    # Get all addon directories that exist
    existing_addon_ids = {
        addon_dir.name for addon_dir in anki_addons_path.iterdir() if addon_dir.is_dir()
    }

    for addon_dir in anki_addons_path.iterdir():
        if not addon_dir.is_dir():
            continue

        meta_json = addon_dir / "meta.json"
        dest_file = media_path / f"_{addon_dir.name}_meta.json"

        # do we have a dest file that differs from the current meta.json file?
        if dest_file.is_file():
            if not meta_json.is_file() or not filecmp.cmp(meta_json, dest_file, False):
                # The files don't match, so copy the dest file to the meta.json
                shutil.copy(dest_file, meta_json)
                # Update feedback lists
                loaded_addons.append(addon_dir.name)
                if is_addon_disabled(dest_file):
                    disabled_addons.append(addon_dir.name)
                if addon_dir.name not in existing_addon_ids:
                    missing_addons.append(addon_dir.name)

    on_finish_callback()


def is_addon_disabled(meta_json_path: Path) -> bool:
    """Check if an addon is marked as disabled in its meta.json file."""
    try:
        with open(meta_json_path, "r", encoding="utf-8") as f:
            meta_data = json.load(f)
            return meta_data.get("disabled", False)
    except Exception:
        return False  # If we can't read the file, assume it's not disabled


def save_configs_menu_action():
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

        meta_json = addon_dir / "meta.json"
        if meta_json.is_file():
            dest_file = media_path / f"_{addon_dir.name}_meta.json"
            shutil.copy(meta_json, dest_file)
            saved_addons.append(addon_dir.name)

            if is_addon_disabled(meta_json):
                disabled_addons.append(addon_dir.name)
        else:
            skipped_addons.append(addon_dir.name)

    # Prepare feedback message
    message = "<b>Save Configs Complete!</b><br><br>"
    message += get_save_configs_message(saved_addons, disabled_addons, skipped_addons)
    showInfo(message, title="Addon Config Sync", textFormat="rich")


def get_save_configs_message(
    saved_addons: list[str],
    disabled_addons: list[str],
    skipped_addons: list[str],
    is_menu_action: bool = False,
) -> str:
    """Get feedback message after saving configs"""
    message = ""

    if saved_addons:
        message += f"<b>✓ Saved {len(saved_addons)} addon config(s):</b><br>"
        for addon_id in saved_addons[:10]:  # Show first 10
            status = " <i>(disabled)</i>" if addon_id in disabled_addons else ""
            message += f"&nbsp;&nbsp;• {addon_id}{status}<br>"
        if len(saved_addons) > 10:
            message += f"&nbsp;&nbsp;• ... and {len(saved_addons) - 10} more<br>"
        message += "<br>"

        if disabled_addons:
            message += (
                f"<i>Note: {len(disabled_addons)} addon(s) are disabled and will sync as"
                " disabled.</i><br><br>"
            )

    if skipped_addons:
        message += f"<b>⊘ Skipped {len(skipped_addons)} addon(s) (no meta.json):</b><br>"
        for addon_id in skipped_addons[:5]:  # Show first 5
            message += f"&nbsp;&nbsp;• {addon_id}<br>"
        if len(skipped_addons) > 5:
            message += f"&nbsp;&nbsp;• ... and {len(skipped_addons) - 5} more<br>"
        message += "<br>"

    if is_menu_action:
        message += "<i>Remember to sync to upload configs to AnkiWeb!</i>"

    return message


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

    # Get all addon directories that exist
    existing_addon_ids = {
        addon_dir.name for addon_dir in anki_addons_path.iterdir() if addon_dir.is_dir()
    }

    # Get all synced config files from media folder
    synced_config_files = [f for f in media_path.glob("_*_meta.json")]
    # Remove leading _ and trailing _meta.json
    synced_addon_ids = [f.name[1:-10] for f in synced_config_files]

    loaded_addons = []
    disabled_addons = []
    missing_addons = []

    for addon_id in synced_addon_ids:
        if addon_id in existing_addon_ids:
            meta_json = media_path / f"_{addon_id}_meta.json"
            dest_file = anki_addons_path / addon_id / "meta.json"
            shutil.copy(meta_json, dest_file)
            loaded_addons.append(addon_id)

            # Check if addon will be disabled
            try:
                with open(meta_json, "r", encoding="utf-8") as f:
                    meta_data = json.load(f)
                    if meta_data.get("disabled", False):
                        disabled_addons.append(addon_id)
            except Exception:
                pass  # If we can't read the file, just skip the disabled check
        else:
            missing_addons.append(addon_id)

    # Prepare feedback message
    message = "<b>Read Configs Complete!</b><br><br>"
    message += get_read_configs_message(loaded_addons, disabled_addons, missing_addons)
    showInfo(message, title="Addon Config Sync", textFormat="rich")


def get_read_configs_message(
    loaded_addons: list[str], disabled_addons: list[str], missing_addons: list[str]
) -> str:
    """Get feedback message after reading configs"""
    message = ""
    if loaded_addons:
        message += f"<b>✓ Loaded {len(loaded_addons)} addon config(s):</b><br>"
        for addon_id in loaded_addons[:10]:  # Show first 10
            status = " <i>(will be disabled)</i>" if addon_id in disabled_addons else ""
            message += f"&nbsp;&nbsp;• {addon_id}{status}<br>"
        if len(loaded_addons) > 10:
            message += f"&nbsp;&nbsp;• ... and {len(loaded_addons) - 10} more<br>"
        message += "<br>"

        if disabled_addons:
            message += (
                f"<i>Note: {len(disabled_addons)} addon(s) are marked as disabled in the synced"
                " config.</i><br><br>"
            )

    if missing_addons:
        message += (
            f"<b>⚠ Found {len(missing_addons)} addon config(s) but addon(s) not installed:</b><br>"
        )
        message += "<i>Install these addons first, then run Read Configs again.</i><br><br>"
        message += "<b>Addon codes to install:</b><br>"
        for addon_id in missing_addons:
            message += f"&nbsp;&nbsp;• <b>{addon_id}</b><br>"
        message += "<br>"
        message += (
            "<i>To install: Go to Tools → Add-ons → Get Add-ons...<br>and enter each code"
            " above.</i><br><br>"
        )

    if loaded_addons:
        message += (
            "<i>Some addons may require a restart of Anki to apply the loaded configs and"
            " enable/disable states.</i>"
        )

    return message


def show_missing_addons():
    """Show a list of addon codes that have synced configs but are not installed"""
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), "collection.media")

    # Get all addon directories that exist
    existing_addon_ids = {
        addon_dir.name for addon_dir in anki_addons_path.iterdir() if addon_dir.is_dir()
    }

    # Get all synced config files from media folder
    synced_config_files = [f for f in media_path.glob("_*_meta.json")]
    # Remove leading _ and trailing _meta.json
    synced_addon_ids = [f.name[1:-10] for f in synced_config_files]

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
        _showMissingAddonsDialog(missing_addons)


def _showMissingAddonsDialog(missing_addons):
    """Show a custom dialog for missing addons with copy to clipboard functionality"""
    dialog = QDialog(mw)
    dialog.setWindowTitle("Missing Addons")
    dialog.setMinimumWidth(600)

    # Limit dialog height to 80% of screen height
    screen = dialog.screen()
    if screen:
        screen_height = screen.availableGeometry().height()
        max_height = int(screen_height * 0.8)
        dialog.setMaximumHeight(max_height)

    # Internal state - list of addons currently displayed
    addon_list = missing_addons.copy()

    # Main layout for the dialog
    main_layout = QVBoxLayout()

    # Header message (not scrollable)
    header = QLabel()
    header.setWordWrap(True)
    main_layout.addWidget(header)

    # Create a frame to contain the addon list
    list_frame = QFrame()
    list_frame.setFrameShape(QFrame.Shape.StyledPanel)
    list_frame.setFrameShadow(QFrame.Shadow.Sunken)
    frame_layout = QVBoxLayout(list_frame)
    frame_layout.setContentsMargins(0, 0, 0, 0)

    # Create scroll area for the addon list
    scroll_area = QScrollArea()
    scroll_area.setWidgetResizable(True)
    scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

    # Widget to contain the scrollable content
    scroll_content = QWidget()
    scroll_layout = QVBoxLayout(scroll_content)

    # List of addon codes with delete buttons
    codes_label = QLabel("<br><b>Addon codes:</b>")
    scroll_layout.addWidget(codes_label)

    # Create a container for the list of addons with delete buttons
    addons_container_widget = QVBoxLayout()
    scroll_layout.addLayout(addons_container_widget)

    scroll_layout.addStretch()
    scroll_area.setWidget(scroll_content)
    frame_layout.addWidget(scroll_area)

    main_layout.addWidget(list_frame)

    # Delete all button right below the addon list
    delete_all_button = QPushButton(f"🗑️ Delete All {len(addon_list)} Configs")
    main_layout.addWidget(delete_all_button)

    # Bottom section (not scrollable)
    # Space-separated list for easy copying
    copy_label = QLabel("<br><b>Copy all codes (space-separated):</b>")
    main_layout.addWidget(copy_label)

    # Text box with space-separated codes
    text_box = QTextEdit()
    text_box.setReadOnly(True)
    text_box.setMaximumHeight(60)
    main_layout.addWidget(text_box)

    # Instructions
    instructions = QLabel(
        "<br><i>Tip: You can paste all codes at once in the Anki add-on installer!</i><br>"
        "<i>Go to: Tools → Add-ons → Get Add-ons... and paste the codes above.</i>"
    )
    instructions.setWordWrap(True)
    main_layout.addWidget(instructions)

    # Buttons
    button_layout = QHBoxLayout()

    copy_button = QPushButton("📋 Copy to Clipboard")
    button_layout.addWidget(copy_button)

    close_button = QPushButton("Close")
    close_button.clicked.connect(dialog.accept)
    button_layout.addWidget(close_button)

    main_layout.addLayout(button_layout)

    dialog.setLayout(main_layout)

    def update_ui():
        """Update the dialog UI to reflect the current addon_list"""
        # Update header
        header.setText(
            f"<b>Found {len(addon_list)} configs with no addon installed for them:</b><br>"
            "<i>You can install missing addons or delete unnecessary files.</i>"
        )

        # Clear existing addon rows
        while addons_container_widget.count():
            child = addons_container_widget.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                while child.layout().count():
                    subchild = child.layout().takeAt(0)
                    if subchild.widget():
                        subchild.widget().deleteLater()

        # Recreate addon rows
        for addon_id in addon_list:
            addon_row = QHBoxLayout()

            addon_label = QLabel(f"&nbsp;&nbsp;• <b>{addon_id}</b>")
            addon_row.addWidget(addon_label)
            addon_row.addStretch()

            delete_btn = QPushButton("🗑️ Delete file")
            delete_btn.setMaximumWidth(140)
            delete_btn.clicked.connect(lambda checked, aid=addon_id: on_delete_single(aid))
            addon_row.addWidget(delete_btn)

            addons_container_widget.addLayout(addon_row)

        # Update text box
        space_separated = " ".join(addon_list)
        text_box.setPlainText(space_separated)

        # Update copy button
        try:
            copy_button.clicked.disconnect()
        except Exception:
            pass
        copy_button.clicked.connect(lambda: _copyToClipboard(space_separated))

        # Update delete all button
        try:
            delete_all_button.clicked.disconnect()
        except Exception:
            pass
        delete_all_button.clicked.connect(on_delete_all)
        delete_all_button.setEnabled(len(addon_list) > 0)

        # Close dialog if no addons left
        if len(addon_list) == 0:
            tooltip("All configs for missing addons deleted! ✓", period=2000)
            dialog.accept()

    def on_delete_single(addon_id):
        """Handle deletion of a single addon config"""
        if _deleteSyncedConfig(addon_id):
            addon_list.remove(addon_id)
            update_ui()

    def on_delete_all():
        """Handle deletion of all configs for missing addons"""
        reply = QMessageBox.question(
            dialog,
            "Confirm Delete All",
            "Are you sure you want to delete synced configs for all"
            f" {len(addon_list)} missing addon(s)?<br><br><i>This action cannot be undone.</i>",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            deleted_count = 0
            failed_addons = []

            for addon_id in addon_list[:]:  # Copy list to avoid modification during iteration
                if _deleteSyncedConfig(addon_id):
                    deleted_count += 1
                else:
                    failed_addons.append(addon_id)

            # Update addon_list to only contain failed ones
            addon_list.clear()
            addon_list.extend(failed_addons)

            message = f"<b>Deleted {deleted_count} synced config(s)</b>"
            if failed_addons:
                message += f"<br><i>Failed to delete {len(failed_addons)} config(s)</i>"

            showInfo(message, title="Delete Complete", textFormat="rich")
            update_ui()

    # Initial UI setup
    update_ui()

    dialog.exec()


def _copyToClipboard(text):
    """Copy text to clipboard and show confirmation"""
    clipboard = QApplication.clipboard()
    clipboard.setText(text)
    tooltip("Copied to clipboard! ✓", period=2000)


def _deleteSyncedConfig(addon_id):
    """Delete the synced config file for a specific addon

    Returns:
        bool: True if deletion was successful, False otherwise
    """
    media_path = Path(mw.pm.profileFolder(), "collection.media")
    config_file = media_path / f"_{addon_id}_meta.json"

    if config_file.exists():
        try:
            config_file.unlink()
            tooltip(f"Deleted config for {addon_id} ✓", period=2000)
            return True
        except Exception as e:
            showInfo(
                f"Error deleting config for {addon_id}: {str(e)}", title="Error", textFormat="rich"
            )
            return False
    else:
        tooltip(f"Config file for {addon_id} not found", period=2000)
        return False
