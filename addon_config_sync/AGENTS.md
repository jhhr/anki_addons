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
| `sync_actions.py` | the file operations: `save_addon_to_media`, `overwrite_addon_from_media`, `remove_addon_from_media`, `save_configs_on_sync`, `read_configs_on_sync`; module state `UPDATED_STATE`, `SUPPRESS_AUTO_SYNC_ACTIONS`, `SUPPRESS_SYNC_FINISH_CALLBACKS` |
| `config_manager_dialog.py` | the manager dialog (about 700 lines): rows, status, diff view, filters, sorting, bulk actions, "Sync media only now" |
| `utils.py` | `meta.json` readers, `json_files_deep_equal`, `show_non_blocking_info`, `standard_icon`, this addon's own config access (`get_main_config`, `write_main_config`, ignore flags) |

## Invariants

- **This addon overwrites other addons' `meta.json`.** A bug here destroys configuration
  that exists nowhere else, including copy_anywhere's definitions. Anything that changes
  when or what `overwrite_addon_from_media` / `read_configs_on_sync` copies needs a second
  look and an explicit note to the user.
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

None, and the addon is not in the root `testpaths`. The file logic in `sync_actions.py` is
testable with `tmp_path` and a stubbed `mw.pm` (`anki_shared.testing.real_anki` provides
`.pm`); add such a test when changing it, and add the path to the root `pytest.ini`.

## Shared code

Declares none. `utils.show_non_blocking_info` and `standard_icon` are generic Qt helpers
with one owner, and this addon has its own variant of the config boilerplate and of
`build_action`. If another addon needs one of these, move it to `anki_shared/` per
[docs/shared-code.md](../docs/shared-code.md) and declare the package in `build.json`
instead of copying.
