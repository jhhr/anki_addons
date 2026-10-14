# addon_config_sync (Addon Config Sync)

Read the root [AGENTS.md](../AGENTS.md) first.

Syncs every addon's `meta.json` between devices by keeping a copy in the media folder as
`collection.media/_<addon_id>_meta.json` and letting AnkiWeb's media sync carry it. The UI
is one dialog: Tools → Manage Addon Configs. User-facing behaviour, the three sync modes and
the conflict rule ("first device to sync wins") are in `README.md` and `config.md`.

## Map

| path | role |
| --- | --- |
| `__init__.py` | Tools action; `sync_will_start` → `sync_on_save`, `media_sync_did_start_or_stop` → `read_on_sync`. Hooks are always registered and read config when they fire, so a config edit needs no restart |
| `sync_actions.py` | the file operations: `save_addon_to_media`, `overwrite_addon_from_media`, `remove_addon_from_media`, `save_configs_on_sync`, `read_configs_on_sync`, and `is_change_made_here`, which says what a media sync stopping leaves alone; module state `UPDATED_STATE`, `OVERWRITTEN_THIS_SESSION`, `SUPPRESS_AUTO_SYNC_ACTIONS`, `SUPPRESS_SYNC_FINISH_CALLBACKS` |
| `matched_copies.py` | per profile folder, the sha256 of each media copy as of the last time it and its add-on's `meta.json` were the same; state in `user_files/matched_copies.json`, never synced. Stdlib only |
| `config_manager_dialog.py` | the manager dialog (about 850 lines): rows, status, diff view, filters, sorting, bulk actions, "Sync media only now" |
| `utils.py` | `meta.json` readers, `json_files_deep_equal`, `show_non_blocking_info`, `standard_icon`, this addon's own config access (`get_main_config`, `write_main_config`, ignore flags) |

## Invariants

- **This addon overwrites other addons' `meta.json`.** A bug here destroys configuration
  that exists nowhere else, including copy_anywhere's definitions. Anything that changes
  when or what `overwrite_addon_from_media` / `read_configs_on_sync` copies needs a second
  look and an explicit note to the user. The addon is published and its sync logic is meant
  to be stable: a change there is a bug fix, or it changes nothing a user could have set
  their devices up around.
- **`read_configs_on_sync` leaves alone a config that only this device changed.** It runs
  after every media sync, and only a collection sync starting sends configs out, so in
  between a config can differ from its media copy because another device's copy came down,
  or because the config changed here and has not gone out. The second used to be copied
  over like the first, and the change was lost. Such a difference is left alone when all
  four of these hold; any other is copied over, as all of them used to be, unless the copy
  is unfit to read (the next point).
  - **Nothing was downloaded over the media copy.** It is byte for byte what it was when
    the two files last matched (`matched_copies.py`). With no match held it reads as
    changed: a first sync, a state file that is missing or malformed, another profile's
    media folder, an add-on that was ignored in between.
  - **No media copy was copied over this add-on's config since Anki started**
    (`OVERWRITTEN_THIS_SESSION`). An add-on reads its config when it loads, and many keep
    it and later write all of it back. Written back after a download replaced the file,
    that would undo on both devices what the other one had just sent.
  - **The `meta.json` is a JSON object, and has a `config` if the copy has one.** A file
    that is missing or cut short is nothing to keep. One without a `config` is a new
    install of the add-on, or a file Anki rebuilt from nothing after failing to read one,
    and has always been given the copy's config.
  - **The two differ in more than what Anki records about the installed package**
    (`INSTALL_KEYS`). An update of an add-on changes only that. Kept, the new `mod` would
    go out with the next sync every time and mark the add-on as updated on devices that
    have not updated it, which Anki then stops offering the update to.

  A first version went by the first of the four alone. Run beside the old code over the
  same histories, it left a second device worse off than before in each case the other
  three now exclude. Before widening what is left alone, do that again
  (`test/old_against_new.py`) and look at what the *other* device ends up with.
  The conflict rule is untouched by it: a downloaded copy wins over a change not sent yet.
- **A sync replaces no config with a broken file or with one without a config**
  (`fit_to_send`, `fit_to_read`). A sync start holds back a `meta.json` that is no JSON
  object, or has no `config` where the copy has one (a file cut short by a crash, a new
  install of the add-on, a file Anki rebuilt from nothing after failing to read one), lists
  it as skipped and leaves the copy alone. Outside ask mode the media sync's end then reads
  the copy over it, the whole file: a new install gets the config set up on another
  device, and also that device's `disabled`, `update_enabled` and version, so Anki may
  offer the version just installed as an update. In ask mode it stays listed at every sync
  until the user settles it in the dialog. A media sync stopping never copies a copy that
  is no JSON object (a valid one that is not an object, such as `[]`, stops Anki loading
  add-ons), nor one without a `config` over a `meta.json` that has one; the next sync start
  sends that `meta.json` in its place. Older versions send new installs' and broken files,
  and any device sends a file without a config where the copy had none. So a copy without
  a config never wins over a config, whichever device synced first, which is the one
  change to the conflict rule. The dialog's two buttons refuse only a file that is no JSON
  object: they are the user's decision, and in ask mode the only way to settle a held-back
  file.
  - **Not covered: an add-on that writes its config when Anki loads it**, as copy_anywhere
    does on every start. Loaded with a new install's file, or after Anki has read a broken
    one as empty, it writes its defaults as a config before any sync starts, and that file
    is sent like any change. A new install is covered when Anki is closed with its sync
    after installing (it asks to restart), not when the add-on was copied in with Anki
    closed, auto-sync on open and close is off, or Anki quit before its sync finished.
  - **What it costs, accepted by the author:** deleting a `config` by hand, or installing
    the add-on again, no longer resets it on every device; setting it back to the defaults
    in its config dialog does.
  - A device on an older version in ask mode never reads what this one sends in place of
    its file, and sends the file again at its every sync start, until its user settles it.
  Everything else a sync start sends, it sends as before, an update's `mod` included.
- **Ask mode lists every difference, including this device's own.** It copies nothing by
  itself, so it has nothing to lose; the rule above applies only where `apply_to_addons` is
  true.
- **It imports on Python 3.9**, which Anki ran on until 26.05 (2.1.50 made it the minimum;
  25.07 began shipping 3.13 without raising that). Signatures here use `X | None`, a
  TypeError at import before 3.10 unless the module has `from __future__ import
  annotations`; `anki_shared/test/test_python39.py` checks every module of every addon for
  that and the rest of the root `AGENTS.md`'s 3.9 rule.
- **The handlers on Anki's sync hooks must not raise.** Anki takes a handler that raises
  off its hook and lets the exception through, which stops the sync it was called from.
  `matched_copies` swallows what reading and writing its file can raise for that reason;
  what it then does not hold only means copying as before.
- To make Anki upload a changed media file, `save_configs_on_sync` **removes the old file and
  copies the new one**; overwriting in place is not enough. It runs before media sync
  starts so that the upload happens in the same sync.
- A difference is only real if the files differ byte-wise **and** as JSON with keys sorted
  (`json_files_deep_equal`). Anki rewrites `meta.json` with different key order; without
  the second check every sync would report changes.
- `media_sync_did_start_or_stop` fires several times. `read_configs_on_sync` returns early
  while `media_sync_status` is `True` and calls `on_finish_callback` exactly once when it
  actually reads; the summary dialog depends on that.
- `SUPPRESS_AUTO_SYNC_ACTIONS` is set by the dialog's "Sync media only now" so the automatic
  handlers stand down; the queued `SUPPRESS_SYNC_FINISH_CALLBACKS` run once when media sync
  stops, then the list is cleared.
- Only one sync mode is meant to be active at a time (`run_on_sync`, `show_summary_on_sync`,
  `ask_on_sync`; combinations in `config.md`). The dialog's radio buttons enforce it; the
  config file does not.
- The media filename pattern `_<addon_id>_meta.json` is parsed positionally in
  `get_configs_in_media` (`f.name[1:-10]`). Files already in users' media folders and on
  AnkiWeb use it; changing it strands them.
- Addon ids are `addons21` folder names, so configs only meet across devices that use the
  same folder name for an addon. When this repo is cloned into `addons21`, the repo folder
  itself shows up there as a directory without a `meta.json` and lands in the skipped list.

## Tests

Both directories are in the root `testpaths`.

- `test/` runs `save_configs_on_sync`, `read_configs_on_sync` and the dialog's three file
  functions over folders made up per test, behind the stub `mw.pm`. Its conftest's `Device`
  is an add-ons folder with a profile's media folder and an Anki session, and its `Server`
  the other end of a media sync by Anki's rule (the server's change wins over one not
  sent), for the tests with two devices. `test_addon_config_sync_actions.py` only looks at
  files. Its first two parts are what a sync start and a media sync stopping do with an
  ordinary difference, as they always have; the sync start's tests of holding back are the
  exception. Its third part is a config only this device changed, and its fourth a broken
  file or one without a config. `test_addon_config_sync_matches.py` is about the state file.
- `test_anki/` runs the addon's own `__init__.py`, which nothing else imports, in a running
  Anki, and has Anki's add-on manager write the `meta.json` files `is_change_made_here` is
  written against: an install, an update, an add-on switched off and on.
- `test/old_against_new.py` is not collected with the suite; run it by its path, with `-s`,
  when what a sync start sends or a media sync reads is to change. It runs the handlers
  from before matches were kept beside today's over random histories of two devices. It
  fails on a sync start that sends something else, an ask-mode read that lists something
  else, a copy the old code would not have made, a config left alone that was not this
  device's own change, and a broken file or one without a config sent or read over a
  config. Its docstring says how to make it longer and how to see each check fail.

The state file is beside the addon's code, so in a checkout that Anki is installed from it
is that Anki's. Both conftests point it into the test's directory; a new test directory
must do the same before anything can call a handler.

The dialog has no tests, and neither have the three sync modes' branches in `__init__.py`
beyond the default one.

## Shared code

Declares none. `utils.show_non_blocking_info` and `standard_icon` are generic Qt helpers
with one owner, and this addon has its own variant of the config boilerplate and of
`build_action`. If another addon needs one of these, move it to `anki_shared/` per
[docs/shared-code.md](../docs/shared-code.md) and declare the package in `build.json`
instead of copying.
