"""Make the monorepo's packages importable from tests, without Anki running.

Two problems to solve, both inherited from the per-addon conftest this replaces:

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

`anki_shared` gets the same treatment as an addon for a different reason: it deliberately
has no `__init__.py` (see build.py's repo-root guard on the same principle).
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def _register(name: str, path: str) -> None:
    mod = types.ModuleType(name)
    mod.__path__ = [path]
    mod.__package__ = name
    sys.modules[name] = mod


_register("anki_shared", os.path.join(ROOT, "anki_shared"))

# Every directory holding a build.json is an addon.
ADDON_PACKAGES = [
    entry
    for entry in sorted(os.listdir(ROOT))
    if os.path.isfile(os.path.join(ROOT, entry, "build.json"))
]
for _entry in ADDON_PACKAGES:
    _register(_entry, os.path.join(ROOT, _entry))


def _install_anki() -> bool:
    """Install real Anki with a stubbed `mw`, or the stand-in. True if real."""
    try:
        from anki_shared.testing import real_anki
    except ImportError:
        return False

    real_anki.qt_offscreen()
    real_anki.install()
    return True


REAL_ANKI = _install_anki()

if not REAL_ANKI:
    from anki_shared.testing import anki_stubs

    anki_stubs.install()
