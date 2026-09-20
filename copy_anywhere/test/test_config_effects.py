"""`effects` stays true across a save (§4, §6).

A definition's effects are derived, not authored, and they are transitive through
`call_definition`: A's effects depend on B's body. The hooks branch on the stored copy --
which pile a definition goes in while a note is being added, and whether an unfocus needs an
undo entry -- so a stale copy is not a cosmetic problem. It makes a definition discard a
session it should have committed, or drop writes it should have made, and says nothing
either way.

These drive the stubbed addon config directly: the analyser never touches a collection.
"""

import pytest

import definitions as d
from conftest import DEFAULT_CONFIG, VOCAB
from copy_anywhere.configuration import (
    Config,
    definition_effects,
    definition_is_add_note_compatible,
)


@pytest.fixture
def config(stub_mw):
    stored = dict(DEFAULT_CONFIG)
    stored["copy_definitions"] = []
    stub_mw.addonManager.configs["copy_anywhere"] = stored
    return stored


def given(config, *definitions):
    """Put these in the config with their effects already agreed, as a save would leave them.

    `d.staged` computes each definition's effects on its own, with nothing to look a callee
    up in, so a caller built that way starts out pessimistic. Resolving them against each
    other first is what makes the assertions below about the save and not about the fixture.
    """
    from copy_anywhere.logic.flow_analysis import refresh_effects

    config["copy_definitions"] = list(definitions)
    refresh_effects(config["copy_definitions"])


def stored(stub_mw, name: str) -> dict:
    """One definition as the config holds it now. `save()` replaces the dict every time."""
    definitions = stub_mw.addonManager.configs["copy_anywhere"]["copy_definitions"]
    return next(one for one in definitions if one["definition_name"] == name)


def caller(callee_guid: str, name: str = "caller") -> dict:
    return d.staged(
        name,
        stages=[d.call_definition(callee_guid)],
        guid=f"def-{name}",
    )


def writes_trigger_only(name: str = "callee") -> dict:
    return d.staged(
        name,
        stages=[d.edit_note("trigger", [d.write("Note", d.text("x"))])],
        guid=f"def-{name}",
    )


def writes_another_note(name: str = "callee") -> dict:
    return d.staged(
        name,
        stages=[
            d.note_query("found", "tag:pool"),
            d.for_each_note("found", [
                d.edit_note("note", [d.write("Note", d.text("x"))]),
            ]),
        ],
        guid=f"def-{name}",
    )


class TestSavingACallee:
    def test_the_callers_effects_are_brought_up_to_date(self, config, stub_mw):
        # The scenario this exists for: A was saved while B only wrote to the trigger note,
        # so A was recorded add-note compatible. B then learns to write elsewhere. Without
        # this, A keeps the old answer, runs in the Add dialog, and the runner throws the
        # whole session away -- A's own writes to the note being added included.
        given(config, caller("def-callee"), writes_trigger_only())
        assert stored(stub_mw, "caller")["effects"]["add_note_compatible"] is True

        saved = Config()
        saved.load()
        saved.update_definition_by_index(1, writes_another_note())

        assert stored(stub_mw, "caller")["effects"]["add_note_compatible"] is False
        assert stored(stub_mw, "caller")["effects"]["edits_other_notes"] is True

    def test_it_works_the_other_way_round_too(self, config, stub_mw):
        # A stale `edits_other_notes` sends the unfocus hook down the within-note branch,
        # where the callee's writes to other notes go into a list nobody saves.
        given(config, caller("def-callee"), writes_another_note())
        assert stored(stub_mw, "caller")["effects"]["edits_other_notes"] is True

        saved = Config()
        saved.load()
        saved.update_definition_by_index(1, writes_trigger_only())

        assert stored(stub_mw, "caller")["effects"]["edits_other_notes"] is False
        assert stored(stub_mw, "caller")["effects"]["add_note_compatible"] is True

    def test_a_chain_is_followed_all_the_way_up(self, config, stub_mw):
        # A -> B -> C. Editing C changes B's effects and A's, and only one of those is a
        # direct caller of C.
        given(
            config,
            caller("def-middle", name="top"),
            caller("def-callee", name="middle"),
            writes_trigger_only(),
        )
        assert stored(stub_mw, "top")["effects"]["add_note_compatible"] is True

        saved = Config()
        saved.load()
        saved.update_definition_by_index(2, writes_another_note())

        assert stored(stub_mw, "middle")["effects"]["add_note_compatible"] is False
        assert stored(stub_mw, "top")["effects"]["add_note_compatible"] is False

    def test_a_definition_that_calls_nothing_is_left_alone(self, config, stub_mw):
        given(config, writes_trigger_only("alone"), writes_trigger_only())
        before = dict(stored(stub_mw, "alone")["effects"])

        saved = Config()
        saved.load()
        saved.update_definition_by_index(1, writes_another_note())

        assert stored(stub_mw, "alone")["effects"] == before


class TestTheOtherWaysTheListChanges:
    def test_removing_a_callee_updates_its_callers(self, config, stub_mw):
        given(config, caller("def-callee"), writes_another_note())
        assert stored(stub_mw, "caller")["effects"]["edits_other_notes"] is True

        saved = Config()
        saved.load()
        saved.remove_definition_by_index(1)

        # A call that names nothing contributes no effects -- the analyser reports it as a
        # problem instead, and the stage fails at runtime saying which guid is missing. The
        # point here is only that the caller stopped claiming the deleted callee's effects.
        assert stored(stub_mw, "caller")["effects"]["edits_other_notes"] is False

    def test_adding_a_callee_updates_the_caller_that_was_waiting_for_it(
        self, config, stub_mw
    ):
        given(config, caller("def-callee"))
        assert stored(stub_mw, "caller")["effects"]["add_note_compatible"] is True

        saved = Config()
        saved.load()
        saved.add_definition(writes_another_note())

        assert stored(stub_mw, "caller")["effects"]["add_note_compatible"] is False

    def test_a_format_1_definition_in_the_list_is_not_given_effects(self, config, stub_mw):
        # It has none to compute: its mode and direction are the exact answer, and
        # `definition_effects` reads them directly.
        given(config, d.within_note(definition_name="old"))

        saved = Config()
        saved.load()
        saved.add_definition(writes_trigger_only())

        assert "effects" not in stored(stub_mw, "old")


class TestAFormat1DefinitionsEffects:
    """`definition_effects` inspects a format-1 definition instead of reading a stored object.

    The add hook sorts on the same `add_note_compatible` it would read off a format-2
    definition, so the fallback has to answer the way the analyser does. Format 1 has no
    target binding to read, but its mode says whose cards a card action reaches: Within
    note and Destination to sources act on the trigger's own, Source to destinations on
    the found notes'.
    """

    def flag(self):
        return d.card_action(VOCAB, "Recognition", set_flag=3)

    def test_a_field_write_on_the_trigger_alone_is_compatible(self):
        definition = d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")])
        assert definition_effects(definition)["add_note_compatible"] is True
        assert definition_is_add_note_compatible(definition)

    def test_a_card_action_on_the_triggers_own_cards_stays_compatible(self):
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            card_actions=[self.flag()],
        )
        effects = definition_effects(definition)
        assert effects["edits_cards"] is True
        assert effects["edits_trigger_cards"] is True
        assert effects["edits_other_cards"] is False
        assert effects["add_note_compatible"] is True
        assert definition_is_add_note_compatible(definition)

    def test_destination_to_sources_acts_on_the_trigger_too(self):
        # Its only destination is the trigger note, so its card actions are the trigger's.
        definition = d.destination_to_sources(
            copy_from_cards_query="Word:neko",
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            card_actions=[self.flag()],
        )
        effects = definition_effects(definition)
        assert effects["edits_trigger_cards"] is True
        assert effects["edits_other_cards"] is False
        assert effects["add_note_compatible"] is True

    def test_a_card_action_only_definition_is_incompatible_too(self):
        # No field write and no tag, so `edits_other_notes` is false and the card action is
        # the only thing that can say no. The cards it reaches are the found notes' own.
        definition = d.source_to_destinations(
            copy_from_cards_query="Word:neko", card_actions=[self.flag()]
        )
        effects = definition_effects(definition)
        assert effects["edits_other_notes"] is False
        assert effects["edits_cards"] is True
        assert effects["edits_trigger_cards"] is False
        assert effects["edits_other_cards"] is True
        assert effects["add_note_compatible"] is False
        assert not definition_is_add_note_compatible(definition)

    def test_writing_a_file_is_incompatible(self):
        # The file is on disk whether or not the user goes through with the add.
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            field_to_file_defs=[d.field_to_file("out.txt", "{{Word}}")],
        )
        effects = definition_effects(definition)
        assert effects["writes_files"] is True
        assert effects["add_note_compatible"] is False
        assert not definition_is_add_note_compatible(definition)


class TestADefinitionStoredBeforeTheCardSplit:
    """`edits_trigger_cards` / `edits_other_cards` are newer than the configs holding them.

    `effects` is derived, so a new key needs no format bump: `read_effects` fills what a
    stored object is missing from the pessimistic set, and the next save of the definition
    list recomputes the whole object from the stages.
    """

    def flags_its_trigger(self) -> dict:
        definition = d.staged(
            "old",
            stages=[
                d.edit_note(
                    "trigger",
                    [d.write("Note", d.text("x"))],
                    card_actions=[d.card_action(VOCAB, "Recognition", set_flag=3)],
                )
            ],
            guid="def-old",
        )
        definition["effects"] = {
            key: value
            for key, value in definition["effects"].items()
            if key not in ("edits_trigger_cards", "edits_other_cards")
        }
        return definition

    def test_the_missing_keys_read_as_the_pessimistic_ones(self, config, stub_mw):
        config["copy_definitions"] = [self.flags_its_trigger()]
        effects = definition_effects(stored(stub_mw, "old"))
        assert effects["edits_trigger_cards"] is True
        assert effects["edits_other_cards"] is True

    def test_a_save_recomputes_them_from_the_stages(self, config, stub_mw):
        config["copy_definitions"] = [self.flags_its_trigger()]

        saved = Config()
        saved.load()
        saved.add_definition(writes_trigger_only())

        effects = stored(stub_mw, "old")["effects"]
        assert effects["edits_trigger_cards"] is True
        assert effects["edits_other_cards"] is False
        assert effects["add_note_compatible"] is True
