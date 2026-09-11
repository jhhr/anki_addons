"""Full-stub mode: loading modules that import aqt, by standing in for Anki.

This is the cheaper of the two modes offered by `anki_shared.testing` (see
`real_anki.py` for the other). Nothing here needs the `anki` PyPI package, Qt, or a
collection on disk, so it suits pure logic that only passes Anki objects around.

It puts a minimal Anki in sys.modules. The root conftest registers addon packages without
running their `__init__` modules, which build menus and register hooks. Only the handful
of names the code under test actually uses are given real behaviour --
`mw.progress.want_cancel` above all, which is how a long-running op learns it has been
cancelled. Everything else resolves to a throwaway class, since the code under test only
passes those around.

The `Note` here is inert: `__getitem__` returns "" and `__setitem__` does nothing. Code
that needs real field storage or real search wants `real_anki.py` instead.

None of this makes an addon module safe to load inside Anki: it is for the test suite,
which runs outside it.
"""

import importlib.util
import sys
import types
from types import ModuleType

# --- The Anki that isn't there ------------------------------------------------------------


class Note:
    """Enough of anki.notes.Note for an op to carry one around."""

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

    def set_title(self, title: str) -> None:
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
        module.__getattr__ = lambda name: type(name, (), {})  # type: ignore[method-assign]
        return module

    def exec_module(self, module) -> None:
        pass


class _StubFinder:
    """Stubs any anki.* or aqt.* submodule that hasn't been given real behaviour above.

    Addons import a long tail of Anki modules for names they only pass along - aqt.qt,
    aqt.import_export.importing and so on - and enumerating them by hand means the suite
    breaks every time one is added. Only submodules: `anki` and `aqt` themselves are set up
    explicitly, so a missing stub there is a mistake worth seeing rather than papering over.
    """

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(("anki.", "aqt.")):
            return None
        if fullname in sys.modules:
            return None
        return importlib.util.spec_from_loader(fullname, _StubLoader())  # type: ignore[arg-type]


def is_installed() -> bool:
    """Whether the stand-in Anki - rather than the real one - is in sys.modules."""
    return "anki" in sys.modules and getattr(sys.modules["anki"], "_is_addon_test_stub", False)


def install() -> None:
    """Put the stand-in Anki in sys.modules. Safe to call more than once."""
    if is_installed():
        return

    _module("anki", _is_addon_test_stub=True)
    _module("anki.notes", Note=Note, NoteId=NoteId)
    _module("anki.collection", Collection=Collection, OpChanges=type("OpChanges", (), {}))
    _module("anki.hooks")
    _module("aqt", mw=mw, gui_hooks=types.SimpleNamespace())

    sys.meta_path.append(_StubFinder())
