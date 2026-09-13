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

    A locally rebuilt tree normally goes on first because it is the one that was resolved
    against this exact interpreter. It is a layer, not a replacement: it is built from
    requirements.txt, so it can never contain the hand-vendored packages that have no PyPI
    release - `mdict_query` is one - and those have to keep resolving from the shipped `lib/`
    behind it.

    "Normally", because a rebuilt tree only fits the interpreter it was rebuilt *for*, and
    `user_files` is the one directory Anki carries across an addon update. A tree rebuilt on a
    pip-installed Python 3.10 is still sitting there after Anki's launcher moves to 3.13, and
    leading with it then shadows a shipped tree that fits - which is not a visible failure but
    a silent one: `psutil` and `rapidfuzz` import their wrong-ABI extensions, fail, and fall
    back to a static concurrency limit and pure-Python Levenshtein. So when the rebuilt tree
    does not fit and the shipped one does, the shipped one leads instead. The rebuilt tree is
    demoted rather than dropped, because it is still a layer and may hold something the
    shipped tree does not.
    """
    lib = shipped_lib(addon_dir)
    tag = platform_tag()
    shipped = [os.path.join(lib, "_platform", tag)] if tag else []
    shipped.append(lib)
    user = user_lib(addon_dir)
    candidates = [*shipped, user] if _shipped_lib_leads(addon_dir) else [user, *shipped]
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
    platforms = manifest.get("platforms")
    if manifest.get("rebuilt_locally"):
        # A locally rebuilt tree was built by this machine, for this machine, so "do we ship a
        # build for this platform" is not a question to ask of it. Asking it anyway is wrong
        # in exactly the case the rebuild exists for: on a platform _TAGS does not name,
        # platform_tag() is None and so is the tag the rebuild recorded, so the tree that fits
        # best was judged unfit - and since a successful rebuild clears the record that stops
        # the offer coming back, the same rebuild was offered again at every startup, forever.
        #
        # Two known and differing tags still mean the tree came from somewhere else, which
        # user_files can do: it is the one directory Anki carries across an addon update, and
        # people copy their Anki folder between machines.
        built_on = platforms[0] if isinstance(platforms, list) and platforms else None
        if tag is not None and built_on is not None and built_on != tag:
            return f"{where} was built on {built_on}, but this machine is {tag}"
        return None
    if tag is None:
        return f"{where} ships no build for {sys.platform}/{platform.machine()}"
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
    if os.path.isdir(user_lib(addon_dir)) and not _shipped_lib_leads(addon_dir):
        # Judge it on its own and do not fall through: it leads on sys.path, so a healthy
        # shipped tree behind it is not a fallback, it is shadowed.
        return _user_lib_mismatch(addon_dir) or _smoke_test()

    # Either there is no rebuilt tree, or add_vendor_paths has demoted it behind the shipped
    # one. Both answer the same question - is the tree that leads sys.path the right one - and
    # they have to answer it the same way, or the rebuild offer fires forever over a tree that
    # is no longer the one being used.
    return _shipped_lib_mismatch(addon_dir) or _smoke_test()


def _user_lib_mismatch(addon_dir: str) -> Optional[str]:
    """Why the locally rebuilt tree does not fit this machine, from its manifest alone."""
    manifest = _read_manifest(user_lib(addon_dir))
    if manifest is None:
        return "the locally rebuilt lib is missing its manifest, so it may be incomplete"
    return _mismatch(manifest, "the locally rebuilt lib")


def _shipped_lib_mismatch(addon_dir: str) -> Optional[str]:
    """Why the tree that came with the addon does not fit this machine, from its manifest."""
    manifest = _read_manifest(shipped_lib(addon_dir))
    if manifest is None:
        return "the vendored lib has no manifest, so what it was built for is unknown"
    return _mismatch(manifest, "the vendored lib")


def _shipped_lib_leads(addon_dir: str) -> bool:
    """Does the shipped tree belong ahead of the locally rebuilt one on sys.path?

    Only when the rebuilt tree does not fit this machine and the shipped tree does - there is
    no point demoting a stale tree behind an equally stale one, and a rebuild is what both of
    those want. Deliberately manifest-only, with no `_smoke_test()`: this decides the sys.path
    order, so it has to be answerable before there is anything on sys.path to import.
    """
    if not os.path.isdir(user_lib(addon_dir)):
        return False
    return _user_lib_mismatch(addon_dir) is not None and _shipped_lib_mismatch(addon_dir) is None


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
