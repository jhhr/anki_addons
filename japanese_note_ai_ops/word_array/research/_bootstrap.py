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

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")


def _import(dotted: str) -> ModuleType:
    if PACKAGE not in sys.modules:
        root = ModuleType(PACKAGE)
        root.__path__ = [str(ADDON_ROOT)]
        sys.modules[PACKAGE] = root
    return importlib.import_module(f"{PACKAGE}.{dotted}")


def load(name: str) -> ModuleType:
    """A word_array module, e.g. load("generator")."""
    return _import(f"word_array.{name}")


def load_shared(dotted: str) -> ModuleType:
    """A shared module, e.g. load_shared("jp_text_processing.word.use_tag_cleaning")."""
    return _import(f"shared.{dotted}")
