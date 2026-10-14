# Addon Config Sync

Sync add-on `meta.json` configs across devices by storing copies in Anki's media folder and syncing those through AnkiWeb.

## Open the manager

Main window → Tools → **Manage Addon Configs**

This is the main UI for all operations.

## What the manager shows

- One row per known add-on config (installed add-ons with `meta.json`, and media-only configs for not-installed add-ons).
- Status per row:
  - **Changed**: the add-on's settings in `meta.json` differ from the media copy (click status to view diff + modified times). What Anki records about the installed version does not count; see Notes.
  - **Not installed**: config exists in media but add-on is not installed locally.
  - **Updated**: config was overwritten in this Anki session.
  - **Up to date**: no relevant difference.

## Per-add-on and bulk actions

Each row supports:

- Save to local media (addon → media)
- Overwrite from local media (media → addon)
- Remove from local media
- Install add-on (when not installed)
- Ignore toggle

Top action buttons apply to selected rows (with shift-range select support).

## Filtering and sorting

- Filter by add-on name.
- Filter by status dimensions (Changed / Installed / Updated).
- Filter by Ignore state.
- Header sorting supports Addon, Status, and Ignore columns with ascending/descending/none cycling.

## Sync modes

Set in the manager dialog (radio buttons):

1. **Update configs on Sync** (default)
   - During normal sync, changed local addon configs are saved to media before media sync.
   - Downloaded media config changes are applied automatically to installed addons after media sync.
   - A config you changed on this device stays as you left it until a normal sync has sent it, also through the media syncs Anki runs by itself in between. It used to be replaced by the last synced copy when such a media sync ended. That still happens when a newer copy of that add-on's config was downloaded, and for an add-on whose config a sync has already updated since Anki was started: the add-on may still be working from the older config, so restart Anki before changing it.
   - Ignored addons are skipped.

2. **Update configs on Sync, show summary**
   - Same behavior as mode 1.
   - After sync, opens the manager dialog **only if at least one addon config was updated**.

3. **Ask about changes to configs**
   - Still saves local changes to media on sync.
   - Does **not** auto-overwrite addon configs from downloaded media.
   - Opens the manager dialog non-blocking, prefiltered to changed or not-installed entries.
   - Ignored addons are skipped.

## Sync media only now button

The manager includes **Sync media only now**, which triggers Anki sync for media only. Configs in the media folder will be update but will not yet overwrite addon configs. This refreshes statuses when sync finishes.

## Notes

- What is synced is what you set: an add-on's config, whether it is switched off, and whether Anki should update it. What Anki records in the same file about the installed version (the version, when it was updated, which Anki versions it supports) stays each device's own, so each device's Anki offers updates for what is installed there. Versions up to 1.0.0 synced that too: after an add-on was updated on one device, Anki on another device could stop offering it the update, offer an update it already had, or, between different Anki versions, stop loading the add-on. A device still on 1.0.0 copies the whole file until this add-on is updated there; once a device with this version has synced, Anki on such a device offers it the update.
- Some add-ons may require an Anki restart after config overwrite.
- The first device to upload a conflicting media config generally wins on subsequent device syncs.
- A sync never replaces a config with a broken file or with one that has no config, in either direction. An add-on newly installed on a device is not uploaded at its first sync; it gets the config stored in the media folder instead. A config file Anki cannot read, such as one cut short by a crash, is not uploaded either, and is repaired from the stored copy. The repaired file has no record of the installed version, so Anki shows the add-on under its folder name and offers it as an update until it is installed again. This does not help with add-ons that write their config as soon as Anki loads them; see "New installs and broken files" in the config documentation for when it applies. To reset an add-on's config on all devices, set it back to its defaults in its config dialog and sync: deleting its config by hand or reinstalling it no longer resets it.
- To tell a config changed on this device from one another device sent, the add-on keeps a small file, `user_files/matched_copies.json`, in its own folder. It is not synced and holds no configs, only a checksum per add-on and profile. Without it the add-on treats every difference as a downloaded change, as older versions always did, until the next sync has written it again.

## Links

- [Github](https://github.com/jhhr/anki-addon-config-sync)
- [Anki forums thread](https://forums.ankiweb.net/t/addon-for-syncing-addon-configs/45118)

