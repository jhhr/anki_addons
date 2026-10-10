"""Import word_array modules from a script without importing the add-on package.

The add-on's __init__.py imports aqt, so the scripts here register a bare package rooted at the
add-on directory (the same trick test/addon_modules.py uses) and import through it; relative
imports like `..shared.jp_text_processing` then resolve as they do inside Anki.
"""

import importlib
import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

ADDON_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "jnaio_dev"
# The scripts' reports, logs and undo files; eval data only on a machine without the test data
# checkout (`eval_file`)
OUTPUT = ADDON_ROOT / "output"

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


_data_paths: Optional[ModuleType] = None


def data_paths() -> ModuleType:
    """dev/data_paths.py, loaded by its path once: it imports nothing of the addon, and dev/ is
    not on a research script's sys.path."""
    global _data_paths
    if _data_paths is None:
        spec = importlib.util.spec_from_file_location(
            "jnaio_data_paths", ADDON_ROOT / "dev" / "data_paths.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _data_paths = module
    return _data_paths


def eval_file(name: str) -> Path:
    """An eval set, hand-label file, hand-checked corpus or answer cache, by file name: in
    `japanese_note_ai_ops/evals/` of the private test data checkout (dev/data_paths.py) when
    this machine has one, else in `output/`.

    They are hand work, paid answers and a pre-migration export that exist nowhere else, and
    they hold the collection's text, so they are kept in that repo rather than in one
    machine's gitignored output/: commit and push there after a script changes one. A file
    only output/ has, such as a new "Export kanjify test data" from the menu, is used from
    there until it is moved; one both have is used from evals/, with a warning when output/'s
    is newer."""
    local = OUTPUT / name
    evals = data_paths().data_dir("evals")
    if evals is None:
        return local
    kept = evals / name
    if not kept.exists():
        if local.exists():
            print(f"{name}: using output/'s; move it to {evals} to keep it", file=sys.stderr)
            return local
        return kept
    if local.exists() and local.stat().st_mtime > kept.stat().st_mtime:
        print(f"{name}: output/ has a newer copy, not used; move it to {evals} to use it",
              file=sys.stderr)
    return kept
