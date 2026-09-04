"""Put an addon's vendored `lib/` on sys.path, including its per-platform half.

Most vendored packages are pure Python, or carry extension modules whose filenames already
spell out the platform and ABI (`cd.cpython-313-darwin.so`), so they can all share one flat
directory. Packages built against the stable ABI cannot: an abi3 wheel names its extension
`_psutil_linux.abi3.so` on every architecture, so five platforms' copies would collide and
the last one written would be the only one shipped - an ImportError for everyone else.

Those packages get a directory per platform instead, and this picks the right one:

    <addon>/lib/                   flat, everything that can be shared
    <addon>/lib/_platform/<tag>/   whole packages containing abi3 extensions

`build.py vendor` writes both halves and uses the same tags.
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Optional

# Keep in step with VENDOR_PLATFORMS in build.py
_TAGS = {
    ("win32", "x86_64"): "win_amd64",
    ("darwin", "x86_64"): "macos_x86_64",
    ("darwin", "arm64"): "macos_arm64",
    ("linux", "x86_64"): "linux_x86_64",
    ("linux", "arm64"): "linux_aarch64",
}

# What platform.machine() reports for the two architectures we ship, across the three OSes
_MACHINES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def platform_tag() -> Optional[str]:
    """The `lib/_platform` subdirectory for this machine, or None if we ship none for it."""
    if sys.platform.startswith("linux"):
        system = "linux"
    elif sys.platform in ("win32", "darwin"):
        system = sys.platform
    else:
        return None
    machine = _MACHINES.get(platform.machine().lower())
    if machine is None:
        return None
    return _TAGS.get((system, machine))


def add_vendor_paths(addon_dir: str) -> None:
    """Make <addon_dir>/lib importable, with this platform's binaries taking precedence.

    Both are appended rather than prepended: Anki bundles some of the same distributions
    (requests among them), and a vendored copy jumping ahead of the one Anki is itself using
    is a bigger change than getting an addon its dependencies. Only the order *between* the
    two matters, and _platform going on first is what makes an abi3 package resolve there
    rather than to whatever the flat directory happens to hold.
    """
    lib = os.path.join(addon_dir, "lib")
    tag = platform_tag()
    if tag:
        per_platform = os.path.join(lib, "_platform", tag)
        if os.path.isdir(per_platform) and per_platform not in sys.path:
            sys.path.append(per_platform)
    if os.path.isdir(lib) and lib not in sys.path:
        sys.path.append(lib)
