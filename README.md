# anki_addons

A monorepo of my Anki addons, so that a change to shared code is atomic with its
call sites and every device needs only a single `git pull`.

This repo lives **inside** `addons21` but is not itself an addon: it has no root
`__init__.py`, so `AddonManager.allAddons()` skips it entirely. Each addon is one
directory here, junctioned into `addons21` under the folder name Anki already
knows it by.

## Layout

    anki_addons/
      build.py            dev-link and release-package tool
      anki_shared/        shared packages; not an addon
      <addon>/            build.json, __init__.py, ...
        shared/           generated, gitignored: links into anki_shared/

Imports into shared code are relative into the vendored `shared/`, identical in
development and in a released zip:

```python
from ..shared.interpolate.interpolate_fields import interpolate_from_text
```

## Setup on a new device

Clone into `addons21` with submodules, then, with Anki closed:

```
python build.py install
```

Each addon is linked into `addons21` under its `package` name from `build.json`. If this
device's Anki already knows an addon under another folder name (so its `meta.json` config
lives there), say so first in a `build.local.json` next to `build.py`. The file is
gitignored and both keys are optional:

```json
{
  "addons_dir": "D:/Anki2/addons21",
  "dev_dir_names": { "copy_anywhere": "my_folder_name" }
}
```

## Commands

| command | what it does |
| --- | --- |
| `python build.py install [addon...]` | per-device setup: `shared/` links plus a junction per addon in `addons21` |
| `python build.py link [addon...]` | just the `shared/` links |
| `python build.py check` | fail if an addon imports a shared package it did not declare |
| `python build.py dist [addon...]` | write `dist/<addon>-<version>.ankiaddon` |

An addon's `README.md` is for whoever works on it. When the addon also has an
`ADDON_README.md`, that is the user guide, and `dist` ships it in place of `README.md`.

## Tests

One command runs every suite, from the repo root:

```
python -m pytest
```

It needs the dev dependencies in the interpreter the tests run on, which should also be the
one mypy and your editor use. Two steps, because `pytest-anki2` has to go in without its
declared dependencies:

```
python -m pip install -r requirements-dev.txt
python -m pip install --no-deps -r requirements-dev-nodeps.txt
```

No `build.py link` is needed first; the root `conftest.py` stands in for a missing
`<addon>/shared/`. How the suites work, and how an addon adds tests of its own, is in
[`anki_shared/testing/README.md`](anki_shared/testing/README.md).

## Hazards

- **Never use Anki's addon-manager "Delete" on a junctioned addon** - it sends the
  junction target (your working tree) to the trash. Disabling is safe.
- A root `__init__.py` here would make Anki load the whole monorepo as one addon.
  `build.py` hard-fails if one appears.
