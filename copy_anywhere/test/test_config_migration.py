"""The startup migration: format-1 definitions become stages, once, with a way back (§11).

`migrate_config()` runs on every Anki start, before anything can read a definition. After it
there is only format 2 to execute, so the two things worth pinning here are that it converts
everything and that it refuses to record a version the stored config never reached.

These drive the stubbed addon config directly rather than a collection: the migration is
pure dictionary work, and the analyser it calls to fill in `effects` never touches one.
"""

import pytest

import definitions as d
from conftest import DEFAULT_CONFIG
from copy_anywhere.configuration import (
    CONFIG_VERSION,
    PRE_STAGE_MIGRATION_KEY,
    migrate_config,
)
from copy_anywhere.logic.definition_schema import is_format_2


@pytest.fixture
def config(stub_mw):
    """The stored config, as `migrate_config` will find it and leave it."""
    stored = dict(DEFAULT_CONFIG)
    stored["copy_definitions"] = []
    stub_mw.addonManager.configs["copy_anywhere"] = stored
    return stored


def stored(stub_mw) -> dict:
    """What is in the config now. `save()` replaces the dict, so re-read it every time."""
    return stub_mw.addonManager.configs["copy_anywhere"]


class TestConvertingTheStoredDefinitions:
    def test_a_format_1_config_becomes_format_2(self, config, stub_mw):
        config["copy_definitions"] = [
            d.within_note(definition_name="w"),
            d.source_to_destinations(definition_name="s", copy_from_cards_query="deck:x"),
        ]

        migrate_config()

        definitions = stored(stub_mw)["copy_definitions"]
        assert [definition["definition_name"] for definition in definitions] == ["w", "s"]
        assert all(is_format_2(definition) for definition in definitions)

    def test_every_migrated_definition_carries_its_effects(self, config, stub_mw):
        config["copy_definitions"] = [
            d.within_note(
                definition_name="w",
                field_to_field_defs=[d.field_to_field("Note", copy_from_text="{{Word}}")],
            )
        ]

        migrate_config()

        effects = stored(stub_mw)["copy_definitions"][0]["effects"]
        # A definition that only writes the trigger note is the add-note-compatible case, and
        # the hooks read exactly this stored flag rather than analysing anything (§8).
        assert effects["edits_trigger"] is True
        assert effects["add_note_compatible"] is True

    def test_the_version_records_that_the_migration_ran(self, config, stub_mw):
        config["copy_definitions"] = [d.within_note()]

        migrate_config()

        assert stored(stub_mw)["version"] == CONFIG_VERSION

    def test_a_definition_with_no_guid_gets_one_before_it_is_staged(self, config, stub_mw):
        # The 0.2.0 migration has to run first: the stage migrator derives its synthesized
        # stage guids from the definition's own, so a definition without one cannot be stable.
        definition = d.within_note()
        del definition["guid"]
        config["copy_definitions"] = [definition]
        config["version"] = "0.1.0"

        migrate_config()

        migrated = stored(stub_mw)["copy_definitions"][0]
        assert migrated["guid"]
        assert is_format_2(migrated)


    def test_a_process_chain_stored_as_null_does_not_stop_the_migration(
        self, config, stub_mw
    ):
        # The format-1 editor writes `null` rather than leaving the key out, so a config
        # last opened before 0.2.0 reaches the guid migration with one in every field def.
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", copy_from_text="{{Word}}")]
        )
        assert definition["field_to_field_defs"][0]["process_chain"] is None
        config["copy_definitions"] = [definition]
        config["version"] = "0.1.0"

        migrate_config()

        assert stored(stub_mw)["version"] == CONFIG_VERSION


class TestTheBackup:
    def test_the_originals_are_kept_under_the_backup_key(self, config, stub_mw):
        config["copy_definitions"] = [d.within_note(definition_name="w")]

        migrate_config()

        backup = stored(stub_mw)[PRE_STAGE_MIGRATION_KEY]
        assert [definition["definition_name"] for definition in backup] == ["w"]
        assert backup[0]["copy_mode"] == "Within note"

    def test_the_backup_is_not_the_same_object_as_the_stored_definitions(
        self, config, stub_mw
    ):
        config["copy_definitions"] = [d.within_note()]

        migrate_config()

        after = stored(stub_mw)
        assert after[PRE_STAGE_MIGRATION_KEY][0] is not after["copy_definitions"][0]

    def test_a_config_that_was_already_format_2_gets_no_backup(self, config, stub_mw):
        config["copy_definitions"] = [d.staged(stages=[d.variable("M", d.text("x"))])]

        migrate_config()

        # There is nothing to go back to, and writing the key anyway would suggest there is.
        assert PRE_STAGE_MIGRATION_KEY not in stored(stub_mw)

    def test_a_second_run_leaves_the_first_backup_alone(self, config, stub_mw):
        config["copy_definitions"] = [d.within_note()]
        migrate_config()
        first = stored(stub_mw)[PRE_STAGE_MIGRATION_KEY]

        stored(stub_mw)["version"] = "0.2.0"
        migrate_config()

        # Overwriting it on a later run would replace the originals with their own migration,
        # which is the one thing the backup exists to protect against.
        assert stored(stub_mw)[PRE_STAGE_MIGRATION_KEY] == first
        assert first[0]["copy_mode"] == "Within note"


    def test_restoring_the_backup_migrates_it_afresh_and_keeps_the_backup(
        self, config, stub_mw
    ):
        # The documented way back: copy the backup over `copy_definitions` and put the
        # version back. The next start migrates them again -- there is no executor for
        # format 1 -- and the originals stay where they were for the next attempt.
        config["copy_definitions"] = [d.within_note(definition_name="w")]
        migrate_config()
        originals = stored(stub_mw)[PRE_STAGE_MIGRATION_KEY]

        stored(stub_mw)["copy_definitions"] = [dict(one) for one in originals]
        stored(stub_mw)["version"] = "0.2.0"
        migrate_config()

        after = stored(stub_mw)
        assert is_format_2(after["copy_definitions"][0])
        assert after[PRE_STAGE_MIGRATION_KEY] == originals
        assert after[PRE_STAGE_MIGRATION_KEY][0]["copy_mode"] == "Within note"


class TestRunningItAgain:
    def test_an_already_migrated_config_is_left_alone(self, config, stub_mw):
        config["copy_definitions"] = [d.within_note()]
        migrate_config()
        after_first = stored(stub_mw)["copy_definitions"]

        migrate_config()

        assert stored(stub_mw)["copy_definitions"] == after_first

    def test_a_definition_added_in_format_2_survives_a_mixed_config(self, config, stub_mw):
        authored = d.staged(stages=[d.variable("M", d.text("x"))], definition_name="new")
        config["copy_definitions"] = [d.within_note(definition_name="old"), authored]

        migrate_config()

        definitions = stored(stub_mw)["copy_definitions"]
        assert [definition["definition_name"] for definition in definitions] == ["old", "new"]
        assert definitions[1]["stages"][0]["result"] == "M"


class TestWhenSomethingCannotBeMigrated:
    @pytest.fixture
    def broken(self):
        """A definition naming no copy mode, which format 1 could not have run either."""
        definition = d.within_note(definition_name="broken")
        del definition["copy_mode"]
        return definition

    def test_the_stored_definitions_are_left_exactly_as_they_were(
        self, config, stub_mw, broken
    ):
        config["copy_definitions"] = [d.within_note(definition_name="fine"), broken]

        migrate_config()

        definitions = stored(stub_mw)["copy_definitions"]
        # Not one of them is converted: saving the good half would drop the broken one from
        # a config the user can still fix by hand.
        assert [definition["definition_name"] for definition in definitions] == [
            "fine",
            "broken",
        ]
        assert not any(is_format_2(definition) for definition in definitions)

    def test_the_version_stays_behind_so_the_next_start_tries_again(
        self, config, stub_mw, broken
    ):
        config["copy_definitions"] = [broken]

        migrate_config()

        assert stored(stub_mw)["version"] != CONFIG_VERSION

    def test_nothing_is_backed_up_when_nothing_was_converted(self, config, stub_mw, broken):
        config["copy_definitions"] = [broken]

        migrate_config()

        assert PRE_STAGE_MIGRATION_KEY not in stored(stub_mw)

    def test_the_guid_half_of_the_migration_is_still_recorded(self, config, stub_mw, broken):
        config["copy_definitions"] = [broken]
        config["version"] = "0.1.0"

        migrate_config()

        # The guids were written and saved, so the version has to say so; only the staged
        # step is left for the next start.
        assert stored(stub_mw)["version"] == "0.2.0"
        assert stored(stub_mw)["copy_definitions"][0]["guid"]

    def test_the_reason_is_reported_rather_than_swallowed(self, config, stub_mw, broken, capsys):
        config["copy_definitions"] = [broken]

        migrate_config()

        printed = capsys.readouterr().out
        assert "broken" in printed
        assert "could not" in printed


class TestAnEmptyConfig:
    def test_a_config_with_no_definitions_still_reaches_the_latest_version(
        self, config, stub_mw
    ):
        migrate_config()

        assert stored(stub_mw)["version"] == CONFIG_VERSION
        assert stored(stub_mw)["copy_definitions"] == []
        assert PRE_STAGE_MIGRATION_KEY not in stored(stub_mw)
