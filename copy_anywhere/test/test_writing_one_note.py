"""What writing one note does: trigger fields, tags, files, card actions.

These started as characterization tests for `copy_into_single_note()`, the format-1 function
that took the pieces of a definition and wrote one destination note. The rollout retired it,
so they now run the same pieces the way a stored definition reaches the executor: as the
within-note definition a migration builds out of them. The behaviour they pin is unchanged
and still user-visible -- an empty `add_tags` must not mark a note modified, a file lands
under its `_` prefix, a card action keyed by a card type reaches only that card -- which is
why they outlived the function whose name they carried.

The order is still fields, then tags, then files, then card actions, and it is still
unconditional: a definition whose field writes are all filtered out by `field_only` runs its
tags, files and actions regardless.
"""

import json

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.configuration import (
    get_field_to_field_unfocus_trigger_fields,
    split_tags,
)
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note


@pytest.fixture
def note(col):
    return real_anki.add_note(
        col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat"}
    )


def run(note, **kwargs):
    """Run these pieces against `note` as the within-note definition they describe.

    Returns what `copy_into_single_note()` used to return, minus the file flag no caller
    reads: whether the note was written into, and the cards the card actions touched. A note
    is "written into" exactly when the executor hands it to the caller to save, which is the
    same thing the old boolean meant.
    """
    field_only = kwargs.pop("field_only", None)
    definition = d.within_note(**kwargs)
    copied_into_notes: list = []
    copied_into_cards: dict = {}
    copy_for_single_trigger_note(
        definition,
        note,
        copied_into_notes=copied_into_notes,
        copied_into_cards_dict=copied_into_cards,
        field_only=field_only,
    )
    return bool(copied_into_notes), list(copied_into_cards.values())


def run_across(note, **kwargs):
    """Run these pieces with several source notes, which the query below finds.

    Format 1 took the sources as a list because one definition assigned the roles globally.
    Format 2 has no such list: a definition that reads several notes says which query found
    them, so "several sources into this note" is the destination-to-sources shape and the
    sources are the notes tagged `pool`.
    """
    definition = d.destination_to_sources(copy_from_cards_query="tag:pool", **kwargs)
    definition["select_card_count"] = "0"
    copy_for_single_trigger_note(
        definition, note, copied_into_notes=[], copied_into_cards_dict={}
    )


def run_failing(note, logger, **kwargs):
    """Run pieces that should fail the definition, and return what it logged.

    Format 1 raised `CopyFailedException` out of the write and left the caller to catch it.
    The staged executor turns it into a stage failure, which fails the definition and is
    reported rather than raised (§7.2); the message is the same one, which is what these
    cases were always about.
    """
    succeeded = copy_for_single_trigger_note(
        d.within_note(**kwargs),
        note,
        copied_into_notes=[],
        copied_into_cards_dict={},
    )
    assert succeeded is False
    return "\n".join(logger.errors)


class TestTriggerFieldSplitting:
    def test_a_quoted_list_splits_into_names(self):
        field_def = d.field_to_field("Note", copy_on_unfocus_trigger_field='"a", "b"')
        assert get_field_to_field_unfocus_trigger_fields(field_def, False) == ["a", "b"]

    def test_a_single_name_splits_to_one(self):
        field_def = d.field_to_field("Note", copy_on_unfocus_trigger_field="a")
        assert get_field_to_field_unfocus_trigger_fields(field_def, False) == ["a"]

    def test_an_empty_value_falls_back_to_the_destination_field_in_the_same_note(self):
        # Within note and Destination to sources write the note being edited, so the
        # destination field doubles as the trigger. Source to destinations writes other
        # notes, where no field of the edited note is the destination, so nothing triggers.
        field_def = d.field_to_field("Note", copy_on_unfocus_trigger_field="")
        assert get_field_to_field_unfocus_trigger_fields(field_def, False) == ["Note"]
        assert get_field_to_field_unfocus_trigger_fields(field_def, True) == []

    def test_field_only_runs_just_the_matching_def(self, note):
        # Both defs are on for unfocus while editing, because that is the only way the hook
        # would reach them with a `field_only` at all; what separates them here is which
        # editor field each one watches.
        modified, _ = run(
            note,
            field_to_field_defs=[
                d.field_to_field(
                    "Meaning",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=True,
                ),
                d.field_to_field(
                    "Note",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Reading",
                    copy_on_unfocus_when_edit=True,
                ),
            ],
            field_only="Word",
        )
        assert modified is True
        assert note["Meaning"] == "neko"
        assert note["Note"] == ""

    def test_field_only_still_lets_tags_and_actions_run(self, note):
        # The field_only filter is a `continue` inside the field-to-field loop only. Tags,
        # files and card actions sit after that loop and always run.
        modified, _ = run(
            note,
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}", copy_on_unfocus_trigger_field="Reading")
            ],
            add_tags="copied",
            field_only="Word",
        )
        assert note["Note"] == ""
        assert note.has_tag("copied")
        assert modified is True


class TestTags:
    def test_tags_are_added(self, note):
        modified, _ = run(note, add_tags='"x", "y"')
        assert note.has_tag("x") and note.has_tag("y")
        assert modified is True

    def test_a_tag_already_present_is_not_re_added(self, note):
        note.add_tag("x")
        modified, _ = run(note, add_tags="x")
        assert modified is False

    def test_tags_are_removed(self, note):
        note.add_tag("x")
        note.add_tag("y")
        modified, _ = run(note, remove_tags='"x", "y"')
        assert not note.has_tag("x") and not note.has_tag("y")
        assert modified is True

    def test_a_tag_not_present_is_not_removed(self, note):
        modified, _ = run(note, remove_tags="absent")
        assert modified is False

    def test_an_empty_add_tags_adds_nothing_and_does_not_mark_the_note_modified(
        self, note
    ):
        # The value in almost every real definition. Before split_tags dropped empty names,
        # this added a tag called "" and set modified_dest_note for every destination note
        # whether or not anything was copied, inflating the processed count, the
        # copied-into-notes list and the undo entry.
        assert split_tags("") == []
        modified, _ = run(note, add_tags="", remove_tags="")
        assert modified is False
        assert note.tags == []

    def test_an_empty_add_tags_does_not_inflate_the_destination_count(self, col):
        # The same thing one level up, where it was actually visible: a definition that
        # copies nothing into this note must not count it as a destination processed.
        target = real_anki.add_note(col, VOCAB, {"Word": "a"}, tags=["pool"])
        trigger = real_anki.add_note(col, VOCAB, {"Word": "trigger"})
        definition = d.source_to_destinations(
            copy_from_cards_query="tag:pool",
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}", copy_on_unfocus_trigger_field="other")
            ],
            select_card_count="0",
            add_tags="",
        )
        copied_into_notes = []
        copy_for_single_trigger_note(
            definition,
            trigger,
            copied_into_notes=copied_into_notes,
            field_only="not-a-trigger-field",
        )
        assert copied_into_notes == []
        assert target.tags == ["pool"]


class TestFiles:
    def test_a_file_is_written_with_an_underscore_prefix(self, note, media_dir):
        run(
            note,
            field_to_file_defs=[d.field_to_file("out.txt", "{{Word}}")],
        )
        assert (media_dir / "_out.txt").read_text(encoding="utf-8") == "neko"

    def test_a_name_that_already_has_the_prefix_is_not_double_prefixed(
        self, note, media_dir
    ):
        run(note, field_to_file_defs=[d.field_to_file("_out.txt", "{{Word}}")])
        assert (media_dir / "_out.txt").exists()
        assert not (media_dir / "__out.txt").exists()

    def test_an_empty_filename_aborts_the_definition(self, note, logger):
        assert "No file name provided" in run_failing(
            note, logger, field_to_file_defs=[d.field_to_file("", "{{Word}}")]
        )

    def test_copy_if_empty_on_a_file_means_do_not_overwrite(self, note, media_dir):
        (media_dir / "_out.txt").write_text("original", encoding="utf-8")
        run(
            note,
            field_to_file_defs=[d.field_to_file("out.txt", "{{Word}}", copy_if_empty=True)],
        )
        assert (media_dir / "_out.txt").read_text(encoding="utf-8") == "original"

    def test_the_filename_sees_what_the_field_write_before_it_wrote(
        self, note, media_dir
    ):
        # Format 1 interpolated a file's name over the live note but `__Dest__` over a copy
        # taken before any def ran, so one field read two different ways in one definition.
        # A migrated definition writes the file in a stage after the one that writes the
        # field, and a stage reads what the stages before it did (§5.3), so the two
        # spellings now agree. One of the migration's intended changes.
        run(
            note,
            field_to_field_defs=[d.field_to_field("Note", "written")],
            field_to_file_defs=[
                d.field_to_file("{{Note}}-{{__Dest__Note}}.txt", "{{Word}}")
            ],
        )
        assert (media_dir / "_written-written.txt").exists()

    def test_the_code_path_runs_once_per_source_note_and_writes_every_tuple(
        self, col, note, media_dir
    ):
        # Several source notes is the destination-to-sources shape: the query finds them and
        # the file code runs once for each, which a within-note definition has no way to say.
        for word in ("a", "b"):
            real_anki.add_note(col, VOCAB, {"Word": word}, tags=["pool"])
        run_across(
            note,
            field_to_file_defs=[
                d.field_to_file(
                    "",
                    use_code=True,
                    copy_as_code=(
                        "return [(note['Word'] + '-1.txt', 'one'),"
                        " (note['Word'] + '-2.txt', 'two')]"
                    ),
                )
            ],
        )
        for word in ("a", "b"):
            assert (media_dir / f"_{word}-1.txt").read_text(encoding="utf-8") == "one"
            assert (media_dir / f"_{word}-2.txt").read_text(encoding="utf-8") == "two"

    def test_the_code_path_bumps_query_note_index_only_for_several_source_notes(
        self, col, note, media_dir
    ):
        for word in ("a", "b"):
            real_anki.add_note(col, VOCAB, {"Word": word}, tags=["pool"])
        run_across(
            note,
            field_to_file_defs=[
                d.field_to_file(
                    "",
                    use_code=True,
                    copy_as_code="return [('{{__Query_Note_Index}}.txt', 'x')]",
                )
            ],
        )
        assert (media_dir / "_1.txt").exists()
        assert (media_dir / "_2.txt").exists()

    def test_code_returning_nothing_writes_nothing_and_is_not_an_error(
        self, note, logger, media_dir
    ):
        modified, _ = run(
            note,
            field_to_file_defs=[d.field_to_file("", use_code=True, copy_as_code="return []")],
        )
        assert modified is False
        assert list(media_dir.iterdir()) == []
        assert logger.errors == []

    @pytest.mark.parametrize(
        "code, message",
        [
            ("return 'not a list'", "Expected a list"),
            ("return ['not a tuple']", "must be a 2-tuple"),
            ("return [(1, 'content')]", "filename must be a str"),
            ("return [('name.txt', 2)]", "content must be a str"),
        ],
    )
    def test_a_malformed_code_result_aborts_the_definition(self, note, logger, code, message):
        assert message in run_failing(
            note, logger, field_to_file_defs=[d.field_to_file("", use_code=True, copy_as_code=code)]
        )


def card_named(cards, template_name):
    """Pick a card out of what copy_into_single_note returned.

    Card actions edit the in-memory card objects and leave writing them to the caller, so
    `note.cards()` -- which re-reads the database -- would show none of it.
    """
    return next(card for card in cards if card.template()["name"] == template_name)


class TestCardActions:
    def test_a_card_type_name_without_the_separator_is_skipped_and_the_rest_still_run(
        self, note, logger
    ):
        malformed = d.card_action(VOCAB, "Recognition", set_flag=1)
        malformed["card_type_name"] = "no separator here"
        _, cards = run(
            note,
            card_actions=[malformed, d.card_action(VOCAB, "Recall", set_flag=2)],
        )
        assert logger.has_error("Invalid card type name")
        assert card_named(cards, "Recall").user_flag() == 2
        assert card_named(cards, "Recognition").user_flag() == 0

    def test_an_action_for_another_note_type_is_skipped(self, note):
        _, cards = run(
            note, card_actions=[d.card_action("Some Other Type", "Recognition", set_flag=1)]
        )
        assert card_named(cards, "Recognition").user_flag() == 0

    def test_two_actions_for_one_template_leave_only_the_later_one(self, note):
        # They are collected into a dict keyed by template name, so the second overwrites.
        _, cards = run(
            note,
            card_actions=[
                d.card_action(VOCAB, "Recognition", set_flag=1),
                d.card_action(VOCAB, "Recognition", set_flag=5),
            ],
        )
        assert card_named(cards, "Recognition").user_flag() == 5

    def test_change_deck_by_name_and_by_id(self, col, note):
        deck_id = col.decks.id_for_name("Other")
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", change_deck="Other")]
        )
        assert card_named(cards, "Recognition").did == deck_id
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recall", change_deck=deck_id)]
        )
        assert card_named(cards, "Recall").did == deck_id

    @pytest.mark.parametrize("value", [None, "-", 0])
    def test_change_deck_no_ops(self, col, note, value):
        before = note.cards()[0].did
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", change_deck=value)]
        )
        assert card_named(cards, "Recognition").did == before
        assert not hasattr(card_named(cards, "Recognition"), "edited")

    def test_change_deck_to_a_name_that_does_not_exist_is_logged_and_does_nothing(
        self, note, logger
    ):
        before = note.cards()[0].did
        _, cards = run(
            note,
            card_actions=[d.card_action(VOCAB, "Recognition", change_deck="No Such Deck")],
        )
        assert logger.has_error("not found. Cannot move card")
        assert card_named(cards, "Recognition").did == before

    def test_a_filtered_deck_card_has_its_odid_rewritten_not_its_did(self, col, note):
        card = card_named(note.cards(), "Recognition")
        original_did = card.did
        card.odid = card.did
        card.did = col.decks.id("Filtered")
        col.update_card(card)
        target = col.decks.id_for_name("Other")
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", change_deck="Other")]
        )
        moved = next(c for c in cards if c.template()["name"] == "Recognition")
        assert moved.odid == target
        assert moved.did != target
        assert moved.did != original_did

    def test_suspend_and_unsuspend(self, note):
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", suspend=True)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.queue == -1
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", suspend=False)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.queue == card.type

    def test_burying_a_suspended_card_is_a_no_op(self, col, note):
        card = card_named(note.cards(), "Recognition")
        card.queue = -1
        col.update_card(card)
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", bury=True)]
        )
        buried = next(c for c in cards if c.template()["name"] == "Recognition")
        assert buried.queue == -1
        assert not hasattr(buried, "edited")

    def test_burying_an_unsuspended_card_works(self, note):
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", bury=True)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.queue == -2

    @pytest.mark.parametrize("flag", [0, 1, 2, 3, 4, 5, 6, 7])
    def test_every_valid_flag_is_set(self, note, flag):
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=flag)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == flag

    @pytest.mark.parametrize("flag", [8, -1])
    def test_an_out_of_range_flag_is_ignored(self, note, flag):
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=flag)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == 0
        assert not hasattr(card, "edited")

    def test_set_flag_true_is_accepted_as_flag_one(self, note):
        # `isinstance(True, int)` is True and the guard does not exclude bool, unlike
        # set_desired_retention's, which does. Pinned so the inconsistency is visible.
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=True)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == 1


class TestDesiredRetention:
    def _card(self, cards, name="Recognition"):
        return card_named(cards, name)

    def test_a_float_between_zero_and_one_is_set_as_is(self, note):
        _, cards = run(
            note,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention=0.85)],
        )
        assert self._card(cards).desired_retention == pytest.approx(0.85)

    def test_an_int_is_read_as_a_percentage(self, note):
        _, cards = run(
            note,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention=90)],
        )
        assert self._card(cards).desired_retention == pytest.approx(0.9)

    def test_a_string_is_looked_up_in_the_cards_custom_data(self, col, note):
        real_anki.set_custom_data(col, self._card(note.cards()).id, json.dumps({"dr": 88}))
        note = col.get_note(note.id)
        _, cards = run(
            note,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention="dr")],
        )
        assert self._card(cards).desired_retention == pytest.approx(0.88)

    def test_a_missing_custom_data_key_sets_nothing(self, col, note):
        real_anki.set_custom_data(col, self._card(note.cards()).id, json.dumps({"other": 1}))
        note = col.get_note(note.id)
        _, cards = run(
            note,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention="dr")],
        )
        assert self._card(cards).desired_retention is None

    def test_empty_custom_data_sets_nothing(self, note):
        _, cards = run(
            note,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention="dr")],
        )
        assert self._card(cards).desired_retention is None

    @pytest.mark.parametrize("value", [0, 1.0, True])
    def test_out_of_range_and_boolean_values_are_ignored(self, note, value):
        # True is excluded explicitly here -- unlike set_flag, which accepts it.
        _, cards = run(
            note,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention=value)],
        )
        assert self._card(cards).desired_retention is None


class TestCardActionCode:
    def test_code_returning_none_skips_every_action_for_that_card_type(self, note):
        _, cards = run(
            note,
            card_actions=[
                d.card_action(
                    VOCAB, "Recognition", set_flag=3, use_code=True, action_code="return None"
                )
            ],
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == 0

    def test_code_returning_a_dict_replaces_the_configured_action(self, note):
        _, cards = run(
            note,
            card_actions=[
                d.card_action(
                    VOCAB,
                    "Recognition",
                    set_flag=3,
                    use_code=True,
                    action_code="return {'set_flag': 6}",
                )
            ],
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == 6

    @pytest.mark.parametrize(
        "code, message",
        [
            ("return 'nope'", "Expected a dict"),
            ("return {'set_flag': 9}", "'set_flag' must be an int"),
            ("return {'suspend': 'yes'}", "'suspend' must be True"),
            ("return {'bury': 1}", "'bury' must be True"),
            ("return {'change_deck': 1.5}", "'change_deck' must be str"),
            ("return {'set_desired_retention': []}", "'set_desired_retention' must be float"),
        ],
    )
    def test_a_malformed_code_result_aborts_the_definition(self, note, logger, code, message):
        assert message in run_failing(
            note,
            logger,
            card_actions=[d.card_action(VOCAB, "Recognition", use_code=True, action_code=code)],
        )


class TestEditedFlag:
    def test_an_untouched_card_gets_no_edited_attribute(self, note):
        # The dynamic `edited` attribute is what drives the progress counts and the filter
        # that decides which cards are handed to update_cards, so its absence matters.
        _, cards = run(note, card_actions=[])
        assert all(not hasattr(card, "edited") for card in cards)

    def test_a_touched_card_is_marked_edited(self, note):
        _, cards = run(
            note, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=1)]
        )
        edited = [card for card in cards if getattr(card, "edited", False)]
        assert [card.template()["name"] for card in edited] == ["Recognition"]


class TestWhichNoteAFileWriteReadsAcrossNotes:
    """Which note stands in as source and which as destination when a file write is migrated.

    Format 1 assigned the two roles once, for the whole definition, and every file write in
    `copy_into_single_note` used them: the filename was interpolated over
    `notes=[destination_note]` with `dest_note=destination_note`, and the code path ran with
    `source_note=` each source note and `dest_note=destination_note`. Format 2 has no such
    global assignment -- a stage says which note it reads -- so the migrator spends the roles
    on the references themselves, per key: a filename's names become the destination's, a
    content expression's the source's. Getting one of them wrong collapses the two roles onto
    one note, which is the failure these pin.
    """

    def test_each_destination_gets_its_own_file(self, col, note, media_dir):
        # Source to destinations: the trigger is the source and each found note is the
        # destination, so a filename built from the destination's own fields names N files.
        # Collapsing the roles makes every iteration interpolate the trigger note instead,
        # so the N writes agree on one name and overwrite each other down to the last.
        for word in ("a", "b"):
            real_anki.add_note(col, VOCAB, {"Word": word}, tags=["pool"])
        definition = d.source_to_destinations(
            copy_from_cards_query="tag:pool",
            field_to_file_defs=[d.field_to_file("{{Word}}.txt", "x")],
            select_card_count="0",
        )
        copy_for_single_trigger_note(
            definition, note, copied_into_notes=[], copied_into_cards_dict={}
        )

        assert sorted(p.name for p in media_dir.iterdir()) == ["_a.txt", "_b.txt"]

    def test_the_dest_prefix_in_a_file_name_reads_the_destination(
        self, col, note, media_dir
    ):
        # The same collapse seen from the other side, and the sharper half: `__Dest__` is the
        # spelling that exists *because* the two roles differ. Reading it off the trigger note
        # makes it a second, slower way of saying `{{Word}}`.
        real_anki.add_note(col, VOCAB, {"Word": "a", "Note": "mine"}, tags=["pool"])
        definition = d.source_to_destinations(
            copy_from_cards_query="tag:pool",
            field_to_file_defs=[d.field_to_file("{{__Dest__Note}}.txt", "x")],
            select_card_count="0",
        )
        copy_for_single_trigger_note(
            definition, note, copied_into_notes=[], copied_into_cards_dict={}
        )

        assert (media_dir / "_mine.txt").exists(), sorted(p.name for p in media_dir.iterdir())

    def test_the_dest_prefix_in_file_code_reads_the_trigger_note(
        self, col, note, media_dir
    ):
        # Destination to sources, the mirror image: the trigger is the destination and the
        # query found the sources, so file code sees `note` as each source -- the loop's own
        # binding, which is what `note` means inside a loop in any code expression -- and
        # `{{__Dest__Word}}` as the trigger, which is where migration sends it.
        for word in ("a", "b"):
            real_anki.add_note(col, VOCAB, {"Word": word}, tags=["pool"])
        run_across(
            note,
            field_to_file_defs=[
                d.field_to_file(
                    "",
                    use_code=True,
                    copy_as_code="return [(note['Word'] + '.txt', '{{__Dest__Word}}')]",
                )
            ],
        )

        assert (media_dir / "_a.txt").read_text(encoding="utf-8") == "neko"
        assert (media_dir / "_b.txt").read_text(encoding="utf-8") == "neko"


class TestAnUnfocusRunOfAMigratedJoin:
    """What a Destination-to-sources unfocus run evaluates, and what it should.

    Format 1 read the query's notes once and then walked `field_to_field_defs`, asking of
    each one whether the field that just lost focus triggers it -- so a write the unfocus did
    not trigger cost nothing, and could not fail the run.

    Migrating that shape moves the per-source read out of the write and in front of it: each
    write becomes a list, a loop that stores one value per source note, and a reduce that
    joins them, and the write itself is left reading `{{legacy_joined_N}}`. Only the write
    carries `unfocus_trigger_fields`, and only `run_edit_note` consults it, so the three
    stages that feed it run unconditionally -- including the loop body that evaluates the
    right-hand side the write was going to use.
    """

    @pytest.fixture
    def source(self, col):
        return real_anki.add_note(col, VOCAB, {"Word": "src", "Meaning": "M"}, tags=["pool"])

    def test_a_write_the_unfocus_did_not_trigger_is_not_evaluated(
        self, col, note, source, media_dir
    ):
        # The visible cost: the untriggered write's code runs once per source note on every
        # unfocus of an unrelated field. Here it is a marker file, so the test can see it at
        # all; in the definitions this shape came from it is a network fetch or a subprocess.
        definition = d.destination_to_sources(
            copy_from_cards_query="tag:pool",
            field_to_field_defs=[
                d.field_to_field(
                    "Note",
                    "{{Word}}",
                    copy_on_unfocus_when_edit=True,
                    copy_on_unfocus_trigger_field="Word",
                ),
                d.field_to_field(
                    "Reading",
                    use_code=True,
                    copy_as_code=(
                        "import pathlib, aqt;"
                        " pathlib.Path(aqt.mw.pm.media_folder(), 'ran.txt').write_text('x');"
                        " return 'value'"
                    ),
                    copy_on_unfocus_when_edit=True,
                    copy_on_unfocus_trigger_field="Reading",
                ),
            ],
            select_card_count="0",
        )

        copy_for_single_trigger_note(
            definition,
            note,
            copied_into_notes=[],
            copied_into_cards_dict={},
            field_only="Word",
        )

        assert note["Note"] == "src"
        assert not (media_dir / "ran.txt").exists()

    def test_an_untriggered_write_that_raises_does_not_lose_the_triggered_one(
        self, col, note, source, logger
    ):
        # The same evaluation, now fatal. The write that did trigger is correct and complete,
        # and the one that failed was never going to be applied -- but the failure is a stage
        # error, so the definition fails and the triggered write is discarded with it. Typing
        # in one field of the editor silently stops a definition that has nothing wrong with
        # the part of it that field drives.
        definition = d.destination_to_sources(
            copy_from_cards_query="tag:pool",
            field_to_field_defs=[
                d.field_to_field(
                    "Note",
                    "{{Word}}",
                    copy_on_unfocus_when_edit=True,
                    copy_on_unfocus_trigger_field="Word",
                ),
                d.field_to_field(
                    "Reading",
                    use_code=True,
                    copy_as_code="raise ValueError('not this field')",
                    copy_on_unfocus_when_edit=True,
                    copy_on_unfocus_trigger_field="Reading",
                ),
            ],
            select_card_count="0",
        )

        copied: list = []
        ok = copy_for_single_trigger_note(
            definition,
            note,
            copied_into_notes=copied,
            copied_into_cards_dict={},
            field_only="Word",
        )

        assert ok is True, logger.errors
        assert [n["Note"] for n in copied] == ["src"]


class TestACopyIfEmptyWriteOfAMigratedJoin:
    """What `copy_if_empty` skips in a migrated Destination-to-sources definition.

    Format 1 looked at the destination field before it read a single source note, so a
    write whose field was already filled cost nothing and could not fail the run. The
    migrated shape carries the policy on the write, as `write_if: "empty"`, and the write
    honours it -- but the write is the last stage, and the list, loop and reduce that feed
    it have already evaluated the right-hand side once per source note by then.
    """

    @pytest.fixture
    def source(self, col):
        return real_anki.add_note(col, VOCAB, {"Word": "src", "Meaning": "M"}, tags=["pool"])

    def test_a_write_whose_field_is_filled_is_not_evaluated(self, col, note, source, logger):
        # `Reading` is already filled, so the write into it is declined either way; the
        # question is whether its right-hand side runs first. Here it raises, which turns
        # the answer into a failed definition and takes the `Note` write down with it.
        definition = d.destination_to_sources(
            copy_from_cards_query="tag:pool",
            field_to_field_defs=[
                d.field_to_field("Note", "{{Word}}"),
                d.field_to_field(
                    "Reading",
                    use_code=True,
                    copy_as_code="raise ValueError('not this field')",
                    copy_if_empty=True,
                ),
            ],
            select_card_count="0",
        )

        copied: list = []
        ok = copy_for_single_trigger_note(
            definition, note, copied_into_notes=copied, copied_into_cards_dict={}
        )

        assert ok is True, logger.errors
        assert [(n["Note"], n["Reading"]) for n in copied] == [("src", "ne-ko")]
