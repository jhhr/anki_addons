"""Loading modules that do import aqt, by standing in for Anki.

addon_modules.py loads api_client and concurrency straight from their files because they
depend on nothing but the stdlib and requests. base_ops cannot be loaded that way: it imports
aqt and anki, and it reaches sideways into the add-on package for utils and make_notes_tsv,
so it needs both a package to live in and an Anki to import.

So this puts a minimal Anki in sys.modules first and then loads the add-on's modules under a
synthetic package name, which is what makes their relative imports resolve without running
the add-on's own __init__ (which builds menus and registers hooks). Only the handful of names
the ops actually use are given real behaviour - `mw.progress.want_cancel` above all, which is
how a run learns it has been cancelled. Everything else resolves to a throwaway class, since
the code under test only passes those around.

None of this makes base_ops safe to load inside Anki: it is for the test suite, which runs
outside it.
"""

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

ADDON_ROOT = Path(__file__).resolve().parent.parent

# The add-on package under a name of our own, so nothing resolves to the real package and
# triggers its __init__
PACKAGE = "addon_under_test_pkg"


# --- The Anki that isn't there ------------------------------------------------------------


class Note:
    """Enough of anki.notes.Note for the ops to carry one around."""

    def __init__(self, note_id: int = 0):
        self.id = note_id
        self.fields: list[str] = []

    def __getitem__(self, key):
        return ""

    def __setitem__(self, key, value):
        pass


class NoteId(int):
    pass


class Collection:
    def add_custom_undo_entry(self, message: str) -> int:
        return 1


class Progress:
    """The progress dialog, reduced to the one thing a run asks it: has it been cancelled."""

    def __init__(self):
        self.cancel = False

    def want_cancel(self) -> bool:
        return self.cancel

    def update(self, **kwargs) -> None:
        pass

    def finish(self) -> None:
        pass


class Taskman:
    def run_on_main(self, callback) -> None:
        callback()


class MainWindow:
    def __init__(self):
        self.progress = Progress()
        self.taskman = Taskman()

    def addonManager(self):
        pass


mw = MainWindow()


def _module(name: str, **attrs) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # every stub is a package, so submodules of it can be stubbed too
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _StubLoader:
    """Creates a module whose every attribute is a fresh throwaway class."""

    def create_module(self, spec):
        module = ModuleType(spec.name)
        module.__path__ = []
        # PEP 562: anything imported from the stub resolves rather than raising ImportError
        module.__getattr__ = lambda attr: type(attr, (), {})
        return module

    def exec_module(self, module) -> None:
        pass


class _StubFinder:
    """Stubs any anki.* or aqt.* submodule that hasn't been given real behaviour above.

    The ops import a long tail of Anki modules for names they only pass along - aqt.qt,
    aqt.import_export.importing and so on - and enumerating them by hand means the suite
    breaks every time one is added. Only submodules: `anki` and `aqt` themselves are set up
    explicitly, so a missing stub there is a mistake worth seeing rather than papering over.
    """

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(("anki.", "aqt.")):
            return None
        if fullname in sys.modules:
            return None
        return importlib.util.spec_from_loader(fullname, _StubLoader())


def install() -> None:
    """Put the stand-in Anki in sys.modules. Safe to call more than once."""
    if "anki" in sys.modules and getattr(sys.modules["anki"], "_is_addon_test_stub", False):
        return

    _module("anki", _is_addon_test_stub=True)
    _module("anki.notes", Note=Note, NoteId=NoteId)
    _module("anki.collection", Collection=Collection, OpChanges=type("OpChanges", (), {}))
    _module("anki.hooks")
    _module("aqt", mw=mw, gui_hooks=types.SimpleNamespace())

    sys.meta_path.append(_StubFinder())


def load_ops_module(name: str) -> ModuleType:
    """Load async_api_ops/<name>.py as part of a synthetic add-on package."""
    install()

    if PACKAGE not in sys.modules:
        root = ModuleType(PACKAGE)
        root.__path__ = [str(ADDON_ROOT)]
        sys.modules[PACKAGE] = root

        ops = ModuleType(f"{PACKAGE}.async_api_ops")
        ops.__path__ = [str(ADDON_ROOT / "async_api_ops")]
        sys.modules[f"{PACKAGE}.async_api_ops"] = ops
        setattr(root, "async_api_ops", ops)

    dotted = f"{PACKAGE}.async_api_ops.{name}"
    if dotted in sys.modules:
        return sys.modules[dotted]

    path = ADDON_ROOT / "async_api_ops" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(dotted, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build a spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before executing, so a relative import that comes back round to this module
    # resolves to the same object
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module
