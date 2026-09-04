"""Make the monorepo's packages importable from tests, without Anki running.

Two problems to solve, both inherited from the per-addon conftest this replaces:

1. The `anki` PyPI package has circular imports that only resolve inside a
   running Anki process, so every anki/aqt module is stubbed with a MagicMock
   before anything can import it.
2. An addon's root `__init__.py` calls `mw.addonManager` at module level, which
   crashes outside Anki. Registering each addon as a stub package whose
   `__path__` points at the real directory lets tests import its submodules --
   and lets the relative imports inside them resolve -- without ever executing
   that `__init__.py`.

`anki_shared` gets the same treatment for a different reason: it deliberately
has no `__init__.py` (see build.py's repo-root guard on the same principle).
"""

import os
import sys
import types
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.abspath(__file__))

for _mod_name in [
    "anki",
    "anki.cards",
    "anki.notes",
    "anki.consts",
    "anki.models",
    "anki.stats",
    "anki.stats_pb2",
    "anki.utils",
    "aqt",
]:
    sys.modules.setdefault(_mod_name, MagicMock())


def _register(name: str, path: str) -> None:
    mod = types.ModuleType(name)
    mod.__path__ = [path]
    mod.__package__ = name
    sys.modules[name] = mod


_register("anki_shared", os.path.join(ROOT, "anki_shared"))

# Every directory holding a build.json is an addon.
for _entry in sorted(os.listdir(ROOT)):
    if os.path.isfile(os.path.join(ROOT, _entry, "build.json")):
        _register(_entry, os.path.join(ROOT, _entry))
