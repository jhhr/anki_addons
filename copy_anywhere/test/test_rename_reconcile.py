"""The reconcile pass: what a stored definition does when Anki renames something.

A definition stores an id beside the name for every object Anki gives a stable id
(`test_object_refs.py`), which is what keeps it running after a rename. What that alone
does not do is tell the *user* which name a definition is still spelling, and it cannot
help a field at all -- a field is stored as the name it is written with, everywhere.

This pass closes both. It runs when the collection loads, after any operation that changed
a note type, and after one that changed the decks' names or ids (not merely reported a deck
change, as every answer does), compares the live names against a snapshot kept per id, and
so has both names of a rename in hand -- which no Anki hook gives. Cached names are
refreshed, a renamed field or card type of a trigger note type is followed into the
definition's field slots and its `{{trigger....}}` tokens -- in a definition with one
trigger note type. What it will not rewrite gets a warning filed at the location that
spells it -- a slot or token of a definition on several note types, a search term, another
binding's token, a sort field, a string in code, a deleted field, and for a deck or note
type any search or code -- and a blocking one keeps the definition from running until the
user has dealt with it.
"""

import copy
import html
import shutil
from contextlib import contextmanager

import pytest
from anki.scheduler.v3 import CardAnswer
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from note_types import DEFAULT_CONFIG, KANJI, VOCAB, VOCAB_FIELDS, VOCAB_TEMPLATES
from copy_anywhere.configuration import Config, migrate_config
from copy_anywhere.hooks import rename_hooks
from copy_anywhere.hooks.rename_hooks import on_operation_did_execute
from copy_anywhere.logic.copy_fields import (
    CacheResults,
    copy_fields_in_background,
    copy_for_single_trigger_note,
)
from copy_anywhere.logic.rename_reconcile import (
    KIND_CARD_TYPE,
    KIND_DECK,
    KIND_FIELD,
    SNAPSHOT_KEY,
    definitions_hold_references,
    log_result,
    reconcile,
    unresolved_references,
)
from copy_anywhere.logic.rename_locations import (
    card_action_key,
    field_write_key,
    stage_key,
    trigger_key,
)
from copy_anywhere.logic.rename_warnings import BLOCKING_ADVICE, WARNINGS_KEY, blocking_messages

ADDON_TAG = "copy_anywhere"

#: A location no stage of these definitions has; the readers file an entry anywhere.
ORPHAN_LOCATION = "definition"


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


def swap_fields(col, note_type_name: str, one: str, other: str) -> None:
    """Two fields trade names in one save, which only an edit of the note type can do."""
    note_type = col.models.by_name(note_type_name)
    for field in note_type["flds"]:
        field["name"] = {one: other, other: one}.get(field["name"], field["name"])
    col.models.update_dict(note_type)


def names(items) -> list[str]:
    return [item.name for item in items]


def marks(definition) -> list:
    """Every warning the pass filed about a definition, wherever it is filed, in order."""
    return [entry for entries in definition[WARNINGS_KEY].values() for entry in entries]


def located(definition) -> dict:
    """Where each warning is filed, and what it says: `{key: [(old, new, blocks_run)]}`."""
    return {
        key: [(entry["old"], entry["new"], entry["blocks_run"]) for entry in entries]
        for key, entries in definition.get(WARNINGS_KEY, {}).items()
    }


@contextmanager
def opened(stub_mw, collection):
    """Run a block with `mw` pointing at this collection, as a profile switch leaves it.

    The pass is given the collection to reconcile, but every other save rebuilds the
    snapshot from `mw.col` (`Config._save_definitions`), so a test that moved one and not
    the other would be testing a state Anki never has.
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


class TestACardTypeTheSnapshotKnewAndTheCollectionNoLongerHas:
    """The same distinction for a card action, whose card type can go two ways.

    The note type can be deleted, taking every template with it, or kept and the one
    template removed from it. Either way the snapshot recorded the template's id under its
    note type's, so the pass knows the user deleted it. The note type here is named only by
    the card action -- the definition triggers on another -- because a trigger note type's
    templates are already watched for the definition's own slots (`_diff`), and this is the
    reference nothing else would report.
    """

    @pytest.fixture
    def extra(self, col):
        return real_anki.make_note_type(
            col,
            "CA Extra",
            ["F"],
            [("Card 1", "{{F}}", "{{F}}"), ("Card 2", "{{F}}x", "{{F}}")],
        )

    def a_definition_flagging(self, config, model, template):
        definition = d.staged(
            definition_name="flags extra",
            note_types=[VOCAB],
            stages=[
                d.edit_note(
                    "trigger", card_actions=[d.card_action_ref(model, template, set_flag=1)]
                )
            ],
        )
        store(config, definition)
        reconcile(config, mw.col)
        return definition

    def test_a_deleted_note_type_is_reported_gone_once(self, col, config, extra):
        self.a_definition_flagging(config, extra, extra["tmpls"][1])

        col.models.remove(extra["id"])
        result = reconcile(config, mw.col)

        # Once, though the template went with it: the note type is the one that was deleted.
        assert names(result.gone) == ["CA Extra<::>Card 2"]
        assert names(result.unresolved) == []
        assert result.gone[0].definition_name == "flags extra"
        assert result.gone[0].kind == "card type"

    def test_a_template_deleted_from_a_live_note_type_is_reported_gone(
        self, col, config, extra
    ):
        self.a_definition_flagging(config, extra, extra["tmpls"][1])

        model = col.models.get(extra["id"])
        col.models.remove_template(model, model["tmpls"][1])
        col.models.update_dict(model)
        result = reconcile(config, mw.col)

        assert names(result.gone) == ["CA Extra<::>Card 2"]
        assert names(result.unresolved) == []

    def test_the_name_reported_is_the_last_one_it_had(self, col, config, extra):
        definition = self.a_definition_flagging(config, extra, extra["tmpls"][1])
        rename_template(col, "CA Extra", "Card 2", "Reverse")
        reconcile(config, mw.col)

        model = col.models.get(extra["id"])
        col.models.remove_template(model, model["tmpls"][1])
        col.models.update_dict(model)
        result = reconcile(config, mw.col)

        # The reference's own cached name, refreshed by the pass before: it is what the
        # picker's live check spells too, so the two show as one entry.
        assert names(result.gone) == ["CA Extra<::>Reverse"]
        assert unresolved_references(definition, col)[0].name == "CA Extra<::>Reverse"

    def test_the_log_says_it_has_been_deleted(self, col, config, extra, logger):
        self.a_definition_flagging(config, extra, extra["tmpls"][1])
        col.models.remove(extra["id"])

        log_result(reconcile(config, mw.col))

        assert any(
            "card type 'CA Extra<::>Card 2'" in line and "has been deleted" in line
            for line in logger.warnings
        )

    def test_the_editor_still_refuses_to_save_it(self, col, config, extra):
        definition = self.a_definition_flagging(config, extra, extra["tmpls"][1])

        col.models.remove(extra["id"])
        reconcile(config, mw.col)

        # Gone is what the report says; the reference still names nothing all the same.
        assert names(unresolved_references(definition, col)) == ["CA Extra<::>Card 2"]

    def test_a_card_type_the_snapshot_never_knew_is_still_unresolved(
        self, col, config, extra
    ):
        action = d.card_action_ref(extra, extra["tmpls"][1], set_flag=1)
        action["card_type"] = {
            "note_type_id": extra["id"],
            "template_id": None,
            "name": "CA Extra<::>Card 3",
        }
        store(
            config,
            d.staged(note_types=[VOCAB], stages=[d.edit_note("trigger", card_actions=[action])]),
        )
        reconcile(config, mw.col)

        result = reconcile(config, mw.col)

        assert names(result.unresolved) == ["CA Extra<::>Card 3"]
        assert names(result.gone) == []

    @pytest.mark.parametrize("deleted", ["note type", "template"])
    def test_a_deletion_in_another_collection_is_unresolved(
        self, col, config, extra, stub_mw, tmp_path, deleted
    ):
        """The other collection shares the ids, but the snapshot is not about it, so
        nothing in it is evidence that the user deleted anything there."""
        self.a_definition_flagging(config, extra, extra["tmpls"][1])

        other = a_copy_of(col, tmp_path / "second.anki2")
        if deleted == "note type":
            other.models.remove(extra["id"])
        else:
            model = other.models.get(extra["id"])
            other.models.remove_template(model, model["tmpls"][1])
            other.models.update_dict(model)
        with opened(stub_mw, other):
            result = reconcile(config, other)
        other.close()

        assert result.collection_changed is not None
        assert names(result.unresolved) == ["CA Extra<::>Card 2"]
        assert names(result.gone) == []


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
    which collection it came from, by its path, and a pass that finds another one
    re-binds by the one rule, replaces the snapshot, rewrites nothing and says so once.
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
        """A copy at another path is another collection, even when it is the same one synced
        to another desktop: the known cost of identifying collections by path."""
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
            again = reconcile(config, other)
        other.close()

        assert result.collection_changed is not None
        assert col.path in result.collection_changed
        assert other.path in result.collection_changed
        assert again.collection_changed is None

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

    def test_a_snapshot_stamped_with_a_creation_time_is_this_collections_and_restamped(
        self, col, config, stub_mw
    ):
        """A version of this addon stamped snapshots with `collection_crt` and no path. Such
        a snapshot is read as this collection's, as one with no stamp is, and the first pass
        restamps it with the path even when nothing else changed, or it would be read so
        in every profile for good."""
        definition = self.a_definition_reading_word()
        store(config, definition)
        reconcile(config, mw.col)
        snapshot = config.data[SNAPSHOT_KEY]
        del snapshot["collection"]
        snapshot["collection_crt"] = col.crt - 30 * 86400
        written = saves(stub_mw)

        restamp = reconcile(config, mw.col)
        restamped = dict(config.data[SNAPSHOT_KEY])
        rename_field(col, VOCAB, "Word", "Term")
        followed = reconcile(config, mw.col)

        assert restamp.collection_changed is None
        assert saves(stub_mw) > written
        assert restamped["collection"] == col.path
        assert "collection_crt" not in restamped
        assert followed.collection_changed is None
        assert definition["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Term}}"


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

    def test_code_is_not_rewritten_and_is_warned_where_it_spells_the_name(
        self, col, reconciled
    ):
        definition, result = reconciled
        assert definition["stages"][1]["value"]["code"] == "note['Word'] + '{{trigger.Word}}'"
        # Half followed: the text reads `Term`, the code still `Word`, so it is not run until
        # the user has looked. The warning is filed at the code, and says only what happened.
        code_key = stage_key(definition["stages"][1]["guid"], "value.code")
        assert definition[WARNINGS_KEY][code_key] == [
            {
                "kind": KIND_FIELD,
                "note_type_id": col.models.id_for_name(VOCAB),
                "object_id": field_id(col, VOCAB, "Term"),
                "old": "Word",
                "new": "Term",
                "blocks_run": True,
                "message": f'Field "Word" of note type "{VOCAB}" was renamed to "Term"',
            }
        ]
        blocking = [stale.location for stale in result.newly_marked if stale.blocks_run]
        assert blocking == [code_key]

    def test_what_was_not_followed_is_warned_where_it_is_spelled(self, reconciled):
        definition, _ = reconciled
        query_guid = definition["stages"][0]["guid"]
        loop_write_guid = definition["stages"][3]["body"][0]["fields"][0]["guid"]
        renamed = ("Word", "Term", False)
        # The trigger's slots and tokens were followed and say nothing; a field search, the
        # sort field and another binding's field name notes whose note types the pass cannot
        # know, so they only warn; code blocks.
        assert located(definition) == {
            stage_key(query_guid, "query.text"): [renamed],
            stage_key(query_guid, "selection.sort_field"): [renamed],
            stage_key(definition["stages"][1]["guid"], "value.code"): [("Word", "Term", True)],
            field_write_key(loop_write_guid, "field"): [renamed],
            field_write_key(loop_write_guid, "value.text"): [renamed],
        }

    def test_code_renamed_again_is_updated(self, col, reconciled, config):
        definition, _ = reconciled

        rename_field(col, VOCAB, "Term", "Headword")
        reconcile(config, mw.col)

        assert definition["stages"][2]["fields"][0]["field"] == "Headword"
        code_key = stage_key(definition["stages"][1]["guid"], "value.code")
        assert [entry["message"] for entry in definition[WARNINGS_KEY][code_key]] == [
            f'Field "Word" of note type "{VOCAB}" was renamed to "Headword"'
        ]
        assert {entry["new"] for entry in marks(definition)} == {"Headword"}

    def test_undoing_it_follows_it_back_and_removes_the_mark(self, col, reconciled, config):
        definition, _ = reconciled

        col.undo()
        reconcile(config, mw.col)

        assert definition["stages"][2]["fields"][0]["field"] == "Word"
        assert WARNINGS_KEY not in definition

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

    def test_a_deleted_field_marks_the_definition_and_nothing_is_rewritten(self, col, config):
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

        assert names(result.gone) == []
        assert names(result.newly_marked) == ["Freq"]
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


def field_id(col, note_type_name: str, name: str) -> int:
    return next(
        field["id"] for field in col.models.by_name(note_type_name)["flds"] if field["name"] == name
    )


def template_id(col, note_type_name: str, name: str) -> int:
    return next(
        template["id"]
        for template in col.models.by_name(note_type_name)["tmpls"]
        if template["name"] == name
    )


class TestARenameInADefinitionOnSeveralNoteTypesIsMarked:
    """A definition triggering on several note types spells a name once for all of them.

    A rename in one of them leaves it wrong whichever name it spells, and every rule that
    tried to decide which one it should spell found a way to decide wrongly. So it is never
    rewritten: it is marked, with a sentence naming the object and both names, and the user
    decides. The number of trigger note types is the number the definition stores, whether
    they resolve or not.
    """

    OTHER = "CA Vocab B"

    @pytest.fixture
    def other(self, col):
        real_anki.make_note_type(
            col, self.OTHER, ["Word", "Meaning"], [("Recognition", "{{Word}}", "{{Meaning}}")]
        )
        return self.OTHER

    def definition(self, *note_types, text="{{trigger.Word}}"):
        return d.staged(
            definition_name="both",
            note_types=list(note_types),
            stages=[d.edit_note("trigger", fields=[d.write("Meaning", d.text(text))])],
            on_unfocus={"edit_fields": ["Word"], "add_fields": []},
        )

    def spelled(self, definition):
        return (
            definition["stages"][0]["fields"][0]["value"]["text"],
            definition["triggers"]["on_unfocus"]["edit_fields"],
        )

    def entry(self, col, old="Word", new="Term", note_type=VOCAB, message=None):
        return {
            "kind": KIND_FIELD,
            "note_type_id": col.models.id_for_name(note_type),
            "object_id": field_id(col, note_type, new if new is not None else old),
            "old": old,
            "new": new,
            "blocks_run": True,
            "message": message or f'Field "{old}" of note type "{note_type}" was renamed to "{new}"',
        }

    @pytest.fixture
    def marked(self, col, config, other):
        definition = self.definition(VOCAB, other)
        store(config, definition)
        reconcile(config, mw.col)
        rename_field(col, VOCAB, "Word", "Term")
        return definition, reconcile(config, mw.col)

    def test_a_field_rename_is_not_followed(self, marked):
        definition, result = marked

        assert self.spelled(definition) == ("{{trigger.Word}}", ["Word"])
        assert result.rewritten == []

    def where(self, definition) -> list[str]:
        """The two places `definition()` spells `Word`: the unfocus list and the value."""
        write_guid = definition["stages"][0]["fields"][0]["guid"]
        return [
            trigger_key("on_unfocus.edit_fields"),
            field_write_key(write_guid, "value.text"),
        ]

    def test_the_definition_is_marked_with_the_object_and_both_names(
        self, col, marked, stub_mw
    ):
        definition, result = marked

        # One entry per place it spells the name, filed where it spells it.
        assert definition[WARNINGS_KEY] == {
            key: [self.entry(col)] for key in self.where(definition)
        }
        assert [(stale.kind, stale.name, stale.location) for stale in result.newly_marked] == [
            (KIND_FIELD, "Word", key) for key in self.where(definition)
        ]
        assert [stale.message for stale in result.broken] == [self.entry(col)["message"]] * 2
        stored = stub_mw.addonManager.configs[ADDON_TAG]["copy_definitions"][0]
        assert stored[WARNINGS_KEY] == definition[WARNINGS_KEY]

    def test_a_later_pass_still_lists_it_but_not_as_new(self, marked, config):
        definition, _ = marked

        result = reconcile(config, mw.col)

        assert names(result.broken) == ["Word", "Word"]
        assert result.newly_marked == []
        assert result.changed is False

    def test_renaming_the_others_too_is_not_followed_either(self, col, marked, config):
        definition, _ = marked

        rename_field(col, self.OTHER, "Word", "Term")
        result = reconcile(config, mw.col)

        # The user decides: two marks, one per note type, and the definition as it was.
        assert self.spelled(definition) == ("{{trigger.Word}}", ["Word"])
        # In the order the snapshot lists the note types, which is no order in particular.
        assert sorted(marks(definition), key=lambda entry: entry["note_type_id"]) == sorted(
            [self.entry(col), self.entry(col, note_type=self.OTHER)] * 2,
            key=lambda entry: entry["note_type_id"],
        )
        assert names(result.newly_marked) == ["Word", "Word"]

    def test_renamed_again_updates_the_entry(self, col, marked, config):
        definition, _ = marked

        rename_field(col, VOCAB, "Term", "Headword")
        result = reconcile(config, mw.col)

        assert marks(definition) == [self.entry(col, new="Headword")] * 2
        assert result.newly_marked == []
        assert result.changed is True

    def test_renamed_back_removes_the_entry(self, col, marked, config):
        definition, _ = marked

        rename_field(col, VOCAB, "Term", "Word")
        result = reconcile(config, mw.col)

        assert WARNINGS_KEY not in definition
        assert result.broken == [] and result.newly_marked == []

    def test_undo_removes_the_entry(self, col, marked, config):
        definition, _ = marked

        col.undo()
        reconcile(config, mw.col)

        assert WARNINGS_KEY not in definition

    def test_renamed_back_differently_cased_removes_the_entry(self, col, marked, config):
        definition, _ = marked

        rename_field(col, VOCAB, "Term", "word")
        reconcile(config, mw.col)

        # `{{trigger.Word}}` reads `word` again: fields are matched without regard to case.
        assert WARNINGS_KEY not in definition

    def test_renamed_back_after_the_snapshot_moved_on_removes_the_entry(
        self, col, marked, config
    ):
        definition, _ = marked
        rename_field(col, VOCAB, "Term", "Word")
        # A save rebuilds the snapshot with the names as they are, so the pass after it sees
        # no rename at all; the entry's own ids are what still show the rename undone.
        config.update_definition_by_index(0, definition)

        result = reconcile(config, mw.col)

        assert WARNINGS_KEY not in config.copy_definitions[0]
        assert result.broken == []

    def test_a_save_keeps_the_mark(self, col, marked, config):
        definition, _ = marked
        reworked = copy.deepcopy(definition)
        reworked["triggers"]["note_types"] = [d.object_ref(self.OTHER)]

        config.update_definition_by_index(0, reworked)
        reconcile(config, mw.col)

        # Only the user takes a mark off, in the editor; a save does not re-derive it.
        assert marks(config.copy_definitions[0]) == [self.entry(col)] * 2

    def test_the_same_rename_in_both_at_once_marks_it_once_per_note_type(
        self, col, config, other
    ):
        definition = self.definition(VOCAB, other)
        store(config, definition)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        rename_field(col, other, "Word", "Term")
        result = reconcile(config, mw.col)

        assert self.spelled(definition) == ("{{trigger.Word}}", ["Word"])
        # In the order the snapshot lists the note types, which is no order in particular.
        assert sorted(marks(definition), key=lambda entry: entry["note_type_id"]) == sorted(
            [self.entry(col), self.entry(col, note_type=self.OTHER)] * 2,
            key=lambda entry: entry["note_type_id"],
        )
        assert names(result.newly_marked) == ["Word"] * 4

    def test_a_template_rename_is_marked_and_not_followed(self, col, config, other):
        definition = self.definition(VOCAB, other, text="{{trigger.Recognition__Card_Due}}")
        store(config, definition)
        reconcile(config, mw.col)

        rename_template(col, VOCAB, "Recognition", "Reading")
        reconcile(config, mw.col)

        assert self.spelled(definition)[0] == "{{trigger.Recognition__Card_Due}}"
        assert marks(definition) == [
            {
                "kind": KIND_CARD_TYPE,
                "note_type_id": col.models.id_for_name(VOCAB),
                "object_id": template_id(col, VOCAB, "Reading"),
                "old": "Recognition",
                "new": "Reading",
                "blocks_run": True,
                "message": f'Card type "Recognition" of note type "{VOCAB}" was renamed to'
                ' "Reading"',
            }
        ]

    def test_a_template_renamed_back_removes_the_entry(self, col, config, other):
        definition = self.definition(VOCAB, other, text="{{trigger.Recognition__Card_Due}}")
        store(config, definition)
        reconcile(config, mw.col)
        rename_template(col, VOCAB, "Recognition", "Reading")
        reconcile(config, mw.col)

        rename_template(col, VOCAB, "Reading", "Recognition")
        reconcile(config, mw.col)

        assert WARNINGS_KEY not in definition

    def test_a_name_the_definition_does_not_spell_marks_nothing(self, col, config, other):
        definition = self.definition(VOCAB, other)
        store(config, definition)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Reading", "Kana")
        rename_template(col, VOCAB, "Recall", "Production")
        result = reconcile(config, mw.col)

        assert WARNINGS_KEY not in definition
        assert result.broken == [] and result.newly_marked == []

    def test_a_name_only_its_code_mentions_is_marked(self, col, config, other):
        definition = d.staged(
            definition_name="both",
            note_types=[VOCAB, other],
            stages=[d.variable("word", d.code("return trigger['Word']"))],
        )
        store(config, definition)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)

        assert definition[WARNINGS_KEY] == {
            stage_key(definition["stages"][0]["guid"], "value.code"): [self.entry(col)]
        }

    def test_a_swap_marks_both_names(self, col, config, other):
        definition = self.definition(VOCAB, other, text="{{trigger.Word}}/{{trigger.Meaning}}")
        store(config, definition)
        reconcile(config, mw.col)

        swap_fields(col, VOCAB, "Word", "Meaning")
        reconcile(config, mw.col)

        assert self.spelled(definition)[0] == "{{trigger.Word}}/{{trigger.Meaning}}"
        assert sorted(
            (entry["old"], entry["new"], entry["object_id"]) for entry in marks(definition)
        ) == [
            # Each spelled twice: the write's target and the value, the value and the
            # unfocus list.
            ("Meaning", "Word", field_id(col, VOCAB, "Word")),
            ("Meaning", "Word", field_id(col, VOCAB, "Word")),
            ("Word", "Meaning", field_id(col, VOCAB, "Meaning")),
            ("Word", "Meaning", field_id(col, VOCAB, "Meaning")),
        ]

    def test_undoing_a_swap_removes_both_and_marks_nothing_new(self, col, config, other):
        definition = self.definition(VOCAB, other, text="{{trigger.Word}}/{{trigger.Meaning}}")
        store(config, definition)
        reconcile(config, mw.col)
        swap_fields(col, VOCAB, "Word", "Meaning")
        reconcile(config, mw.col)

        # The undo is itself a swap the snapshot shows, of two names the definition spells.
        swap_fields(col, VOCAB, "Word", "Meaning")
        result = reconcile(config, mw.col)

        assert WARNINGS_KEY not in definition
        assert result.newly_marked == []

    def test_two_stored_note_types_are_several_even_if_one_resolves_to_nothing(
        self, col, config
    ):
        definition = self.definition(VOCAB, d.object_ref("Nonsuch", 1_000_000_001))
        store(config, definition)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)

        assert self.spelled(definition) == ("{{trigger.Word}}", ["Word"])
        assert marks(definition) == [self.entry(col)] * 2

    def test_a_single_trigger_note_type_is_followed_and_not_marked(self, col, config):
        definition = self.definition(VOCAB)
        store(config, definition)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        result = reconcile(config, mw.col)

        assert self.spelled(definition) == ("{{trigger.Term}}", ["Term"])
        assert WARNINGS_KEY not in definition and result.broken == []

    def test_an_entry_naming_no_object_is_read_and_left_alone(self, col, marked, config):
        definition, _ = marked
        # Edited by hand into naming no field or card type by id: nothing to refresh it by.
        unnamed = [
            {"kind": KIND_FIELD, "old": "Word", "blocks_run": True, "message": "no ids"},
            {
                "kind": KIND_CARD_TYPE,
                "object_id": template_id(col, VOCAB, "Recognition"),
                "old": "Recognition",
                "blocks_run": True,
                "message": "no note type",
            },
        ]
        definition[WARNINGS_KEY] = {"elsewhere": copy.deepcopy(unnamed)}
        # Even the rename those entries were about, undone: they carry no ids to see it by.
        rename_field(col, VOCAB, "Term", "Word")

        result = reconcile(config, mw.col)

        assert definition[WARNINGS_KEY] == {"elsewhere": unnamed}
        assert blocking_messages(definition) == ["no ids", "no note type"]
        assert [(stale.kind, stale.name) for stale in result.broken] == [
            (KIND_FIELD, "Word"),
            (KIND_CARD_TYPE, "Recognition"),
        ]


class TestADeletedFieldOrCardTypeIsMarked:
    """A field or card type deleted from a trigger note type marks what spells it.

    Whatever the number of trigger note types: there is nothing to follow a deletion to. A
    report of it would be gone by the next pass, since the snapshot is rebuilt without the
    id; a mark is stored and stays until the user dismisses it.
    """

    OTHER = "CA Vocab B"

    def reading(self, *note_types, text="{{trigger.Freq}}", name="reads"):
        return d.staged(
            definition_name=name,
            note_types=list(note_types),
            stages=[d.edit_note("trigger", fields=[d.write("Meaning", d.text(text))])],
        )

    def delete_field(self, col, name):
        model = col.models.by_name(VOCAB)
        deleted = next(field for field in model["flds"] if field["name"] == name)
        col.models.remove_field(model, deleted)
        col.models.update_dict(model)
        return deleted["id"]

    def test_every_definition_spelling_it_is_marked(self, col, config):
        real_anki.make_note_type(
            col, self.OTHER, ["Word", "Meaning", "Freq"], [("Card 1", "{{Word}}", "{{Freq}}")]
        )
        single = self.reading(VOCAB, name="single")
        several = self.reading(VOCAB, self.OTHER, name="several")
        elsewhere = self.reading(VOCAB, text="{{trigger.Word}}", name="elsewhere")
        store(config, single, several, elsewhere)
        reconcile(config, mw.col)

        deleted_id = self.delete_field(col, "Freq")
        result = reconcile(config, mw.col)

        expected = {
            "kind": KIND_FIELD,
            "note_type_id": col.models.id_for_name(VOCAB),
            "object_id": deleted_id,
            "old": "Freq",
            "new": None,
            "blocks_run": True,
            "message": f'Field "Freq" of note type "{VOCAB}" was deleted',
        }
        assert marks(single) == [expected]
        assert marks(several) == [expected]
        assert WARNINGS_KEY not in elsewhere
        assert single["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Freq}}"
        assert names(result.gone) == []
        assert [stale.definition_name for stale in result.newly_marked] == ["single", "several"]

    def test_the_mark_survives_a_second_pass(self, col, config):
        definition = self.reading(VOCAB)
        store(config, definition)
        reconcile(config, mw.col)
        self.delete_field(col, "Freq")
        reconcile(config, mw.col)

        result = reconcile(config, mw.col)

        assert names(result.broken) == ["Freq"]
        assert [entry["message"] for entry in marks(definition)] == [
            f'Field "Freq" of note type "{VOCAB}" was deleted'
        ]
        assert result.changed is False

    def test_a_name_only_its_code_mentions_is_marked(self, col, config):
        definition = d.staged(
            definition_name="code",
            note_types=[VOCAB],
            stages=[d.variable("freq", d.code("return trigger['Freq']"))],
        )
        store(config, definition)
        reconcile(config, mw.col)

        self.delete_field(col, "Freq")
        reconcile(config, mw.col)

        assert located(definition) == {
            stage_key(definition["stages"][0]["guid"], "value.code"): [("Freq", None, True)]
        }
        assert [entry["message"] for entry in marks(definition)] == [
            f'Field "Freq" of note type "{VOCAB}" was deleted'
        ]

    def test_a_field_renamed_into_the_deleted_ones_name_marks_only_what_spelled_it(
        self, col, config
    ):
        spelled = self.reading(VOCAB, name="spelled")
        other_field = self.reading(VOCAB, text="{{trigger.Note}}", name="other field")
        store(config, spelled, other_field)
        reconcile(config, mw.col)

        self.delete_field(col, "Freq")
        rename_field(col, VOCAB, "Note", "Freq")
        reconcile(config, mw.col)

        # The rewrite hands `Freq` to the second one, which never read the deleted field.
        assert other_field["stages"][0]["fields"][0]["value"]["text"] == "{{trigger.Freq}}"
        assert WARNINGS_KEY not in other_field
        assert [entry["old"] for entry in marks(spelled)] == ["Freq"]

    def test_a_deleted_template_is_marked(self, col, config):
        definition = self.reading(VOCAB, text="{{trigger.Recall__Card_Interval}}")
        store(config, definition)
        reconcile(config, mw.col)
        model = col.models.by_name(VOCAB)
        recall = model["tmpls"][1]
        col.models.remove_template(model, recall)
        col.models.update_dict(model)

        reconcile(config, mw.col)

        assert [
            (entry["kind"], entry["object_id"], entry["new"]) for entry in marks(definition)
        ] == [(KIND_CARD_TYPE, recall["id"], None)]
        assert marks(definition)[0]["message"] == (
            f'Card type "Recall" of note type "{VOCAB}" was deleted'
        )


# Where a warning is filed ------------------------------------------------------------------


def a_code_action(guid: str, code: str, use_code: bool = True, change_deck=None) -> dict:
    """A card action that runs code (or keeps code it does not run), naming no card type."""
    return {
        "guid": guid,
        "card_type": None,
        "change_deck": change_deck,
        "set_flag": None,
        "suspend": None,
        "bury": None,
        "set_desired_retention": None,
        "use_code": use_code,
        "action_code": code,
    }


class TestEachLocationIsWarnedWhereItSpellsTheName:
    """A warning is filed under the location whose text or slot spells the old name, one
    per location and object, blocking or not as SPEC decisions 4 and 5 say."""

    OTHER = "CA Vocab B"

    @pytest.fixture
    def other(self, col):
        real_anki.make_note_type(
            col, self.OTHER, ["Word", "Meaning"], [("Recognition", "{{Word}}", "{{Meaning}}")]
        )
        return self.OTHER

    def everywhere(self, *note_types) -> dict:
        """A definition spelling the field `Word` once in each kind of location."""
        trigger_write = d.write("Word", d.text("{{trigger.Word}}"))
        trigger_write["guid"] = "w-trigger"
        trigger_write["unfocus_trigger_fields"] = ["Word"]
        code_write = d.write("Meaning", d.code("return trigger['Word']"))
        code_write["guid"] = "w-code"
        loop_write = d.write("Word", d.text("{{note.Word}}"))
        loop_write["guid"] = "w-loop"
        query = d.note_query("found", "Word:neko", guid="query")
        query["selection"]["sort_field"] = "Word"
        gated = d.variable("gated", d.text("{{trigger.Word}}"), guid="gated")
        gated["unfocus_trigger_fields"] = ["Word"]
        gated["write_if_field"] = "Word"
        # Kept for switching back, never run: nothing there breaks.
        switched_off = d.variable("off", d.text("plain"), guid="off")
        switched_off["value"]["code"] = "return trigger['Word']"
        return d.staged(
            definition_name="everywhere",
            note_types=list(note_types),
            on_unfocus={"edit_fields": ["Word"], "add_fields": []},
            stages=[
                query,
                gated,
                switched_off,
                d.read_file("file", "{{trigger.Word}}.txt", guid="file"),
                d.edit_note(
                    "trigger",
                    fields=[trigger_write, code_write],
                    card_actions=[
                        a_code_action("action", "return {'x': note['Word']}"),
                        a_code_action("action-off", "return note['Word']", use_code=False),
                    ],
                ),
                d.condition(
                    d.text("Word:neko"), then=[], predicate_kind="note_query", guid="cond"
                ),
                d.condition(
                    d.code("return 'Word' in fields"),
                    then=[],
                    predicate_kind="value",
                    guid="cond-code",
                ),
                d.for_each_note("found", [d.edit_note("note", fields=[loop_write])]),
            ],
        )

    def renamed(self, col, config, definition) -> dict:
        store(config, definition)
        reconcile(config, mw.col)
        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)
        return located(definition)

    #: What only warns whatever the definition triggers on: a field search, a sort field
    #: and another binding's field name can be any note type's.
    WARN_ONLY = {
        stage_key("query", "query.text"),
        stage_key("query", "selection.sort_field"),
        stage_key("cond", "predicate.text"),
        field_write_key("w-loop", "field"),
        field_write_key("w-loop", "value.text"),
    }
    #: Code: blocks whatever the definition triggers on.
    CODE = {
        field_write_key("w-code", "value.code"),
        card_action_key("action", "action_code"),
        stage_key("cond-code", "predicate.code"),
    }
    #: The trigger's own slots and tokens.
    THROUGH_THE_TRIGGER = {
        trigger_key("on_unfocus.edit_fields"),
        stage_key("gated", "value.text"),
        stage_key("gated", "unfocus_trigger_fields"),
        stage_key("gated", "write_if_field"),
        stage_key("file", "filename.text"),
        field_write_key("w-trigger", "field"),
        field_write_key("w-trigger", "value.text"),
        field_write_key("w-trigger", "unfocus_trigger_fields"),
    }

    def test_on_several_note_types_every_location_is_warned_at_its_key(
        self, col, config, other
    ):
        definition = self.everywhere(VOCAB, other)

        warnings = self.renamed(col, config, definition)

        assert warnings == {
            **{key: [("Word", "Term", False)] for key in self.WARN_ONLY},
            **{key: [("Word", "Term", True)] for key in self.CODE | self.THROUGH_THE_TRIGGER},
        }
        # Nothing was followed into it.
        assert definition["stages"][1]["write_if_field"] == "Word"

    def test_on_one_note_type_the_trigger_is_followed_and_the_rest_warned(self, col, config):
        definition = self.everywhere(VOCAB)

        warnings = self.renamed(col, config, definition)

        assert warnings == {
            **{key: [("Word", "Term", False)] for key in self.WARN_ONLY},
            **{key: [("Word", "Term", True)] for key in self.CODE},
        }
        assert definition["stages"][1]["write_if_field"] == "Term"
        assert definition["stages"][3]["filename"]["text"] == "{{trigger.Term}}.txt"

    def test_a_deleted_field_blocks_through_the_trigger_and_code_and_warns_elsewhere(
        self, col, config
    ):
        definition = self.everywhere(VOCAB)
        store(config, definition)
        reconcile(config, mw.col)

        model = col.models.by_name(VOCAB)
        col.models.remove_field(model, model["flds"][0])
        col.models.update_dict(model)
        reconcile(config, mw.col)

        # Decision 4 as corrected: a field search or another binding may reach a note type
        # that still has the name, so a deletion only warns there.
        assert located(definition) == {
            **{key: [("Word", None, False)] for key in self.WARN_ONLY},
            **{key: [("Word", None, True)] for key in self.CODE | self.THROUGH_THE_TRIGGER},
        }
        assert definition["stages"][1]["write_if_field"] == "Word"

    def test_a_card_term_and_another_bindings_card_value_only_warn(self, col, config):
        definition = d.staged(
            note_types=[VOCAB],
            stages=[
                d.card_query("cards", "card:Recognition", guid="cards"),
                d.variable("due", d.text("{{trigger.Recognition__Card_Due}}"), guid="due"),
                d.variable("other", d.text("{{card.Recognition__Card_Due}}"), guid="other"),
            ],
        )
        store(config, definition)
        reconcile(config, mw.col)

        rename_template(col, VOCAB, "Recognition", "Reading")
        reconcile(config, mw.col)

        assert located(definition) == {
            stage_key("cards", "query.text"): [("Recognition", "Reading", False)],
            stage_key("other", "value.text"): [("Recognition", "Reading", False)],
        }
        assert definition["stages"][1]["value"]["text"] == "{{trigger.Reading__Card_Due}}"

    def test_a_field_write_saved_without_a_guid_is_given_one_to_be_warned_under(
        self, col, config, other
    ):
        definition = TestARenameInADefinitionOnSeveralNoteTypesIsMarked().definition(
            VOCAB, other
        )
        write = definition["stages"][0]["fields"][0]
        assert "guid" not in write

        store(config, definition)
        reconcile(config, mw.col)
        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)

        assert write["guid"] == f"{definition['guid']}::field-write-1"
        assert field_write_key(write["guid"], "value.text") in definition[WARNINGS_KEY]

    def test_a_definition_on_another_note_type_is_warned_only_where_it_can_reach_it(
        self, col, config
    ):
        on_vocab = d.staged(definition_name="vocab", note_types=[VOCAB])
        on_kanji = d.staged(
            definition_name="kanji",
            note_types=[KANJI],
            stages=[
                d.note_query("found", "Word:neko", guid="kanji-query"),
                d.variable("code", d.code("return note['Word']"), guid="kanji-code"),
                d.variable("own", d.text("{{trigger.Word}}"), guid="kanji-own"),
            ],
        )
        store(config, on_vocab, on_kanji)
        reconcile(config, mw.col)

        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)

        # Its search and its code can reach the renamed note type's notes, but whether they
        # mean that note type's field is a guess, so they only warn; its own
        # `{{trigger.Word}}` is a Kanji note's and is left alone.
        assert located(on_kanji) == {
            stage_key("kanji-query", "query.text"): [("Word", "Term", False)],
            stage_key("kanji-code", "value.code"): [("Word", "Term", False)],
        }
        assert not blocking_messages(on_kanji)
        assert on_kanji["stages"][2]["value"]["text"] == "{{trigger.Word}}"
        assert WARNINGS_KEY not in on_vocab


def rename_note_type(col, old_name: str, new_name: str) -> None:
    model = col.models.by_name(old_name)
    model["name"] = new_name
    col.models.update_dict(model)


class TestADeckOrNoteTypeRenameIsWarnedInSearchesAndCode:
    """A deck or note type is referenced by id and followed silently; a search or code that
    spells its name cannot be, so it is warned about there, and blocks."""

    def searching(self) -> dict:
        return d.staged(
            definition_name="searches",
            note_types=[VOCAB],
            deck_names=["JP vocab"],
            stages=[
                d.note_query("found", 'deck:"JP vocab" note:"CA Vocab"', guid="query"),
                d.variable("deck", d.code("return find('deck:Other')"), guid="code"),
                d.variable("kanji", d.code("return 'CA Kanji'"), guid="kanji"),
            ],
        )

    @pytest.fixture
    def definition(self, config):
        definition = self.searching()
        store(config, definition)
        reconcile(config, mw.col)
        return definition

    def test_a_renamed_deck_is_warned_in_the_search_and_its_reference_followed(
        self, col, config, definition
    ):
        deck_id = col.decks.id_for_name("JP vocab")

        col.decks.rename(deck_id, "Japanese")
        result = reconcile(config, mw.col)

        assert definition[WARNINGS_KEY] == {
            stage_key("query", "query.text"): [
                {
                    "kind": KIND_DECK,
                    "object_id": deck_id,
                    "note_type_id": None,
                    "old": "JP vocab",
                    "new": "Japanese",
                    "blocks_run": True,
                    "message": 'Deck "JP vocab" was renamed to "Japanese"',
                }
            ]
        }
        # The trigger's deck is a reference: followed by id, never warned about.
        assert definition["triggers"]["deck_names"] == [{"id": deck_id, "name": "Japanese"}]
        assert names(result.newly_marked) == ["JP vocab"]

    def test_a_deck_only_a_search_or_code_names_is_seen_too(self, col, config, definition):
        # "Other" is not referenced by any definition; the snapshot holds every deck.
        col.decks.rename(col.decks.id_for_name("Other"), "Elsewhere")
        reconcile(config, mw.col)

        assert located(definition) == {
            stage_key("code", "value.code"): [("Other", "Elsewhere", True)]
        }

    def test_a_renamed_note_type_is_warned_in_the_search_and_code(
        self, col, config, definition
    ):
        rename_note_type(col, VOCAB, "Vocab")
        rename_note_type(col, KANJI, "Kanji")
        reconcile(config, mw.col)

        assert located(definition) == {
            stage_key("query", "query.text"): [(VOCAB, "Vocab", True)],
            stage_key("kanji", "value.code"): [(KANJI, "Kanji", True)],
        }
        assert definition["triggers"]["note_types"][0]["name"] == "Vocab"
        assert marks(definition)[0]["message"] == f'Note type "{VOCAB}" was renamed to "Vocab"'

    def test_a_deleted_deck_or_note_type_blocks(self, col, config, definition):
        col.decks.remove([col.decks.id_for_name("Other")])
        col.models.remove(col.models.id_for_name(KANJI))
        reconcile(config, mw.col)

        assert located(definition) == {
            stage_key("code", "value.code"): [("Other", None, True)],
            stage_key("kanji", "value.code"): [(KANJI, None, True)],
        }
        assert [entry["message"] for entry in marks(definition)] == [
            'Deck "Other" was deleted',
            f'Note type "{KANJI}" was deleted',
        ]

    def test_renamed_again_updates_and_renamed_back_removes(self, col, config, definition):
        other = col.decks.id_for_name("Other")
        col.decks.rename(other, "Elsewhere")
        rename_note_type(col, KANJI, "Kanji")
        reconcile(config, mw.col)

        col.decks.rename(other, "Far away")
        rename_note_type(col, "Kanji", "Kanji cards")
        result = reconcile(config, mw.col)

        assert [entry["message"] for entry in marks(definition)] == [
            'Deck "Other" was renamed to "Far away"',
            f'Note type "{KANJI}" was renamed to "Kanji cards"',
        ]
        assert result.newly_marked == []

        col.decks.rename(other, "Other")
        rename_note_type(col, "Kanji cards", KANJI)
        result = reconcile(config, mw.col)

        assert WARNINGS_KEY not in definition
        assert result.newly_marked == []

    def test_a_change_of_case_is_not_a_rename(self, col, config, definition):
        col.decks.rename(col.decks.id_for_name("Other"), "OTHER")
        reconcile(config, mw.col)

        # Anki finds `deck:Other` in `OTHER` all the same.
        assert WARNINGS_KEY not in definition

    def moving(self, *actions) -> dict:
        return d.staged(
            definition_name="moves",
            note_types=[VOCAB],
            stages=[d.edit_note("trigger", card_actions=list(actions))],
        )

    def test_a_card_action_moving_to_a_renamed_deck_is_warned_and_blocks(self, col, config):
        definition = self.moving(a_code_action("move", "", use_code=False, change_deck="other"))
        store(config, definition)
        reconcile(config, mw.col)

        col.decks.rename(col.decks.id_for_name("Other"), "Elsewhere")
        reconcile(config, mw.col)

        # The action moves the card by name, and a name Anki no longer has moves nothing.
        assert located(definition) == {
            card_action_key("move", "change_deck"): [("Other", "Elsewhere", True)]
        }

    def test_a_card_action_moving_to_a_deleted_deck_is_warned_and_blocks(self, col, config):
        definition = self.moving(a_code_action("move", "", use_code=False, change_deck="Other"))
        store(config, definition)
        reconcile(config, mw.col)

        col.decks.remove([col.decks.id_for_name("Other")])
        reconcile(config, mw.col)

        assert located(definition) == {
            card_action_key("move", "change_deck"): [("Other", None, True)]
        }

    def test_a_deck_the_action_does_not_move_to_by_that_name_is_not_warned(self, col, config):
        other_id = col.decks.id_for_name("Other")
        definition = self.moving(
            # Code that runs returns the action; the stored deck is not what moves the card.
            a_code_action("coded", "return {'suspend': True}", change_deck="Other"),
            # Switched on with no code: the stored deck is what runs.
            a_code_action("blank-code", "  ", change_deck="Other"),
            a_code_action("no-move", "", use_code=False, change_deck="-"),
            a_code_action("by-id", "", use_code=False, change_deck=other_id),
        )
        store(config, definition)
        reconcile(config, mw.col)

        col.decks.rename(other_id, "Elsewhere")
        reconcile(config, mw.col)

        assert located(definition) == {
            card_action_key("blank-code", "change_deck"): [("Other", "Elsewhere", True)]
        }

    def test_a_definition_that_spells_nothing_is_untouched_and_nothing_saves_after(
        self, col, config, stub_mw
    ):
        silent = d.staged(
            definition_name="silent",
            note_types=[VOCAB],
            stages=[d.note_query("found", "deck:Other", guid="query")],
        )
        store(config, silent)
        reconcile(config, mw.col)
        before = copy.deepcopy(silent)

        col.decks.rename(col.decks.id_for_name("JP vocab"), "Japanese")
        assert reconcile(config, mw.col).changed is True
        saved = saves(stub_mw)
        result = reconcile(config, mw.col)

        assert silent == before
        assert result.changed is False and saves(stub_mw) == saved


class TestADefinitionBrokenByARenameIsNotRun:
    """A marked definition is refused on every run path, and says why in the log.

    Run as it stands it would read or write a field some note type it triggers on no longer
    has. The note type that still has it is the realistic case: a run on its notes would
    succeed and quietly keep writing what the user may be about to rework, so the refusal
    has to be the mark itself, not a failure the missing field happens to cause.
    """

    OTHER = "CA Vocab B"

    @pytest.fixture
    def marked(self, col, config):
        real_anki.make_note_type(
            col, self.OTHER, ["Word", "Meaning"], [("Card 1", "{{Word}}", "{{Meaning}}")]
        )
        definition = d.staged(
            definition_name="both",
            guid="both-guid",
            note_types=[VOCAB, self.OTHER],
            stages=[
                d.edit_note("trigger", fields=[d.write("Meaning", d.text("{{trigger.Word}}"))])
            ],
        )
        store(config, definition)
        reconcile(config, mw.col)
        rename_field(col, VOCAB, "Word", "Term")
        reconcile(config, mw.col)
        assert definition.get(WARNINGS_KEY), "the pass did not mark the definition"
        return definition

    def other_note(self, col, word="neko"):
        """A note of the note type that still has the field, so only the mark can stop it."""
        return real_anki.add_note(col, self.OTHER, {"Word": word, "Meaning": "cat"})

    def message(self) -> str:
        return f'Field "Word" of note type "{VOCAB}" was renamed to "Term"'

    def test_a_run_on_one_note_writes_nothing_and_logs_the_message(self, col, marked, logger):
        note = self.other_note(col)
        copied: list = []

        ok = copy_for_single_trigger_note(marked, note, copied_into_notes=copied)

        assert ok is False
        assert copied == []
        assert note["Meaning"] == "cat"
        assert logger.errors == [
            f"Error in copy fields: 'both' was not run: {self.message()}. {BLOCKING_ADVICE}"
        ]

    def test_a_bulk_run_logs_it_once_not_once_per_note(self, col, marked, logger):
        self.other_note(col, "neko")
        self.other_note(col, "inu")
        copied: list = []

        copy_fields_in_background(
            copy_definition=marked,
            copied_into_cards_dict={},
            copied_into_notes=copied,
            results=CacheResults(result_text="", changes=None),
        )

        assert copied == []
        assert logger.errors == [
            f"Error in copy fields: 'both' was not run: {self.message()}. {BLOCKING_ADVICE}"
        ]

    def test_a_caller_of_it_fails_with_the_message_and_writes_nothing(
        self, col, marked, logger
    ):
        note = self.other_note(col)
        caller = d.staged(
            "caller",
            guid="caller-guid",
            note_types=[self.OTHER],
            stages=[
                # Written before the call: a failed run discards what it staged, so this
                # must not reach the note either.
                d.edit_note("trigger", fields=[d.write("Meaning", d.text("before the call"))]),
                d.call_definition("both-guid"),
            ],
        )
        copied: list = []

        ok = copy_for_single_trigger_note(
            caller, note, copied_into_notes=copied, definitions_for_calls=[marked, caller]
        )

        assert ok is False
        assert copied == []
        assert note["Meaning"] == "cat"
        assert col.get_note(note.id)["Meaning"] == "cat"
        assert logger.errors == [
            f"calls definition 'both', which was not run: {self.message()}. {BLOCKING_ADVICE}"
        ]

    def test_it_runs_again_once_the_rename_is_undone(self, col, marked, config, logger):
        note = self.other_note(col)
        rename_field(col, VOCAB, "Term", "Word")
        reconcile(config, mw.col)
        note = col.get_note(note.id)

        assert WARNINGS_KEY not in marked
        assert copy_for_single_trigger_note(marked, note) is True
        assert note["Meaning"] == "neko"
        assert logger.errors == []

    def test_every_message_of_a_definition_marked_twice_is_logged(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.staged(
            definition_name="twice",
            stages=[d.edit_note("trigger", fields=[d.write("Note", d.text("x"))])],
        )
        # In two locations: every blocking warning is said, wherever it is filed.
        definition[WARNINGS_KEY] = {
            "a-stage.value.text": [d.rename_warning("first.", old="Word")],
            ORPHAN_LOCATION: [d.rename_warning("second", old="Meaning")],
        }

        assert copy_for_single_trigger_note(definition, note) is False
        assert logger.errors == [
            f"Error in copy fields: 'twice' was not run: first. {BLOCKING_ADVICE}",
            f"Error in copy fields: 'twice' was not run: second. {BLOCKING_ADVICE}",
        ]
        assert note["Note"] == ""

    @pytest.mark.parametrize(
        "stored, messages",
        [
            ({ORPHAN_LOCATION: [d.rename_warning("gone")]}, ["gone"]),
            (
                {ORPHAN_LOCATION: [d.rename_warning("gone"), "junk", {"blocks_run": True}]},
                ["gone"],
            ),
            ({ORPHAN_LOCATION: [d.rename_warning(""), {"message": 3, "blocks_run": True}]}, []),
            ({ORPHAN_LOCATION: {"message": "not in a list", "blocks_run": True}}, []),
            # Not blocking: said, but it does not hold the definition back.
            ({ORPHAN_LOCATION: [d.rename_warning("warned", blocks_run=False)]}, []),
            ({ORPHAN_LOCATION: [{"message": "no flag"}]}, []),
            (
                {"a.value.text": [d.rename_warning("one")], "b.field": [d.rename_warning("two")]},
                ["one", "two"],
            ),
            # A list is not the store's shape (it was `broken_by_rename`'s): not read.
            ([d.rename_warning("gone")], []),
            ("gone", []),
            (None, []),
        ],
    )
    def test_only_well_formed_blocking_entries_with_a_message_count(self, stored, messages):
        definition = d.staged(stages=[])
        definition[WARNINGS_KEY] = stored

        assert blocking_messages(definition) == messages

    def test_a_mark_with_nothing_to_say_does_not_stop_the_run(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.staged(
            stages=[d.edit_note("trigger", fields=[d.write("Note", d.text("ran"))])],
        )
        definition[WARNINGS_KEY] = {ORPHAN_LOCATION: ["junk"]}

        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == "ran"
        assert logger.errors == []

    def test_a_warning_that_does_not_block_does_not_stop_the_run(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.warned(
            d.staged(stages=[d.edit_note("trigger", fields=[d.write("Note", d.text("ran"))])]),
            d.rename_warning('Card type "Recall" was renamed', blocks_run=False),
        )

        assert copy_for_single_trigger_note(definition, note) is True
        assert note["Note"] == "ran"
        assert logger.errors == []


class TestTheWarningAfterAFieldsSave:
    """A marked definition is said so in a dialog after a note type operation.

    The log is not read at the default level, and the user who just saved the Fields
    dialog is the one who knows what the definition should say now.
    """

    @pytest.fixture
    def warnings(self, monkeypatch):
        from copy_anywhere.hooks import rename_hooks

        shown: list = []
        monkeypatch.setattr(
            rename_hooks, "showWarning", lambda text, **kwargs: shown.append(text)
        )
        return shown

    @pytest.fixture
    def marked(self, col, config):
        real_anki.make_note_type(
            col, "CA Vocab B", ["Word"], [("Card 1", "{{Word}}", "{{Word}}")]
        )
        definition = d.staged(
            definition_name="both <&>",
            note_types=[VOCAB, "CA Vocab B"],
            stages=[
                d.edit_note("trigger", fields=[d.write("Meaning", d.text("{{trigger.Word}}"))])
            ],
        )
        store(config, definition)
        reconcile(config, mw.col)
        rename_field(col, VOCAB, "Word", "Term")
        return definition

    def test_a_note_type_operation_shows_it(self, marked, warnings):
        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        assert len(warnings) == 1
        assert "both &lt;&amp;&gt;" in warnings[0]
        assert html.escape(f'Field "Word" of note type "{VOCAB}" was renamed to "Term"') in (
            warnings[0]
        )
        assert "renamed or deleted" in warnings[0]
        assert "dismiss its mark in the definition editor" in warnings[0]

    def test_a_deck_operation_shows_it_too(self, col, marked, warnings, passes):
        # A deck rename can mark a definition itself (a search or code spelling the deck),
        # so what the pass it runs added is said as after a note type change. An answer,
        # which reports a deck change as well, does not run the pass at all.
        changes = col.decks.rename(col.decks.id_for_name("Other"), "Elsewhere")

        on_operation_did_execute(changes, None)

        assert len(passes) == 1
        assert blocking_messages(marked)
        assert len(warnings) == 1 and "both &lt;&amp;&gt;" in warnings[0]

    def test_a_deck_rename_a_search_spells_is_shown(self, col, config, warnings):
        definition = d.staged(
            definition_name="searches",
            note_types=[VOCAB],
            stages=[d.note_query("found", "deck:Other")],
        )
        store(config, definition)
        reconcile(config, mw.col)

        changes = col.decks.rename(col.decks.id_for_name("Other"), "Elsewhere")
        on_operation_did_execute(changes, None)

        assert len(warnings) == 1
        assert html.escape('Deck "Other" was renamed to "Elsewhere"') in warnings[0]

    def test_a_name_spelled_in_several_places_is_one_line(self, col, marked, warnings):
        marked["triggers"]["on_unfocus"]["edit_fields"] = ["Word"]

        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        assert len(marks(marked)) == 2
        assert warnings[0].count("was renamed to") == 1

    def test_a_mark_already_shown_is_not_shown_again(self, col, marked, warnings):
        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)
        rename_field(col, KANJI, "Keyword", "Gloss")

        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        assert len(warnings) == 1
        assert blocking_messages(marked)

    def test_only_the_marks_this_pass_added_are_listed(self, col, config, marked, warnings):
        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)
        definition = d.staged(
            definition_name="both again",
            note_types=[VOCAB, "CA Vocab B"],
            stages=[
                d.edit_note(
                    "trigger", fields=[d.write("Meaning", d.text("{{trigger.Reading}}"))]
                )
            ],
        )
        config.data["copy_definitions"].append(definition)
        reconcile(config, mw.col)
        rename_field(col, VOCAB, "Reading", "Kana")

        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        assert len(warnings) == 2
        assert "both again" in warnings[1] and html.escape('"Reading"') in warnings[1]
        assert "both &lt;&amp;&gt;" not in warnings[1]

    def test_nothing_marked_shows_nothing(self, col, config, warnings):
        store(config, d.staged(note_types=[VOCAB]))

        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        assert warnings == []


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

    def test_a_note_type_change_runs_it_even_with_the_decks_unchanged(
        self, col, config, passes
    ):
        store(config, d.staged(note_types=[VOCAB]))
        rename_hooks.on_collection_did_load(col)

        on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        assert len(passes) == 2

    def test_answering_a_card_does_not_run_it(self, col, config, passes, config_loads):
        """An answer reports a deck change; the decks it did not change are what say so."""
        store(config, d.staged(note_types=[VOCAB]))
        rename_hooks.on_collection_did_load(col)
        del passes[:], config_loads[:]

        changes = answer_a_card(col)
        on_operation_did_execute(changes, None)

        assert changes.deck is True and changes.notetype is False
        assert passes == [] and config_loads == []

    def test_answering_does_not_reread_a_config_with_no_references(
        self, col, config, passes, config_loads
    ):
        """The load pass stops early here, and it still has to count as the decks seen."""
        store(config, d.staged(note_types=[]))
        rename_hooks.on_collection_did_load(col)
        del config_loads[:]

        on_operation_did_execute(answer_a_card(col), None)

        assert passes == [] and config_loads == []

    @pytest.mark.parametrize("deck_operation", ["rename", "remove", "add"])
    def test_a_deck_renamed_removed_or_added_runs_it(
        self, col, config, passes, deck_operation
    ):
        store(config, d.staged(note_types=[VOCAB], deck_names=["JP vocab"]))
        rename_hooks.on_collection_did_load(col)
        other = col.decks.id_for_name("Other")
        if deck_operation == "rename":
            changes = col.decks.rename(other, "Elsewhere")
        elif deck_operation == "remove":
            changes = col.decks.remove([other]).changes
        else:
            changes = col.decks.add_normal_deck_with_name("Brand new").changes

        on_operation_did_execute(changes, None)

        assert changes.notetype is False
        assert len(passes) == 2

    def test_once_it_has_run_the_same_decks_do_not_run_it_again(self, col, config, passes):
        store(config, d.staged(note_types=[VOCAB], deck_names=["JP vocab"]))
        rename_hooks.on_collection_did_load(col)
        # Not "Other", which the answered card's note is added to.
        changes = col.decks.rename(col.decks.id_for_name("JP vocab::10-80::x"), "Elsewhere")
        on_operation_did_execute(changes, None)

        on_operation_did_execute(answer_a_card(col), None)

        assert len(passes) == 2

    def test_the_decks_of_another_collection_do_not_stand_for_this_ones(
        self, col, config, stub_mw, passes, tmp_path
    ):
        """A copy has the very same deck ids and names, and is still a pass not yet run."""
        store(config, d.staged(note_types=[VOCAB]))
        rename_hooks.on_collection_did_load(col)
        other = a_copy_of(col, tmp_path / "other.anki2")
        try:
            with opened(stub_mw, other):
                on_operation_did_execute(FakeChanges(notetype=False, deck=True), None)
        finally:
            other.close()

        assert passes[-1] is other

    def test_a_pass_that_failed_is_tried_again_at_the_next_deck_change(
        self, col, config, passes, monkeypatch
    ):
        store(config, d.staged(note_types=[VOCAB]))
        rename_hooks.on_collection_did_load(col)

        def fail(self):
            raise ValueError("meta.json is not JSON")

        with monkeypatch.context() as patch:
            patch.setattr(Config, "load", fail)
            on_operation_did_execute(FakeChanges(notetype=True, deck=False), None)

        on_operation_did_execute(answer_a_card(col), None)

        assert len(passes) == 2

    def test_a_pass_that_saves_takes_the_snapshot_once(self, col, config, stub_mw, monkeypatch):
        """The pass stores the snapshot itself; its save must not take it a second time."""
        from copy_anywhere.logic import rename_reconcile

        builds: list = []
        build = rename_reconcile.build_name_snapshot

        def counted(definitions, collection):
            builds.append(collection)
            return build(definitions, collection)

        monkeypatch.setattr(rename_reconcile, "build_name_snapshot", counted)
        store(config, d.staged(note_types=[VOCAB], deck_names=["JP vocab"]))

        result = reconcile(config, mw.col)

        assert result.changed is True and saves(stub_mw) == 1
        assert len(builds) == 1
        assert stub_mw.addonManager.configs[ADDON_TAG][SNAPSHOT_KEY] == build(
            config.copy_definitions, mw.col
        )

    def test_every_search_of_a_pass_shares_one_name_list(self, col, config, monkeypatch):
        listings: list = []
        list_all = col.models.all

        def counted():
            listings.append(1)
            return list_all()

        monkeypatch.setattr(col.models, "all", counted)
        store(
            config,
            *[
                d.staged(
                    definition_name=name,
                    note_types=[VOCAB],
                    stages=[
                        d.note_query("found", "Nowhere:x card:Nothing"),
                        d.note_query("again", "Elsewhere:y"),
                    ],
                )
                for name in ("one", "two")
            ],
        )

        result = reconcile(config, mw.col)

        assert len(result.stale_terms) == 6
        # One for the field names and one for the card type names, whatever the count of
        # searches.
        assert len(listings) == 2


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


@pytest.fixture
def passes(monkeypatch) -> list:
    """The collection of every pass the hooks run, in order."""
    from copy_anywhere.hooks import rename_hooks

    ran: list = []
    real = rename_hooks.reconcile

    def spy(config, col):
        ran.append(col)
        return real(config, col)

    monkeypatch.setattr(rename_hooks, "reconcile", spy)
    return ran


@pytest.fixture
def config_loads(monkeypatch) -> list:
    """One entry per read of the config from disk, which the deck check must never do."""
    loads: list = []
    load = Config.load

    def counted(self):
        loads.append(self)
        return load(self)

    monkeypatch.setattr(Config, "load", counted)
    return loads


def answer_a_card(col):
    """Answer a new card for real and return the `OpChanges` Anki hands the hook.

    As `test_review_hook.answer_card` does it, keeping what `answer_card` returns.
    """
    note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"}, deck_name="Other")
    card = note.cards()[0]
    col.decks.select(card.did)
    queued = col.sched.get_queued_cards(fetch_limit=50)
    states = next(entry.states for entry in queued.cards if entry.card.id == card.id)
    card.start_timer()
    return col.sched.answer_card(
        col.sched.build_answer(card=card, states=states, rating=CardAnswer.GOOD)
    )


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
