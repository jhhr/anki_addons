"""Characterization tests for `copy_into_single_note`: trigger fields, tags, files, actions.

This is where one destination note is actually written: the field-to-field defs, then tags,
then files, then card actions, in that order and unconditionally -- a definition whose
field-to-field half is filtered out by `field_only` still runs its tags, files and actions.
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
from copy_anywhere.logic.copy_fields import (
    CopyFailedException,
    copy_for_single_trigger_note,
    copy_into_single_note,
)


@pytest.fixture
def note(col):
    return real_anki.add_note(
        col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat"}
    )


def run(note, logger, **kwargs):
    return copy_into_single_note(
        field_to_field_defs=kwargs.pop("field_to_field_defs", []),
        field_to_file_defs=kwargs.pop("field_to_file_defs", []),
        card_actions=kwargs.pop("card_actions", []),
        destination_note=note,
        source_notes=kwargs.pop("source_notes", [note]),
        logger=logger,
        **kwargs,
    )


class TestTriggerFieldSplitting:
    def test_a_quoted_list_splits_into_names(self):
        field_def = d.field_to_field("Note", copy_on_unfocus_trigger_field='"a", "b"')
        assert get_field_to_field_unfocus_trigger_fields(field_def, False) == ["a", "b"]

    def test_a_single_name_splits_to_one(self):
        field_def = d.field_to_field("Note", copy_on_unfocus_trigger_field="a")
        assert get_field_to_field_unfocus_trigger_fields(field_def, False) == ["a"]

    def test_an_empty_value_splits_to_one_empty_name(self):
        # `"".strip('""').split('", "')` is `['']`, which is truthy, so the
        # `or [copy_into_note_field]` fallback beside it is dead code: a Within-note def with
        # no trigger field configured is never matched by field_only, even though the
        # fallback reads as though it should be. Pinned as-is -- fixing it changes which
        # definitions fire on unfocus, which belongs in its own commit.
        field_def = d.field_to_field("Note", copy_on_unfocus_trigger_field="")
        assert get_field_to_field_unfocus_trigger_fields(field_def, False) == [""]
        assert get_field_to_field_unfocus_trigger_fields(field_def, True) == [""]

    def test_field_only_runs_just_the_matching_def(self, note, logger):
        modified, _, _ = run(
            note,
            logger,
            field_to_field_defs=[
                d.field_to_field("Meaning", "{{Word}}", copy_on_unfocus_trigger_field="Word"),
                d.field_to_field("Note", "{{Word}}", copy_on_unfocus_trigger_field="Reading"),
            ],
            field_only="Word",
        )
        assert modified is True
        assert note["Meaning"] == "neko"
        assert note["Note"] == ""

    def test_field_only_still_lets_tags_and_actions_run(self, note, logger):
        # The field_only filter is a `continue` inside the field-to-field loop only. Tags,
        # files and card actions sit after that loop and always run.
        modified, _, _ = run(
            note,
            logger,
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
    def test_tags_are_added(self, note, logger):
        modified, _, _ = run(note, logger, add_tags='"x", "y"')
        assert note.has_tag("x") and note.has_tag("y")
        assert modified is True

    def test_a_tag_already_present_is_not_re_added(self, note, logger):
        note.add_tag("x")
        modified, _, _ = run(note, logger, add_tags="x")
        assert modified is False

    def test_tags_are_removed(self, note, logger):
        note.add_tag("x")
        note.add_tag("y")
        modified, _, _ = run(note, logger, remove_tags='"x", "y"')
        assert not note.has_tag("x") and not note.has_tag("y")
        assert modified is True

    def test_a_tag_not_present_is_not_removed(self, note, logger):
        modified, _, _ = run(note, logger, remove_tags="absent")
        assert modified is False

    def test_an_empty_add_tags_adds_nothing_and_does_not_mark_the_note_modified(
        self, note, logger
    ):
        # The value in almost every real definition. Before split_tags dropped empty names,
        # this added a tag called "" and set modified_dest_note for every destination note
        # whether or not anything was copied, inflating the processed count, the
        # copied-into-notes list and the undo entry.
        assert split_tags("") == []
        modified, _, _ = run(note, logger, add_tags="", remove_tags="")
        assert modified is False
        assert note.tags == []

    def test_an_empty_add_tags_does_not_inflate_the_destination_count(self, col, logger):
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
            logger=logger,
        )
        assert copied_into_notes == []
        assert target.tags == ["pool"]


class TestFiles:
    def test_a_file_is_written_with_an_underscore_prefix(self, note, logger, media_dir):
        run(
            note,
            logger,
            field_to_file_defs=[d.field_to_file("out.txt", "{{Word}}")],
        )
        assert (media_dir / "_out.txt").read_text(encoding="utf-8") == "neko"

    def test_a_name_that_already_has_the_prefix_is_not_double_prefixed(
        self, note, logger, media_dir
    ):
        run(note, logger, field_to_file_defs=[d.field_to_file("_out.txt", "{{Word}}")])
        assert (media_dir / "_out.txt").exists()
        assert not (media_dir / "__out.txt").exists()

    def test_an_empty_filename_aborts_the_definition(self, note, logger):
        with pytest.raises(CopyFailedException):
            run(note, logger, field_to_file_defs=[d.field_to_file("", "{{Word}}")])
        assert logger.has_error("No file name provided")

    def test_copy_if_empty_on_a_file_means_do_not_overwrite(self, note, logger, media_dir):
        (media_dir / "_out.txt").write_text("original", encoding="utf-8")
        run(
            note,
            logger,
            field_to_file_defs=[d.field_to_file("out.txt", "{{Word}}", copy_if_empty=True)],
        )
        assert (media_dir / "_out.txt").read_text(encoding="utf-8") == "original"

    def test_the_filename_sees_the_live_note_while_dest_prefix_sees_the_pre_copy_copy(
        self, note, logger, media_dir
    ):
        # Genuinely surprising, and worth pinning: the filename is interpolated over
        # `notes=[destination_note]` -- the live, already-modified note -- while `dest_note`
        # is the copy taken before any def ran. So a field written by an earlier
        # field-to-field def shows up in `{{Field}}` but not in `{{__Dest__Field}}`.
        run(
            note,
            logger,
            field_to_field_defs=[d.field_to_field("Note", "written")],
            field_to_file_defs=[
                d.field_to_file("{{Note}}-{{__Dest__Note}}.txt", "{{Word}}")
            ],
        )
        assert (media_dir / "_written-.txt").exists()

    def test_the_code_path_runs_once_per_source_note_and_writes_every_tuple(
        self, col, note, logger, media_dir
    ):
        sources = [
            real_anki.add_note(col, VOCAB, {"Word": "a"}),
            real_anki.add_note(col, VOCAB, {"Word": "b"}),
        ]
        run(
            note,
            logger,
            source_notes=sources,
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
        self, col, note, logger, media_dir
    ):
        sources = [
            real_anki.add_note(col, VOCAB, {"Word": "a"}),
            real_anki.add_note(col, VOCAB, {"Word": "b"}),
        ]
        variables = {}
        run(
            note,
            logger,
            source_notes=sources,
            variable_values_dict=variables,
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
        _, wrote, _ = run(
            note,
            logger,
            field_to_file_defs=[d.field_to_file("", use_code=True, copy_as_code="return []")],
        )
        assert wrote is False
        assert list(media_dir.iterdir()) == []

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
        with pytest.raises(CopyFailedException, match=message):
            run(
                note,
                logger,
                field_to_file_defs=[d.field_to_file("", use_code=True, copy_as_code=code)],
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
        _, _, cards = run(
            note,
            logger,
            card_actions=[malformed, d.card_action(VOCAB, "Recall", set_flag=2)],
        )
        assert logger.has_error("Invalid card type name")
        assert card_named(cards, "Recall").user_flag() == 2
        assert card_named(cards, "Recognition").user_flag() == 0

    def test_an_action_for_another_note_type_is_skipped(self, note, logger):
        _, _, cards = run(
            note, logger, card_actions=[d.card_action("Some Other Type", "Recognition", set_flag=1)]
        )
        assert card_named(cards, "Recognition").user_flag() == 0

    def test_two_actions_for_one_template_leave_only_the_later_one(self, note, logger):
        # They are collected into a dict keyed by template name, so the second overwrites.
        _, _, cards = run(
            note,
            logger,
            card_actions=[
                d.card_action(VOCAB, "Recognition", set_flag=1),
                d.card_action(VOCAB, "Recognition", set_flag=5),
            ],
        )
        assert card_named(cards, "Recognition").user_flag() == 5

    def test_change_deck_by_name_and_by_id(self, col, note, logger):
        deck_id = col.decks.id_for_name("Other")
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", change_deck="Other")]
        )
        assert card_named(cards, "Recognition").did == deck_id
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recall", change_deck=deck_id)]
        )
        assert card_named(cards, "Recall").did == deck_id

    @pytest.mark.parametrize("value", [None, "-", 0])
    def test_change_deck_no_ops(self, col, note, logger, value):
        before = note.cards()[0].did
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", change_deck=value)]
        )
        assert card_named(cards, "Recognition").did == before
        assert not hasattr(card_named(cards, "Recognition"), "edited")

    def test_change_deck_to_a_name_that_does_not_exist_is_logged_and_does_nothing(
        self, note, logger
    ):
        before = note.cards()[0].did
        _, _, cards = run(
            note,
            logger,
            card_actions=[d.card_action(VOCAB, "Recognition", change_deck="No Such Deck")],
        )
        assert logger.has_error("not found. Cannot move card")
        assert card_named(cards, "Recognition").did == before

    def test_a_filtered_deck_card_has_its_odid_rewritten_not_its_did(self, col, note, logger):
        card = card_named(note.cards(), "Recognition")
        original_did = card.did
        card.odid = card.did
        card.did = col.decks.id("Filtered")
        col.update_card(card)
        target = col.decks.id_for_name("Other")
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", change_deck="Other")]
        )
        moved = next(c for c in cards if c.template()["name"] == "Recognition")
        assert moved.odid == target
        assert moved.did != target
        assert moved.did != original_did

    def test_suspend_and_unsuspend(self, note, logger):
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", suspend=True)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.queue == -1
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", suspend=False)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.queue == card.type

    def test_burying_a_suspended_card_is_a_no_op(self, col, note, logger):
        card = card_named(note.cards(), "Recognition")
        card.queue = -1
        col.update_card(card)
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", bury=True)]
        )
        buried = next(c for c in cards if c.template()["name"] == "Recognition")
        assert buried.queue == -1
        assert not hasattr(buried, "edited")

    def test_burying_an_unsuspended_card_works(self, note, logger):
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", bury=True)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.queue == -2

    @pytest.mark.parametrize("flag", [0, 1, 2, 3, 4, 5, 6, 7])
    def test_every_valid_flag_is_set(self, note, logger, flag):
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=flag)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == flag

    @pytest.mark.parametrize("flag", [8, -1])
    def test_an_out_of_range_flag_is_ignored(self, note, logger, flag):
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=flag)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == 0
        assert not hasattr(card, "edited")

    def test_set_flag_true_is_accepted_as_flag_one(self, note, logger):
        # `isinstance(True, int)` is True and the guard does not exclude bool, unlike
        # set_desired_retention's, which does. Pinned so the inconsistency is visible.
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=True)]
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == 1


class TestDesiredRetention:
    def _card(self, cards, name="Recognition"):
        return card_named(cards, name)

    def test_a_float_between_zero_and_one_is_set_as_is(self, note, logger):
        _, _, cards = run(
            note,
            logger,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention=0.85)],
        )
        assert self._card(cards).desired_retention == pytest.approx(0.85)

    def test_an_int_is_read_as_a_percentage(self, note, logger):
        _, _, cards = run(
            note,
            logger,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention=90)],
        )
        assert self._card(cards).desired_retention == pytest.approx(0.9)

    def test_a_string_is_looked_up_in_the_cards_custom_data(self, col, note, logger):
        real_anki.set_custom_data(col, self._card(note.cards()).id, json.dumps({"dr": 88}))
        note = col.get_note(note.id)
        _, _, cards = run(
            note,
            logger,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention="dr")],
        )
        assert self._card(cards).desired_retention == pytest.approx(0.88)

    def test_a_missing_custom_data_key_sets_nothing(self, col, note, logger):
        real_anki.set_custom_data(col, self._card(note.cards()).id, json.dumps({"other": 1}))
        note = col.get_note(note.id)
        _, _, cards = run(
            note,
            logger,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention="dr")],
        )
        assert self._card(cards).desired_retention is None

    def test_empty_custom_data_sets_nothing(self, note, logger):
        _, _, cards = run(
            note,
            logger,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention="dr")],
        )
        assert self._card(cards).desired_retention is None

    @pytest.mark.parametrize("value", [0, 1.0, True])
    def test_out_of_range_and_boolean_values_are_ignored(self, note, logger, value):
        # True is excluded explicitly here -- unlike set_flag, which accepts it.
        _, _, cards = run(
            note,
            logger,
            card_actions=[d.card_action(VOCAB, "Recognition", set_desired_retention=value)],
        )
        assert self._card(cards).desired_retention is None


class TestCardActionCode:
    def test_code_returning_none_skips_every_action_for_that_card_type(self, note, logger):
        _, _, cards = run(
            note,
            logger,
            card_actions=[
                d.card_action(
                    VOCAB, "Recognition", set_flag=3, use_code=True, action_code="return None"
                )
            ],
        )
        card = next(c for c in cards if c.template()["name"] == "Recognition")
        assert card.user_flag() == 0

    def test_code_returning_a_dict_replaces_the_configured_action(self, note, logger):
        _, _, cards = run(
            note,
            logger,
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
        with pytest.raises(CopyFailedException, match=message):
            run(
                note,
                logger,
                card_actions=[
                    d.card_action(VOCAB, "Recognition", use_code=True, action_code=code)
                ],
            )


class TestEditedFlag:
    def test_an_untouched_card_gets_no_edited_attribute(self, note, logger):
        # The dynamic `edited` attribute is what drives the progress counts and the filter
        # that decides which cards are handed to update_cards, so its absence matters.
        _, _, cards = run(note, logger, card_actions=[])
        assert all(not hasattr(card, "edited") for card in cards)

    def test_a_touched_card_is_marked_edited(self, note, logger):
        _, _, cards = run(
            note, logger, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=1)]
        )
        edited = [card for card in cards if getattr(card, "edited", False)]
        assert [card.template()["name"] for card in edited] == ["Recognition"]
