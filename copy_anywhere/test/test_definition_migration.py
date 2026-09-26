"""Tests for the pure format-1 -> format-2 migrator.

These run without a collection: the migrator loads no config, never touches `mw`, and is a
function from one dict to another. What they pin is the mapping itself -- which stages a
legacy definition becomes, in what order, carrying which compatibility markers -- because
that mapping is the only reason the characterization suite can keep asserting format-1
behaviour while the staged executor is the thing running.
"""

import pytest

import definitions as d
from copy_anywhere.logic.definition_migration import (
    LEGACY_QUERY_RESULT,
    STAGE_EXPRESSION_KEYS,
    MigrationError,
    migrate_definition_v1_to_v2,
    migrate_definitions,
)
from copy_anywhere.logic.definition_schema import (
    FORMAT_VERSION,
    validate_definition_structure,
    walk_stages,
)
from copy_anywhere.logic.flow_analysis import analyze_definition


def stage_types(stages):
    return [stage["type"] for stage in stages]


def expressions_of(stage):
    """Every expression one stage holds, wherever the stage type keeps it."""
    found = [
        stage[key]
        for key in STAGE_EXPRESSION_KEYS.get(stage.get("type", ""), ())
        if isinstance(stage.get(key), dict)
    ]
    found.extend(
        field_write["value"]
        for field_write in stage.get("fields") or []
        if isinstance(field_write.get("value"), dict)
    )
    return found


def find(stages, stage_type):
    for stage in stages:
        if stage["type"] == stage_type:
            return stage
    raise AssertionError(f"no {stage_type} stage among {stage_types(stages)}")


class TestWithinNote:
    def test_it_becomes_one_edit_of_the_trigger_note(self):
        migrated = migrate_definition_v1_to_v2(
            d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")])
        )
        assert migrated["format_version"] == FORMAT_VERSION
        assert stage_types(migrated["stages"]) == ["edit_note"]
        edit = migrated["stages"][0]
        assert edit["target"] == {"binding": "trigger"}
        # No separate source binding: reading and writing the same note is what makes the
        # stage's own entry snapshot the source, which is what lets two fields swap.
        assert "legacy_source" not in edit
        assert [write["field"] for write in edit["fields"]] == ["Note"]
        # The reference is promoted on the way out: the note `{{Word}}` meant is named, so
        # nothing downstream has to know this definition was ever format 1.
        assert edit["fields"][0]["value"]["text"] == "{{trigger.Word}}"
        assert "syntax_version" not in edit["fields"][0]["value"]

    def test_copy_if_empty_becomes_write_if_empty(self):
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}", copy_if_empty=True)]
            )
        )
        assert migrated["stages"][0]["fields"][0]["write_if"] == "empty"

    def test_tags_and_card_actions_ride_along_on_the_same_stage(self):
        # They target the same note and share its snapshot, so they are one stage, not three.
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
                add_tags=d.quoted_list(["one", "two"]),
                remove_tags=d.quoted_list(["old"]),
                card_actions=[d.card_action("CA Vocab", "Recognition", set_flag=2)],
            )
        )
        edit = migrated["stages"][0]
        assert edit["tags"] == {"add": ["one", "two"], "remove": ["old"]}
        assert len(edit["card_actions"]) == 1
        # Note-level card actions keep their card type selector; `edit_card` is the stage
        # without one. The selector is a reference, with null ids for the same reason the
        # trigger references have them.
        assert edit["card_actions"][0]["card_type"] == {
            "note_type_id": None,
            "template_id": None,
            "name": "CA Vocab<::>Recognition",
        }
        assert "card_type_name" not in edit["card_actions"][0]

    def test_a_file_definition_becomes_a_following_write_file_stage(self):
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
                field_to_file_defs=[d.field_to_file("{{Word}}.txt", "{{Meaning}}")],
            )
        )
        assert stage_types(migrated["stages"]) == ["edit_note", "write_file"]
        write = migrated["stages"][1]
        assert write["overwrite"] is True
        assert write["skip_if_exists"] is False

    def test_a_file_definitions_copy_if_empty_becomes_skip_if_exists(self):
        # Format 1 skipped an existing file silently, which `overwrite: false` does not
        # mean -- that fails the stage -- so it is carried as its own flag.
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_file_defs=[d.field_to_file("f.txt", "x", copy_if_empty=True)],
            )
        )
        write = find(migrated["stages"], "write_file")
        assert (write["overwrite"], write["skip_if_exists"]) == (True, True)


class TestVariables:
    def test_they_come_first_and_keep_their_order(self):
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_field_defs=[d.field_to_field("Note", "{{a}}")],
                field_to_variable_defs=[
                    d.field_to_variable("a", "{{Word}}"),
                    d.field_to_variable("b", "{{Meaning}}"),
                ],
            )
        )
        assert stage_types(migrated["stages"]) == ["variable", "variable", "edit_note"]
        assert [stage["result"] for stage in migrated["stages"][:2]] == ["a", "b"]

    def test_each_one_reads_the_trigger_note_and_nothing_else(self):
        # Format 1 evaluated every variable against the trigger note alone, so none of them
        # could read the ones declared before it. Promotion says that outright -- `{{Word}}`
        # becomes a field of the trigger -- instead of carrying an isolation marker, and a
        # later variable that did mean an earlier one would have kept its bare name.
        migrated = migrate_definition_v1_to_v2(
            d.within_note(field_to_variable_defs=[d.field_to_variable("a", "{{Word}}")])
        )
        value = migrated["stages"][0]["value"]
        assert value["text"] == "{{trigger.Word}}"
        assert "legacy_isolated_variables" not in value


class TestCondition:
    def test_it_wraps_everything_that_follows_it(self):
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
                copy_condition_query="Word:neko",
            )
        )
        assert stage_types(migrated["stages"]) == ["condition"]
        branch = migrated["stages"][0]
        assert stage_types(branch["then"]) == ["edit_note"]
        assert branch["else"] == []
        assert branch["predicate_kind"] == "note_query"
        assert branch["predicate_target"] == {"binding": "trigger"}

    def test_variables_stay_outside_it(self):
        # Format 1 resolved variables before checking the condition, and the condition query
        # itself can read them.
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_variable_defs=[d.field_to_variable("search", "Word:{{Word}}")],
                copy_condition_query="{{search}}",
            )
        )
        assert stage_types(migrated["stages"]) == ["variable", "condition"]

    def test_condition_only_on_sync_is_preserved_as_a_guard(self):
        migrated = migrate_definition_v1_to_v2(
            d.within_note(copy_condition_query="tag:x", condition_only_on_sync=True)
        )
        assert migrated["stages"][0]["only_on_sync"] is True


class TestSourceToDestinations:
    def build(self, **extra):
        return migrate_definition_v1_to_v2(
            d.source_to_destinations(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
                **extra,
            )
        )

    def test_it_becomes_a_query_and_a_loop_that_edits_each_found_note(self):
        migrated = self.build()
        assert stage_types(migrated["stages"]) == ["note_query", "for_each_note"]
        loop = migrated["stages"][1]
        assert loop["input"] == {"binding": LEGACY_QUERY_RESULT}
        edit = loop["body"][0]
        assert edit["target"] == {"binding": "note"}
        # The values come from the trigger note while the note being written is the loop's,
        # which is what `{{Word}}` and `{{__Dest__Note}}` meant in this mode. Promotion
        # spells both out, so no source binding is left on the stage.
        assert "legacy_source" not in edit
        assert edit["fields"][0]["value"]["text"] == "{{trigger.Word}}"

    def test_the_query_is_not_counted_as_sources(self):
        # The trigger note was the one source here; the query found destinations.
        assert self.build()["stages"][0]["counts_as_sources"] is False
        assert self.build()["legacy"]["trigger_is_source"] is True

    def test_an_empty_query_does_not_skip_the_block(self):
        # There was never a zero-source guard in this direction: an empty query just means
        # the destination loop body never runs.
        assert self.build()["stages"][0]["if_empty"] == "continue"


class TestDestinationToSources:
    def build(self, **extra):
        return migrate_definition_v1_to_v2(
            d.destination_to_sources(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
                select_card_separator="+",
                **extra,
            )
        )

    def test_the_many_notes_join_is_synthesized_explicitly(self):
        # Format 2 has no implicit list-to-text conversion, so "one value out of N notes"
        # becomes a list, a loop that stores one value per note, and a reduce that joins.
        migrated = self.build()
        assert stage_types(migrated["stages"]) == [
            "note_query",
            "list_variable",
            "for_each_note",
            "reduce",
            "edit_note",
        ]
        reduce_stage = migrated["stages"][3]
        assert reduce_stage["operation"] == "join"
        assert reduce_stage["separator"] == "+"

    def test_the_edit_reads_the_joined_result(self):
        migrated = self.build()
        edit = migrated["stages"][-1]
        assert edit["target"] == {"binding": "trigger"}
        assert edit["fields"][0]["value"]["text"] == "{{legacy_joined_1}}"

    def test_the_process_chain_stays_on_the_joined_value(self):
        # It ran once, on the joined text, not once per source note.
        process = d.regex_process("a", "b")
        migrated = migrate_definition_v1_to_v2(
            d.destination_to_sources(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[
                    d.field_to_field("Note", "{{Word}}", process_chain=[process])
                ],
            )
        )
        loop = find(migrated["stages"], "for_each_note")
        assert loop["body"][0]["value"]["process_chain"] == []
        assert migrated["stages"][-1]["fields"][0]["value"]["process_chain"] == [process]

    def test_each_field_gets_its_own_join(self):
        migrated = migrate_definition_v1_to_v2(
            d.destination_to_sources(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[
                    d.field_to_field("Note", "{{Word}}"),
                    d.field_to_field("Reading", "{{Meaning}}"),
                ],
            )
        )
        joins = [stage for stage in migrated["stages"] if stage["type"] == "reduce"]
        assert [stage["result"] for stage in joins] == ["legacy_joined_1", "legacy_joined_2"]

    def test_copy_if_empty_gates_the_join_that_feeds_the_write(self):
        # The write declines when its field is filled, but only after the list, loop and
        # reduce in front of it have evaluated the right-hand side once per source note. So
        # they carry the policy and the field it asks about, and the executor asks the same
        # question of them. The store inside the loop does not need it: skipping the loop
        # skips its body.
        migrated = migrate_definition_v1_to_v2(
            d.destination_to_sources(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[
                    d.field_to_field("Note", "{{Word}}"),
                    d.field_to_field("Reading", "{{Meaning}}", copy_if_empty=True),
                ],
            )
        )
        stages = migrated["stages"]
        gated = [stage for stage in stages if stage.get("write_if") == "empty"]
        assert stage_types(gated) == ["list_variable", "for_each_note", "reduce"]
        assert {stage["write_if_field"] for stage in gated} == {"Reading"}
        assert all("write_if" not in stage for stage in stages[1:4]), "the always-write's join"
        loop = gated[1]
        assert "write_if" not in loop["body"][0]
        # The gate must not be mistaken for a stage that may leave its result undefined, or
        # for a key the definition cannot be saved with.
        assert validate_definition_structure(migrated) == []
        assert analyze_definition(migrated).problem_messages() == []

    def test_the_query_is_counted_as_sources(self):
        assert self.build()["stages"][0]["counts_as_sources"] is True
        assert self.build()["legacy"]["trigger_is_source"] is False

    def test_an_empty_query_skips_the_block_unless_the_flag_says_otherwise(self):
        assert self.build()["stages"][0]["if_empty"] == "skip_block"
        assert self.build(run_also_if_no_sources_found=True)["stages"][0]["if_empty"] == (
            "continue"
        )


class TestSelection:
    def selection(self, **extra):
        migrated = migrate_definition_v1_to_v2(
            d.destination_to_sources(copy_from_cards_query="Word:a", **extra)
        )
        return find(migrated["stages"], "note_query")["selection"]

    def test_none_becomes_first(self):
        assert self.selection(select_card_by="None")["strategy"] == "first"

    def test_random_stays_random(self):
        assert self.selection(select_card_by="Random")["strategy"] == "random"

    def test_count_zero_becomes_all_and_drops_the_count(self):
        selection = self.selection(select_card_count="0")
        assert (selection["strategy"], selection["count"]) == ("all", None)

    def test_a_positive_count_now_counts_notes(self):
        assert self.selection(select_card_count="3")["count"] == 3

    def test_least_reps_becomes_random_and_is_reported(self):
        definition = d.destination_to_sources(
            definition_name="repsy", copy_from_cards_query="Word:a", select_card_by="Least_reps"
        )
        migrated = migrate_definition_v1_to_v2(definition)
        assert find(migrated["stages"], "note_query")["selection"]["strategy"] == "random"
        assert any("Least_reps" in warning for warning in migrated["migration_warnings"])
        assert any("repsy" in warning for warning in migrated["migration_warnings"])

    def test_an_unparsable_count_carries_the_complaint_format_1_logged(self):
        selection = self.selection(select_card_count="not a number")
        assert "Incorrect 'select_card_count' value" in selection["selection_error"]

    @pytest.mark.parametrize(
        "select_card_by, fragment",
        [(None, "was missing"), ("Most_reps", "incorrect 'select_card_by' value")],
    )
    def test_an_unusable_select_card_by_carries_the_complaint_too(
        self, select_card_by, fragment
    ):
        # Format 1 selected nothing at all and said why. Mapping this to "take the first
        # note" instead would make a definition that has never written anything start
        # writing, so the refusal migrates with it.
        selection = self.selection(select_card_by=select_card_by)
        assert fragment in selection["selection_error"]

    def test_an_unusable_select_card_by_keeps_the_count_and_sort_field(self):
        # The refusal is the strategy's alone. The count and sort field the user had are
        # what they get back once they fix the strategy in the editor, so they migrate too.
        selection = self.selection(
            select_card_by="Bogus", select_card_count="3", sort_by_field="Word"
        )
        assert "incorrect 'select_card_by' value" in selection["selection_error"]
        assert selection["strategy"] == "all"
        assert selection["count"] == 3
        assert selection["sort_field"] == "Word"
        assert selection["sort_numeric"] is True

    def test_a_sort_field_sorts_numerically_and_descending_as_it_did(self):
        selection = self.selection(sort_by_field="Freq")
        assert selection["sort_field"] == "Freq"
        assert selection["sort_order"] == "descending"
        assert selection["sort_numeric"] is True

    def test_the_placeholder_sort_field_means_no_sort(self):
        assert self.selection(sort_by_field="-")["sort_field"] is None


class TestTriggers:
    def test_note_types_and_decks_become_arrays_of_references(self):
        # References rather than bare names, with null ids: the migrator runs from
        # `migrate_config()` at import time, when there is no collection to bind an id in.
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                note_types=["A", "B"],
                only_copy_into_decks=d.quoted_list(["JP vocab", "Other"]),
                include_subdecks=True,
            )
        )
        assert migrated["triggers"]["note_types"] == [
            {"id": None, "name": "A"},
            {"id": None, "name": "B"},
        ]
        assert migrated["triggers"]["deck_names"] == [
            {"id": None, "name": "JP vocab"},
            {"id": None, "name": "Other"},
        ]
        assert migrated["triggers"]["include_subdecks"] is True

    def test_the_placeholder_deck_list_means_no_whitelist(self):
        migrated = migrate_definition_v1_to_v2(d.within_note(only_copy_into_decks="-"))
        assert migrated["triggers"]["deck_names"] == []

    def test_event_flags_move_across(self):
        migrated = migrate_definition_v1_to_v2(
            d.within_note(copy_on_sync=True, copy_on_add=True, copy_on_review=True)
        )
        triggers = migrated["triggers"]
        assert (triggers["on_sync"], triggers["on_add"], triggers["on_review"]) == (
            True,
            True,
            True,
        )

    def test_unfocus_metadata_is_collected_at_definition_level(self):
        # Format 2 watches fields per definition, so the per-field flags are unioned into
        # the two trigger lists -- and the per-write copies stay, so a run still applies
        # only the writes the changed field triggers.
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_field_defs=[
                    d.field_to_field("Note", "{{Word}}", copy_on_unfocus_when_edit=True),
                    d.field_to_field(
                        "Reading",
                        "{{Word}}",
                        copy_on_unfocus_when_add=True,
                        copy_on_unfocus_trigger_field="Word",
                    ),
                ]
            )
        )
        assert migrated["triggers"]["on_unfocus"] == {
            "edit_fields": ["Note"],
            "add_fields": ["Word"],
        }
        writes = migrated["stages"][0]["fields"]
        assert writes[0]["unfocus_trigger_fields"] == ["Note"]
        assert writes[1]["unfocus_trigger_fields"] == ["Word"]


class TestPurity:
    def test_the_input_is_not_mutated(self):
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            card_actions=[d.card_action("CA Vocab", "Recognition")],
        )
        before = repr(definition)
        migrate_definition_v1_to_v2(definition)
        assert repr(definition) == before

    def test_the_guids_are_derived_from_the_definitions_own_guid(self):
        migrated = migrate_definition_v1_to_v2(
            d.destination_to_sources(
                definition_name="x",
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            )
        )
        assert find(migrated["stages"], "note_query")["guid"] == "def-x::query"
        assert find(migrated["stages"], "edit_note")["guid"] == "def-x::edit-trigger"

    def test_migrating_twice_gives_the_same_thing(self):
        definition = d.destination_to_sources(
            copy_from_cards_query="Word:a",
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            field_to_variable_defs=[d.field_to_variable("a", "{{Word}}")],
            copy_condition_query="Word:neko",
        )
        assert migrate_definition_v1_to_v2(definition) == migrate_definition_v1_to_v2(definition)

    def test_a_format_2_definition_passes_through_unchanged(self):
        staged = d.staged(stages=[d.edit_note("trigger")])
        assert migrate_definition_v1_to_v2(staged) == staged

    def test_no_guid_of_its_own_uses_the_injected_generator(self):
        definition = d.within_note()
        del definition["guid"]
        migrated = migrate_definition_v1_to_v2(definition, new_guid=lambda: "injected")
        assert migrated["guid"] == "injected"

    def test_the_result_is_structurally_valid(self):
        for definition in (
            d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")]),
            d.source_to_destinations(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            ),
            d.destination_to_sources(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
                field_to_file_defs=[d.field_to_file("f.txt", "{{Word}}")],
                copy_condition_query="Word:neko",
            ),
        ):
            migrated = migrate_definition_v1_to_v2(definition)
            assert validate_definition_structure(migrated) == []


class TestTheSyntaxThatComesOut:
    """Nothing the migrator hands back still speaks format 1 inside its expressions.

    Migration turns a definition into stages; promotion turns the references inside them
    into format-2 syntax, so a bare `{{Word}}` says which note it meant. The rewrite's own
    truth table is `test_syntax_promotion.py`; what is pinned here is that every definition
    leaving the migrator has been through it, because the executor has only the one
    resolution path to run it with.
    """

    @pytest.mark.parametrize(
        "builder",
        [
            pytest.param(
                lambda: d.within_note(
                    field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
                    field_to_variable_defs=[d.field_to_variable("a", "{{Meaning}}")],
                    field_to_file_defs=[d.field_to_file("{{Word}}.txt", "{{Meaning}}")],
                    copy_condition_query="Word:{{Word}}",
                ),
                id="within note",
            ),
            pytest.param(
                lambda: d.source_to_destinations(
                    copy_from_cards_query="Word:{{Word}}",
                    field_to_field_defs=[
                        d.field_to_field("Note", "{{__Dest__Note}}, {{Word}}")
                    ],
                ),
                id="source to destinations",
            ),
            pytest.param(
                lambda: d.destination_to_sources(
                    copy_from_cards_query="Word:a",
                    field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
                    field_to_file_defs=[d.field_to_file("out.txt", "{{Word}}")],
                ),
                id="destination to sources",
            ),
        ],
    )
    def test_no_stage_or_expression_carries_a_legacy_marker(self, builder):
        migrated = migrate_definition_v1_to_v2(builder())
        for stage in walk_stages(migrated["stages"]):
            assert "legacy_source" not in stage, stage["guid"]
            assert "legacy_destination" not in stage, stage["guid"]
            for expression in expressions_of(stage):
                assert "syntax_version" not in expression, stage["guid"]
                assert "legacy_isolated_variables" not in expression, stage["guid"]

    def test_the_references_name_the_notes_format_1_left_implicit(self):
        # The across-notes write is the one where the two notes are different: the value
        # comes from the trigger and `__Dest__` is the note being written.
        migrated = migrate_definition_v1_to_v2(
            d.source_to_destinations(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[
                    d.field_to_field("Note", "{{__Dest__Note}}, {{Word}}")
                ],
            )
        )
        edit = migrated["stages"][1]["body"][0]
        assert edit["fields"][0]["value"]["text"] == "{{note.Note}}, {{trigger.Word}}"

    def test_a_definition_that_was_never_format_1_is_still_passed_through(self):
        # Promotion is idempotent, so the format-2 shortcut at the top of the migrator is
        # still the whole story for a definition that never had legacy syntax in it.
        staged = d.staged(stages=[d.variable("M", d.text("{{trigger.Word}}"))])
        assert migrate_definition_v1_to_v2(staged) == staged


class TestRefusals:
    def test_a_missing_copy_mode_is_refused_with_the_message_format_1_logged(self):
        definition = d.within_note()
        definition["copy_mode"] = None
        with pytest.raises(MigrationError, match="missing copy mode value"):
            migrate_definition_v1_to_v2(definition)

    def test_a_bogus_across_direction_is_refused(self):
        definition = d.destination_to_sources(copy_from_cards_query="Word:a")
        definition["across_mode_direction"] = "Sideways"
        with pytest.raises(MigrationError, match="missing across mode direction value"):
            migrate_definition_v1_to_v2(definition)

    def test_one_broken_definition_does_not_stop_the_rest_of_a_config(self):
        broken = d.within_note(definition_name="broken")
        broken["copy_mode"] = None
        migrated, problems = migrate_definitions([broken, d.within_note(definition_name="fine")])
        assert [definition["definition_name"] for definition in migrated] == ["fine"]
        assert any("broken" in problem for problem in problems)


class TestAConfigEntryThatIsNotADefinition:
    """What `migrate_definitions` does with a `copy_definitions` entry it cannot read at all.

    The contract the function states, and the reason it returns problems rather than raising,
    is that one broken definition does not stop the rest of the config from being usable. It
    keeps that promise for exactly one kind of broken: `MigrationError`, which the migrator
    raises deliberately for a missing copy mode or across-note direction. Anything else --
    an entry that is not a dict, or a list-shaped key holding something that is not a list --
    comes out of `deepcopy` and `.get` as a `TypeError` or `AttributeError` and is not caught.

    Where that lands is what makes it worth more than a tidier traceback. `migrate_config()`
    runs at import time from `__init__.py`, so the exception escapes into Anki's addon
    loader and the addon does not load: no browser action, no hooks, and no editor to repair
    the entry with. The config is hand-editable JSON and is written by older versions of this
    addon and by the user, so a malformed entry is reachable without anything else going
    wrong -- and once it is there, every subsequent start hits it again.
    """

    def good(self):
        return d.within_note(definition_name="fine")

    @pytest.mark.parametrize(
        "entry, description",
        [
            (None, "a null left where a definition was removed"),
            ("copy_definitions", "a bare string"),
            ([], "a list"),
        ],
    )
    def test_an_entry_that_is_not_a_dict_is_reported_not_raised(self, entry, description):
        migrated, problems = migrate_definitions([entry, self.good()])

        assert [definition["definition_name"] for definition in migrated] == ["fine"], description
        assert len(problems) == 1

    def test_a_list_key_holding_something_else_is_reported_not_raised(self):
        broken = d.within_note(definition_name="broken")
        broken["field_to_field_defs"] = {"Note": "{{Word}}"}

        migrated, problems = migrate_definitions([broken, self.good()])

        assert [definition["definition_name"] for definition in migrated] == ["fine"]
        assert any("broken" in problem for problem in problems)

    def test_a_field_write_that_is_not_a_dict_is_reported_not_raised(self):
        broken = d.within_note(definition_name="broken")
        broken["field_to_field_defs"] = ["Note"]

        migrated, problems = migrate_definitions([broken, self.good()])

        assert [definition["definition_name"] for definition in migrated] == ["fine"]
        assert any("broken" in problem for problem in problems)


class TestAProcessChainThatReadEveryNote:
    """`use_all_notes` on a regex process, which format 2 has nowhere to put.

    Format 1 kept one list of source notes and handed it to the whole process chain, so a
    regex process with this flag interpolated its pattern across all of them. A stage reads
    one note, so the migrated definition silently reads the trigger note instead. Nothing
    here repairs that -- the point is that the user is told, since a config that has already
    been migrated is never migrated again.
    """

    def warnings(self, **extra):
        chain = [d.regex_process("{{Word}}", "[{{Meaning}}]", use_all_notes=True)]
        definition = d.destination_to_sources(
            definition_name="allnotes",
            copy_from_cards_query="deck:x",
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}", process_chain=chain)
            ],
            **{"select_card_count": "0", **extra},
        )
        return migrate_definition_v1_to_v2(definition).get("migration_warnings", [])

    def test_it_is_reported_with_the_definition_and_the_field(self):
        warnings = self.warnings()
        assert len(warnings) == 1
        assert "allnotes" in warnings[0]
        assert "use all notes" in warnings[0]
        assert "Note" in warnings[0]

    def test_a_file_write_is_reported_by_its_filename(self):
        chain = [d.regex_process("{{Word}}", "x", use_all_notes=True)]
        definition = d.destination_to_sources(
            copy_from_cards_query="deck:x",
            select_card_count="0",
            field_to_file_defs=[
                d.field_to_file("out.txt", "{{Word}}", process_chain=chain)
            ],
        )
        warnings = migrate_definition_v1_to_v2(definition)["migration_warnings"]
        assert any("out.txt" in warning for warning in warnings)

    def test_one_source_note_is_not_more_than_one(self):
        # `use_all_notes and len(notes) > 1`: with a count of 1 the flag never fired in
        # format 1 either, so there is no change to report.
        assert self.warnings(select_card_count="1") == []

    def test_a_count_above_one_is_reported(self):
        assert len(self.warnings(select_card_count="2")) == 1

    def test_a_selection_that_refused_to_select_is_not_reported(self):
        # Format 1 selected no sources at all and said so. The refusal migrates with it and
        # is the thing to fix; a second warning about a flag that had no notes to read would
        # only point away from it.
        warnings = self.warnings(select_card_by="Most_reps")
        assert not any("use all notes" in warning for warning in warnings)

    def test_the_flag_turned_off_says_nothing(self):
        chain = [d.regex_process("{{Word}}", "x", use_all_notes=False)]
        definition = d.destination_to_sources(
            copy_from_cards_query="deck:x",
            select_card_count="0",
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}", process_chain=chain)
            ],
        )
        assert migrate_definition_v1_to_v2(definition).get("migration_warnings", []) == []

    @pytest.mark.parametrize(
        "builder", [d.within_note, d.source_to_destinations], ids=["within", "to_dests"]
    )
    def test_the_modes_with_one_source_note_say_nothing(self, builder):
        # Within note read a copy of the trigger note and Source-to-destinations read the
        # trigger note, so the flag was already dead in both before any migration.
        chain = [d.regex_process("{{Word}}", "x", use_all_notes=True)]
        definition = builder(
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}", process_chain=chain)
            ],
            # Set so that the selection is not what is doing the work here: with a count of
            # 1 the warning would stay quiet whatever the mode, and the test would pass
            # without the mode ever being consulted.
            select_card_count="0",
        )
        assert migrate_definition_v1_to_v2(definition).get("migration_warnings", []) == []

    def test_a_variable_is_not_reported(self):
        # Format 1 evaluated every variable against one note (`notes=[note]`), so the flag
        # did nothing there in any mode. Reporting it would send the user hunting for a
        # change that never happened.
        chain = [d.regex_process("{{Word}}", "x", use_all_notes=True)]
        definition = d.destination_to_sources(
            copy_from_cards_query="deck:x",
            select_card_count="0",
            field_to_variable_defs=[
                d.field_to_variable("V", "{{Word}}", process_chain=chain)
            ],
        )
        assert migrate_definition_v1_to_v2(definition).get("migration_warnings", []) == []

    def test_two_affected_writes_are_named_in_one_warning(self):
        chain = [d.regex_process("{{Word}}", "x", use_all_notes=True)]
        definition = d.destination_to_sources(
            copy_from_cards_query="deck:x",
            select_card_count="0",
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}", process_chain=chain),
                d.field_to_field("Other", "{{Word}}", process_chain=chain),
            ],
        )
        warnings = migrate_definition_v1_to_v2(definition)["migration_warnings"]
        assert len(warnings) == 1
        assert "Note" in warnings[0] and "Other" in warnings[0]

    def test_the_whole_config_migration_collects_it(self):
        chain = [d.regex_process("{{Word}}", "x", use_all_notes=True)]
        definition = d.destination_to_sources(
            definition_name="allnotes",
            copy_from_cards_query="deck:x",
            select_card_count="0",
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}", process_chain=chain)
            ],
        )
        _migrated, problems = migrate_definitions([definition])
        assert any("use all notes" in problem for problem in problems)
