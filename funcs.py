import shutil
import json
from pathlib import Path

from aqt import mw
from aqt.qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                    QTextEdit, QMessageBox, QApplication)
from aqt.utils import showInfo, tooltip


def saveConfigs():
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), 'collection.media')

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

            # Check if addon is disabled
            try:
                with open(meta_json, 'r', encoding='utf-8') as f:
                    meta_data = json.load(f)
                    if meta_data.get('disabled', False):
                        disabled_addons.append(addon_dir.name)
            except:
                pass  # If we can't read the file, just skip the disabled check
        else:
            skipped_addons.append(addon_dir.name)

    # Prepare feedback message
    message = f"<b>Save Configs Complete!</b><br><br>"

    if saved_addons:
        message += f"<b>✓ Saved {len(saved_addons)} addon config(s):</b><br>"
        for addon_id in saved_addons[:10]:  # Show first 10
            status = " <i>(disabled)</i>" if addon_id in disabled_addons else ""
            message += f"&nbsp;&nbsp;• {addon_id}{status}<br>"
        if len(saved_addons) > 10:
            message += f"&nbsp;&nbsp;• ... and {len(saved_addons) - 10} more<br>"
        message += "<br>"

        if disabled_addons:
            message += f"<i>Note: {len(disabled_addons)} addon(s) are disabled and will sync as disabled.</i><br><br>"

    if skipped_addons:
        message += f"<b>⊘ Skipped {len(skipped_addons)} addon(s) (no meta.json):</b><br>"
        for addon_id in skipped_addons[:5]:  # Show first 5
            message += f"&nbsp;&nbsp;• {addon_id}<br>"
        if len(skipped_addons) > 5:
            message += f"&nbsp;&nbsp;• ... and {len(skipped_addons) - 5} more<br>"
        message += "<br>"

    message += "<i>Remember to sync to upload configs to AnkiWeb!</i>"

    showInfo(message, title="Addon Config Sync", textFormat="rich")


def readConfigs():
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), 'collection.media')

    # Get all addon directories that exist
    existing_addon_ids = {
        addon_dir.name for addon_dir in anki_addons_path.iterdir() if addon_dir.is_dir()}

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
                with open(meta_json, 'r', encoding='utf-8') as f:
                    meta_data = json.load(f)
                    if meta_data.get('disabled', False):
                        disabled_addons.append(addon_id)
            except:
                pass  # If we can't read the file, just skip the disabled check
        else:
            missing_addons.append(addon_id)

    # Prepare feedback message
    message = f"<b>Read Configs Complete!</b><br><br>"

    if loaded_addons:
        message += f"<b>✓ Loaded {len(loaded_addons)} addon config(s):</b><br>"
        for addon_id in loaded_addons[:10]:  # Show first 10
            status = " <i>(will be disabled)</i>" if addon_id in disabled_addons else ""
            message += f"&nbsp;&nbsp;• {addon_id}{status}<br>"
        if len(loaded_addons) > 10:
            message += f"&nbsp;&nbsp;• ... and {len(loaded_addons) - 10} more<br>"
        message += "<br>"

        if disabled_addons:
            message += f"<i>Note: {len(disabled_addons)} addon(s) are marked as disabled in the synced config.</i><br><br>"

    if missing_addons:
        message += f"<b>⚠ Found {len(missing_addons)} addon config(s) but addon(s) not installed:</b><br>"
        message += "<i>Install these addons first, then run Read Configs again.</i><br><br>"
        message += "<b>Addon codes to install:</b><br>"
        for addon_id in missing_addons:
            message += f"&nbsp;&nbsp;• <b>{addon_id}</b><br>"
        message += "<br>"
        message += "<i>To install: Go to Tools → Add-ons → Get Add-ons...<br>and enter each code above.</i><br><br>"

    if loaded_addons:
        message += "<i>Restart Anki to apply the loaded configs and enable/disable states.</i>"

    showInfo(message, title="Addon Config Sync", textFormat="rich")


def showMissingAddons():
    """Show a list of addon codes that have synced configs but are not installed"""
    anki_addons_path = Path(mw.pm.addonFolder()).resolve(strict=True)
    media_path = Path(mw.pm.profileFolder(), 'collection.media')

    # Get all addon directories that exist
    existing_addon_ids = {
        addon_dir.name for addon_dir in anki_addons_path.iterdir() if addon_dir.is_dir()}

    # Get all synced config files from media folder
    synced_config_files = [f for f in media_path.glob("_*_meta.json")]
    # Remove leading _ and trailing _meta.json
    synced_addon_ids = [f.name[1:-10] for f in synced_config_files]

    missing_addons = [
        addon_id for addon_id in synced_addon_ids if addon_id not in existing_addon_ids]

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
    dialog.setMinimumWidth(500)

    layout = QVBoxLayout()

    # Header message
    header = QLabel(f"<b>Found {len(missing_addons)} addon(s) to install:</b><br>"
                    "<i>These addons have synced configs but are not installed yet.</i>")
    header.setWordWrap(True)
    layout.addWidget(header)

    # List of addon codes
    codes_label = QLabel("<br><b>Addon codes:</b>")
    layout.addWidget(codes_label)

    codes_list = QLabel()
    codes_text = ""
    for addon_id in missing_addons:
        codes_text += f"&nbsp;&nbsp;• <b>{addon_id}</b><br>"
    codes_list.setText(codes_text)
    codes_list.setWordWrap(True)
    layout.addWidget(codes_list)

    # Space-separated list for easy copying
    space_separated = " ".join(missing_addons)

    copy_label = QLabel("<br><b>Copy all codes (space-separated):</b>")
    layout.addWidget(copy_label)

    # Text box with space-separated codes
    text_box = QTextEdit()
    text_box.setPlainText(space_separated)
    text_box.setReadOnly(True)
    text_box.setMaximumHeight(60)
    layout.addWidget(text_box)

    # Instructions
    instructions = QLabel(
        "<br><i>Tip: You can paste all codes at once in the Anki add-on installer!</i><br>"
        "<i>Go to: Tools → Add-ons → Get Add-ons... and paste the codes above.</i>"
    )
    instructions.setWordWrap(True)
    layout.addWidget(instructions)

    # Buttons
    button_layout = QHBoxLayout()

    copy_button = QPushButton("📋 Copy to Clipboard")
    copy_button.clicked.connect(lambda: _copyToClipboard(space_separated))
    button_layout.addWidget(copy_button)

    close_button = QPushButton("Close")
    close_button.clicked.connect(dialog.accept)
    button_layout.addWidget(close_button)

    layout.addLayout(button_layout)

    dialog.setLayout(layout)
    dialog.exec()


def _copyToClipboard(text):
    """Copy text to clipboard and show confirmation"""
    clipboard = QApplication.clipboard()
    clipboard.setText(text)
    tooltip("Copied to clipboard! ✓", period=2000)
