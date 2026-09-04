"""Put an addon's vendored `lib/` on sys.path, and say whether it fits this machine.

Most vendored packages are pure Python, or carry extension modules whose filenames already
spell out the platform and ABI (`cd.cpython-313-darwin.so`), so they can all share one flat
directory. Packages built against the stable ABI cannot: an abi3 wheel names its extension
`_psutil_linux.abi3.so` on every architecture, so five platforms' copies would collide and
the last one written would be the only one shipped - an ImportError for everyone else.

Those packages get a directory per platform instead, and this picks the right one:

    <addon>/user_files/lib/        a tree rebuilt on this machine, if there is one
    <addon>/lib/_platform/<tag>/   whole packages containing abi3 extensions
    <addon>/lib/                   flat, everything that can be shared

`build.py vendor` writes the two shipped halves and uses the same tags. The first is written
by `vendor_rebuild.rebuild_libs` when `vendor_health` finds the shipped tree does not fit the
Python Anki is currently running - which is not hypothetical: the tree this replaced held
cp39 binaries on a 3.13 runtime.

It lives under `user_files` because that is the only directory Anki carries across an addon
update; everything else is sent to the trash and re-extracted.
"""

from __future__ import annotations

import json
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

# Written by `build.py vendor` into lib/, and by rebuild_libs into user_files/lib/ in the
# same shape, so the health check below is one code path whichever tree is live.
VENDOR_MANIFEST = ".vendored.json"

# One package that is expected to be importable once either tree is on sys.path. Only a
# backstop for a half-extracted directory - see vendor_health.
_SMOKE_MODULE = "psutil"


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


def runtime_python_version() -> str:
    """"3.13" - the granularity a wheel's ABI tag cares about."""
    return "{}.{}".format(*sys.version_info[:2])


def shipped_lib(addon_dir: str) -> str:
    """The vendored tree that came with the addon, replaced wholesale on every update."""
    return os.path.join(addon_dir, "lib")


def user_lib(addon_dir: str) -> str:
    """The tree rebuilt on this machine, if it has one. Survives addon updates."""
    return os.path.join(addon_dir, "user_files", "lib")


def add_vendor_paths(addon_dir: str) -> None:
    """Make the addon's vendored packages importable, best-fitting tree first.

    All three entries are appended rather than prepended: Anki bundles some of the same
    distributions (requests among them), and a vendored copy jumping ahead of the one Anki is
    itself using is a bigger change than getting an addon its dependencies. Only the order
    *among* the three matters.

    A locally rebuilt tree goes on first because it is the one that was resolved against this
    exact interpreter. It is a layer, not a replacement: it is built from requirements.txt, so
    it can never contain the hand-vendored packages that have no PyPI release - `mdict_query`
    is one - and those have to keep resolving from the shipped `lib/` behind it.
    """
    lib = shipped_lib(addon_dir)
    tag = platform_tag()
    candidates = [user_lib(addon_dir)]
    if tag:
        candidates.append(os.path.join(lib, "_platform", tag))
    candidates.append(lib)
    for path in candidates:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)


def _read_manifest(lib: str) -> Optional[dict]:
    """The manifest in `lib`, or None if there is not a readable one."""
    try:
        with open(os.path.join(lib, VENDOR_MANIFEST), encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _mismatch(manifest: dict, where: str) -> Optional[str]:
    """Why this manifest does not describe the machine we are on, or None if it does."""
    built_for = manifest.get("python_version")
    running = runtime_python_version()
    if built_for != running:
        return (
            f"{where} was built for Python {built_for or '(unrecorded)'}, "
            f"but Anki is running Python {running}"
        )
    tag = platform_tag()
    if tag is None:
        return f"{where} ships no build for {sys.platform}/{platform.machine()}"
    platforms = manifest.get("platforms")
    if not isinstance(platforms, list) or tag not in platforms:
        return f"{where} has no build for {tag}"
    return None


def vendor_health(addon_dir: str) -> Optional[str]:
    """None if the live vendored tree fits this machine, else a short reason it does not.

    This compares what the tree was *built for* against what is *running*. It deliberately
    does not try importing anything, and that is the whole point: the tree this check was
    written for held cp39 binaries on a 3.13 runtime, and both `import rapidfuzz` and
    `import charset_normalizer` succeeded there, silently falling back to pure Python. A check
    built on "does it import" passes on precisely the breakage this exists to catch.

    Two string comparisons and a small JSON read, so it is cheap enough to run at every
    startup - and it has to, because Anki's launcher can move Anki's Python underneath an
    addon that has not itself changed.
    """
    user = user_lib(addon_dir)
    if os.path.isdir(user):
        # It is first on sys.path, so a stale one shadows a shipped tree that would have been
        # fine. Judge it on its own and do not fall through.
        manifest = _read_manifest(user)
        if manifest is None:
            return "the locally rebuilt lib is missing its manifest, so it may be incomplete"
        return _mismatch(manifest, "the locally rebuilt lib") or _smoke_test()

    manifest = _read_manifest(shipped_lib(addon_dir))
    if manifest is None:
        return "the vendored lib has no manifest, so what it was built for is unknown"
    return _mismatch(manifest, "the vendored lib") or _smoke_test()


def _smoke_test() -> Optional[str]:
    """Backstop for a corrupted or half-extracted tree that the manifest still vouches for.

    Never the primary signal - see vendor_health - but a manifest is only a claim about what
    was written, and an interrupted extraction leaves one that is no longer true.
    """
    import importlib.util

    try:
        found = importlib.util.find_spec(_SMOKE_MODULE) is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        return f"the vendored lib is on sys.path but {_SMOKE_MODULE} is not in it"
    return None
