"""Id-carrying references: what a definition stores for a note type, deck or card type.

A definition names three kinds of object Anki gives a stable id -- note types, decks and
card templates -- and Anki has no hook that carries a rename of any of them (see "Following
a rename in Anki" in `docs/follow-ups.md`). Storing the id beside the name is what lets a
renamed object still be found: the id wins if it still exists, and only when it does not is
the stored name looked up and the reference re-bound.

These are the reference shape and the readers that go through it. The reconcile pass that
refreshes the stored names, and the editor slots that report a reference resolving to
nothing, are separate.
"""

import pytest
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import KANJI, VOCAB
from copy_anywhere.hooks.note_hooks import (
    get_copy_definitions_for_add_note,
    run_copy_fields_on_review,
    run_copy_fields_on_unfocus_field,
)
from copy_anywhere.logic.copy_fields import (
    CacheResults,
    copy_fields_in_background,
    copy_for_single_trigger_note,
    note_passes_deck_whitelist,
)
from copy_anywhere.logic.definition_schema import validate_definition_structure
from copy_anywhere.logic.object_refs import (
    card_type_ref,
    deck_ref,
    normalize_card_type_ref,
    normalize_ref,
    note_type_ref,
    ref_matches_note_type,
    resolve_card_type,
    resolve_deck_id,
    resolve_note_type,
    resolve_template,
)

ADDON_TAG = "copy_anywhere"


@pytest.fixture
def set_definitions(col):
    """Put copy definitions where `Config.load()` will find them."""

    def apply(*definitions):
        mw.addonManager.configs[ADDON_TAG]["copy_definitions"] = list(definitions)

    return apply


def rename_note_type(col, old_name: str, new_name: str) -> None:
    note_type = col.models.by_name(old_name)
    note_type["name"] = new_name
    col.models.update_dict(note_type)


def rename_template(col, note_type_name: str, index: int, new_name: str) -> None:
    note_type = col.models.by_name(note_type_name)
    note_type["tmpls"][index]["name"] = new_name
    col.models.update_dict(note_type)


# The shape ---------------------------------------------------------------------------


class TestReadingAStoredValue:
    def test_a_bare_string_is_read_as_a_null_id_reference(self):
        assert normalize_ref(VOCAB) == {"id": None, "name": VOCAB}

    def test_a_reference_keeps_only_the_keys_it_knows(self):
        assert normalize_ref({"id": 7, "name": VOCAB, "colour": "red"}) == {
            "id": 7,
            "name": VOCAB,
        }

    def test_a_card_type_string_is_read_as_a_null_id_reference(self):
        assert normalize_card_type_ref("CA Vocab<::>Recognition") == {
            "note_type_id": None,
            "template_id": None,
            "name": "CA Vocab<::>Recognition",
        }


class TestBuildingAReferenceFromALiveObject:
    def test_a_note_type_reference_carries_its_id(self, col):
        note_type = col.models.by_name(VOCAB)
        assert note_type_ref(note_type) == {"id": note_type["id"], "name": VOCAB}

    def test_a_deck_reference_carries_its_id(self, col):
        deck_id = col.decks.id_for_name("Other")
        assert deck_ref(deck_id, "Other") == {"id": deck_id, "name": "Other"}

    def test_a_card_type_reference_carries_both_ids_and_the_display_name(self, col):
        note_type = col.models.by_name(VOCAB)
        template = note_type["tmpls"][0]
        assert card_type_ref(note_type, template) == {
            "note_type_id": note_type["id"],
            "template_id": template["id"],
            "name": f"{VOCAB}<::>Recognition",
        }

    def test_a_template_saved_before_anki_23_10_has_no_id_to_carry(self, col):
        # Template ids are nullable: a note type saved with them stripped keeps None and the
        # backend does not backfill, so a collection upgraded from before 23.10 has these.
        note_type = col.models.by_name(VOCAB)
        template = dict(note_type["tmpls"][0], id=None)
        assert card_type_ref(note_type, template)["template_id"] is None


# Resolving ---------------------------------------------------------------------------


class TestResolvingANoteType:
    def test_the_id_wins_over_a_name_that_is_no_longer_its_own(self, col):
        note_type = col.models.by_name(VOCAB)
        ref = note_type_ref(note_type)
        rename_note_type(col, VOCAB, "Renamed vocab")

        assert resolve_note_type(ref, col)["id"] == note_type["id"]

    def test_a_null_id_resolves_by_name(self, col):
        resolved = resolve_note_type({"id": None, "name": VOCAB}, col)
        assert resolved["name"] == VOCAB

    def test_an_id_that_is_gone_falls_back_to_the_name(self, col):
        note_type = col.models.by_name(VOCAB)
        resolved = resolve_note_type({"id": note_type["id"] + 10_000, "name": VOCAB}, col)
        assert resolved["id"] == note_type["id"]

    def test_neither_an_id_nor_a_name_that_exists_resolves_to_nothing(self, col):
        assert resolve_note_type({"id": 999, "name": "Gone"}, col) is None


class TestResolvingADeck:
    def test_the_id_wins_over_a_name_that_is_no_longer_its_own(self, col):
        deck_id = col.decks.id_for_name("Other")
        ref = deck_ref(deck_id, "Other")
        col.decks.rename(col.decks.get(deck_id), "Renamed deck")

        assert resolve_deck_id(ref, col) == deck_id

    def test_a_null_id_resolves_by_name(self, col):
        assert resolve_deck_id({"id": None, "name": "Other"}, col) == col.decks.id_for_name(
            "Other"
        )

    def test_neither_resolves_to_nothing(self, col):
        assert resolve_deck_id({"id": 999, "name": "Gone"}, col) is None


class TestResolvingACardTemplate:
    def test_the_template_id_wins_over_a_name_that_is_no_longer_its_own(self, col):
        note_type = col.models.by_name(VOCAB)
        ref = card_type_ref(note_type, note_type["tmpls"][0])
        rename_template(col, VOCAB, 0, "Reading card")

        resolved = resolve_template(ref, col.models.by_name(VOCAB))
        assert resolved["name"] == "Reading card"

    def test_a_null_template_id_resolves_by_the_card_type_half_of_the_name(self, col):
        ref = {
            "note_type_id": None,
            "template_id": None,
            "name": f"{VOCAB}<::>Recall",
        }
        assert resolve_template(ref, col.models.by_name(VOCAB))["name"] == "Recall"

    def test_a_reference_to_another_note_type_resolves_to_nothing(self, col):
        ref = normalize_card_type_ref(f"{KANJI}<::>Card 1")
        assert resolve_template(ref, col.models.by_name(VOCAB)) is None

    def test_a_card_type_that_is_gone_resolves_to_nothing(self, col):
        ref = normalize_card_type_ref(f"{VOCAB}<::>Gone")
        assert resolve_template(ref, col.models.by_name(VOCAB)) is None

    def test_the_note_type_half_is_matched_by_id_when_it_has_one(self, col):
        note_type = col.models.by_name(VOCAB)
        ref = card_type_ref(note_type, note_type["tmpls"][0])
        rename_note_type(col, VOCAB, "Renamed vocab")

        assert ref_matches_note_type(
            {"id": ref["note_type_id"], "name": VOCAB}, col.models.by_name("Renamed vocab")
        )


class TestResolvingACardTypeInOneStep:
    """`resolve_card_type` is the one place the two steps are taken.

    Every reader of a card type reference -- the save blocker, the picker's label, the
    reconcile pass -- goes through here, so the note type half and the template half are
    looked up once, by the one rule, and drift is not possible.
    """

    def test_both_ids_win_over_names_that_are_no_longer_their_own(self, col):
        note_type = col.models.by_name(VOCAB)
        ref = card_type_ref(note_type, note_type["tmpls"][0])
        rename_note_type(col, VOCAB, "Renamed vocab")
        rename_template(col, "Renamed vocab", 0, "Reading card")

        model, template = resolve_card_type(ref, col)
        assert (model["name"], template["name"]) == ("Renamed vocab", "Reading card")

    def test_a_stale_note_type_id_falls_back_to_the_stored_name(self, col):
        # Both halves follow the rule, and the template half follows it inside the note
        # type the first half *found*: a definition carried to another collection has ids
        # that mean nothing there and names that mean everything, and a card type that
        # would not re-bind is one the reconcile pass would report as naming nothing.
        note_type = col.models.by_name(VOCAB)
        ref = {
            "note_type_id": note_type["id"] + 10_000,
            "template_id": note_type["tmpls"][1]["id"] + 10_000,
            "name": f"{VOCAB}<::>Recall",
        }

        model, template = resolve_card_type(ref, col)
        assert model["id"] == note_type["id"]
        assert template["id"] == note_type["tmpls"][1]["id"]

    def test_a_null_template_id_resolves_the_template_by_name(self, col):
        # The pre-23.10 shape: the note type carries an id, the template never had one.
        note_type = col.models.by_name(VOCAB)
        ref = {
            "note_type_id": note_type["id"],
            "template_id": None,
            "name": f"{VOCAB}<::>Recall",
        }
        rename_note_type(col, VOCAB, "Renamed vocab")

        model, template = resolve_card_type(ref, col)
        assert (model["name"], template["name"]) == ("Renamed vocab", "Recall")

    def test_a_reference_to_nothing_resolves_to_a_pair_of_nothings(self, col):
        ref = {"note_type_id": 999, "template_id": 999, "name": "Gone<::>Gone"}
        assert resolve_card_type(ref, col) == (None, None)

    def test_a_live_note_type_with_a_gone_card_type_keeps_the_note_type(self, col):
        # Either half can go on its own, and the caller has to be able to tell which.
        note_type = col.models.by_name(VOCAB)
        model, template = resolve_card_type(f"{VOCAB}<::>Gone", col)
        assert (model["id"], template) == (note_type["id"], None)


# The readers -------------------------------------------------------------------------


class TestTheHooksFollowARenamedNoteType:
    """All three note-type comparisons take the same route through `resolve_note_type`."""

    def definition_for(self, col, **extra):
        note_type = col.models.by_name(VOCAB)
        return d.staged(
            stages=[d.edit_note("trigger", fields=[d.write("Note", d.text("{{trigger.Word}}"))])],
            note_types=[note_type_ref(note_type)],
            **extra,
        )

    def test_the_add_hook_still_picks_the_definition(self, col, set_definitions):
        definition = self.definition_for(col, on_add=True)
        set_definitions(definition)
        note = col.new_note(col.models.by_name(VOCAB))
        rename_note_type(col, VOCAB, "Renamed vocab")

        assert get_copy_definitions_for_add_note(note) == [definition]

    def test_the_review_hook_still_runs_the_definition(self, col, set_definitions):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        set_definitions(self.definition_for(col, on_review=True))
        rename_note_type(col, VOCAB, "Renamed vocab")

        run_copy_fields_on_review(note.cards()[0])

        assert col.get_note(note.id)["Note"] == "neko"

    def test_the_unfocus_hook_still_runs_the_definition(self, col, set_definitions):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        set_definitions(
            self.definition_for(col, on_unfocus={"edit_fields": ["Word"], "add_fields": []})
        )
        rename_note_type(col, VOCAB, "Renamed vocab")

        run_copy_fields_on_unfocus_field(False, note, 0)

        assert note["Note"] == "neko"


class TestTheBulkPathFollowsARenamedNoteType:
    def test_a_renamed_note_type_still_selects_its_notes(self, col, logger):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        note_type = col.models.by_name(VOCAB)
        definition = d.staged(
            stages=[d.edit_note("trigger", fields=[d.write("Note", d.text("{{trigger.Word}}"))])],
            note_types=[note_type_ref(note_type)],
        )
        rename_note_type(col, VOCAB, "Renamed vocab")

        copied_into_notes: list = []
        copy_fields_in_background(
            copy_definition=definition,
            copied_into_cards_dict={},
            copied_into_notes=copied_into_notes,
            results=CacheResults(result_text="", changes=None),
        )

        assert [note["Note"] for note in copied_into_notes] == ["neko"]
        assert logger.errors == []


class TestTheDeckWhitelistFollowsARenamedDeck:
    def test_a_renamed_deck_still_lets_its_notes_through(self, col):
        note = real_anki.add_note(
            col, VOCAB, {"Word": "neko", "Meaning": "cat"}, deck_name="JP vocab"
        )
        stored = deck_ref(col.decks.id_for_name("JP vocab"), "JP vocab")
        col.decks.rename(col.decks.by_name("JP vocab"), "Japanese vocab")

        assert note_passes_deck_whitelist([stored], False, note) is True

    def test_a_deck_reference_that_resolves_to_nothing_still_matches_nothing(self, col):
        note = real_anki.add_note(
            col, VOCAB, {"Word": "neko", "Meaning": "cat"}, deck_name="JP vocab"
        )
        assert note_passes_deck_whitelist([{"id": 999, "name": "Gone"}], False, note) is False


class TestACardActionFollowsARenamedCardType:
    def card_action(self, col, template_index: int, **extra):
        note_type = col.models.by_name(VOCAB)
        return d.card_action_ref(note_type, note_type["tmpls"][template_index], **extra)

    def test_the_action_lands_on_the_card_type_it_was_stored_for(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            card_actions=[self.card_action(col, 0, set_flag=2)],
        )
        rename_template(col, VOCAB, 0, "Reading card")

        copied_into_cards: dict = {}
        copy_for_single_trigger_note(
            definition, note, copied_into_cards_dict=copied_into_cards
        )

        # Only a card an action edited is handed over, so the untouched Recall card is not.
        flags = {card.template()["name"]: card.flags for card in copied_into_cards.values()}
        assert flags == {"Reading card": 2}
        assert logger.errors == []

    def test_a_card_type_that_cannot_be_resolved_at_all_is_reported(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            card_actions=[d.card_action(VOCAB, "Gone", set_flag=2)],
        )

        copy_for_single_trigger_note(definition, note)

        assert logger.errors or logger.warnings


class TestWhatValidationSaysAboutAReferenceSlot:
    """A slot takes a reference or the bare name that came before it, and nothing else."""

    def definition_with(self, **triggers):
        definition = d.staged(stages=[])
        definition["triggers"].update(triggers)
        return definition

    def test_a_reference_passes(self):
        assert validate_definition_structure(self.definition_with()) == []

    def test_a_bare_name_still_passes(self):
        definition = self.definition_with(note_types=[VOCAB], deck_names=["Other"])
        assert validate_definition_structure(definition) == []

    def test_anything_else_in_a_note_type_slot_is_reported(self):
        definition = self.definition_with(note_types=[7])
        assert [str(problem) for problem in validate_definition_structure(definition)] == [
            "'triggers.note_types' has an entry that is neither a name nor a reference: 7"
        ]

    def test_a_note_type_list_that_is_not_a_list_is_reported(self):
        definition = self.definition_with(note_types=VOCAB)
        assert "'triggers.note_types' is not a list" in str(
            validate_definition_structure(definition)[0]
        )

    def test_anything_else_in_a_deck_slot_is_reported(self):
        definition = self.definition_with(deck_names=[None])
        assert "'triggers.deck_names' has an entry" in str(
            validate_definition_structure(definition)[0]
        )

    def test_a_card_action_naming_no_card_type_at_all_is_reported(self):
        definition = d.staged(
            stages=[d.edit_note("trigger", card_actions=[{"guid": "a", "card_type": 7}])]
        )
        assert "card action's 'card_type'" in str(
            validate_definition_structure(definition)[0]
        )

    def test_an_edit_card_stage_without_card_actions_passes(self):
        stage = d.edit_card("trigger")
        del stage["card_actions"]
        assert validate_definition_structure(d.staged(stages=[stage])) == []

    def test_an_edit_note_stage_without_card_actions_passes(self):
        stage = d.edit_note("trigger")
        del stage["card_actions"]
        assert validate_definition_structure(d.staged(stages=[stage])) == []

    def test_card_actions_that_are_not_a_list_are_still_reported(self):
        stage = d.edit_card("trigger")
        stage["card_actions"] = {"guid": "a"}
        assert "'card_actions' is not a list" in str(
            validate_definition_structure(d.staged(stages=[stage]))[0]
        )
