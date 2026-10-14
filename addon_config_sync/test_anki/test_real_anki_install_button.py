"""The manager dialog's Install button: an add-on another device has, installed on this one.

A media copy names the add-on's folder and holds its settings, and the folder of an add-on
from AnkiWeb is named by its AnkiWeb id. That id is what Anki's own Get Add-ons installs by
(`aqt.addons.download_addons`), and the button goes by it too. An add-on in a folder of any
other name was installed from a file, which nothing here has.

The devices, AnkiWeb and media syncs are `devices.py`'s.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from typing import Any, Iterator

import aqt.addons
import pytest
from aqt.qt import QPushButton
from aqt.utils import tr

from .devices import (
    ADDON_ID,
    FOLDER,
    T1,
    T2,
    AnkiDevice,
    FakeAnkiWeb,
    Server,
    row_of,
    row_status,
    write_file,
)

OTHER_ID = 2345678901
NOT_FROM_ANKIWEB = "an-addon-from-github"


@pytest.fixture(autouse=True)
def downloads_from(monkeypatch: pytest.MonkeyPatch, ankiweb: FakeAnkiWeb) -> FakeAnkiWeb:
    """What Anki downloads comes from `ankiweb`."""
    monkeypatch.setattr(aqt.addons, "HttpClient", ankiweb.http_client)
    return ankiweb


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch, addon: Any) -> list[tuple[str, str]]:
    """The title and text of each message box that would have been shown, Anki's and the
    add-on's. A message box waits for a click, which nothing here gives."""
    messages: list[tuple[str, str]] = []

    def show(text: str, *args: Any, **kwargs: Any) -> None:
        messages.append((kwargs.get("title", "Anki"), str(text)))

    monkeypatch.setattr(aqt.addons, "showInfo", show)
    monkeypatch.setattr(aqt.addons, "showWarning", show)
    monkeypatch.setattr(sys.modules[f"{addon.__name__}.config_manager_dialog"], "showInfo", show)
    return messages


@contextmanager
def manager_of(device: AnkiDevice) -> Iterator[Any]:
    """Tools → Manage Addon Configs, on this device, until the block ends."""
    with device.active():
        module = sys.modules[f"{device.acs.__name__}.config_manager_dialog"]
        dialog = module.open_addon_config_manager(blocking=False)
        try:
            yield dialog
        finally:
            dialog.close()


def install_button(dialog: Any, addon_id: str) -> QPushButton:
    actions = dialog.table.cellWidget(row_of(dialog, addon_id), 2)
    return next(b for b in actions.findChildren(QPushButton) if b.toolTip() == "Install addon")


def copy_from_another_device(device: AnkiDevice, folder: str) -> None:
    """The media copy of an add-on this device does not have, as a media sync brings it."""
    copy = {"name": "Tiny Addon", "mod": T1 - 1, "config": {"speed": 22}}
    write_file(device.media / f"_{folder}_meta.json", json.dumps(copy).encode("utf-8"))


def test_the_install_button_installs_from_ankiweb_an_addon_another_device_has(
    make_device, ankiweb, shown
):
    """Anki says what it installed and asks for a restart, as after Get Add-ons. The new
    install has no config of its own until the next sync gives it that of the copy, beside
    the record of its own install."""
    ankiweb.publish("1.0", T1)
    laptop = make_device("laptop")
    copy_from_another_device(laptop, FOLDER)

    with manager_of(laptop) as dialog:
        assert row_status(dialog, FOLDER) == "Not installed"
        install_button(dialog, FOLDER).click()
        laptop.qtbot.waitUntil(lambda: bool(shown), timeout=15000)
        assert row_status(dialog, FOLDER) == "Changed"

    [(title, text)] = shown
    assert text.startswith(tr.addons_download_complete_please_restart_anki_to())
    assert f"{ADDON_ID}: {tr.addons_installed_successfully()}" in text
    assert ankiweb.downloads == [ADDON_ID]
    assert laptop.code_version() == "1.0"
    assert laptop.meta()["mod"] == T1 and "config" not in laptop.meta()

    laptop.syncs(Server())
    assert laptop.meta()["config"] == {"speed": 22} and laptop.meta()["mod"] == T1


def test_the_install_button_says_why_it_cannot_install_an_addon_not_from_ankiweb(
    make_device, ankiweb, shown
):
    laptop = make_device("laptop")
    copy_from_another_device(laptop, NOT_FROM_ANKIWEB)

    with manager_of(laptop) as dialog:
        install_button(dialog, NOT_FROM_ANKIWEB).click()
        assert row_status(dialog, NOT_FROM_ANKIWEB) == "Not installed"

    [(title, text)] = shown
    assert title == "Install Addon" and NOT_FROM_ANKIWEB in text and "AnkiWeb" in text
    assert ankiweb.downloads == []
    assert not (laptop.addons / NOT_FROM_ANKIWEB).exists()


def test_installing_the_selected_addons_downloads_those_from_ankiweb_in_one_go(
    make_device, ankiweb, shown
):
    ankiweb.publish("1.0", T1)
    ankiweb.publish("2.0", T2, addon_id=OTHER_ID)
    laptop = make_device("laptop")
    for folder in (FOLDER, str(OTHER_ID), NOT_FROM_ANKIWEB):
        copy_from_another_device(laptop, folder)

    with manager_of(laptop) as dialog:
        dialog.select_all_checkbox.setChecked(True)
        dialog.bulk_install_btn.click()
        laptop.qtbot.waitUntil(lambda: len(shown) == 2, timeout=15000)
        assert row_status(dialog, FOLDER) == row_status(dialog, str(OTHER_ID)) == "Changed"
        assert row_status(dialog, NOT_FROM_ANKIWEB) == "Not installed"

    (title, text), (_, log) = shown
    assert title == "Install Addon" and NOT_FROM_ANKIWEB in text
    for addon_id in (ADDON_ID, OTHER_ID):
        assert f"{addon_id}: {tr.addons_installed_successfully()}" in log
    assert ankiweb.downloads == [ADDON_ID, OTHER_ID]
