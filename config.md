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
