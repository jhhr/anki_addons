"""The reconcile pass: what a stored definition does when Anki renames something.

A definition stores an id beside the name for every object Anki gives a stable id
(`test_object_refs.py`), which is what keeps it running after a rename. What that alone
does not do is tell the *user* which name a definition is still spelling, and it cannot
help a field at all -- a field is stored as the name it is written with, everywhere.

This pass closes both. It runs when the collection loads and after any operation that
changed a note type or a deck, compares the live names against a snapshot kept per id, and
so has both names of a rename in hand -- which no Anki hook gives. Cached names are
refreshed, a renamed field or card type of a trigger note type is followed into the
definition's field slots and its `{{trigger....}}` tokens, and everything it will not
rewrite -- a search term, code, a deleted field -- is reported instead.
"""

import pytest
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import DEFAULT_CONFIG, KANJI, VOCAB
from copy_anywhere.configuration import Config, migrate_config
from copy_anywhere.hooks.rename_hooks import on_operation_did_execute
from copy_anywhere.logic.rename_reconcile import definitions_hold_references, reconcile

ADDON_TAG = "copy_anywhere"


@pytest.fixture
def config(col, stub_mw):
    """A loaded `Config` over a fresh stored config, as the pass will find it."""
    stub_mw.addonManager.configs[ADDON_TAG] = dict(DEFAULT_CONFIG)
    stub_mw.addonManager.written.clear()
    configuration = Config()
    configuration.load()
    return configuration


def store(config, *definitions):
    config.data["copy_definitions"] = list(definitions)


def saves(stub_mw) -> int:
    """How many times the config has been written. The pass writes only when it must."""
    return len(stub_mw.addonManager.written)


def rename_field(col, note_type_name: str, old_name: str, new_name: str) -> None:
    note_type = col.models.by_name(note_type_name)
    for field in note_type["flds"]:
        if field["name"] == old_name:
            field["name"] = new_name
    col.models.update_dict(note_type)


def rename_template(col, note_type_name: str, old_name: str, new_name: str) -> None:
    note_type = col.models.by_name(note_type_name)
    for template in note_type["tmpls"]:
        if template["name"] == old_name:
            template["name"] = new_name
    col.models.update_dict(note_type)


def names(items) -> list[str]:
    return [item.name for item in items]


# Binding and refreshing ------------------------------------------------------------------


class TestBindingAReferenceThatHasNoIdYet:
    def test_a_null_id_is_bound_from_the_name(self, col, config):
        definition = d.staged(note_types=[VOCAB], deck_names=["JP vocab"])
        store(config, definition)

        result = reconcile(config, mw.col)

        assert definition["triggers"]["note_types"][0]["id"] == col.models.by_name(VOCAB)["id"]
        assert definition["triggers"]["deck_names"][0]["id"] == col.decks.id_for_name("JP vocab")
        assert result.changed is True

    def test_a_bare_string_is_stored_back_as_a_reference(self, col, config):
        definition = d.staged(note_types=[VOCAB])
        definition["triggers"]["note_types"] = [VOCAB]
        store(config, definition)

        reconcile(config, mw.col)

        assert definition["triggers"]["note_types"] == [
            {"id": col.models.by_name(VOCAB)["id"], "name": VOCAB}
        ]

    def test_a_card_action_is_bound_too(self, col, config):
        note_type = col.models.by_name(VOCAB)
        action = d.card_action_ref(note_type, note_type["tmpls"][0], set_flag=2)
        action["card_type"] = {"note_type_id": None, "template_id": None, "name": action["card_type"]["name"]}
        definition = d.staged(
            note_types=[VOCAB],
            stages=[d.edit_note("trigger", card_actions=[action])],
        )
        store(config, definition)

        reconcile(config, mw.col)

        assert action["card_type"] == {
            "note_type_id": note_type["id"],
            "template_id": note_type["tmpls"][0]["id"],
            "name": f"{VOCAB}<::>Recognition",
        }

    def test_a_name_nothing_answers_to_is_reported_and_left_alone(self, col, config):
        definition = d.staged(definition_name="orphan", note_types=["CA Gone"])
        store(config, definition)

        result = reconcile(config, mw.col)

        assert names(result.unresolved) == ["CA Gone"]
        assert result.unresolved[0].definition_name == "orphan"
        assert definition["triggers"]["note_types"] == [{"id": None, "name": "CA Gone"}]


class TestRefreshingACachedName:
    def test_a_renamed_deck_keeps_its_id_and_gets_its_new_name(self, col, config):
        definition = d.staged(deck_names=["JP vocab"])
        store(config, definition)
        reconcile(config, mw.col)
        deck_id = col.decks.id_for_name("JP vocab")

        col.decks.rename(deck_id, "JP words")
        result = reconcile(config, mw.col)

        assert definition["triggers"]["deck_names"] == [{"id": deck_id, "name": "JP words"}]
        assert result.changed is True

    def test_a_renamed_note_type_keeps_its_id_and_gets_its_new_name(self, col, config):
        definition = d.staged(note_types=[VOCAB])
        store(config, definition)
        reconcile(config, mw.col)
        note_type = col.models.by_name(VOCAB)

        note_type["name"] = "CA Words"
        col.models.update_dict(note_type)
        reconcile(config, mw.col)

        assert definition["triggers"]["note_types"] == [
            {"id": note_type["id"], "name": "CA Words"}
        ]

    def test_a_deleted_deck_is_reported_rather_than_rebound(self, col, config):
        definition = d.staged(definition_name="whitelisted", deck_names=["Other"])
        store(config, definition)
        reconcile(config, mw.col)

        col.decks.remove([col.decks.id_for_name("Other")])
        result = reconcile(config, mw.col)

        assert "Other" in names(result.gone) + names(result.unresolved)


# Following a renamed field ---------------------------------------------------------------


class TestAFieldOfTheTriggerNoteTypeIsRenamed:
    """The one thing an id cannot do for a definition: a field is stored as its name.

    The snapshot keeps the field ids of every trigger note type with the names they had, so
    a changed name under an unchanged id is a rename with both names in hand -- and the
    definition's field slots and its `{{trigger.X}}` tokens are rewritten in one step.
    """

    def a_definition_reading_word(self):
        write = d.write("Word", d.text("{{trigger.Word}} and {{c1::{{trigger.Word}}}}"))
        write["unfocus_trigger_fields"] = ["Word"]
        loop_write = d.write("Word", d.text("{{note.Word}}"))
        query = d.note_query("found", "Word:neko {{trigger.Word}}")
        query["selection"]["sort_field"] = "Word"
        return d.staged(
            definition_name="reads word",
            note_types=[VOCAB],
            stages=[
                query,
                d.variable("code_value", d.code("note['Word'] + '{{trigger.Word}}'")),
                d.edit_note("trigger", fields=[write]),
                d.for_each_note("found", [d.edit_note("note", fields=[loop_write])]),
            ],
            on_unfocus={"edit_fields": ["Word"], "add_fields": ["Word"]},
        )

    @pytest.fixture
    def reconciled(self, col, config):
        definition = self.a_definition_reading_word()
        store(config, definition)
        reconcile(config, mw.col)
        rename_field(col, VOCAB, "Word", "Term")
        result = reconcile(config, mw.col)
        return definition, result

    def test_a_field_write_on_the_trigger_names_the_new_field(self, reconciled):
        definition, _ = reconciled
        assert definition["stages"][2]["fields"][0]["field"] == "Term"

    def test_a_field_write_on_another_binding_is_left_alone(self, reconciled):
        definition, _ = reconciled
        loop_body = definition["stages"][3]["body"][0]
        assert loop_body["fields"][0]["field"] == "Word"

    def test_the_unfocus_lists_name_the_new_field(self, reconciled):
        definition, _ = reconciled
        assert definition["triggers"]["on_unfocus"] == {
            "edit_fields": ["Term"],
            "add_fields": ["Term"],
        }
        assert definition["stages"][2]["fields"][0]["unfocus_trigger_fields"] == ["Term"]

    def test_a_reference_in_a_value_is_rewritten_inside_a_cloze_too(self, reconciled):
        definition, _ = reconciled
        assert (
            definition["stages"][2]["fields"][0]["value"]["text"]
            == "{{trigger.Term}} and {{c1::{{trigger.Term}}}}"
        )

    def test_a_reference_in_a_query_is_rewritten_but_a_search_term_is_not(self, reconciled):
        definition, _ = reconciled
        assert definition["stages"][0]["query"]["text"] == "Word:neko {{trigger.Term}}"

    def test_the_sort_field_is_not_rewritten(self, reconciled):
        definition, _ = reconciled
        assert definition["stages"][0]["selection"]["sort_field"] == "Word"

    def test_code_is_not_rewritten_but_is_reported(self, reconciled):
        definition, result = reconciled
        assert definition["stages"][1]["value"]["code"] == "note['Word'] + '{{trigger.Word}}'"
        assert names(result.not_rewritten) == ["Word"]

    def test_a_reference_on_another_binding_is_left_alone(self, reconciled):
        definition, _ = reconciled
        loop_body = definition["stages"][3]["body"][0]
        assert loop_body["fields"][0]["value"]["text"] == "{{note.Word}}"

    def test_the_rewrite_is_reported(self, reconciled):
        _, result = reconciled
        assert any("Word" in line and "Term" in line for line in result.rewritten)

    def test_a_definition_on_another_note_type_is_untouched(self, col, config):
        other = d.staged(
            definition_name="kanji",
            note_types=[KANJI],
            stages=[d.edit_note("trigger", fields=[d.write("Keyword", d.text("{{trigger.Word}}"))])],
        )
        store(config, other)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)

        assert other["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Word}}"

    def test_two_fields_that_swap_names_swap_the_references_too(self, col, config):
        definition = d.staged(
            note_types=[VOCAB],
            stages=[
                d.edit_note(
                    "trigger",
                    fields=[d.write("Note", d.text("{{trigger.Word}}/{{trigger.Meaning}}"))],
                )
            ],
        )
        store(config, definition)
        reconcile(config, mw.col)

        note_type = col.models.by_name(VOCAB)
        by_name = {field["name"]: field for field in note_type["flds"]}
        by_name["Word"]["name"] = "Meaning"
        by_name["Meaning"]["name"] = "Word"
        col.models.update_dict(note_type)
        reconcile(config, mw.col)

        assert (
            definition["stages"][0]["fields"][0]["value"]["text"]
            == "{{trigger.Meaning}}/{{trigger.Word}}"
        )

    def test_a_deleted_field_is_reported_and_nothing_is_rewritten(self, col, config):
        definition = d.staged(
            definition_name="reads freq",
            note_types=[VOCAB],
            stages=[
                d.edit_note("trigger", fields=[d.write("Note", d.text("{{trigger.Freq}}"))])
            ],
        )
        store(config, definition)
        reconcile(config, mw.col)

        note_type = col.models.by_name(VOCAB)
        freq = next(field for field in note_type["flds"] if field["name"] == "Freq")
        col.models.remove_field(note_type, freq)
        col.models.update_dict(note_type)
        result = reconcile(config, mw.col)

        assert names(result.gone) == ["Freq"]
        assert definition["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Freq}}"

    def test_an_undone_rename_is_followed_back(self, col, config):
        definition = d.staged(
            note_types=[VOCAB],
            stages=[
                d.edit_note("trigger", fields=[d.write("Note", d.text("{{trigger.Word}}"))])
            ],
        )
        store(config, definition)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)
        assert definition["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Term}}"

        col.undo()
        reconcile(config, mw.col)

        assert definition["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Word}}"

    def test_a_field_with_no_id_cannot_be_followed_and_says_so(self, col, config):
        note_type = col.models.by_name(VOCAB)
        for field in note_type["flds"]:
            field["id"] = None
        col.models.update_dict(note_type)
        definition = d.staged(
            definition_name="unfollowable",
            note_types=[VOCAB],
            stages=[
                d.edit_note("trigger", fields=[d.write("Note", d.text("{{trigger.Word}}"))])
            ],
        )
        store(config, definition)
        result = reconcile(config, mw.col)

        assert "Word" in names(result.unfollowable)
        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)

        assert definition["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Word}}"


class TestACardTypeOfTheTriggerNoteTypeIsRenamed:
    def test_a_card_value_reference_names_the_new_card_type(self, col, config):
        definition = d.staged(
            note_types=[VOCAB],
            stages=[
                d.edit_note(
                    "trigger",
                    fields=[d.write("Note", d.text("{{trigger.Recognition__Card_Due}}"))],
                )
            ],
        )
        store(config, definition)
        reconcile(config, mw.col)

        rename_template(col, VOCAB, "Recognition", "Reading card")
        reconcile(config, mw.col)

        assert (
            definition["stages"][0]["fields"][0]["value"]["text"]
            == "{{trigger.Reading card__Card_Due}}"
        )

    def test_a_card_action_gets_its_new_display_name(self, col, config):
        note_type = col.models.by_name(VOCAB)
        action = d.card_action_ref(note_type, note_type["tmpls"][0], set_flag=2)
        definition = d.staged(
            note_types=[VOCAB], stages=[d.edit_note("trigger", card_actions=[action])]
        )
        store(config, definition)
        reconcile(config, mw.col)

        rename_template(col, VOCAB, "Recognition", "Reading card")
        reconcile(config, mw.col)

        assert action["card_type"]["name"] == f"{VOCAB}<::>Reading card"


class TestAnActionThatNamesNoCardType:
    """An `edit_card` stage's actions name no card type: the stage already named the card.

    The old editor wrote one empty `card_type_name` string for them, so a config the 0.5.0
    step has been over is full of references with no name and no ids. That is not a
    reference to anything, and the pass has nothing to bind, report or refresh for it.
    """

    def a_definition_with_one(self):
        action = d.card_action("CA Vocab", "Recognition", set_flag=2)
        del action["card_type_name"]
        action["card_type"] = {"note_type_id": None, "template_id": None, "name": ""}
        return d.staged(
            definition_name="single card",
            note_types=[],
            stages=[
                d.card_query("cards", "deck:JP vocab"),
                d.for_each_card("cards", [d.edit_card("card", [action])]),
            ],
        )

    def test_it_is_not_reported_as_unresolved(self, col, config):
        store(config, self.a_definition_with_one())

        result = reconcile(config, mw.col)

        assert names(result.unresolved) == []

    def test_a_config_that_holds_only_those_has_nothing_to_follow(self, col, config):
        store(config, self.a_definition_with_one())

        assert definitions_hold_references(config.copy_definitions) is False

    def test_a_migrated_0_4_0_config_gives_the_pass_nothing_to_do(self, col, stub_mw):
        """End to end: what 0.4.0 stored, through the migration, into the pass."""
        action = d.card_action("CA Vocab", "Recognition", set_flag=2)
        action["card_type_name"] = ""
        stored = dict(DEFAULT_CONFIG)
        stored["version"] = "0.4.0"
        stored["copy_definitions"] = [
            d.staged(
                definition_name="single card",
                note_types=[],
                stages=[
                    d.card_query("cards", "deck:JP vocab"),
                    d.for_each_card("cards", [d.edit_card("card", [action])]),
                ],
            )
        ]
        stub_mw.addonManager.configs[ADDON_TAG] = stored
        migrate_config()
        configuration = Config()
        configuration.load()

        result = reconcile(configuration, mw.col)

        assert names(result.unresolved) == []
        assert definitions_hold_references(configuration.copy_definitions) is False


# What the pass reports without rewriting ------------------------------------------------


class TestTheSearchTermsInTheReport:
    """A query's names are checked every run, not only after a rename.

    Nothing rewrites them -- `col.replace_in_search_node` swaps every term of a kind at
    once, so one deck inside a query naming two cannot be renamed -- and a query can go
    stale on another device, where no rename this profile can see ever happened.
    """

    def query_definition(self, query: str) -> dict:
        return d.staged(note_types=[VOCAB], stages=[d.note_query("found", query)])

    def test_a_deck_a_query_names_but_the_collection_has_not_is_reported(self, col, config):
        store(config, self.query_definition("deck:Nowhere"))

        result = reconcile(config, mw.col)

        assert [(stale.kind, stale.name) for stale in result.stale_terms] == [
            ("deck", "Nowhere")
        ]

    def test_a_renamed_field_left_in_a_search_term_is_reported(self, col, config):
        store(config, self.query_definition("Word:neko"))
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        result = reconcile(config, mw.col)

        assert [(stale.kind, stale.name) for stale in result.stale_terms] == [
            ("field", "Word")
        ]

    def test_a_query_naming_only_live_things_reports_nothing(self, col, config):
        store(config, self.query_definition('deck:"JP vocab" Word:neko'))

        assert reconcile(config, mw.col).stale_terms == []

    def test_a_search_built_by_code_is_left_to_the_code_report(self, col, config):
        stage = d.note_query("found", "")
        stage["query"] = d.code('return "deck:Nowhere"')
        store(config, d.staged(note_types=[VOCAB], stages=[stage]))

        assert reconcile(config, mw.col).stale_terms == []

    def test_the_report_does_not_make_the_pass_write(self, col, config, stub_mw):
        store(config, self.query_definition("deck:Nowhere"))
        reconcile(config, mw.col)
        before = saves(stub_mw)

        result = reconcile(config, mw.col)

        assert result.stale_terms and result.changed is False
        assert saves(stub_mw) == before


# When it runs ------------------------------------------------------------------------------


class TestWhenThePassRuns:
    def test_an_operation_that_changed_neither_does_nothing(self, col, config, stub_mw):
        definition = d.staged(note_types=[VOCAB])
        store(config, definition)

        on_operation_did_execute(FakeChanges(notetype=False, deck=False), None)

        assert definition["triggers"]["note_types"][0]["id"] is None
        assert saves(stub_mw) == 0

    def test_an_operation_that_changed_a_note_type_runs_the_pass(self, col, config, stub_mw):
        definition = d.staged(note_types=[VOCAB])
        store(config, definition)

        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        stored = stub_mw.addonManager.configs[ADDON_TAG]["copy_definitions"][0]
        assert stored["triggers"]["note_types"][0]["id"] == col.models.by_name(VOCAB)["id"]

    def test_nothing_is_written_when_nothing_changed(self, col, config, stub_mw):
        definition = d.staged(note_types=[VOCAB], deck_names=["JP vocab"])
        store(config, definition)
        reconcile(config, mw.col)
        before = saves(stub_mw)

        result = reconcile(config, mw.col)

        assert result.changed is False
        assert saves(stub_mw) == before

    def test_a_config_with_no_references_is_left_alone(self, col, config, stub_mw):
        store(config, d.staged(note_types=[]))

        result = reconcile(config, mw.col)

        assert result.changed is False
        assert saves(stub_mw) == 0


    def test_both_hooks_get_a_handler(self):
        """The pass is only as good as its registration, and nothing else would say."""
        from aqt.gui_hooks import collection_did_load, operation_did_execute
        from copy_anywhere.hooks import rename_hooks

        before = (collection_did_load.count(), operation_did_execute.count())
        try:
            rename_hooks.init_rename_hooks()
            assert collection_did_load.count() == before[0] + 1
            assert operation_did_execute.count() == before[1] + 1
        finally:
            collection_did_load.remove(rename_hooks.on_collection_did_load)
            operation_did_execute.remove(rename_hooks.on_operation_did_execute)


class FakeChanges:
    """The two booleans of `OpChanges` the pass reads."""

    def __init__(self, notetype: bool, deck: bool) -> None:
        self.notetype = notetype
        self.deck = deck


def test_the_pass_does_not_disturb_a_definition_that_still_runs(col, config, logger):
    """A rename the pass followed leaves a definition that does what it always did."""
    note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
    definition = d.staged(
        note_types=[VOCAB],
        stages=[d.edit_note("trigger", fields=[d.write("Note", d.text("{{trigger.Word}}"))])],
    )
    store(config, definition)
    reconcile(config, mw.col)
    rename_field(col, VOCAB, "Word", "Term")
    reconcile(config, mw.col)

    from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note

    note = col.get_note(note.id)
    assert copy_for_single_trigger_note(definition, note) is True
    assert note["Note"] == "neko"
    assert not logger.errors
