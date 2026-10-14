"""The monorepo's addons as importable packages, without Anki running their `__init__.py`.

An addon's root `__init__.py` calls `mw.addonManager` at import and builds menus, which a test
or a script does not want even when `mw` answers. Registered as a module whose `__path__` is
the addon's directory, the addon's submodules import, and the relative imports inside them
resolve, without that file ever running. `anki_shared` is registered the same way because it
deliberately has no `__init__.py`.

An addon reaches shared code as `from ..shared.<pkg> import ...`, and `<addon>/shared/` exists
only once `python build.py link` has made it (it is gitignored). Where it is missing,
`<addon>.shared` is registered over `anki_shared/` itself, so a fresh clone runs before any
build step; that view is wider than the linked one, and `build.py check` is what catches an
undeclared import.

The root conftest loads this file by its path, so that nothing under `anki_shared` is imported
before it is registered, and registers every addon for the test suites; `japanese_note_ai_ops/dev/headless`
registers the two its scripts run, keeping what a conftest already registered when a test
imports it. Stdlib only: this is imported before anything else can be.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


def register_package(name: str, path: PathLike, keep_existing: bool = False) -> types.ModuleType:
    """`name` as a package rooted at `path`, its `__init__.py` never run. With
    `keep_existing`, a module already registered under the name is left as it is."""
    existing = sys.modules.get(name)
    if keep_existing and existing is not None:
        return existing
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    module.__package__ = name
    sys.modules[name] = module
    return module


def register_addon(root: PathLike, name: str, keep_existing: bool = False) -> types.ModuleType:
    """The addon directory `root/name` as package `name`, with `name.shared` over
    `root/anki_shared` where `python build.py link` has made no `shared/` yet."""
    directory = Path(root) / name
    addon = register_package(name, directory, keep_existing)
    if not (directory / "shared").is_dir() and not hasattr(addon, "shared"):
        # setattr: the parent is a plain ModuleType, so the submodule is an attribute only the
        # import system would otherwise add
        shared = register_package(f"{name}.shared", Path(root) / "anki_shared", keep_existing)
        setattr(addon, "shared", shared)
    return addon


def addon_names(root: PathLike) -> list[str]:
    """Every directory of `root` holding a build.json: the monorepo's addons."""
    return sorted(entry.name for entry in Path(root).iterdir() if (entry / "build.json").is_file())
