"""Make the monorepo's packages importable from tests, without Anki running.

Four things to arrange, the first two inherited from the per-addon conftest this replaces:

1. An addon module cannot be imported unless `anki` and `aqt` resolve and `aqt.mw` answers
   at import time. There are two ways to arrange that, and `anki_shared/testing/` owns
   both: the real `anki` package with only `mw` stubbed (`real_anki`), or a stand-in Anki
   made entirely of stubs (`anki_stubs`). Real mode is preferred, because tests that store
   a field value or run a search need a real collection behind them, and the stand-in
   `Note` stores nothing. It needs `anki`, `aqt` and PyQt6 installed; without them the
   stand-in is still enough for the pure-logic tests, so the fallback is not a failure.

2. An addon's root `__init__.py` calls `mw.addonManager` at module level and builds menus,
   which is not what a test wants even when `mw` does answer. Registering each addon as a
   stub package whose `__path__` points at the real directory lets tests import its
   submodules -- and lets the relative imports inside them resolve -- without ever
   executing that `__init__.py`.

   `anki_shared` gets the same treatment for a different reason: it deliberately has no
   `__init__.py` (see build.py's repo-root guard on the same principle).

3. Addon code reaches shared code as `from ..shared.<pkg> import ...`, and `<addon>/shared/`
   only exists once `python build.py link` (or `install`) has materialised it: it is
   gitignored. Where it is missing, `<addon>.shared` is registered as a stub package over
   `anki_shared/` itself, so a fresh clone can run its tests before any build step. That
   view is wider than the linked one -- every shared package resolves, not only those
   build.json declares -- and `python build.py check` is what catches an undeclared import.

4. Once a real Anki has run in the process, it crashes on the way out. The shutdown guard
   in `anki_shared/testing/pytest_plugin.py` prevents that, and is registered from here so
   that it covers every suite however pytest was invoked.
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
SHARED_ROOT = os.path.join(ROOT, "anki_shared")
sys.path.insert(0, ROOT)


def _register(name: str, path: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = [path]
    mod.__package__ = name
    sys.modules[name] = mod
    return mod


_register("anki_shared", SHARED_ROOT)

# Loaded by pytest after this module has run, so the registration above is what lets the
# name resolve. Only the root conftest may declare plugins.
pytest_plugins = ["anki_shared.testing.pytest_plugin"]

# Every directory holding a build.json is an addon.
ADDON_PACKAGES = [
    entry
    for entry in sorted(os.listdir(ROOT))
    if os.path.isfile(os.path.join(ROOT, entry, "build.json"))
]
for _entry in ADDON_PACKAGES:
    _addon = _register(_entry, os.path.join(ROOT, _entry))
    if not os.path.isdir(os.path.join(ROOT, _entry, "shared")):
        _addon.shared = _register(f"{_entry}.shared", SHARED_ROOT)


def _install_anki() -> bool:
    """Install real Anki with a stubbed `mw`, or the stand-in. True if real."""
    try:
        from anki_shared.testing import real_anki

        real_anki.qt_offscreen()
        real_anki.install()
    except ImportError:
        return False
    return True


REAL_ANKI = _install_anki()

if not REAL_ANKI:
    from anki_shared.testing import anki_stubs

    anki_stubs.install()
