# How the monorepo fits together

The mechanics behind the rules in the root [AGENTS.md](../AGENTS.md). `build.py` is the
source of truth and is heavily commented; this is the map.

## Three views of the same code

Anki loads an addon from `addons21/<folder>/`, where `<folder>` is the name Anki and the
user's `meta.json` already know. Addon code reaches shared code through a `shared/`
subpackage of its own. The same relative import has to work in all three places the code
runs:

| where | `<addon>/shared/<pkg>` is | made by |
| --- | --- | --- |
| development, inside Anki | a directory junction (symlink on POSIX) into `anki_shared/<pkg>` | `python build.py link` |
| a released `.ankiaddon` | a real copy of `anki_shared/<pkg>` | `python build.py dist` |
| pytest | the junction if present, otherwise a stub package whose `__path__` is `anki_shared/` | root `conftest.py` |

Hence the one import form, with the dot count set by how deep the importing module sits:

```python
from .shared.anki.write_custom_data import write_custom_data          # <addon>/x.py
from ..shared.interpolate.interpolate_fields import intr_format       # <addon>/logic/x.py
```

The addon's entry in `addons21` is itself a junction to `<addon>/`, made once per device by
`python build.py install`. Its name is **per device**: it has to be the folder that
device's Anki already keeps the addon's `meta.json` under, which need not match the repo
directory name. It defaults to the addon's `package` (the folder a released zip creates) and
is overridden in the gitignored `build.local.json` at the repo root:

```json
{
  "addons_dir": "D:/Anki2/addons21",
  "dev_dir_names": { "copy_anywhere": "my_folder_name" }
}
```

Both keys are optional, the file may be absent, and a key naming no addon directory is an
error. No code may depend on the install folder name: addons find themselves with
`mw.addonManager.addonFromModule(__name__)`. Anki loads addons in folder-name order, so load
order between two addons is a property of an install, not of this repo.

Consequences worth knowing:

- A file under `<addon>/shared/` and the file under `anki_shared/` are the same file. Tools
  that walk the tree see it several times; `mypy.ini` and `build.py`
  both exclude `*/shared/` for that reason. Exclude it from your own searches too.
- The pytest view is wider than the linked one: every shared package resolves whether or not
  `build.json` declares it. A test can pass while the addon fails to import inside Anki.
  `python build.py check` is the guard.
- The Windows junction needs no admin rights or Developer Mode, which is why links are
  generated per device instead of being committed as symlinks.

## build.json

```json
{
  "package": "my_addon",                // manifest package id; the folder a zip installs to
  "name": "My Addon",
  "human_version": "1.0.0",
  "homepage": "https://github.com/jhhr/anki_addons",
  "shared": ["interpolate", "ui"],      // anki_shared packages to link and ship
  "exclude": ["word_array/research"],   // addon-relative paths kept out of the zip
  "vendor_keep": ["mdict_query"],       // hand-placed lib/ entries vendoring must not delete
  "vendor_no_binaries": ["rapidfuzz"]   // vendor these pure-Python
}
```

A directory is an addon if and only if it holds a `build.json`; `build.py` and `conftest.py`
both discover addons that way. `manifest.json` is generated into the zip from these keys and
is never a file in the tree.

## What `check` does and does not catch

`check` greps each addon for `from .shared.<pkg>` and compares against `shared`. Undeclared
is a failure, unused is a warning. Shared packages that a declared package imports as a
sibling (`from ..<sibling>`) count as optionally used, so declaring them is not flagged.

It does **not** verify that a declared package's own sibling imports are satisfied. Today:

- `interpolate/execute_code.py` imports `jp_text_processing` and `word_array` inside
  `try/except ImportError`; without them, code mode just lacks those names.
- Four `ui` modules (`interpolated_text_edit`, `code_edit_layout`, `add_model_options_to_dict`,
  `add_intersecting_model_field_options_to_dict`) import `..interpolate` at module top,
  unguarded. An addon that uses them must declare `interpolate` too. Both current users do.

## What goes into a release zip

`dist` runs `check`, then zips the addon's own files plus real copies of its declared shared
packages. Left out everywhere: `test/`, `tests/`, `test_anki/`, `*_tests.py`, `user_files/`,
`logs/`, `output/`, `dist/`, caches, `meta.json`, `build.json`, `requirements.in`,
`AGENTS.md`, `CLAUDE.md`, VCS and editor files, plus the addon's own `exclude` list.
`requirements.txt` and `lib/` do ship. Name a directory `test` or `test_anki` and it stays
out; name it anything else and it ships unless `exclude` lists it.

## Tests without Anki

An addon module cannot be imported unless `anki` and `aqt` resolve and `aqt.mw` answers at
import time. The root `conftest.py`:

1. registers `anki_shared` and every addon as stub packages whose `__path__` is the real
   directory, so submodules import and relative imports resolve while the addon's root
   `__init__.py` (which builds menus and registers hooks) never runs;
2. supplies `<addon>.shared` over `anki_shared/` when the junction is missing;
3. installs the real `anki` with a stubbed `mw` (`anki_shared/testing/real_anki`), falling
   back to an all-stub Anki (`anki_stubs`) when `anki`/`aqt`/PyQt6 are not installed;
4. registers the plugin that lets running-Anki tests (`<addon>/test_anki/`, pytest-anki2)
   share the process.

Details, fixtures and the three things that bite are in
[anki_shared/testing/README.md](../anki_shared/testing/README.md).

`japanese_note_ai_ops/test` is the exception: it has its own `pytest.ini`, uses
`--import-mode=importlib`, and is run from that addon's root. It is not in the root
`testpaths`. See that addon's `AGENTS.md`.

## Type checking

`python -m mypy .` from the root, on the interpreter that has `anki` and `aqt` installed.
`mypy.ini` explains each of its own exclusions; the ones that surprise people:

- `*/shared/` is excluded from the crawl **and** silenced by module name, because the
  junctions are also reached through imports.
- `anki_shared/jp_text_processing` is excluded and silenced; it has its own mypy run. Its
  types still flow into importers.
- Three directories are on `mypy_path` because their scripts import each other by bare name
  at runtime. Two `conftest.py` files and `research/migrate.py` are excluded for what that
  does to their module names, not for their content.

`pyrightconfig.json` and the root `.vscode/` are gitignored: they name one machine's
interpreter. If you create them, exclude `**/shared/**`, `**/lib/**` and `**/dist/**` as
`mypy.ini` does, or every shared file is reported once per addon.
