This addon reads and writes addon configs to the media folder, allowing you to sync addon configurations across multiple devices.

## Menu Options

Main window > Tools -> Sync Addon Configs

- **Save Configs**: Writes all current addon configs into files in the media folder. Shows a summary of saved configs.
- **Read Configs**: Reads all addon configs from files in the media folder and overwrites current addon configs. Shows which configs were loaded and which addons are missing.
- **Show Missing Addons**: Displays a list of addon codes for synced configs where the addon is not yet installed. Includes a space-separated list and a "Copy to Clipboard" button for easy installation.

## How to use:

### On Device A (Source):
1. Click "Save Configs"
2. Review the summary dialog
3. Sync your collection (to upload configs to AnkiWeb)

### On Device B (Destination):
1. Sync your collection (to download configs from AnkiWeb)
2. Click "Show Missing Addons" to see which addons you need to install
3. Click "Copy to Clipboard" button in the dialog to copy all addon codes at once
4. Install all addons at once (Tools → Add-ons → Get Add-ons... and paste all codes)
5. Click "Read Configs" to load the configurations
6. Restart Anki to apply the loaded configs

## Features

- **Complete Config Sync**: Syncs all addon configurations including:
  - Addon settings and preferences
  - **Enabled/Disabled state** - If an addon is toggled off on Device A, it will be toggled off on Device B after syncing
  - All other metadata stored in `meta.json`

- **Informative Feedback**: All operations now show detailed dialogs with:
  - Number of configs saved/loaded
  - List of addon IDs processed
  - **Which addons are disabled** (shown inline)
  - Warnings for missing addons
  - Clear next steps

- **Missing Addon Detection**: The addon will detect which addons have synced configs but are not installed, and provide their codes for easy installation.

- **One-Click Copy**: Click the "Copy to Clipboard" button to copy all missing addon codes at once. Anki supports pasting multiple addon codes separated by spaces, making batch installation quick and easy.

- **Smart Processing**: Only processes addons with valid `meta.json` files and provides clear feedback about what was skipped.

## Notes

- Configs in device B will match device A only after all the same addons are installed.
- Remember to sync after saving and before reading on the other device.
- **Restart Anki after reading configs** to ensure all settings and enable/disable states take effect.
- The enabled/disabled state of each addon is automatically synced along with other configurations.

## BEWARE

- As you are about to save your recently edited configs for syncing to another device, accidentally clicking Read Configs instead will overwrite your current edits. A proper syncing system should remove this shortfall (TODO soon).
