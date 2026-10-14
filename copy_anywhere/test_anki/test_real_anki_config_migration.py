"""`migrate_config()` against the meta.json files Anki itself leaves, read by its own
`AddonManager`.

A start must write nothing into a meta.json that holds no config. Addon Config Sync sends a
meta.json that has one over the copy the user's other devices read theirs from, so the
defaults a start wrote into a new install's file, or into one Anki could not read, replaced
everyone's definitions with none. `test/test_config_migration.py` pins the rule against a
stub; these pin what `addonMeta` and `getConfig` make of the files the rule is about.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import ADDON_PACKAGE, BASE_CONFIG


@pytest.fixture
def meta_json(real_mw, anki_session):
    """Where the addon's meta.json goes, beside the shipped config.json; removed afterwards."""
    with anki_session.addon_config_created(
        package_name=ADDON_PACKAGE, default_config=dict(BASE_CONFIG)
    ):
        path = Path(real_mw.addonManager.addonsFolder(ADDON_PACKAGE), "meta.json")
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)


def test_a_new_installs_meta_json_gets_no_config(real_mw, meta_json):
    from copy_anywhere.configuration import migrate_config

    # What Anki's install writes there: the manifest's keys and no config
    real_mw.addonManager.writeAddonMeta(ADDON_PACKAGE, {"name": "CopyAnywhere", "mod": 1})
    before = meta_json.read_bytes()

    migrate_config()

    assert meta_json.read_bytes() == before


def test_a_meta_json_anki_cannot_read_is_left_for_a_sync_to_repair(real_mw, meta_json):
    from copy_anywhere.configuration import migrate_config

    cut_short = '{"config": {"copy_definitions": [{"definition_name": "mine", "gu'
    meta_json.write_text(cut_short, encoding="utf-8")
    # Anki reads it as no meta.json at all, and answers getConfig with config.json
    assert real_mw.addonManager.addonMeta(ADDON_PACKAGE) == {}

    migrate_config()

    assert meta_json.read_text(encoding="utf-8") == cut_short


def test_an_addon_copied_in_without_a_meta_json_gets_none(real_mw, meta_json):
    from copy_anywhere.configuration import migrate_config

    migrate_config()

    assert not meta_json.exists()
