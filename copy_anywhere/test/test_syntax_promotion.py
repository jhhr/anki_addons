"""Tests for the rewrite that retires format-1 syntax: `promote_expression` and friends.

Migration turns a format-1 definition into stages; promotion turns the *references* inside
those stages into format-2 syntax, so that `{{Word}}` says which note it reads and nothing
downstream has to know that format 1 ever existed. These tests are the rewrite's own truth
table -- one case per rule -- plus the two questions the table cannot answer on its own:
that a promoted note value and a promoted card value still resolve against a real note, and
that every shape the migrator can produce comes out with no legacy marker left in it.

The migrator promotes its own output, so a definition that still speaks format 1 is built
here by hand -- `legacy()` -- wherever one is needed.
"""

import ast
from pathlib import Path

import pytest

import definitions as d

# Imported as a module, not by name: pytest would collect a `Test*` class pulled into this
# module's namespace a second time.
import test_definition_migration as migration_tests
from anki_shared.interpolate.interpolate_fields import CARD_VALUES, NOTE_VALUES
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic import definition_schema
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note
from copy_anywhere.logic.definition_migration import (
    STAGE_EXPRESSION_KEYS,
    SYNTAX_VERSION_LEGACY,
    migrate_definition_v1_to_v2,
    promote_definition,
    promote_expression,
    promote_stage,
)
from copy_anywhere.logic.definition_schema import (
    validate_definition_structure,
    value_expression,
    walk_stages,
)
from copy_anywhere.logic.flow_analysis import analyze_definition

LEGACY_KEYS = ("syntax_version", "legacy_isolated_variables")
LEGACY_STAGE_KEYS = ("legacy_source", "legacy_destination")


def legacy(text: str = "", code: str = "", **extra):
    """A `syntax_version: 1` expression, the way the migrator writes them."""
    return value_expression(
        text=text, code=code, syntax_version=SYNTAX_VERSION_LEGACY, **extra
    )


def promoted_text(text, source="trigger", destination=None, known_names=()):
    return promote_expression(legacy(text), source, destination, known_names)["text"]


def expressions_of(definition):
    """Every expression in a definition, wherever it lives."""
    found = []
    for stage in walk_stages(definition.get("stages") or []):
        for key in STAGE_EXPRESSION_KEYS.get(stage.get("type", ""), ()):
            if isinstance(stage.get(key), dict):
                found.append(stage[key])
        for field_write in stage.get("fields") or []:
            if isinstance(field_write, dict) and isinstance(field_write.get("value"), dict):
                found.append(field_write["value"])
    return found


def assert_no_legacy_markers(definition):
    for stage in walk_stages(definition.get("stages") or []):
        for key in LEGACY_STAGE_KEYS:
            assert key not in stage, f"{key} left on {stage.get('guid')}"
    for expression in expressions_of(definition):
        for key in LEGACY_KEYS:
            assert key not in expression, f"{key} left on {expression}"


class TestTheDestinationPrefix:
    def test_it_names_the_destination_binding(self):
        assert promoted_text("{{__Dest__Word}}", "trigger", "note") == "{{note.Word}}"

    def test_with_no_destination_it_collapses_onto_the_source(self):
        # Format 1 had nowhere else to read it from either; what it did with the prefix it
        # could not strip was to report an invalid field and write an empty string.
        assert promoted_text("{{__Dest__Word}}", "trigger", None) == "{{trigger.Word}}"

    def test_a_differently_cased_prefix_was_never_the_prefix(self):
        # `interpolate_from_text` matched `__Dest__` exactly, so this read the source note
        # in format 1 and reads it here.
        assert promoted_text("{{__dest__Word}}", "trigger", "note") == (
            "{{trigger.__dest__Word}}"
        )

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("__Note_Tags", id="a note value"),
            pytest.param("Recognition__Card_Due", id="a card value"),
        ],
    )
    def test_it_prefixes_whatever_the_menu_offered(self, name):
        # The format-1 menu offered a `__Dest__` spelling of every note and card value, not
        # just of the fields, and the prefix is stripped before the rest is looked up.
        assert promoted_text("{{__Dest__" + name + "}}", "trigger", "note") == (
            "{{note." + name + "}}"
        )

    def test_the_rest_of_the_text_is_untouched(self):
        assert promoted_text("a {{__Dest__Word}}: {{Meaning}}!", "note", "trigger") == (
            "a {{trigger.Word}}: {{note.Meaning}}!"
        )


class TestABareName:
    def test_a_binding_the_migrator_named_stays_bare(self):
        assert promoted_text(
            "{{legacy_joined_1}}", known_names=["legacy_joined_1"]
        ) == "{{legacy_joined_1}}"

    def test_a_binding_is_matched_however_it_is_spelled(self):
        # Format 1 matched every name case-insensitively; format 2 resolves a binding
        # exactly, so the binding's own spelling is what the promoted reference carries.
        assert promoted_text("{{MYVAR}}", known_names=["myVar"]) == "{{myVar}}"

    @pytest.mark.parametrize("name", ["__Target_Notes_Count", "__Query_Note_Index"])
    def test_a_runtime_value_stays_bare(self, name):
        assert promoted_text("{{" + name + "}}") == "{{" + name + "}}"

    def test_a_runtime_value_takes_its_canonical_spelling(self):
        assert promoted_text("{{__target_notes_count}}") == "{{__Target_Notes_Count}}"

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("Word", id="a field"),
            pytest.param("__Note_Tags", id="a note value"),
            pytest.param("Recognition__Card_Due", id="a card value"),
            pytest.param("__Note_Has_Tag==x", id="a note value with an argument"),
            pytest.param("__Card_Due", id="a card value with no card type name"),
        ],
    )
    def test_anything_else_is_a_name_on_the_source_note(self, name):
        assert promoted_text("{{" + name + "}}", source="note") == "{{note." + name + "}}"

    @pytest.mark.parametrize("key", NOTE_VALUES + CARD_VALUES)
    def test_every_key_the_old_fallback_could_answer_comes_out_qualified(self, key):
        # The enumeration behind the rule above. `interpolate_from_text` answered a bare
        # name from the note's fields, then the note-value keys, then the card-value ones,
        # then the variables it was handed. The first three belong to a note, so all three
        # are qualified onto one; the variables are the bindings the migrator names, bar
        # the two the run supplies, which no binding holds and which stay bare. Nothing
        # else was ever resolvable, so nothing else is lost by refusing it (§11).
        assert promoted_text("{{" + key + "}}") == "{{trigger." + key + "}}"

    @pytest.mark.parametrize("key", CARD_VALUES)
    def test_a_card_value_keeps_its_card_type_name_inside_the_qualified_name(self, key):
        assert (
            promoted_text("{{Recognition" + key + "}}")
            == "{{trigger.Recognition" + key + "}}"
        )

    def test_the_users_spelling_of_a_name_is_kept(self):
        # Qualified or not, the note-side lookup is case-insensitive, so there is nothing
        # to correct and correcting it would rewrite text the user recognises.
        assert promoted_text("{{wOrD}}") == "{{trigger.wOrD}}"

    def test_a_reference_that_already_names_a_binding_is_left_alone(self):
        assert promoted_text("{{trigger.Word}}", "trigger", "note") == "{{trigger.Word}}"
        assert promoted_text("{{note.Word}}", "trigger", "note") == "{{note.Word}}"

    def test_a_dot_in_a_field_name_is_not_a_binding(self):
        assert promoted_text("{{Word.2}}") == "{{trigger.Word.2}}"


class TestClozeMarkers:
    def test_the_marker_stays_and_its_content_is_promoted(self):
        assert promoted_text("{{c1::{{Word}}}}") == "{{c1::{{trigger.Word}}}}"

    def test_a_marker_beside_ordinary_references(self):
        assert promoted_text("{{Word}} {{c2::{{Meaning}} and {{__Dest__Note}}}} end") == (
            "{{trigger.Word}} {{c2::{{trigger.Meaning}} and {{trigger.Note}}}} end"
        )

    def test_a_marker_with_no_reference_in_it_is_untouched(self):
        assert promoted_text("{{c1::plain}}") == "{{c1::plain}}"

    def test_a_cloze_inside_code_is_promoted_the_same_way(self):
        expression = legacy(code="return '{{c1::{{Word}}}}'")
        assert promote_expression(expression)["code"] == "return '{{c1::{{trigger.Word}}}}'"


class TestTheExpressionItself:
    def test_the_legacy_markers_are_dropped(self):
        expression = legacy("{{Word}}", legacy_isolated_variables=True)
        promoted = promote_expression(expression)
        assert "syntax_version" not in promoted
        assert "legacy_isolated_variables" not in promoted

    def test_both_the_text_and_the_code_are_rewritten(self):
        expression = legacy(text="{{Word}}", code="return note['{{Meaning}}']")
        promoted = promote_expression(expression)
        assert promoted["text"] == "{{trigger.Word}}"
        assert promoted["code"] == "return note['{{trigger.Meaning}}']"

    def test_the_process_chain_and_the_mode_ride_along(self):
        process = d.regex_process("a", "b")
        expression = legacy(code="{{Word}}", process_chain=[process])
        promoted = promote_expression(expression)
        assert promoted["mode"] == "code"
        assert promoted["process_chain"] == [process]

    def test_the_input_is_not_mutated(self):
        expression = legacy("{{Word}}")
        promote_expression(expression)
        assert expression["text"] == "{{Word}}"
        assert expression["syntax_version"] == SYNTAX_VERSION_LEGACY

    def test_a_current_syntax_expression_comes_back_unchanged(self):
        expression = value_expression(text="{{trigger.Word}}")
        assert promote_expression(expression) == expression

    def test_promoting_twice_is_promoting_once(self):
        once = promote_expression(legacy("{{Word}} {{__Dest__Note}}"), "note", "trigger")
        assert promote_expression(once, "note", "trigger") == once


class TestWhichKeysAStageHolds:
    def test_the_map_covers_every_stage_type_that_holds_an_expression(self):
        # Read off `definition_schema`'s stage shapes. `edit_note` is absent because its
        # expressions are one level down, on each field write; the rest hold none.
        assert STAGE_EXPRESSION_KEYS == {
            "variable": ("value",),
            "note_query": ("query",),
            "card_query": ("query",),
            "read_file": ("filename",),
            "write_file": ("filename", "content"),
            "store": ("value",),
            "reduce": ("value", "initial"),
            "condition": ("predicate",),
        }

    def test_the_map_holds_every_key_the_structural_validator_calls_an_expression(self):
        # Read off `validate_stage_structure` rather than off a reading of it: a stage type
        # that grows an expression grows a `_require_expression` call, and a key this map
        # does not know about is exactly the reference that would be left speaking format 1.
        # `edit_note`'s field writes go through the same call and share the name `value`.
        tree = ast.parse(Path(definition_schema.__file__).read_text(encoding="utf-8"))
        required = {
            call.args[1].value
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and getattr(call.func, "id", "") == "_require_expression"
            and isinstance(call.args[1], ast.Constant)
        }
        assert required == {key for keys in STAGE_EXPRESSION_KEYS.values() for key in keys}

    def test_tags_and_card_action_code_are_not_references(self, col):
        # Neither is interpolated, in either format: `run_edit_note` adds the tag string it
        # was given, and `execute_code_for_card_action` runs the code without resolving
        # anything in it. So neither is promoted, and a `{{...}}` in either is still the
        # literal text it always was.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        definition = migrate_definition_v1_to_v2(
            d.within_note(
                add_tags="{{Word}}",
                card_actions=[
                    d.card_action(
                        VOCAB,
                        "Recognition",
                        use_code=True,
                        action_code=(
                            "return {'set_flag': 1 if '{{Word}}'.startswith('{{') else 2}"
                        ),
                    )
                ],
            )
        )
        edit = definition["stages"][0]
        assert edit["tags"]["add"] == ["{{Word}}"]
        assert "'{{Word}}'" in edit["card_actions"][0]["action_code"]
        cards: dict = {}
        assert copy_for_single_trigger_note(
            definition, note, copied_into_cards_dict=cards
        ) is True
        assert note.has_tag("{{Word}}")
        # Flag 1 is the branch the code takes when it still sees the braces; had the
        # reference been promoted and resolved, it would have seen "neko" and flagged 2.
        flags = {card.template()["name"]: card.user_flag() for card in cards.values()}
        assert flags["Recognition"] == 1

    @pytest.mark.parametrize("stage_type, keys", sorted(STAGE_EXPRESSION_KEYS.items()))
    def test_every_key_of_every_type_is_promoted(self, stage_type, keys):
        stage = {
            "guid": f"g-{stage_type}",
            "type": stage_type,
            "name": stage_type,
            "enabled": True,
            **{key: legacy("{{Word}}") for key in keys},
        }
        promoted = promote_stage(stage)
        for key in keys:
            assert promoted[key]["text"] == "{{trigger.Word}}", key
            assert "syntax_version" not in promoted[key], key

    def test_an_edit_notes_field_writes_are_promoted(self):
        stage = d.edit_note(
            "trigger",
            fields=[d.write("Note", legacy("{{Word}}")), d.write("Reading", legacy("{{a}}"))],
        )
        promoted = promote_stage(stage, known_names=["a"])
        assert [write["value"]["text"] for write in promoted["fields"]] == [
            "{{trigger.Word}}",
            "{{a}}",
        ]

    @pytest.mark.parametrize(
        "stage",
        [
            pytest.param(d.edit_card("card"), id="edit_card"),
            pytest.param(d.list_variable("L"), id="list_variable"),
            pytest.param(d.for_each_note("N", []), id="for_each_note"),
            pytest.param(d.for_each_card("C", []), id="for_each_card"),
            pytest.param(d.call_definition("other"), id="call_definition"),
        ],
    )
    def test_a_stage_with_no_expression_comes_back_as_it_was(self, stage):
        assert promote_stage(stage) == stage


class TestPromoteStage:
    def test_the_legacy_bindings_say_which_note_and_are_then_dropped(self):
        stage = d.store(
            "L",
            legacy("{{Word}} from {{__Dest__Note}}"),
            legacy_source={"binding": "note"},
            legacy_destination={"binding": "trigger"},
        )
        promoted = promote_stage(stage)
        assert promoted["value"]["text"] == "{{note.Word}} from {{trigger.Note}}"
        assert "legacy_source" not in promoted
        assert "legacy_destination" not in promoted

    def test_a_stage_without_them_reads_the_trigger(self):
        promoted = promote_stage(d.variable("v", legacy("{{Word}}")))
        assert promoted["value"]["text"] == "{{trigger.Word}}"

    def test_it_recurses_into_then_else_and_body(self):
        stage = d.condition(
            legacy("{{Word}}"),
            then=[
                d.for_each_note(
                    "N",
                    [d.store("L", legacy("{{Meaning}}"), legacy_source={"binding": "note"})],
                )
            ],
            otherwise=[d.variable("v", legacy("{{Freq}}"))],
        )
        promoted = promote_stage(stage)
        assert promoted["predicate"]["text"] == "{{trigger.Word}}"
        assert promoted["then"][0]["body"][0]["value"]["text"] == "{{note.Meaning}}"
        assert promoted["else"][0]["value"]["text"] == "{{trigger.Freq}}"

    def test_the_input_is_not_mutated(self):
        stage = d.variable("v", legacy("{{Word}}"), legacy_source={"binding": "note"})
        promote_stage(stage)
        assert stage["legacy_source"] == {"binding": "note"}
        assert stage["value"]["text"] == "{{Word}}"


class TestWhichNoteEachKeyReads:
    """The two roles are per key, not per stage.

    Format 1 assigned "source" and "destination" once per copy mode and then let each
    action decide which of them it used: a file's name was interpolated over the
    destination note while its content read each source note, and an Edit Note read
    `__Dest__` off the note it was writing however far away the values came from. The
    migrator recorded the pair on the stage; promotion has to hand each key the pair the
    executor hands it, or a reference moves onto the other note.
    """

    def test_an_edit_notes_dest_prefix_names_the_note_it_writes(self):
        stage = d.edit_note(
            "note",
            fields=[d.write("Note", legacy("{{__Dest__Note}}, {{Word}}"))],
            legacy_source={"binding": "trigger"},
        )
        promoted = promote_stage(stage)
        assert promoted["fields"][0]["value"]["text"] == "{{note.Note}}, {{trigger.Word}}"

    def test_an_edit_note_with_no_source_reads_the_note_it_writes(self):
        stage = d.edit_note("note", fields=[d.write("Note", legacy("{{Word}}"))])
        assert promote_stage(stage)["fields"][0]["value"]["text"] == "{{note.Word}}"

    def test_a_file_name_reads_the_destination_and_its_content_the_source(self):
        stage = d.write_file(
            "ignored",
            legacy("{{Word}}"),
            legacy_source={"binding": "trigger"},
            legacy_destination={"binding": "note"},
        )
        stage["filename"] = legacy("{{Word}}-{{__Dest__Note}}.txt")
        promoted = promote_stage(stage)
        assert promoted["filename"]["text"] == "{{note.Word}}-{{note.Note}}.txt"
        assert promoted["content"]["text"] == "{{trigger.Word}}"

    @pytest.mark.parametrize(
        "stage, key",
        [
            pytest.param(d.variable("v", legacy("{{__Dest__Word}}")), "value", id="variable"),
            pytest.param(
                d.condition(
                    legacy("Word:{{__Dest__Word}}"),
                    then=[],
                    predicate_kind="note_query",
                    predicate_target={"binding": "trigger"},
                ),
                "predicate",
                id="condition",
            ),
        ],
    )
    def test_a_dest_prefix_in_a_stage_with_no_destination_collapses_onto_its_own_note(
        self, stage, key
    ):
        # Format 1 handed these no destination note at all, so `{{__Dest__Word}}` fell
        # through to the source and then read empty. Collapsing it onto the note the stage
        # does read is the nearest thing that resolves, and it is what the reference has
        # always been worth here.
        assert promote_stage(stage)[key]["text"].replace("Word:", "") == "{{trigger.Word}}"

    def test_a_within_note_write_has_only_one_note_for_both_roles(self):
        # Within-note: the note being written is the note being read, so the prefix says
        # nothing. It used to be dropped silently; now it names the trigger outright.
        stage = d.edit_note(
            "trigger",
            fields=[d.write("Note", legacy("{{__Dest__Word}}/{{Word}}"))],
            legacy_source={"binding": "trigger"},
        )
        promoted = promote_stage(stage)
        assert promoted["fields"][0]["value"]["text"] == "{{trigger.Word}}/{{trigger.Word}}"

    def test_a_stores_destination_falls_back_to_the_trigger_not_to_its_source(self):
        stage = d.store("L", legacy("{{__Dest__Note}}"), legacy_source={"binding": "note"})
        assert promote_stage(stage)["value"]["text"] == "{{trigger.Note}}"

    def test_a_search_condition_reads_the_note_it_is_scoped_to(self):
        stage = d.condition(
            legacy("Word:{{Word}}"),
            then=[],
            predicate_kind="note_query",
            predicate_target={"binding": "note"},
        )
        assert promote_stage(stage)["predicate"]["text"] == "Word:{{note.Word}}"

    def test_the_roles_survive_a_whole_migrated_definition(self):
        migrated = migrate_definition_v1_to_v2(
            d.source_to_destinations(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Note", "{{__Dest__Note}}, {{Word}}")],
                field_to_file_defs=[d.field_to_file("{{Word}}.txt", "{{Word}}")],
            )
        )
        body = promote_definition(migrated)["stages"][1]["body"]
        assert body[0]["fields"][0]["value"]["text"] == "{{note.Note}}, {{trigger.Word}}"
        # The found note is the destination here, so it is the one that names the file.
        assert body[1]["filename"]["text"] == "{{note.Word}}.txt"
        assert body[1]["content"]["text"] == "{{trigger.Word}}"


class TestPromoteDefinition:
    def test_a_variable_result_is_a_binding_and_a_field_is_not(self):
        # Format 1 resolved `{{search}}` out of the variables and `{{Word}}` out of the
        # note; the promoted definition says which is which rather than trying both.
        migrated = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_variable_defs=[d.field_to_variable("search", "Word:{{Word}}")],
                copy_condition_query="{{search}}",
            )
        )
        promoted = promote_definition(migrated)
        assert promoted["stages"][0]["value"]["text"] == "Word:{{trigger.Word}}"
        assert promoted["stages"][1]["predicate"]["text"] == "{{search}}"

    def test_a_loop_binding_is_not_a_bare_name_a_definition_could_have_meant(self):
        # The migrator calls the loop's note `note`, and `Note` is a field name. Format 1
        # had no way to name the loop, so a bare `{{Note}}` is the field it always was.
        migrated = migrate_definition_v1_to_v2(
            d.source_to_destinations(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Reading", "{{Note}}")],
            )
        )
        promoted = promote_definition(migrated)
        write = promoted["stages"][1]["body"][0]["fields"][0]
        assert write["value"]["text"] == "{{trigger.Note}}"

    def test_the_join_a_migrated_definition_synthesizes_keeps_its_names(self):
        migrated = migrate_definition_v1_to_v2(
            d.destination_to_sources(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            )
        )
        promoted = promote_definition(migrated)
        store = promoted["stages"][2]["body"][0]
        assert store["value"]["text"] == "{{note.Word}}"
        assert promoted["stages"][-1]["fields"][0]["value"]["text"] == "{{legacy_joined_1}}"

    def test_the_definition_level_legacy_dict_stays(self):
        # It is about counts and defaults, not syntax.
        migrated = migrate_definition_v1_to_v2(
            d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")])
        )
        assert promote_definition(migrated)["legacy"] == migrated["legacy"]

    def test_the_input_is_not_mutated(self):
        # Built by hand: the migrator promotes its own output now, so there is no longer a
        # definition it hands back that still has something left to promote.
        definition = d.staged(
            stages=[d.edit_note("trigger", fields=[d.write("Note", legacy("{{Word}}"))])]
        )
        promote_definition(definition)
        value = definition["stages"][0]["fields"][0]["value"]
        assert value["text"] == "{{Word}}"
        assert value["syntax_version"] == SYNTAX_VERSION_LEGACY

    def test_promoting_twice_is_promoting_once(self):
        migrated = migrate_definition_v1_to_v2(
            d.destination_to_sources(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            )
        )
        once = promote_definition(migrated)
        assert promote_definition(once) == once

    def test_a_definition_that_never_was_format_1_is_unchanged(self):
        definition = d.staged(
            stages=[d.edit_note("trigger", fields=[d.write("Note", d.text("{{trigger.Word}}"))])]
        )
        assert promote_definition(definition) == definition


# Every shape the migrator can produce. The builders come from the migration suite so the
# two files cannot drift apart about what a migrated definition looks like.
def _within_note(**extra):
    return migrate_definition_v1_to_v2(
        d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}: {{__Dest__Meaning}}")],
            **extra,
        )
    )


MIGRATED_DEFINITIONS = {
    "within note": lambda: _within_note(),
    "within note with tags and card actions": lambda: _within_note(
        add_tags=d.quoted_list(["one"]),
        remove_tags=d.quoted_list(["old"]),
        card_actions=[d.card_action("CA Vocab", "Recognition", set_flag=2)],
    ),
    "within note with variables": lambda: _within_note(
        field_to_variable_defs=[
            d.field_to_variable("a", "{{Word}}"),
            d.field_to_variable("b", "{{a}}-{{__Query_Note_Index}}"),
        ]
    ),
    "within note with a condition": lambda: _within_note(copy_condition_query="Word:{{Word}}"),
    "within note with a file": lambda: _within_note(
        field_to_file_defs=[d.field_to_file("{{Word}}.txt", "{{Meaning}}")]
    ),
    "source to destinations": lambda: migration_tests.TestSourceToDestinations().build(),
    "source to destinations with a file": (
        lambda: migration_tests.TestSourceToDestinations().build(
            field_to_file_defs=[d.field_to_file("{{__Dest__Word}}.txt", "{{Word}}")]
        )
    ),
    "destination to sources": lambda: migration_tests.TestDestinationToSources().build(),
    "destination to sources with two writes": lambda: migrate_definition_v1_to_v2(
        d.destination_to_sources(
            copy_from_cards_query="Word:a",
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}"),
                d.field_to_field("Reading", "{{Meaning}}", copy_if_empty=True),
            ],
        )
    ),
    "destination to sources with a joined file": (
        lambda: migration_tests.TestDestinationToSources().build(
            field_to_file_defs=[d.field_to_file("out.txt", "{{Word}}")]
        )
    ),
    "destination to sources with a code file": (
        lambda: migration_tests.TestDestinationToSources().build(
            field_to_file_defs=[
                d.field_to_file(
                    "", use_code=True, copy_as_code="return [('{{Word}}.txt', 'x')]"
                )
            ]
        )
    ),
}


@pytest.mark.parametrize("name", sorted(MIGRATED_DEFINITIONS))
class TestEveryShapeTheMigratorProduces:
    def test_nothing_legacy_is_left_in_it(self, name):
        assert_no_legacy_markers(promote_definition(MIGRATED_DEFINITIONS[name]()))

    def test_it_is_still_a_definition_that_saves(self, name):
        promoted = promote_definition(MIGRATED_DEFINITIONS[name]())
        assert validate_definition_structure(promoted) == []
        assert analyze_definition(promoted).problem_messages() == []

    def test_promoting_it_twice_changes_nothing(self, name):
        once = promote_definition(MIGRATED_DEFINITIONS[name]())
        assert promote_definition(once) == once


class TestAgainstARealNote:
    """The promoted reference has to resolve, not just look right.

    `{{trigger.X}}` goes through `_note_reference`, which asks the same interpolation
    format 1 asked -- so a field, a note value and a template-prefixed card value all
    resolve, and this is the check that the qualification did not take one of them out of
    reach.
    """

    @pytest.fixture
    def note(self, col):
        return real_anki.add_note(
            col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat", "Freq": "5"}
        )

    def run(self, note, from_text, expected_reference):
        definition = promote_definition(
            migrate_definition_v1_to_v2(
                d.within_note(field_to_field_defs=[d.field_to_field("Note", from_text)])
            )
        )
        # Nothing legacy is left, so what runs is the format-2 resolution and not the
        # format-1 interpolation the migrated definition would otherwise have used.
        assert_no_legacy_markers(definition)
        assert definition["stages"][0]["fields"][0]["value"]["text"] == expected_reference
        assert copy_for_single_trigger_note(definition, note) is True
        return note["Note"]

    def test_a_promoted_field_reference_resolves(self, note):
        written = self.run(
            note, "{{Word}}: {{Meaning}}", "{{trigger.Word}}: {{trigger.Meaning}}"
        )
        assert written == "neko: cat"

    def test_a_promoted_note_value_resolves(self, note):
        # CA Vocab has two templates, so the note has two cards.
        written = self.run(
            note, "{{__Note_Card_Count}}", "{{trigger.__Note_Card_Count}}"
        )
        assert written == "2"

    def test_a_promoted_card_value_resolves(self, note):
        # A template-prefixed card value: the prefix stays inside the qualified reference.
        written = self.run(
            note, "{{Recognition__Card_Type}}", "{{trigger.Recognition__Card_Type}}"
        )
        assert written == "New"

    def test_a_promoted_cloze_keeps_its_marker(self, note):
        written = self.run(note, "{{c1::{{Word}}}}", "{{c1::{{trigger.Word}}}}")
        assert written == "{{c1::neko}}"

    def test_a_cloze_inside_code_resolves_the_same_way(self, note):
        # Code is interpolated before it runs, by the same rewrite: a marker inside a code
        # expression is a marker, and what it wraps is promoted.
        definition = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_field_defs=[
                    d.field_to_field(
                        "Note", "", use_code=True, copy_as_code="return '{{c1::{{Word}}}}'"
                    )
                ]
            )
        )
        value = definition["stages"][0]["fields"][0]["value"]
        assert value["code"] == "return '{{c1::{{trigger.Word}}}}'"
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == "{{c1::neko}}"

    def test_a_note_value_and_a_card_value_the_menu_offers_still_resolve(self, note):
        # The two a bare name used to fall through to. `__Note_Type_ID` and
        # `Recognition__Card_Due` were never fields, so qualifying them onto the note is
        # the whole of what keeps them reachable.
        written = self.run(
            note,
            "{{__Note_Type_ID}}/{{Recognition__Card_Due}}",
            "{{trigger.__Note_Type_ID}}/{{trigger.Recognition__Card_Due}}",
        )
        note_type_id, card_due = written.split("/")
        assert note_type_id == str(note.note_type()["id"])
        assert card_due.isdigit()

    def test_a_dest_prefixed_note_value_and_card_value_reach_the_written_note(
        self, col, note
    ):
        # `__Dest__` in front of a note or card value, which is the spelling with four
        # underscores in the middle: the prefix is stripped and the key qualified onto the
        # note being written, not onto the one the values come from.
        target = real_anki.add_note(col, VOCAB, {"Word": "a"})
        definition = migrate_definition_v1_to_v2(
            d.source_to_destinations(
                copy_from_cards_query="Word:a",
                field_to_field_defs=[
                    d.field_to_field(
                        "Note", "{{__Dest____Note_ID}}/{{__Dest__Recognition__Card_Type}}"
                    )
                ],
                select_card_count="0",
            )
        )
        write = definition["stages"][1]["body"][0]["fields"][0]["value"]
        assert write["text"] == "{{note.__Note_ID}}/{{note.Recognition__Card_Type}}"
        copied: list = []
        assert copy_for_single_trigger_note(
            definition, note, copied_into_notes=copied
        ) is True
        assert copied[0]["Note"] == f"{target.id}/New"

    def test_a_variable_named_after_a_field_wins_over_the_field(self, note):
        # Format 1 asked the note first and fell back to its variables, so the field won.
        # Promotion keeps a name the migrator created bare and format 2 resolves a bare
        # name as the binding, so the variable wins instead. The reversal is deliberate --
        # format 2 has no shadowing, so there is no promoted spelling that could mean the
        # field while a binding of that name is in scope -- and the migrator does not check
        # for it (§11).
        definition = migrate_definition_v1_to_v2(
            d.within_note(
                field_to_variable_defs=[d.field_to_variable("Word", "from the variable")],
                field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            )
        )
        assert definition["stages"][-1]["fields"][0]["value"]["text"] == "{{Word}}"
        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == "from the variable"

    def test_a_destination_still_rewrites_its_own_field_through_the_dest_prefix(
        self, col, note
    ):
        # The sharpest of the two roles, run rather than read: each found note rewrites its
        # own field out of values taken from the trigger. `{{__Dest__Note}}` is the found
        # note's own pre-copy value and `{{Word}}` the trigger's, and the promotion has to
        # keep them apart.
        target = real_anki.add_note(col, VOCAB, {"Word": "a", "Note": "existing"})
        definition = promote_definition(
            migrate_definition_v1_to_v2(
                d.source_to_destinations(
                    copy_from_cards_query="Word:a",
                    field_to_field_defs=[
                        d.field_to_field("Note", "{{__Dest__Note}}, {{Word}}")
                    ],
                    select_card_count="0",
                )
            )
        )
        copied_into_notes = []
        assert copy_for_single_trigger_note(
            definition, note, copied_into_notes=copied_into_notes
        ) is True
        assert [n.id for n in copied_into_notes] == [target.id]
        assert copied_into_notes[0]["Note"] == "existing, neko"
