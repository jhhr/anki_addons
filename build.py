#!/usr/bin/env python3
"""Dev-link and release-package the addons in this monorepo.

Layout this assumes:

    anki_addons/                     <- repo root, this file lives here
      anki_shared/                   <- shared packages, NOT an addon
        interpolate/                 (interpolate_fields.py, execute_code.py, ...)
        ui/                          (pasteable_text_edit.py, code_edit_layout.py, ...)
        jp_text_processing/          (submodule; carries mecab_controller)
      copy_anywhere/    build.json, __init__.py, logic/, ui/, ...
      related_card_disperse/
      custom_schedule_helper/

Each addon declares in build.json which shared packages it uses. Both commands
below materialise those at <addon>/shared/<pkg>, so the import path is identical
in development and in the released zip:

    from .shared.interpolate.interpolate_fields import interpolate_from_text

  link     <addon>/shared/<pkg> -> link into anki_shared/  (gitignore it)
  install  addons21/<dev_dir_name> -> link to <addon>/     (once per device)
  vendor   <addon>/lib from requirements.txt, for every platform Anki runs on
  dist     dist/<addon>-<version>.ankiaddon with real file copies
  check    fail if an addon imports a shared package it did not declare

Stdlib only. Windows uses directory junctions, which need no admin rights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
SHARED_ROOT = ROOT / "anki_shared"
DIST_DIR = ROOT / "dist"

EXCLUDE_DIRS = {
    "__pycache__", ".git", ".github", ".idea", ".vscode", ".pytest_cache",
    "test", "tests", "dist", "node_modules",
    # Per-install state, not addon content. Anki preserves user_files across updates, so a
    # dev machine's copy has no business in the zip - and it is where the mdx dictionaries
    # this addon reads end up, which is hundreds of megabytes of somebody else's data.
    "user_files", "logs", "output",
}
EXCLUDE_FILES = {
    # meta.json is per-install state written by Anki; shipping it is meaningless
    # and Anki rewrites it on install anyway.
    "meta.json", ".gitignore", ".gitmodules", ".gitattributes",
    "pytest.ini", "build.json", "manifest.json",
    # The pinned requirements.txt compiled from this does ship - the runtime rebuild reads
    # it - but the source it was compiled from is a build-time input only.
    "requirements.in",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".ankiaddon"}
EXCLUDE_PATTERNS = (re.compile(r".*_tests\.py$"),)

SHARED_IMPORT_RE = re.compile(r"from\s+\.{1,3}shared\.(\w+)")


# --------------------------------------------------------------------------
# addon discovery
# --------------------------------------------------------------------------

class Addon:
    def __init__(self, path: Path, meta: dict):
        self.path = path
        self.meta = meta
        self.package: str = meta["package"]
        self.name: str = meta["name"]
        self.dev_dir_name: str = meta.get("dev_dir_name", path.name)
        self.shared: list[str] = meta.get("shared", [])
        self.version: str = meta.get("human_version", "0.0.1")
        self.extra_excludes: set[str] = set(meta.get("exclude", []))

    @property
    def shared_dir(self) -> Path:
        return self.path / "shared"

    def __repr__(self) -> str:
        return f"<Addon {self.path.name}>"


def discover(names: list[str] | None = None) -> list[Addon]:
    found = []
    for entry in sorted(ROOT.iterdir()):
        cfg = entry / "build.json"
        if entry.is_dir() and cfg.is_file():
            found.append(Addon(entry, json.loads(cfg.read_text("utf-8"))))
    if names:
        wanted = set(names)
        found = [a for a in found if a.path.name in wanted or a.package in wanted]
        missing = wanted - {a.path.name for a in found} - {a.package for a in found}
        if missing:
            sys.exit(f"unknown addon(s): {', '.join(sorted(missing))}")
    if not found:
        sys.exit(f"no addons found under {ROOT} (each needs a build.json)")
    return found


# --------------------------------------------------------------------------
# links
# --------------------------------------------------------------------------

def is_link(p: Path) -> bool:
    """True for POSIX symlinks and for Windows junctions/symlinked dirs."""
    if p.is_symlink():
        return True
    try:
        return bool(os.readlink(p))
    except OSError:
        return False


def link_dir(src: Path, dst: Path) -> None:
    """Point dst at src. Junction on Windows: no admin, no Developer Mode."""
    if is_link(dst):
        try:
            dst.unlink()
        except (OSError, PermissionError):
            os.rmdir(dst)
    elif dst.exists():
        sys.exit(f"refusing to replace real directory {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
            check=True, capture_output=True,
        )
    else:
        os.symlink(src, dst, target_is_directory=True)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_link(addons: list[Addon]) -> None:
    """Materialise <addon>/shared/<pkg> as links into anki_shared/."""
    for addon in addons:
        addon.shared_dir.mkdir(exist_ok=True)
        init = addon.shared_dir / "__init__.py"
        if not init.exists():
            init.write_text("# generated by build.py; not committed\n", "utf-8")
        for pkg in addon.shared:
            src = SHARED_ROOT / pkg
            if not src.is_dir():
                sys.exit(
                    f"{addon.path.name}: declared shared package '{pkg}' not in {SHARED_ROOT}"
                )
            link_dir(src, addon.shared_dir / pkg)
        print(f"linked {addon.path.name}/shared -> {', '.join(addon.shared) or '(none)'}")


def default_addons_dir() -> Path:
    """This repo is expected to live inside addons21, so the parent is it."""
    env = os.environ.get("ANKI_ADDONS_DIR")
    if env:
        return Path(env)
    if ROOT.parent.name == "addons21":
        return ROOT.parent
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Anki2" / "addons21"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Anki2" / "addons21"
    return Path.home() / ".local" / "share" / "Anki2" / "addons21"


def guard_repo_root() -> None:
    """Anki loads every addons21 child holding __init__.py.  This repo sits in
    addons21 but is not itself an addon, so it must never grow one."""
    if (ROOT / "__init__.py").exists():
        sys.exit(
            f"{ROOT / '__init__.py'} exists: Anki would load the monorepo root as an "
            "addon. Delete it; the addons are the subdirectories."
        )


def cmd_install(addons: list[Addon], addons_dir: Path) -> None:
    """Link each addon into the local addons21 folder for development."""
    if not addons_dir.is_dir():
        sys.exit(f"addons folder not found: {addons_dir} (set ANKI_ADDONS_DIR)")
    cmd_link(addons)
    for addon in addons:
        link_dir(addon.path, addons_dir / addon.dev_dir_name)
        print(f"installed {addons_dir / addon.dev_dir_name} -> {addon.path}")


def excluded(rel: Path, addon: Addon) -> bool:
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    name = rel.name
    if name in EXCLUDE_FILES or rel.suffix in EXCLUDE_SUFFIXES:
        return True
    if any(pat.match(name) for pat in EXCLUDE_PATTERNS):
        return True
    return str(rel).replace("\\", "/") in addon.extra_excludes


def walk_files(base: Path, addon: Addon, prefix: Path = Path(".")):
    """Yield (absolute_path, arcname) pairs, following links, applying excludes."""
    for dirpath, dirnames, filenames in os.walk(base, followlinks=True):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        here = Path(dirpath)
        for fn in filenames:
            abs_path = here / fn
            rel = Path(os.path.normpath(prefix / abs_path.relative_to(base)))
            if excluded(rel, addon):
                continue
            yield abs_path, rel.as_posix()


# --------------------------------------------------------------------------
# vendoring
# --------------------------------------------------------------------------

# Tags must stay in step with anki_shared/utils/vendor_path.py, which picks one at runtime.
VENDOR_PLATFORMS = {
    "win_amd64": "x86_64-pc-windows-msvc",
    "macos_x86_64": "x86_64-apple-darwin",
    "macos_arm64": "aarch64-apple-darwin",
    "linux_x86_64": "x86_64-manylinux_2_36",
    "linux_aarch64": "aarch64-manylinux_2_36",
}
# Supplies the .dist-info metadata and wins any tie in the flat directory, so which machine
# the command runs on makes no difference to what comes out.
PRIMARY_PLATFORM = "win_amd64"

# The Python Anki ships (25.09 runs CPython 3.13). Resolving for an older one is how lib/ came
# to be full of cp39 binaries that 3.13 cannot load - silently, because every package carrying
# one falls back to pure Python rather than raising.
VENDOR_PYTHON_VERSION = "3.13"

# The floor the pins have to stay valid down to, which is not the same number. The shipped
# lib/ is resolved for VENDOR_PYTHON_VERSION, but the *runtime* rebuild re-resolves the same
# file with whatever Python the user's Anki has - and there are Ankis on 3.10 (the pip-installed
# `(ao)` builds run on the system Python). Compiled against 3.13 alone, requirements.txt pins
# rapidfuzz 3.14.6, which requires >=3.11, and the rebuild dies there. Compiled with this floor,
# uv forks the resolution and emits `; python_full_version` markers, so each Python gets the
# newest version that supports it and the >=3.11 branch is unchanged.
#
# 3.9 because that is aqt's own Requires-Python: no Anki runs on less, so nothing is gained by
# resolving further back.
VENDOR_PYTHON_FLOOR = "3.9"

VENDOR_MANIFEST = ".vendored.json"
# The direct dependencies, and the fully pinned set compiled from them. requirements.txt is
# the only one either half reads: `vendor` resolves it here, and the addon re-resolves it on
# the user's machine when the shipped lib/ does not fit their Python.
REQUIREMENTS_IN = "requirements.in"
REQUIREMENTS_TXT = "requirements.txt"
REQUIREMENTS_HEADER = """# Compiled from requirements.in by build.py vendor; do not edit by hand.
#    uv pip compile requirements.in --universal --python-version {floor} -o requirements.txt
#
# Pinned because `build.py vendor` is no longer the only thing that reads it: the addon
# rebuilds lib/ on the user's own machine when the shipped one does not fit their Python
# (anki_shared/utils/vendor_rebuild.py). Unpinned, two users rebuilding a year apart would
# get different versions, and a breaking release would land on them and not on the developer.
#
# The `python_full_version` markers are why the floor above is not the {version} lib/ itself is
# built for: that rebuild runs on the user's Python, which can be older, and a single pin for
# the newest version would simply not install there.
"""
# uv's own bookkeeping and console scripts; an addon can never run either.
VENDOR_SKIP_ENTRIES = {"bin", ".lock"}
EXTENSION_SUFFIXES = (".pyd", ".so", ".dylib", ".dll")
# Compared with line endings normalised: Windows wheels ship their .py files with CRLF and the
# others with LF, so raw bytes would call every pure-Python package platform-specific.
TEXT_SUFFIXES = {
    ".py", ".pyi", ".typed", ".txt", ".cfg", ".ini", ".json", ".md", ".rst", ".toml", ".pem",
}


def find_uv() -> str:
    """uv resolves wheels for platforms it is not running on, which is the whole trick here."""
    found = shutil.which("uv")
    if found:
        return found
    # Anki bundles one for its own addon installs; on a dev machine that may be the only copy.
    bundled = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            bundled.append(Path(local) / "Programs" / "Anki" / "uv.exe")
    elif sys.platform == "darwin":
        bundled.append(Path("/Applications/Anki.app/Contents/MacOS/uv"))
    for candidate in bundled:
        if candidate.is_file():
            return str(candidate)
    sys.exit(
        "vendor needs uv, which is not on PATH and was not found bundled with Anki.\n"
        "Install it from https://docs.astral.sh/uv/ and try again."
    )


def compile_requirements(addon: Addon, uv: str) -> Path:
    """Recompile <addon>/requirements.txt from requirements.in when the .in is newer.

    Pinning is not cosmetic here: the runtime rebuild re-resolves this file on the user's
    machine, so an unpinned entry means the version they get depends on the day they rebuild.
    Keeping the direct requirements in a separate .in is what stops the pins from burying
    which four packages this addon actually asked for.
    """
    source = addon.path / REQUIREMENTS_IN
    compiled = addon.path / REQUIREMENTS_TXT
    if not source.is_file():
        return compiled
    if compiled.is_file() and compiled.stat().st_mtime >= source.stat().st_mtime:
        return compiled

    print(f"  compiling {addon.path.name}/{REQUIREMENTS_TXT} from {REQUIREMENTS_IN}")
    # uv reads the input before writing the output, but they are the same directory and a
    # failed run must not leave a truncated pin file behind either.
    scratch = ROOT / "build" / f"{addon.path.name}-requirements.txt"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            # Named relative to the addon, and run from there: uv writes the input's path
            # into its `# via` comments, and an absolute one would differ per machine.
            uv, "pip", "compile", REQUIREMENTS_IN,
            # Without this uv pins for the machine it runs on, and the result would be a
            # lock describing one of the five platforms and one Python version. It is also
            # what lets the resolution fork on python_full_version; see VENDOR_PYTHON_FLOOR.
            "--universal",
            "--python-version", VENDOR_PYTHON_FLOOR,
            "--quiet",
            "-o", str(scratch),
        ],
        cwd=addon.path,
        check=True,
    )
    # uv's own header records the absolute path it was invoked with, which differs per
    # machine and would show up as a diff on every developer's re-vendor. The `# via` lines
    # are indented and stay: they are the record of which four requirements are direct.
    body = [
        line for line in scratch.read_text("utf-8").splitlines() if not line.startswith("#")
    ]
    compiled.write_text(
        REQUIREMENTS_HEADER.format(
            version=VENDOR_PYTHON_VERSION, floor=VENDOR_PYTHON_FLOOR
        )
        + "\n".join(body).strip("\n")
        + "\n",
        "utf-8",
    )
    scratch.unlink()
    return compiled


def is_extension(name: str) -> bool:
    return name.endswith(EXTENSION_SUFFIXES)


def top_level(tree: Path) -> set[str]:
    return {p.name for p in tree.iterdir() if p.name != "__pycache__"}


def dist_key(name: str) -> str:
    """Normalised distribution name, so certifi-2025.1.31.dist-info matches certifi."""
    for suffix in (".dist-info", ".egg-info", ".data"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].rsplit("-", 1)[0]
            break
    return re.sub(r"[-_.]+", "-", name).lower()


def hash_tree(tree: Path, skip_dist_info: bool = True) -> dict[str, str]:
    """Every file under tree as {relative posix path: sha256}."""
    digests: dict[str, str] = {}
    for path in tree.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(tree).as_posix()
        top = rel.split("/", 1)[0]
        if top in VENDOR_SKIP_ENTRIES:
            continue
        if skip_dist_info and top.endswith((".dist-info", ".egg-info")):
            continue
        content = path.read_bytes()
        if path.suffix.lower() in TEXT_SUFFIXES:
            content = content.replace(b"\r\n", b"\n")
        digests[rel] = hashlib.sha256(content).hexdigest()
    return digests


def platform_specific_packages(trees: dict[str, Path]) -> set[str]:
    """Top-level packages whose contents are not identical on every platform.

    Filenames are not a safe signal here, which is where the obvious version of this goes
    wrong. A macOS extension is tagged `.cpython-313-darwin.so` with no architecture in it, so
    an Intel build and an ARM build of the same module have the same name and different bytes;
    a stable-ABI (abi3) module drops the interpreter tag too and collides across all five.
    Either way a flat directory keeps whichever platform was written last and every other
    platform gets an ImportError - silently, because only one of the five is ever loaded here.
    Comparing bytes catches both cases, and anything else that turns out to differ.

    The whole package moves, not just the extension: a package's .py files and its .so have to
    live in one directory or the import fails, because __path__ would point at the flat lib and
    the extension would not be visible from there.

    .dist-info is exempt. Its WHEEL and RECORD legitimately name the wheel each platform
    resolved, and five copies of metadata that nothing imports would be pure weight.
    """
    per_path: dict[str, set[str]] = {}
    for tree in trees.values():
        for rel, digest in hash_tree(tree).items():
            per_path.setdefault(rel, set()).add(digest)
    return {rel.split("/", 1)[0] for rel, digests in per_path.items() if len(digests) > 1}


def copy_missing(src: Path, dst: Path, strip_extensions: bool = False) -> None:
    """Copy src into dst, never replacing a file already there.

    Called once per platform for the flat directory. Only files that are byte-identical
    everywhere get that far - anything that differs went to _platform - so "first one wins" and
    "last one wins" produce the same tree; not overwriting just saves the copying. What this
    does add is each platform's *own* extension modules, when their names do not collide:
    charset_normalizer's speedups reach Linux and macOS this way instead of Windows alone.
    """
    if src.is_file():
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return
    for path in sorted(src.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if strip_extensions and is_extension(path.name):
            continue
        target = dst / path.relative_to(src)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        dirs_exist_ok=True,
    )


def check_per_platform_output(lib: Path, packages: set[str]) -> None:
    """Every platform got its own copy, and the copies are not all the same thing.

    This is the check that would have caught abdnh's vendor.py writing one platform's abi3
    build over the rest: nothing else would, because only one of the five is ever loaded on
    the machine that vendors them.

    Two tags *may* share a build - a macOS universal2 wheel is one file serving both
    architectures - so identical copies are only wrong when every tag has the same one, which
    means the split bought nothing.
    """
    for package in sorted(packages):
        builds: dict[str, list[str]] = {}
        for tag in VENDOR_PLATFORMS:
            tree = lib / "_platform" / tag / package
            files = hash_tree(tree, skip_dist_info=False)
            if not any(is_extension(Path(rel).name) for rel in files):
                sys.exit(
                    f"vendor: {lib.name}/_platform/{tag}/{package} has no extension module.\n"
                    "That platform would import a package missing its compiled half."
                )
            fingerprint = hashlib.sha256(
                json.dumps(files, sort_keys=True).encode()
            ).hexdigest()
            builds.setdefault(fingerprint, []).append(tag)
        if len(builds) == 1:
            sys.exit(
                f"vendor: every platform's {package} is byte-identical, so one wheel was used "
                "for all of them and four platforms would fail to import."
            )
        print(f"  {package}: {len(builds)} distinct builds across {len(VENDOR_PLATFORMS)} tags")


def clear_previous_vendoring(
    lib: Path, incoming: set[str], keep: set[str], unwanted: set[str]
) -> None:
    """Remove what a previous vendor run put here, and nothing else.

    lib/ can also hold packages vendored by hand that no requirements.txt names - mdict_query
    is one - so this cannot just empty the directory. The manifest records exactly what the
    last run wrote; before there is one, fall back to removing only what this run is about to
    replace, and report the rest instead of guessing.
    """
    manifest_path = lib / VENDOR_MANIFEST
    known: Optional[list[str]] = None
    if manifest_path.is_file():
        try:
            known = json.loads(manifest_path.read_text("utf-8"))["flat"] + ["_platform"]
        except (ValueError, OSError, KeyError):
            known = None

    # Entries vendoring deliberately drops. Past runs (and pip installs before them) left
    # some behind, and they are ours to clean up whether or not a manifest lists them.
    doomed = [n for n in unwanted if n not in keep and (lib / n).exists()]

    if known is not None:
        doomed += [n for n in known if n not in keep and n not in doomed]
    else:
        incoming_keys = {dist_key(n) for n in incoming}
        doomed += [
            entry.name
            for entry in lib.iterdir()
            if entry.name not in keep
            and entry.name not in doomed
            and dist_key(entry.name) in incoming_keys
        ]
        leftover = sorted(
            entry.name
            for entry in lib.iterdir()
            if entry.name not in keep
            and entry.name not in doomed
            and entry.name not in ("__pycache__", VENDOR_MANIFEST)
        )
        if leftover:
            print(
                "  note: no vendor manifest yet, so these were left alone. Delete any that are\n"
                '        stale, or list them under "vendor_keep" in build.json:\n'
                "        " + ", ".join(leftover)
            )

    for name in doomed:
        target = lib / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def vendor_addon(addon: Addon, uv: str) -> None:
    requirements = compile_requirements(addon, uv)
    if not requirements.is_file():
        print(f"skip {addon.path.name}: no {REQUIREMENTS_TXT}")
        return

    keep = set(addon.meta.get("vendor_keep", []))
    # Packages to ship pure-Python. rapidfuzz is the case this exists for: its extensions are
    # ~6 MB per platform and its darwin builds collide by name, so shipping them properly would
    # mean five copies of the package - and it has a complete Python fallback, which is what it
    # has silently been running on 3.13 anyway.
    strip = set(addon.meta.get("vendor_no_binaries", []))
    # auditwheel/delvewheel park a package's bundled shared libraries in a `<name>.libs`
    # sibling. With the extensions gone nothing loads them.
    dead_libs = {f"{name}.libs" for name in strip}

    scratch = ROOT / "build" / addon.path.name
    if scratch.exists():
        shutil.rmtree(scratch)
    trees: dict[str, Path] = {}
    for tag, target in VENDOR_PLATFORMS.items():
        dest = scratch / tag
        print(f"  resolving {addon.path.name} for {tag} ({target})")
        subprocess.run(
            [
                uv, "pip", "install",
                "--requirements", str(requirements),
                "--target", str(dest),
                "--python-version", VENDOR_PYTHON_VERSION,
                "--python-platform", target,
                "--link-mode", "copy",
                "--quiet",
            ],
            check=True,
        )
        trees[tag] = dest

    per_platform = platform_specific_packages(trees) - strip
    everything = set().union(*(top_level(tree) for tree in trees.values()))
    flat = sorted(everything - per_platform - VENDOR_SKIP_ENTRIES - dead_libs)

    lib = addon.path / "lib"
    lib.mkdir(exist_ok=True)
    clear_previous_vendoring(
        lib, set(flat) | {"_platform"}, keep, VENDOR_SKIP_ENTRIES | dead_libs
    )

    ordered = [PRIMARY_PLATFORM] + [t for t in trees if t != PRIMARY_PLATFORM]
    for tag in ordered:
        tree = trees[tag]
        for name in flat:
            src = tree / name
            # Metadata comes from one platform only, so RECORD and WHEEL stay self-consistent
            if not src.exists() or (tag != PRIMARY_PLATFORM and name.endswith(".dist-info")):
                continue
            copy_missing(src, lib / name, strip_extensions=name in strip)
    for tag, tree in trees.items():
        for name in sorted(per_platform):
            copy_tree(tree / name, lib / "_platform" / tag / name)
    check_per_platform_output(lib, per_platform)

    (lib / VENDOR_MANIFEST).write_text(
        json.dumps(
            {
                "python_version": VENDOR_PYTHON_VERSION,
                "platforms": sorted(VENDOR_PLATFORMS),
                "flat": flat,
                "per_platform": sorted(per_platform),
            },
            indent=2,
        )
        + "\n",
        "utf-8",
    )

    size_mb = sum(p.stat().st_size for p in lib.rglob("*") if p.is_file()) / 1e6
    print(
        f"vendored {addon.path.name}/lib: {len(flat)} flat entries, "
        f"{len(per_platform)} per-platform x {len(trees)} platforms ({size_mb:.1f} MB)"
    )
    if per_platform:
        print(f"  per-platform: {', '.join(sorted(per_platform))}")
    if strip:
        print(f"  pure-Python only: {', '.join(sorted(strip))}")


def cmd_vendor(addons: list[Addon]) -> None:
    """Rebuild each addon's lib/ from its requirements.txt, for every platform Anki runs on."""
    uv = find_uv()
    for addon in addons:
        vendor_addon(addon, uv)


def cmd_dist(addons: list[Addon]) -> None:
    DIST_DIR.mkdir(exist_ok=True)
    for addon in addons:
        out = DIST_DIR / f"{addon.path.name}-{addon.version}.ankiaddon"
        manifest = {
            "package": addon.package,
            "name": addon.name,
            "human_version": addon.version,
        }
        for key in ("homepage", "conflicts", "min_point_version", "max_point_version"):
            if key in addon.meta:
                manifest[key] = addon.meta[key]

        entries: list[tuple[Path, str]] = []
        # the addon's own files, minus shared/ (added explicitly below)
        for abs_path, arc in walk_files(addon.path, addon):
            if arc == "shared" or arc.startswith("shared/"):
                continue
            entries.append((abs_path, arc))
        # declared shared packages, copied for real regardless of link state
        for pkg in addon.shared:
            src = SHARED_ROOT / pkg
            if not src.is_dir():
                sys.exit(f"{addon.path.name}: declared shared package '{pkg}' missing")
            entries.extend(walk_files(src, addon, prefix=Path("shared") / pkg))

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for abs_path, arc in sorted(entries, key=lambda e: e[1]):
                z.write(abs_path, arc)
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            if addon.shared:
                z.writestr("shared/__init__.py", "")

        size_mb = out.stat().st_size / 1e6
        print(f"{out.name}: {len(entries)} files, {size_mb:.1f} MB")
        if size_mb > 25:
            print("  ! large - check AnkiWeb's current upload limit before uploading")


def cmd_check(addons: list[Addon]) -> int:
    """Catch shared packages that are imported but not declared in build.json."""
    failures = 0
    for addon in addons:
        used: set[str] = set()
        for path in addon.path.rglob("*.py"):
            rel_parts = path.relative_to(addon.path).parts
            if any(p in EXCLUDE_DIRS for p in rel_parts) or "shared" in rel_parts:
                continue
            used |= set(SHARED_IMPORT_RE.findall(path.read_text("utf-8", errors="replace")))
        undeclared = used - set(addon.shared)
        unused = set(addon.shared) - used
        if undeclared:
            print(f"FAIL {addon.path.name}: imports undeclared shared pkg(s): "
                  f"{', '.join(sorted(undeclared))}")
            failures += 1
        if unused:
            print(f"warn {addon.path.name}: declares unused shared pkg(s): "
                  f"{', '.join(sorted(unused))}")
        if not undeclared and not unused:
            print(f"ok   {addon.path.name}: {', '.join(sorted(used)) or 'no shared imports'}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["link", "install", "vendor", "dist", "check"])
    ap.add_argument("addons", nargs="*", help="addon dir names; default all")
    ap.add_argument("--addons-dir", type=Path, default=None)
    args = ap.parse_args()

    guard_repo_root()
    addons = discover(args.addons)
    if args.command == "link":
        cmd_link(addons)
    elif args.command == "install":
        cmd_install(addons, args.addons_dir or default_addons_dir())
    elif args.command == "vendor":
        cmd_vendor(addons)
    elif args.command == "dist":
        if cmd_check(addons):
            return 1
        cmd_dist(addons)
    elif args.command == "check":
        return cmd_check(addons)
    return 0


if __name__ == "__main__":
    sys.exit(main())
