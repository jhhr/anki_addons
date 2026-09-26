"""Import word_array modules from a script without importing the add-on package.

The add-on's __init__.py imports aqt, so the scripts here register a bare package rooted at the
add-on directory (the same trick test/addon_modules.py uses) and import through it; relative
imports like `..shared.jp_text_processing` then resolve as they do inside Anki.
"""

import importlib
import io
import sys
from pathlib import Path
from types import ModuleType

ADDON_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "jnaio_dev"

# Japanese output on a Windows console: every script that imports this gets utf-8 stdout, so
# none of them repeats the call. The check is what makes it safe when stdout is not a console
# (a pipe a test replaced with StringIO has no reconfigure).
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")


def _register(name: str, path: Path) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


def _import(dotted: str) -> ModuleType:
    if PACKAGE not in sys.modules:
        root = _register(PACKAGE, ADDON_ROOT)
        # `shared/` only exists once `python build.py link` has run, and a fresh clone has not
        # run it; the root conftest's stand-in covers `japanese_note_ai_ops.shared`, not this
        # package's. Same stand-in: `shared` over anki_shared/ itself.
        if not (ADDON_ROOT / "shared").is_dir():
            shared = _register(f"{PACKAGE}.shared", ADDON_ROOT.parent / "anki_shared")
            setattr(root, "shared", shared)
    return importlib.import_module(f"{PACKAGE}.{dotted}")


def load(name: str) -> ModuleType:
    """A word_array module, e.g. load("generator") or load("research.old_word_lists")."""
    return _import(f"word_array.{name}")


def load_root(name: str) -> ModuleType:
    """A module at the add-on root, e.g. load_root("html_stripping")."""
    return _import(name)


def load_shared(dotted: str) -> ModuleType:
    """A shared module, e.g. load_shared("jp_text_processing.word.use_tag_cleaning")."""
    return _import(f"shared.{dotted}")
