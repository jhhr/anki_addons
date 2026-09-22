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

import shutil
from contextlib import contextmanager

import pytest
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import DEFAULT_CONFIG, KANJI, VOCAB, VOCAB_FIELDS, VOCAB_TEMPLATES
from copy_anywhere.configuration import Config, migrate_config
from copy_anywhere.hooks.rename_hooks import on_operation_did_execute
from copy_anywhere.logic.rename_reconcile import (
    SNAPSHOT_KEY,
    definitions_hold_references,
    reconcile,
)

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


@contextmanager
def opened(stub_mw, collection):
    """Run a block with `mw` pointing at this collection, as a profile switch leaves it.

    The pass is given the collection to reconcile, but the save at the end of it rebuilds
    the snapshot from `mw.col` (`Config._save_definitions`), so a test that moved one and
    not the other would be testing a state Anki never has.
    """
    previous = stub_mw.col
    stub_mw.col = collection
    try:
        yield collection
    finally:
        stub_mw.col = previous


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
        deck_id = col.decks.id_for_name("Other")

        col.decks.remove([deck_id])
        result = reconcile(config, mw.col)

        # Gone, not unresolved: the snapshot watched this deck being bound, so the pass
        # knows the user deleted it rather than that the name never answered to anything.
        assert names(result.gone) == ["Other"]
        assert names(result.unresolved) == []
        assert definition["triggers"]["deck_names"] == [{"id": deck_id, "name": "Other"}]


class TestAnObjectTheSnapshotKnewAndTheCollectionNoLongerHas:
    """`gone` is what the snapshot remembers; `unresolved` is what nothing ever bound.

    The two lists answer different questions for the user. A name in `unresolved` is one
    this collection has never had -- a definition written for a note type not made yet, a
    typo, a definition copied from another profile -- and the fix is to make the object or
    correct the name. A name in `gone` is one the pass watched being bound and then
    watched disappear, so it is the user's own deletion, and the last name it had is the
    one the report can name it by. The deck half is
    `TestRefreshingACachedName.test_a_deleted_deck_is_reported_rather_than_rebound`.
    """

    def test_a_deleted_note_type_is_reported_gone_rather_than_unresolved(self, col, config):
        definition = d.staged(definition_name="on kanji", note_types=[KANJI])
        store(config, definition)
        reconcile(config, mw.col)

        col.models.remove(col.models.by_name(KANJI)["id"])
        result = reconcile(config, mw.col)

        assert names(result.gone) == [KANJI]
        assert names(result.unresolved) == []
        assert result.gone[0].definition_name == "on kanji"

    def test_a_name_the_snapshot_never_knew_is_still_unresolved(self, col, config):
        definition = d.staged(definition_name="orphan", note_types=["CA Gone"])
        store(config, definition)

        result = reconcile(config, mw.col)

        assert names(result.unresolved) == ["CA Gone"]
        assert names(result.gone) == []

    def test_a_note_type_deleted_and_remade_under_its_name_is_rebound_quietly(
        self, col, config
    ):
        definition = d.staged(note_types=[KANJI])
        store(config, definition)
        reconcile(config, mw.col)

        # Made before the old one is removed, so the two cannot share an id: Anki keys a
        # note type by the millisecond it was made, and a suite is quick enough to make
        # the replacement inside the same one.
        replacement = real_anki.make_note_type(col, "CA Kanji 2", ["Kanji"], None)
        col.models.remove(col.models.by_name(KANJI)["id"])
        replacement["name"] = KANJI
        col.models.update_dict(replacement)
        result = reconcile(config, mw.col)

        assert definition["triggers"]["note_types"] == [
            {"id": col.models.by_name(KANJI)["id"], "name": KANJI}
        ]
        assert names(result.gone) == [] and names(result.unresolved) == []


# A pass on another collection --------------------------------------------------------------


def a_copy_of(collection, path):
    """The same collection opened at a second path: a backup restored as another profile.

    What a copy keeps is the *ids*, which is the case a config shared by every profile
    cannot tell from a rename on its own: the second collection answers to the first's note
    type, deck and field ids, under whatever names it has been given since. The checkpoint
    is because Anki writes through a WAL, so the file on its own is the collection without
    its last few operations in it.
    """
    collection.db.execute("pragma wal_checkpoint(TRUNCATE)")
    shutil.copyfile(collection.path, path)
    return real_anki.open_collection(path)


class TestAPassOnAnotherCollection:
    """Ids belong to the collection that issued them; this addon's config belongs to none.

    The config lives in the addon's `meta.json`, which every profile on the machine shares,
    so a second profile's first `collection_did_load` hands the pass definitions bound to
    another collection's ids and a snapshot of names that were never this collection's.
    Read as a rename, that rewrites a definition's field slots and `{{trigger....}}` tokens
    against the wrong collection, and switching back does it again. So the snapshot records
    which collection it came from, and a pass that finds another one re-binds by the one
    rule, replaces the snapshot, rewrites nothing and says so once.
    """

    def a_definition_reading_word(self):
        return d.staged(
            definition_name="reads word",
            note_types=[VOCAB],
            stages=[
                d.edit_note("trigger", fields=[d.write("Note", d.text("{{trigger.Word}}"))])
            ],
        )

    def test_a_field_renamed_in_the_other_collection_is_not_a_rename(
        self, col, config, stub_mw, tmp_path
    ):
        definition = self.a_definition_reading_word()
        store(config, definition)
        reconcile(config, mw.col)

        other = a_copy_of(col, tmp_path / "second.anki2")
        rename_field(other, VOCAB, "Word", "Term")
        with opened(stub_mw, other):
            result = reconcile(config, other)
        other.close()

        assert definition["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Word}}"
        assert result.rewritten == []

    def test_the_snapshot_is_replaced_with_the_collection_in_front_of_it(
        self, col, config, stub_mw, tmp_path
    ):
        store(config, self.a_definition_reading_word())
        reconcile(config, mw.col)

        other = a_copy_of(col, tmp_path / "second.anki2")
        rename_field(other, VOCAB, "Word", "Term")
        with opened(stub_mw, other):
            reconcile(config, other)
        snapshot = config.data[SNAPSHOT_KEY]
        other.close()

        assert snapshot["collection"] == other.path
        entry = snapshot["note_types"][str(col.models.by_name(VOCAB)["id"])]
        assert sorted(entry["fields"].values()) == sorted(
            ["Term" if name == "Word" else name for name in VOCAB_FIELDS]
        )

    def test_the_change_of_collection_is_reported_once(
        self, col, config, stub_mw, tmp_path
    ):
        store(config, self.a_definition_reading_word())
        reconcile(config, mw.col)

        other = a_copy_of(col, tmp_path / "second.anki2")
        with opened(stub_mw, other):
            result = reconcile(config, other)
        other.close()

        assert result.collection_changed is not None
        assert col.path in result.collection_changed
        assert other.path in result.collection_changed

    def test_the_same_collection_still_follows_its_own_renames(self, col, config):
        definition = self.a_definition_reading_word()
        store(config, definition)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        result = reconcile(config, mw.col)

        assert definition["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Term}}"
        assert result.collection_changed is None

    def test_a_collection_that_never_had_the_ids_re_binds_by_name(
        self, col, config, stub_mw, tmp_path
    ):
        """The other kind of second profile: made separately, so the names are the shared
        thing and the ids are not. Every reference falls through to its name, the card
        action's two halves included."""
        note_type = col.models.by_name(VOCAB)
        action = d.card_action_ref(note_type, note_type["tmpls"][0], set_flag=2)
        definition = d.staged(
            note_types=[VOCAB],
            stages=[d.edit_note("trigger", card_actions=[action])],
        )
        store(config, definition)
        reconcile(config, mw.col)

        other = real_anki.open_collection(tmp_path / "separate.anki2")
        theirs = real_anki.make_note_type(other, VOCAB, VOCAB_FIELDS, VOCAB_TEMPLATES)
        with opened(stub_mw, other):
            result = reconcile(config, other)
        other.close()

        assert definition["triggers"]["note_types"] == [
            {"id": theirs["id"], "name": VOCAB}
        ]
        assert action["card_type"] == {
            "note_type_id": theirs["id"],
            "template_id": theirs["tmpls"][0]["id"],
            "name": f"{VOCAB}<::>Recognition",
        }
        assert names(result.unresolved) == []


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


class TestAConfigThePassCannotRead:
    """A config the pass chokes on must not take the pass out for the rest of the session.

    `operation_did_execute.__call__` drops a hook that raises and re-raises, so anything
    the handler lets escape unregisters it silently until Anki is restarted.
    """

    def test_a_config_that_cannot_be_read_at_all_does_not_escape(
        self, col, config, stub_mw, monkeypatch
    ):
        """`getConfig` on a corrupt `meta.json` raises, and the load is the first thing done."""
        from copy_anywhere.hooks import rename_hooks

        def raise_on_load(self):
            raise ValueError("meta.json is not JSON")

        monkeypatch.setattr(Config, "load", raise_on_load)

        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        assert rename_hooks._running is False

    def test_a_definition_the_scan_cannot_read_does_not_escape(
        self, col, config, stub_mw, monkeypatch
    ):
        """A stored definition shaped wrongly enough to trip the scan for references."""
        from copy_anywhere.hooks import rename_hooks

        definition = d.staged(note_types=[VOCAB])
        definition["triggers"] = [VOCAB]

        def load_a_broken_config(self):
            self.data = dict(DEFAULT_CONFIG)
            self.data["copy_definitions"] = [definition]

        monkeypatch.setattr(Config, "load", load_a_broken_config)

        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        assert rename_hooks._running is False


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
