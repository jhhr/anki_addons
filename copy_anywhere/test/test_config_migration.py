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
from copy_anywhere.logic.definition_migration import SYNTAX_VERSION_LEGACY
from copy_anywhere.logic.definition_schema import is_format_2, walk_stages


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


def no_legacy_syntax(definition) -> bool:
    """Whether a stored definition has any format-1 marker left anywhere in it."""
    for stage in walk_stages(definition.get("stages") or []):
        if "legacy_source" in stage or "legacy_destination" in stage:
            return False
        expressions = [stage.get(key) for key in ("value", "query", "predicate", "filename",
                                                  "content", "initial")]
        expressions.extend(write.get("value") for write in stage.get("fields") or [])
        for expression in expressions:
            if isinstance(expression, dict) and (
                "syntax_version" in expression or "legacy_isolated_variables" in expression
            ):
                return False
    return True


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


class TestRetiringFormat1Syntax:
    """The 0.4.0 migration: no stored expression still speaks format 1 (§11).

    0.3.0 turned the definitions into stages but left their references as format 1 wrote
    them -- a bare `{{Word}}` meaning a field of whichever note the stage happened to read,
    recorded in `legacy_source` / `legacy_destination`. Nothing resolves those any more, so
    a config an earlier start of this version already staged has to be rewritten too.
    """

    def a_staged_definition_that_still_speaks_format_1(self):
        """What 0.3.0 stored: staged, but with the old syntax inside the expressions."""
        definition = d.staged(
            definition_name="old",
            stages=[
                d.edit_note(
                    "trigger",
                    fields=[d.write("Note", d.text("{{Word}}"))],
                )
            ],
        )
        definition["migrated_from_format"] = 1
        edit = definition["stages"][0]
        edit["legacy_source"] = {"binding": "trigger"}
        edit["fields"][0]["value"]["syntax_version"] = SYNTAX_VERSION_LEGACY
        return definition

    def test_a_stored_definition_is_promoted_where_it_stands(self, config, stub_mw):
        config["copy_definitions"] = [self.a_staged_definition_that_still_speaks_format_1()]
        config["version"] = "0.3.0"

        migrate_config()

        edit = stored(stub_mw)["copy_definitions"][0]["stages"][0]
        assert "legacy_source" not in edit
        assert edit["fields"][0]["value"]["text"] == "{{trigger.Word}}"
        assert "syntax_version" not in edit["fields"][0]["value"]
        assert stored(stub_mw)["version"] == CONFIG_VERSION

    def test_a_promoted_definition_keeps_its_effects_up_to_date(self, config, stub_mw):
        # Promotion rewrites the expressions the analyser reads, and nothing on the load
        # path recomputes `effects` -- only a save does.
        config["copy_definitions"] = [self.a_staged_definition_that_still_speaks_format_1()]
        config["version"] = "0.3.0"

        migrate_config()

        effects = stored(stub_mw)["copy_definitions"][0]["effects"]
        assert effects["edits_trigger"] is True

    def test_a_format_1_config_arrives_promoted_in_one_pass(self, config, stub_mw):
        # The fresh path goes through the migrator, which promotes as its last act; the
        # 0.4.0 step then has nothing left to do.
        config["copy_definitions"] = [
            d.within_note(
                definition_name="w",
                field_to_field_defs=[d.field_to_field("Note", copy_from_text="{{Word}}")],
            )
        ]

        migrate_config()

        definition = stored(stub_mw)["copy_definitions"][0]
        assert no_legacy_syntax(definition)
        assert definition["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Word}}"

    def test_a_definition_that_never_spoke_format_1_is_untouched(self, config, stub_mw):
        authored = d.staged(stages=[d.variable("M", d.text("{{trigger.Word}}"))])
        config["copy_definitions"] = [authored]
        config["version"] = "0.3.0"

        migrate_config()

        assert stored(stub_mw)["copy_definitions"][0]["stages"] == authored["stages"]

    def test_a_store_holding_both_kinds_comes_out_all_promoted(self, config, stub_mw):
        # What a user who started Anki once on 0.3.0 and then authored a definition has:
        # one of each, side by side. The step runs over every definition rather than
        # picking the ones with a marker, so neither is the odd one out.
        authored = d.staged(
            definition_name="new",
            stages=[d.edit_note("trigger", [d.write("Note", d.text("{{trigger.Word}}"))])],
        )
        config["copy_definitions"] = [
            self.a_staged_definition_that_still_speaks_format_1(),
            authored,
        ]
        config["version"] = "0.3.0"

        migrate_config()

        definitions = stored(stub_mw)["copy_definitions"]
        assert [one["definition_name"] for one in definitions] == ["old", "new"]
        assert all(no_legacy_syntax(one) for one in definitions)
        assert [
            one["stages"][0]["fields"][0]["value"]["text"] for one in definitions
        ] == ["{{trigger.Word}}", "{{trigger.Word}}"]

    def test_a_hand_edited_definition_with_no_source_binding_is_promoted_anyway(
        self, config, stub_mw
    ):
        # `legacy_source` is how the migrator recorded which note a bare name meant, but it
        # is only ever written where it differs from the default. A stage without one -- a
        # definition edited by hand, or one whose stage always read the trigger -- has to
        # come out promoted just the same, reading the trigger, and a variable's result
        # still has to survive as the binding it is rather than becoming a field.
        definition = d.staged(
            definition_name="hand",
            stages=[
                d.variable("v", d.text("{{Word}}")),
                d.edit_note("trigger", [d.write("Note", d.text("{{v}}/{{Meaning}}"))]),
            ],
        )
        definition["migrated_from_format"] = 1
        definition["stages"][0]["value"]["syntax_version"] = SYNTAX_VERSION_LEGACY
        definition["stages"][1]["fields"][0]["value"]["syntax_version"] = (
            SYNTAX_VERSION_LEGACY
        )
        config["copy_definitions"] = [definition]
        config["version"] = "0.3.0"

        migrate_config()

        promoted = stored(stub_mw)["copy_definitions"][0]
        assert no_legacy_syntax(promoted)
        assert promoted["stages"][0]["value"]["text"] == "{{trigger.Word}}"
        assert promoted["stages"][1]["fields"][0]["value"]["text"] == (
            "{{v}}/{{trigger.Meaning}}"
        )

    def test_a_definition_the_step_cannot_promote_is_kept_rather_than_dropped(
        self, config, stub_mw
    ):
        # Only a definition in stages can be promoted, and 0.3.0 is all or nothing, so this
        # is a store someone edited by hand. The step leaves such a definition exactly as it
        # found it -- the one thing it must not do is quietly lose it.
        config["copy_definitions"] = [
            d.within_note(definition_name="never staged"),
            d.staged(definition_name="staged", stages=[]),
        ]
        config["version"] = "0.3.0"

        migrate_config()

        definitions = stored(stub_mw)["copy_definitions"]
        assert [one["definition_name"] for one in definitions] == [
            "never staged",
            "staged",
        ]
        assert not is_format_2(definitions[0])


class TestIdCarryingReferences:
    """The 0.5.0 migration: the trigger and card-type slots become structured references.

    Only the shape changes here. `migrate_config()` runs at import time, before `mw.col`
    exists, so no name can be resolved to an id yet and every one comes out null; the
    editor binds them when the definition is saved.
    """

    def a_0_4_0_definition(self):
        """What 0.4.0 stored: bare names in the slots that now carry ids."""
        definition = d.staged(
            definition_name="old",
            stages=[
                d.edit_note(
                    "trigger",
                    fields=[d.write("Note", d.text("{{trigger.Word}}"))],
                    card_actions=[d.card_action("CA Vocab", "Recognition", set_flag=2)],
                )
            ],
        )
        definition["triggers"]["note_types"] = ["CA Vocab"]
        definition["triggers"]["deck_names"] = ["JP vocab"]
        return definition

    def test_the_trigger_names_become_references_with_no_id(self, config, stub_mw):
        config["copy_definitions"] = [self.a_0_4_0_definition()]
        config["version"] = "0.4.0"

        migrate_config()

        triggers = stored(stub_mw)["copy_definitions"][0]["triggers"]
        assert triggers["note_types"] == [{"id": None, "name": "CA Vocab"}]
        assert triggers["deck_names"] == [{"id": None, "name": "JP vocab"}]

    def test_a_card_action_names_its_card_type_as_a_reference(self, config, stub_mw):
        config["copy_definitions"] = [self.a_0_4_0_definition()]
        config["version"] = "0.4.0"

        migrate_config()

        action = stored(stub_mw)["copy_definitions"][0]["stages"][0]["card_actions"][0]
        assert action["card_type"] == {
            "note_type_id": None,
            "template_id": None,
            "name": "CA Vocab<::>Recognition",
        }
        assert "card_type_name" not in action

    def test_the_version_records_that_the_step_ran(self, config, stub_mw):
        config["copy_definitions"] = [self.a_0_4_0_definition()]
        config["version"] = "0.4.0"

        migrate_config()

        assert stored(stub_mw)["version"] == CONFIG_VERSION

    def test_running_it_again_leaves_the_bound_ids_alone(self, config, stub_mw):
        definition = self.a_0_4_0_definition()
        definition["triggers"]["note_types"] = [{"id": 42, "name": "CA Vocab"}]
        config["copy_definitions"] = [definition]
        config["version"] = "0.4.0"

        migrate_config()
        stub_mw.addonManager.configs["copy_anywhere"]["version"] = "0.4.0"
        migrate_config()

        triggers = stored(stub_mw)["copy_definitions"][0]["triggers"]
        assert triggers["note_types"] == [{"id": 42, "name": "CA Vocab"}]

    def test_an_empty_card_type_name_becomes_no_reference_at_all(self, config, stub_mw):
        """An `edit_card` stage's actions name no card type -- the stage named the card.

        The old editor wrote one empty `card_type_name` string for them. A reference with
        no name and no ids is not a reference to anything, so the slot comes out as the
        `None` the editor itself writes there now.
        """
        action = d.card_action("CA Vocab", "Recognition", set_flag=2)
        action["card_type_name"] = ""
        definition = d.staged(
            definition_name="single card",
            stages=[
                d.card_query("cards", "deck:x"),
                d.for_each_card("cards", [d.edit_card("card", [action])]),
            ],
        )
        config["copy_definitions"] = [definition]
        config["version"] = "0.4.0"

        migrate_config()

        stage = stored(stub_mw)["copy_definitions"][0]["stages"][1]["body"][0]
        assert stage["card_actions"][0]["card_type"] is None
        assert "card_type_name" not in stage["card_actions"][0]

    def test_a_format_1_config_arrives_structured_in_one_pass(self, config, stub_mw):
        config["copy_definitions"] = [
            d.within_note(
                definition_name="w",
                card_actions=[d.card_action("CA Vocab", "Recognition", set_flag=2)],
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            )
        ]
        config["version"] = "0.1.0"

        migrate_config()

        definition = stored(stub_mw)["copy_definitions"][0]
        assert definition["triggers"]["note_types"] == [{"id": None, "name": "CA Vocab"}]
        assert "card_type" in definition["stages"][0]["card_actions"][0]


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

    def test_the_reason_is_reported_rather_than_swallowed(self, config, stub_mw, broken, logger):
        config["copy_definitions"] = [broken]

        migrate_config()

        # Logged through the addon's logger, where the `logger` fixture's handler is; at
        # startup the migration opens an operation log of its own for it to land in.
        reported = "\n".join(logger.errors)
        assert "broken" in reported
        assert "could not" in reported


class TestAnEmptyConfig:
    def test_a_config_with_no_definitions_still_reaches_the_latest_version(
        self, config, stub_mw
    ):
        migrate_config()

        assert stored(stub_mw)["version"] == CONFIG_VERSION
        assert stored(stub_mw)["copy_definitions"] == []
        assert PRE_STAGE_MIGRATION_KEY not in stored(stub_mw)
