# untracked_media_syncer (Untracked Media Syncer)

Read the root [AGENTS.md](../AGENTS.md) first. One eight-line file, `__init__.py`; no
config, no tests, no shared packages.

Anki only looks for changed media files when the `collection.media` folder's own mtime has
changed, so a file edited in place (not added or removed) is never uploaded. On
`sync_will_start` the addon touches the folder, which makes Anki rescan it and upload edited
files in that sync.

## Invariants

- `mw.pm.profileFolder()` is read inside the hook, not at import: no profile is loaded at
  import time.
- It touches the **folder**, not the files. Touching files would mark all of them changed.
- It runs before every sync, so it must stay trivial. Anything slower belongs behind a
  condition.
- First device to sync wins when the same file was edited in two places (README).
  `addon_config_sync` relies on the same media-sync behaviour but forces the upload itself
  by removing and re-copying its files; the two addons do not depend on each other.

The README's GitHub link points at the addon's former standalone repository.

## Shared code

Declares none. Before adding a helper here, check
[docs/shared-code.md](../docs/shared-code.md).
