"""build.py dist: the folder Anki installs each .ankiaddon into.

`--local-names` exists so that an addon installed from a file on a device without a clone lands
in the folder it has on this one, which is the name Addon Config Sync keeps its config under.
Anki's own installer decides that folder, so it is what installs the zips here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki_shared.testing import anki_stubs

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def build(tmp_path: Path, monkeypatch):
    spec = importlib.util.spec_from_file_location("_build_for_dist_test", ROOT / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(module, "LOCAL_DIST_DIR", tmp_path / "dist" / "local")
    return module


@pytest.fixture
def addon(build, tmp_path: Path):
    path = tmp_path / "repo" / "my_addon"
    path.mkdir(parents=True)
    meta = {"package": "my_addon", "name": "My Addon", "human_version": "1.2.3"}
    (path / "build.json").write_text(json.dumps(meta), "utf-8")
    (path / "__init__.py").write_text("", "utf-8")
    return build.Addon(path, meta, "my_folder_name")


@pytest.mark.parametrize(
    "local_names, written, folder",
    [
        (False, "dist/my_addon-1.2.3.ankiaddon", "my_addon"),
        (True, "dist/local/my_addon-1.2.3.ankiaddon", "my_folder_name"),
    ],
)
def test_anki_installs_the_zip_into_the_folder_it_is_built_for(
    build, addon, tmp_path: Path, local_names: bool, written: str, folder: str
):
    if anki_stubs.is_installed():
        pytest.skip("needs Anki's own add-on installer")
    from aqt.addons import AddonManager, InstallOk

    build.cmd_dist([addon], local_names=local_names)

    addons21 = tmp_path / "addons21"
    addons21.mkdir()
    # Only what install() reaches for: the add-ons folder. The constructor wires up menus.
    mw: Any = SimpleNamespace(pm=SimpleNamespace(addonFolder=lambda: str(addons21)))
    manager = AddonManager.__new__(AddonManager)
    manager.mw = mw
    result = manager.install(str(tmp_path / written))

    assert isinstance(result, InstallOk)
    assert sorted(p.name for p in addons21.iterdir()) == [folder]
    assert (addons21 / folder / "__init__.py").is_file()
