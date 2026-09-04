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
  dist     dist/<addon>-<version>.ankiaddon with real file copies
  check    fail if an addon imports a shared package it did not declare

Stdlib only. Windows uses directory junctions, which need no admin rights.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHARED_ROOT = ROOT / "anki_shared"
DIST_DIR = ROOT / "dist"

EXCLUDE_DIRS = {
    "__pycache__", ".git", ".github", ".idea", ".vscode", ".pytest_cache",
    "test", "tests", "dist", "node_modules",
}
EXCLUDE_FILES = {
    # meta.json is per-install state written by Anki; shipping it is meaningless
    # and Anki rewrites it on install anyway.
    "meta.json", ".gitignore", ".gitmodules", ".gitattributes",
    "pytest.ini", "build.json", "manifest.json",
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
    ap.add_argument("command", choices=["link", "install", "dist", "check"])
    ap.add_argument("addons", nargs="*", help="addon dir names; default all")
    ap.add_argument("--addons-dir", type=Path, default=None)
    args = ap.parse_args()

    guard_repo_root()
    addons = discover(args.addons)
    if args.command == "link":
        cmd_link(addons)
    elif args.command == "install":
        cmd_install(addons, args.addons_dir or default_addons_dir())
    elif args.command == "dist":
        if cmd_check(addons):
            return 1
        cmd_dist(addons)
    elif args.command == "check":
        return cmd_check(addons)
    return 0


if __name__ == "__main__":
    sys.exit(main())
