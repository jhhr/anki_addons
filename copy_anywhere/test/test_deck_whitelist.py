"""Characterization tests for Step 2 of `copy_for_single_trigger_note`: the deck whitelist.

`only_copy_into_decks` gates a definition on which deck the trigger note's cards live in, and
`include_subdecks` widens each whitelisted deck to its whole subtree. This file pins the gate
itself (a card outside the whitelist skips the note benignly, returning `True`), the exact
card attribute it reads (`odid or did`, so a filtered-deck card is judged by its home deck),
the `deck_id` override the add-note path uses, the empty-card-list short circuit that lets a
not-yet-added note through, and where the gate sits relative to the other steps.
"""

import time

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import ProgressUpdater, copy_for_single_trigger_note


def copy_note_field(**extra):
    """A Within-note definition that writes Word into Note, so a run is visible."""
    return d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")], **extra)


def note_in(col, deck_name):
    return real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"}, deck_name=deck_name)


def make_progress_updater() -> ProgressUpdater:
    return ProgressUpdater(
        start_time=time.time(),
        definition_name="whitelisted",
        total_notes_count=1,
        is_across=False,
        title=None,
    )


class TestNoWhitelistMeansNoFiltering:
    def test_the_default_none_lets_every_deck_through(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field()
        assert definition["only_copy_into_decks"] is None
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == "neko"

    def test_an_empty_whitelist_lets_every_deck_through(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks="")
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_a_lone_dash_lets_every_deck_through(self, col, logger):
        # "-" is the editor's "no deck selected" placeholder, and it is special-cased rather
        # than resolved: as a deck name it would yield None and match nothing at all.
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks="-")
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_no_whitelist_logs_nothing_about_deck_ids(self, col, logger):
        note = note_in(col, "Other")
        copy_for_single_trigger_note(copy_note_field(), note, logger=logger)
        assert not logger.has_debug("unique_whitelist_dids")


class TestDeckMembership:
    def test_a_card_inside_the_whitelist_copies(self, col, logger):
        note = note_in(col, "JP vocab")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == "neko"

    def test_a_card_outside_the_whitelist_skips_the_note_benignly(self, col, logger):
        # True, not False: a note the whitelist rejects is a normal outcome and the caller's
        # bulk loop keeps going.
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""
        assert logger.has_debug("No deck id in whitelist, skipping copy for note")

    def test_any_one_whitelisted_name_is_enough(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab", "Other"]))
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_one_whitelisted_card_of_two_is_enough(self, col, logger):
        # The guard is `not any(...)`, so a note straddling two decks passes as soon as one
        # of its cards is inside -- it is an "any card" test, not an "all cards" one.
        note = note_in(col, "Other")
        stray = note.cards()[0]
        stray.did = col.decks.id_for_name("JP vocab")
        col.update_card(stray)
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_a_sibling_deck_does_not_match(self, col, logger):
        note = note_in(col, "JP vocab::10-80")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab::10-80::x"]))
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""


class TestTheStoredNameFormat:
    def test_names_are_split_on_the_quote_comma_quote_the_editor_writes(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab", "Other"]))
        assert definition["only_copy_into_decks"] == 'JP vocab", "Other'
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_a_full_path_name_resolves_despite_the_comment_saying_it_cannot(self, col, logger):
        # The production comment says parent names cannot be included "since adding :: would
        # break the filter text". That is a constraint on the editor's filter widget, not on
        # this code: the split is on '", "', so a "::" path survives it and resolves fine.
        note = note_in(col, "JP vocab::10-80")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab::10-80"]))
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_a_bare_leaf_name_does_not_resolve(self, col, logger):
        # Decks are named by their full path, so the leaf-only name the comment implies you
        # must use ("10-80") is simply not a deck name and resolves to None.
        note = note_in(col, "JP vocab::10-80")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["10-80"]))
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""


class TestIncludeSubdecks:
    def test_a_child_deck_is_excluded_when_include_subdecks_is_false(self, col, logger):
        note = note_in(col, "JP vocab::10-80")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=False
        )
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""

    def test_a_child_deck_is_included_when_include_subdecks_is_true(self, col, logger):
        note = note_in(col, "JP vocab::10-80")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=True
        )
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_a_grandchild_deck_is_included_because_children_is_recursive(self, col, logger):
        note = note_in(col, "JP vocab::10-80::x")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=True
        )
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_a_grandchild_deck_is_excluded_when_include_subdecks_is_false(self, col, logger):
        note = note_in(col, "JP vocab::10-80::x")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=False
        )
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""

    def test_subdecks_widen_downwards_only_and_never_pull_in_the_parent(self, col, logger):
        note = note_in(col, "JP vocab")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab::10-80"]), include_subdecks=True
        )
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""

    def test_the_whitelisted_deck_itself_still_matches_with_subdecks_on(self, col, logger):
        note = note_in(col, "JP vocab")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=True
        )
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_an_unrelated_deck_is_still_excluded_with_subdecks_on(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=True
        )
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""


class TestANonExistentDeckName:
    def test_it_becomes_a_none_in_the_whitelist_set(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["No Such Deck"]))
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert logger.has_debug("unique_whitelist_dids={None}")

    def test_the_none_matches_nothing_so_every_note_is_skipped(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["No Such Deck"]))
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""

    def test_a_real_name_beside_it_still_matches(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["No Such Deck", "Other"]))
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note["Note"] == "neko"

    def test_include_subdecks_leaves_it_a_none_that_skips_the_note(self, col, logger):
        # The None must not reach decks.children(): the backend would look up deck 0 and
        # raise NotFoundError, aborting the caller's whole bulk loop over a typo'd name.
        note = note_in(col, "JP vocab")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["No Such Deck"]), include_subdecks=True
        )
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""
        assert logger.has_debug("unique_whitelist_dids={None}")

    def test_with_include_subdecks_a_real_name_beside_it_still_matches(self, col, logger):
        note = note_in(col, "JP vocab::10-80")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab", "No Such Deck"]),
            include_subdecks=True,
        )
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == "neko"


class TestFilteredDeckCards:
    """`card.odid or card.did`, so a card in a filtered deck is judged by its home deck."""

    @staticmethod
    def move_into_filtered_deck(col, note):
        filtered_did = col.decks.id("Filtered")
        for card in note.cards():
            card.odid = card.did
            card.did = filtered_did
            col.update_card(card)
        return filtered_did

    def test_the_home_deck_in_odid_is_what_matches(self, col, logger):
        note = note_in(col, "Other")
        self.move_into_filtered_deck(col, note)
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["Other"]))
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == "neko"

    def test_the_filtered_deck_the_card_is_actually_in_does_not_match(self, col, logger):
        note = note_in(col, "Other")
        self.move_into_filtered_deck(col, note)
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["Filtered"]))
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""


class TestAnExplicitDeckId:
    """The add-note path passes `deck_id` because the note's cards do not exist yet."""

    def test_it_replaces_the_cards_decks_entirely(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        deck_id = col.decks.id_for_name("JP vocab")
        assert copy_for_single_trigger_note(definition, note, deck_id=deck_id, logger=logger)
        assert note["Note"] == "neko"

    def test_it_can_reject_a_note_whose_cards_are_whitelisted(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["Other"]))
        deck_id = col.decks.id_for_name("JP vocab")
        assert (
            copy_for_single_trigger_note(definition, note, deck_id=deck_id, logger=logger) is True
        )
        assert note["Note"] == ""

    def test_it_is_widened_by_include_subdecks_too(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=True
        )
        deck_id = col.decks.id_for_name("JP vocab::10-80::x")
        copy_for_single_trigger_note(definition, note, deck_id=deck_id, logger=logger)
        assert note["Note"] == "neko"

    def test_a_deck_id_of_zero_is_used_rather_than_ignored(self, col, logger):
        # The test is `deck_id is not None`, not truthiness, so a falsy 0 still overrides the
        # cards -- and 0 is no deck, so the note is skipped even though its cards qualify.
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["Other"]))
        assert copy_for_single_trigger_note(definition, note, deck_id=0, logger=logger) is True
        assert note["Note"] == ""
        assert logger.has_debug("deck_ids=[0]")


class TestANoteWithNoCards:
    def test_it_passes_the_whitelist_because_the_empty_list_short_circuits(self, col, logger):
        # The guard is `if deck_ids_of_cards and not any(...)`, so a note with no cards is
        # never rejected -- whatever the whitelist says. This is what lets the add-note and
        # editor paths copy into a note before its cards exist.
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        assert copy_for_single_trigger_note(definition, new_note, logger=logger) is True
        assert new_note["Note"] == "neko"
        assert logger.has_debug("deck_ids=[]")

    def test_a_non_existent_whitelisted_deck_does_not_stop_it_either(self, col, logger):
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["No Such Deck"]))
        copy_for_single_trigger_note(definition, new_note, logger=logger)
        assert new_note["Note"] == "neko"

    def test_an_explicit_deck_id_puts_it_back_under_the_whitelist(self, col, logger):
        new_note = col.new_note(col.models.by_name(VOCAB))
        new_note["Word"] = "neko"
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        deck_id = col.decks.id_for_name("Other")
        copy_for_single_trigger_note(definition, new_note, deck_id=deck_id, logger=logger)
        assert new_note["Note"] == ""


class TestWhereTheWhitelistSitsAmongTheSteps:
    def test_variables_are_not_computed_for_a_note_it_rejects(self, col, logger):
        # Intentional format-2 change: trigger filtering sits outside the stage interpreter,
        # so the whitelist is checked before any stage runs and a rejected note costs
        # nothing. Format 1 resolved every variable first and only then looked at the deck,
        # which is why the invalid field used to be reported here.
        note = note_in(col, "Other")
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        definition["field_to_variable_defs"] = [d.field_to_variable("v", "{{Nonexistent}}")]
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert note["Note"] == ""
        assert logger.errors == []

    def test_the_condition_query_never_runs_for_a_note_it_rejects(self, col, logger):
        # An uninterpolatable condition returns False and stops the caller's bulk loop, but
        # only if it is reached. Step 2 gets there first, so this returns a benign True.
        note = note_in(col, "Other")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), copy_condition_query="{{Freq}}"
        )
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True
        assert logger.errors == []

    def test_the_source_query_never_runs_for_a_note_it_rejects(self, col, logger):
        note = note_in(col, "Other")
        destination = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        definition = d.source_to_destinations(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            copy_from_cards_query="Word:inu",
            only_copy_into_decks=d.quoted_list(["JP vocab"]),
        )
        copied_into_notes = []
        assert (
            copy_for_single_trigger_note(
                definition, note, copied_into_notes=copied_into_notes, logger=logger
            )
            is True
        )
        assert copied_into_notes == []
        assert destination["Note"] == ""

    def test_even_a_syntactically_invalid_source_query_is_never_reached(self, col, logger):
        # The same query raises SearchError out of the function once Step 4 runs it.
        note = note_in(col, "Other")
        definition = d.source_to_destinations(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            copy_from_cards_query="Word:(",
            only_copy_into_decks=d.quoted_list(["JP vocab"]),
        )
        assert copy_for_single_trigger_note(definition, note, logger=logger) is True

    def test_a_rejected_note_is_not_counted_by_the_progress_updater(self, col, logger):
        note = note_in(col, "Other")
        updater = make_progress_updater()
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        copy_for_single_trigger_note(definition, note, logger=logger, progress_updater=updater)
        assert updater.get_counts() == (0, 0, 0, 0, 0)

    def test_an_accepted_note_is_counted_by_the_progress_updater(self, col, logger):
        note = note_in(col, "JP vocab")
        updater = make_progress_updater()
        definition = copy_note_field(only_copy_into_decks=d.quoted_list(["JP vocab"]))
        copy_for_single_trigger_note(definition, note, logger=logger, progress_updater=updater)
        note_cnt, sources, destinations, _files, _cards = updater.get_counts()
        assert (note_cnt, sources, destinations) == (1, 1, 1)

    def test_tags_are_not_applied_to_a_note_it_rejects(self, col, logger):
        note = note_in(col, "Other")
        definition = copy_note_field(
            only_copy_into_decks=d.quoted_list(["JP vocab"]), add_tags="copied"
        )
        copy_for_single_trigger_note(definition, note, logger=logger)
        assert note.tags == []
