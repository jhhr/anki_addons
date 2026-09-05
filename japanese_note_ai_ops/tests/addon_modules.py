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
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ADDON_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = ADDON_ROOT / "async_api_ops"
DEFAULT_SUBDIR = "async_api_ops"

sys.path.insert(0, str(ADDON_ROOT.parent / "anki_shared" / "utils"))
from vendor_path import add_vendor_paths  # noqa: E402

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

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)
