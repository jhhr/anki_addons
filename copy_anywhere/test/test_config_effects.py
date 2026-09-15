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
from conftest import DEFAULT_CONFIG
from copy_anywhere.configuration import Config


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
