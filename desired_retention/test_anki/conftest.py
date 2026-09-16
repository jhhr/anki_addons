"""Fixtures for desired_retention tests that need a real, running Anki."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from anki_shared.testing import real_anki, running_anki

REBOUND_PACKAGES = ["anki_shared"]
NOTE_TYPE = "DR Basic"
ADDON_MODULE_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
ADDON_MODULE_NAME = "desired_retention_runtime_test"


def load_addon() -> object:
    from aqt.browser.card_info import CardInfoDialog

    if (
        CardInfoDialog._setup_ui.__module__ == ADDON_MODULE_NAME
        and CardInfoDialog.update_card.__module__ == ADDON_MODULE_NAME
    ):
        return sys.modules[ADDON_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(
        ADDON_MODULE_NAME, ADDON_MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[ADDON_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def restore_stub_mw() -> Iterator[Any]:
    from aqt.browser.card_info import CardInfoDialog

    original_setup_ui = CardInfoDialog._setup_ui
    original_update_card = CardInfoDialog.update_card
    try:
        with running_anki.stub_mw_restored(REBOUND_PACKAGES, []) as stub:
            yield stub
    finally:
        CardInfoDialog._setup_ui = original_setup_ui
        CardInfoDialog.update_card = original_update_card


@pytest.fixture
def real_mw(anki_session, restore_stub_mw) -> Iterator[Any]:
    with running_anki.main_window(anki_session, REBOUND_PACKAGES) as mw:
        real_anki.make_note_type(mw.col, NOTE_TYPE, ["Front", "Back"])
        yield mw
