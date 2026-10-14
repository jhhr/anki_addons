## Addon options

- `run_on_sync`: Configs are saved and read during syncing. Defaults to `true`.
- `show_summary_on_sync`: When `run_on_sync` is enabled, open the config manager dialog after syncing.
- `ask_on_sync`: Ask mode. When syncing downloads changed configs, open the config manager dialog (non-blocking) pre-filtered to changed/missing addons instead of overwriting addon configs automatically.
- `addon_settings`: Per-addon settings map keyed by addon id. Currently supports `ignore`.

Only one sync mode should be active at a time:

1. `run_on_sync: true`, `show_summary_on_sync: false`, `ask_on_sync: false`
2. `run_on_sync: true`, `show_summary_on_sync: true`, `ask_on_sync: false`
3. `run_on_sync: false`, `show_summary_on_sync: false`, `ask_on_sync: true`

### Conflicting change handling during sync

In a nutshell, **the first device to sync to AnkiWeb** is the one whose addon config edits will overwrite conflicting configs when syncing on other devices.
It appears that changes to a media file in AnkiWeb trumps not yet uploaded changes to the same media file which is why the first to sync wins.

In more detail:

1. Edit configs on device A without syncing.
2. Edit configs on device B without syncing.

Device A and B now have conflicting changes.

3. Sync on device A. Edited configs files are uploaded to AnkiWeb
4. Sync on device B. Config files are downloaded from AnkiWeb, overwriting conflicting edits on device B

### Changes that have not been synced yet

Configs are uploaded when a normal sync starts. Anki also syncs media by itself, by default every 15 minutes, and configs downloaded by such a sync are applied too.

A config changed on this device since the last normal sync is left as it is when a media sync ends, and the next normal sync uploads it. In two cases the copy in the media folder replaces it instead, as it always did:

- A newer copy of that same config was downloaded. That is the rule above.
- A downloaded copy was already applied to that add-on since Anki was started. An add-on usually reads its config when Anki starts and may write the older one back, so restart Anki after a sync has updated an add-on's config and before you change it.

What Anki itself records in the file about the installed add-on, such as its version and when it was updated, is not synced at all: each device keeps its own, so Anki on each device offers the updates that device has not installed. A difference only in that is no change, and the manager's Save and Overwrite buttons leave it as it is on either side.

In ask mode the dialog lists every config whose settings differ from its media copy, including those changed only on this device.

### New installs and broken files

A sync never replaces a config with a broken file or with one that has no config, whichever way the copy would go:

- An add-on newly installed on a device has no config of its own yet. Its file is not uploaded at the next sync; that sync gives it the config stored in the media folder instead, from your other devices or from an earlier install, and also whether the add-on was switched off there. The version recorded stays the one you installed. In ask mode the new install is listed at every sync until you choose in the dialog: Overwrite from local media takes the stored config, Save to local media uploads the new install's file.
- A config file that Anki cannot read, such as one cut short by a crash, is not uploaded, and the stored copy repairs it. The repaired file has no record of the installed version, so Anki shows the add-on under its folder name and offers it as an update until you install it again.
- A stored copy that is broken, or that has no config where yours has one, is not applied. Older versions of this add-on can upload such files. The next sync uploads your config in its place. So a file without a config never wins over a config, whichever device synced first.

This depends on the file still having no config when Anki syncs. Some add-ons write their config as soon as Anki loads them, and then a new install's file, or a broken one Anki read as empty, already holds the add-on's defaults, which are uploaded like any change. A new install is safe when you install it with Anki running and let Anki sync as it closes to restart. It is not when the add-on is copied in while Anki is closed, when syncing on open and close is turned off, or when Anki quits before its sync has finished.

To reset an add-on's config on all your devices, set it back to its defaults in its config dialog and sync. Deleting its config by hand, or reinstalling the add-on, no longer resets it.

The manager's Save and Overwrite buttons copy what you tell them to, a file without a config included, but never a file that is not valid config at all.
