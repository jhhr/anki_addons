"""Loading the modules under test without importing the add-on package.

The add-on's __init__.py imports aqt, which only exists inside Anki, so importing
`simple_anki_ai_prompts.async_api_ops.api_client` the normal way needs a running Anki. The
modules these tests cover - api_client and concurrency - deliberately depend on nothing but
the stdlib and the add-on's own vendored lib/, so they can be loaded straight from their files
and tested on their own. Keeping them that way is worth some care: an `aqt` import added to
either one takes the whole suite offline.

Putting lib/ on sys.path is the one thing __init__.py does that these modules still need, so
it happens here instead - through the same helper, so a test resolves psutil and requests the
way Anki will rather than a way only the test knows about.

The Anki stubs come from the monorepo's anki_shared/, which the repo-root conftest makes
importable for the other suites. That conftest is out of reach here - test/pytest.ini makes
this directory the rootdir, and unittest never reads conftests - so the repo root goes on
sys.path here instead. anki_shared has no __init__.py and resolves as a namespace package.
"""

import importlib.util
import sys
import time as real_time
from pathlib import Path
from types import ModuleType
from typing import Callable

ADDON_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ADDON_ROOT.parent
OPS_DIR = ADDON_ROOT / "async_api_ops"
DEFAULT_SUBDIR = "async_api_ops"
PACKAGE = "addon_under_test_pkg"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from anki_shared.testing import anki_stubs  # noqa: E402
from anki_shared.testing.anki_stubs import mw  # noqa: E402
from anki_shared.utils.vendor_path import add_vendor_paths  # noqa: E402

add_vendor_paths(str(ADDON_ROOT))


def load_addon_module(name: str, subdir: str = DEFAULT_SUBDIR) -> ModuleType:
    """Load <subdir>/<name>.py as a standalone module.

    Only for modules that keep to the stdlib and the vendored lib/, whatever directory they
    live in: mdx_memo sits beside the aqt-importing mdx_dictionary but imports neither it nor
    anything else of the add-on's, which is what lets it be tested this way.
    """
    path = ADDON_ROOT / subdir / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"No module {name} at {path}")
    spec = importlib.util.spec_from_file_location(f"addon_under_test_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build a spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before executing so anything the module does at import time that looks itself
    # up by name resolves to the same object
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_ops_module(name: str, subdir: str = DEFAULT_SUBDIR) -> ModuleType:
    """Load an Anki-dependent module as part of a synthetic add-on package."""
    anki_stubs.install()

    if PACKAGE not in sys.modules:
        root = ModuleType(PACKAGE)
        root.__path__ = [str(ADDON_ROOT)]
        sys.modules[PACKAGE] = root

    # A nested subdir ("word_array/research") needs a package per level: the relative imports
    # inside such a module count dots up through them.
    package_name = PACKAGE
    directory = ADDON_ROOT
    for part in subdir.split("/") if subdir else []:
        parent = sys.modules[package_name]
        package_name = f"{package_name}.{part}"
        directory = directory / part
        if package_name not in sys.modules:
            ops = ModuleType(package_name)
            ops.__path__ = [str(directory)]
            sys.modules[package_name] = ops
            setattr(parent, part, ops)

    dotted = f"{package_name}.{name}"
    if dotted in sys.modules:
        return sys.modules[dotted]

    path = directory / f"{name}.py"
    spec = importlib.util.spec_from_file_location(dotted, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build a spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


class FakeClock:
    """Stand-in for the `time` module, so cooldowns can be tested without waiting for them.

    Install it over a module's `time` global. Sleeping moves the clock instead of blocking,
    which is what lets a test drive a 60-second backoff in no time at all.
    """

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        # The same clock as monotonic: code that measures wall-clock deadlines (an automatic
        # pause's resume_at) moves on with the sleeps too
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds

    # Formatting is not time passing, so the real functions do it; with these the clock can
    # stand in for base_ops' `time`, whose progress labels format their durations
    def gmtime(self, seconds: float) -> real_time.struct_time:
        return real_time.gmtime(seconds)

    def strftime(self, fmt: str, moment: real_time.struct_time) -> str:
        return real_time.strftime(fmt, moment)

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


class PausingClock(FakeClock):
    """A FakeClock that calls `on_sleep(sleeps_so_far)` after every sleep.

    For driving a paused run: a pause nothing lifts polls forever under a fake clock, so the
    test resumes or cancels from `on_sleep`. Past `max_sleeps` a sleep raises instead, so a
    test whose pause never ends fails rather than hangs.
    """

    def __init__(
        self, on_sleep: Callable[[int], None], max_sleeps: int = 1000, start: float = 1000.0
    ):
        super().__init__(start)
        self.on_sleep = on_sleep
        self.max_sleeps = max_sleeps

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        if len(self.slept) > self.max_sleeps:
            raise AssertionError(f"Still waiting after {self.max_sleeps} sleeps")
        self.on_sleep(len(self.slept))
