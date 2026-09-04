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

## Commands

| command | what it does |
| --- | --- |
| `python build.py install [addon...]` | per-device setup: `shared/` links plus the `addons21/<dev_dir_name>` junctions |
| `python build.py link [addon...]` | just the `shared/` links |
| `python build.py check` | fail if an addon imports a shared package it did not declare |
| `python build.py dist [addon...]` | write `dist/<addon>-<version>.ankiaddon` |

## Hazards

- **Never use Anki's addon-manager "Delete" on a junctioned addon** - it sends the
  junction target (your working tree) to the trash. Disabling is safe.
- A root `__init__.py` here would make Anki load the whole monorepo as one addon.
  `build.py` hard-fails if one appears.
