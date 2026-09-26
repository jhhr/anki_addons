# Third-party packages in an addon

An addon runs inside Anki's Python, where the user cannot `pip install`. A dependency
therefore ships inside the addon, under `<addon>/lib/`. Only `japanese_note_ai_ops` does this
today. The build half is in `build.py` (`vendor`), the runtime half in
`anki_shared/utils/vendor_path.py`, `vendor_rebuild.py` and `vendor_rebuild_ui.py`. Both
halves are commented at length with the failures that shaped them; read those comments
before changing either.

Prefer not adding a dependency. The stdlib, `anki` and `aqt` cost nothing to ship.

## The files

| file | committed | role |
| --- | --- | --- |
| `<addon>/requirements.in` | yes | the direct dependencies, unpinned. Edit this one. |
| `<addon>/requirements.txt` | yes | compiled from `.in` by `build.py vendor`; fully pinned, with `python_full_version` markers. **Never edit by hand.** It ships, because the runtime rebuild reads it. |
| `<addon>/lib/` | no | the vendored tree for all five platforms |
| `<addon>/lib/.vendored.json` | no | manifest of what the last run wrote and the Python it was built for |
| `<addon>/user_files/lib/` | no | a rebuild made on the user's machine |
| `build/` | no | scratch: one resolution per platform |

## Adding or upgrading a dependency

1. Edit `requirements.in`.
2. `python build.py vendor <addon>`. Needs `uv` on PATH, or the one bundled with Anki.
3. Commit `requirements.in` and the recompiled `requirements.txt` together.
4. Import it in the addon only **after** `add_vendor_paths(ADDON_DIR)` has run, and inside
   the guarded import block if the addon has one.

`vendor` resolves once per platform tag (`win_amd64`, `macos_x86_64`, `macos_arm64`,
`linux_x86_64`, `linux_aarch64`) for the Python Anki ships (`VENDOR_PYTHON_VERSION`).
Packages that are byte-identical on every platform land flat in `lib/`; any package whose
contents differ goes whole into `lib/_platform/<tag>/`. The comparison is by bytes, not by
filename, because macOS and abi3 extension names collide across platforms.

The pins are compiled against `VENDOR_PYTHON_FLOOR` (3.9), not the build version, so that a
user's older Python can still resolve them at rebuild time. The platform tags in `build.py`
must stay in step with `vendor_path.py`, which picks one at runtime.

`build.json` knobs: `vendor_keep` protects hand-placed `lib/` entries that no requirement
names (`mdict_query`); `vendor_no_binaries` ships a package pure-Python (`rapidfuzz`: about
6 MB of extensions per platform, with a complete Python fallback).

## At runtime

`add_vendor_paths` puts `user_files/lib`, then `lib/_platform/<tag>`, then `lib` on
`sys.path`. `vendor_health` compares the manifest's Python version and a digest of
`requirements.txt` with what is running. When the tree does not fit, or a package is missing,
`install_rebuild_ui` offers to rebuild into `user_files/lib` using Anki's bundled `uv`, or
`pip` where there is none. It asks first, remembers refusals, and adds a Tools action.

The shared layer knows only that a package is absent. Whether that costs speed or a whole
feature is the addon's knowledge, so the addon passes the name of the package it failed to
import (`missing=`). Keep that division: no addon-specific package names in `anki_shared`.

A missing package must cost the addon an operation, not its import. Catch `ImportError`
only, record it, and skip registering the hooks that need it; never swallow it silently.
