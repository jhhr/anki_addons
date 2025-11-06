This addon reads and writes addon configs to the media folder, allowing you to sync addon configurations across multiple devices.

Note that some addons will require restarting Anki for them to load the new configs.

## Addon options

By default the addon will save configs to the media fodler when you sync Anki. This can be disabled in this addon's config by setting `run_on_sync: false`: Main window > Add-ons > Addon Config Sync > Config (on the right)

### Usage with auto-sync

1. Edit configs on device A
2. Sync Anki on device A
3. Sync Anki on device B

#### Conflicting change handling during auto-sync

In a nutshell, **the first device to sync to AnkiWeb** is the one whose addon config edits will overwrite conflicting configs when syncing on other devices.
It appears that changes to a media file in AnkiWeb trumps not yet uploaded changes to the same media file which is why the first to sync wins.

In more detail:

1. Edit configs on device A without syncing.
2. Edit configs on device B without syncing.

Device A and B now have conflicting changes.

3. Sync Anki on device A. Edited configs files are uploaded to AnkiWeb
4. Sync Anki on device B. Config files are downloaded from Ankiweb, overwriting conflicting edits on device B

## Menu Options

Main window > Tools -> Sync Addon Configs

The menu functions are usable regardless of whether auto-sync is enabled.

- **Save Configs**: Writes all current addon configs into files in the media folder. Shows a summary of saved configs. Note that if you perform the same action on a different device
- **Read Configs**: Reads all addon configs from files in the media folder and overwrites current addon configs. Shows which configs were loaded and which addons are missing.
- **Show Missing Addons**: Displays a list of addon codes for synced configs where the addon is not yet installed. Includes a space-separated list and a "Copy to Clipboard" button for easy installation.

### How to use

#### Basic sync

##### On Device A (Source)

1. Click "Save Configs"
2. Review the summary dialog
3. Sync your collection (to upload configs to AnkiWeb)

##### On Device B (Destination)

1. Sync Anki (to download configs from AnkiWeb)
2. Click "Read Configs"

#### Sync with installing addons

When you need install addons you don't yet have on device B

##### On Device A (Source)

Same as above

##### On Device B (Destination)

1. Sync Anki (to download configs from AnkiWeb)
2. Click "Show Missing Addons" to see which addons you need to install
3. Click "Copy to Clipboard" button in the dialog to copy all addon codes at once
4. Install all addons at once (Tools → Add-ons → Get Add-ons... and paste all codes)
5. Click "Read Configs" to load the configurations
6. (If some addons need it) Restart Anki to apply the loaded configs

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

- **One-Click Copy addon ids**: Click the "Copy to Clipboard" button to copy all missing addon codes at once. Anki supports pasting multiple addon codes separated by spaces, making batch installation quick and easy.

- **Smart Processing**: Only processes addons with valid `meta.json` files and provides clear feedback about what was skipped.

## Notes

- Configs in device B will match device A only after all the same addons are installed.
- Remember to sync after saving and before reading on the other device.
- **Restart Anki after reading configs** to ensure all settings and enable/disable states take effect.
- The enabled/disabled state of each addon is automatically synced along with other configurations.

## BEWARE

- As you are about to save your recently edited configs for syncing to another device, accidentally clicking Read Configs instead will overwrite your current edits. A proper syncing system should remove this shortfall (TODO soon).
