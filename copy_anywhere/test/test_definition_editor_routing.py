"""Which editor a definition opens in, now that there is only one.

The startup migration means a stored definition is format 2 by the time the picker sees it.
The one case left is a config the migration could not convert, whose definitions are still
format 1: those go through the same pure migrator on the way to the stage editor rather than
opening in an editor that no longer exists.
"""

import pytest

import definitions as d
from copy_anywhere.configuration import Config
from copy_anywhere.logic.definition_schema import is_format_2
from copy_anywhere.ui.pick_copy_definition_dialog import PickCopyDefinitionDialog


@pytest.fixture
def picker(col, qapp, stub_mw, monkeypatch):
    """The picker, with the stage editor replaced by a recorder of what it was handed."""
    opened: list = []

    class FakeDialog:
        def __init__(self, parent, definition, all_definitions=None):
            opened.append(definition)
            self.definition = definition

        def exec(self):
            return 1

        def get_copy_definition(self):
            return self.definition

    monkeypatch.setattr(
        "copy_anywhere.ui.pick_copy_definition_dialog.EditStagedDefinitionDialog", FakeDialog
    )
    dialog = PickCopyDefinitionDialog.__new__(PickCopyDefinitionDialog)
    dialog.opened = opened
    return dialog


def config_with(stub_mw, definitions):
    stub_mw.addonManager.configs["copy_anywhere"] = {
        "log_level": "error",
        "copy_fields_shortcut": "x",
        "copy_definitions": definitions,
    }
    config = Config()
    config.load()
    return config


def test_a_format_2_definition_opens_as_it_is(picker, stub_mw):
    definition = d.staged(stages=[d.variable("M", d.text("x"))])
    config = config_with(stub_mw, [definition])

    picker.run_definition_editor(definition, config)

    assert picker.opened == [definition]


def test_a_new_definition_opens_the_stage_editor_with_nothing(picker, stub_mw):
    config = config_with(stub_mw, [])

    picker.run_definition_editor(None, config)

    assert picker.opened == [None]


def test_a_format_1_definition_is_converted_on_the_way_in(picker, stub_mw):
    definition = d.within_note(definition_name="old")
    config = config_with(stub_mw, [definition])

    picker.run_definition_editor(definition, config)

    handed_over = picker.opened[0]
    assert is_format_2(handed_over)
    assert handed_over["definition_name"] == "old"
    # And the stored one is untouched: the conversion is only saved if the user saves.
    assert definition["copy_mode"] == "Within note"


def test_a_definition_that_cannot_be_converted_is_reported_rather_than_opened(
    picker, stub_mw, monkeypatch
):
    said: list = []
    monkeypatch.setattr(
        "copy_anywhere.ui.pick_copy_definition_dialog.showInfo", said.append
    )
    broken = d.within_note()
    del broken["copy_mode"]
    config = config_with(stub_mw, [broken])

    assert picker.run_definition_editor(broken, config) is None

    assert picker.opened == []
    assert any("could not be converted" in message for message in said)
