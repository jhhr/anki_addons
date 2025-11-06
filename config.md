## Addon options

- `run_on_sync`: Configs are saved and read during syncing. This can overwrite configs one way of the other. Defaults to `true`. Set to `false` if you only want read and save configs manually.
- `show_summary_on_sync`: When `run_on_sync` is enabled, also show a message once it finishes like what is shown when you save/read configs using the menu actions.

### Conflicting change handling during sync

In a nutshell, **the first device to sync to AnkiWeb** is the one whose addon config edits will overwrite conflicting configs when syncing on other devices.
It appears that changes to a media file in AnkiWeb trumps not yet uploaded changes to the same media file which is why the first to sync wins.

In more detail:

1. Edit configs on device A without syncing.
2. Edit configs on device B without syncing.

Device A and B now have conflicting changes.

3. Sync on device A. Edited configs files are uploaded to AnkiWeb
4. Sync on device B. Config files are downloaded from Ankiweb, overwriting conflicting edits on device B
