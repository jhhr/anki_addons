"""anki_shared/testing/packages.py: addon directories as packages whose __init__.py never runs."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from anki_shared.testing import packages


@pytest.fixture
def root(tmp_path: Path):
    """A monorepo of two addons, one linked (shared/ present) and one not, and anki_shared."""
    for name, linked in (("linked", True), ("fresh", False)):
        addon = tmp_path / name
        (addon / ("shared" if linked else "sub")).mkdir(parents=True)
        (addon / "build.json").write_text("{}", encoding="utf-8")
        # Running it would fail the test: it is exactly what registering avoids
        (addon / "__init__.py").write_text("raise RuntimeError('ran')\n", encoding="utf-8")
        (addon / "module.py").write_text(f"NAME = {name!r}\n", encoding="utf-8")
    (tmp_path / "anki_shared" / "pkg").mkdir(parents=True)
    (tmp_path / "anki_shared" / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "not_an_addon").mkdir()
    return tmp_path


@pytest.fixture
def names(monkeypatch):
    """Module names of this test only, and sys.modules put back after it."""
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    suffix = uuid.uuid4().hex[:8]
    return lambda name: f"{name}_{suffix}"


def test_the_addons_are_the_directories_holding_a_build_json(root):
    assert packages.addon_names(root) == ["fresh", "linked"]


def test_an_addon_s_modules_import_without_its_init(root, names, monkeypatch):
    name = names("fresh")
    (root / name).mkdir()
    (root / name / "module.py").write_text("NAME = 'fresh'\n", encoding="utf-8")
    (root / name / "__init__.py").write_text("raise RuntimeError('ran')\n", encoding="utf-8")

    packages.register_addon(root, name)

    module = __import__(f"{name}.module", fromlist=["NAME"])
    assert module.NAME == "fresh"


def test_an_addon_not_linked_reaches_anki_shared_as_its_shared(root, names):
    name = names("fresh")
    (root / name).mkdir()

    addon = packages.register_addon(root, name)

    pkg = __import__(f"{name}.shared.pkg", fromlist=["VALUE"])
    assert pkg.VALUE == 1
    assert addon.shared is sys.modules[f"{name}.shared"]


def test_a_linked_addon_s_shared_is_its_own(root, names):
    name = names("linked")
    (root / name / "shared").mkdir(parents=True)

    addon = packages.register_addon(root, name)

    assert not hasattr(addon, "shared")
    assert f"{name}.shared" not in sys.modules


def test_keep_existing_leaves_a_registered_package_as_it_is(root, names):
    name = names("fresh")
    first = packages.register_package(name, root / "fresh")

    assert packages.register_package(name, root / "linked", keep_existing=True) is first
    assert packages.register_package(name, root / "linked") is not first
