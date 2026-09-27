"""The rename warning store's readers and the location keys it is filed by.

The pass writes the store and many places read it (`logic/rename_warnings.py`); the keys are
built by one helper for both the pass and the editor (`logic/rename_locations.py`). Neither
needs a collection.
"""

import pytest

import definitions as d
from copy_anywhere.hooks.rename_hooks import broken_definitions_warning
from copy_anywhere.logic.rename_locations import (
    card_action_key,
    field_write_key,
    split_key,
    stage_key,
    trigger_key,
)
from copy_anywhere.logic.rename_reconcile import ReconcileResult, StaleName
from copy_anywhere.logic.rename_warnings import (
    BLOCKING_ADVICE,
    WARNINGS_KEY,
    blocking_explanation,
    blocking_messages,
    blocking_tooltip,
    blocks_run,
    non_blocking_messages,
    remove_rename_warning,
    rename_warning_entries,
)


class TestLocationKeys:
    @pytest.mark.parametrize(
        "key, expected",
        [
            (stage_key("s-1", "query.text"), "s-1.query.text"),
            (stage_key("s-1", "selection.sort_field"), "s-1.selection.sort_field"),
            (field_write_key("w-1", "value.code"), "w-1.value.code"),
            (field_write_key("w-1", "field"), "w-1.field"),
            (card_action_key("a-1", "action_code"), "a-1.action_code"),
            (trigger_key("on_unfocus.edit_fields"), "triggers.on_unfocus.edit_fields"),
        ],
    )
    def test_each_kind_of_holder_spells_its_key(self, key, expected):
        assert key == expected

    @pytest.mark.parametrize(
        "build, anchor, path",
        [
            (stage_key, "0b6f7c1e-3d4a-4f7e-9d1c-2a7b8c9d0e1f", "value.text"),
            (field_write_key, "def-guid::field-write-2", "value.code"),
            (card_action_key, "def-guid::stage-1", "action_code"),
            (stage_key, "s", "target"),
        ],
    )
    def test_a_key_splits_back_into_its_anchor_and_path(self, build, anchor, path):
        assert split_key(build(anchor, path)) == (anchor, path)

    def test_a_trigger_key_splits_on_the_fixed_anchor(self):
        assert split_key(trigger_key("on_unfocus.add_fields")) == (
            "triggers",
            "on_unfocus.add_fields",
        )

    def test_a_key_with_no_path_splits_to_an_empty_one(self):
        assert split_key("definition") == ("definition", "")


#: Where `d.warned` files entries by default; the readers do not look at the key.
LOCATION = "definition"


class TestTheReaders:
    def test_every_entry_comes_with_its_location_in_stored_order(self):
        first = d.rename_warning("first")
        second = d.rename_warning("second", blocks_run=False)
        third = d.rename_warning("third")
        definition = d.staged()
        definition[WARNINGS_KEY] = {"a.value.text": [first, "junk"], "b.field": [second, third]}

        assert rename_warning_entries(definition) == [
            ("a.value.text", first),
            ("b.field", second),
            ("b.field", third),
        ]
        # The entries themselves, which is what the editor dismisses by.
        assert rename_warning_entries(definition)[0][1] is first

    def test_only_blocking_entries_block(self):
        definition = d.warned(
            d.staged(),
            d.rename_warning("warned only", blocks_run=False),
            d.rename_warning("blocks"),
        )

        assert blocking_messages(definition) == ["blocks"]
        assert blocks_run(definition) is True

    def test_the_entries_that_do_not_block_are_the_rest(self):
        definition = d.warned(
            d.staged(),
            d.rename_warning("first warning", blocks_run=False),
            d.rename_warning("blocks"),
            d.rename_warning("second warning", blocks_run=False),
        )

        assert non_blocking_messages(definition) == ["first warning", "second warning"]
        assert non_blocking_messages(d.staged()) == []

    def test_a_store_of_warnings_only_does_not_block(self):
        definition = d.warned(d.staged(), d.rename_warning("warned only", blocks_run=False))

        assert blocking_messages(definition) == []
        assert blocks_run(definition) is False

    def test_no_store_blocks_nothing(self):
        assert blocks_run(d.staged()) is False
        assert blocks_run(None) is False
        assert rename_warning_entries("not a definition") == []

    def test_the_tooltip_and_the_explanation_say_the_messages_and_the_advice(self):
        assert blocking_tooltip(["one", "two"]).splitlines() == [
            "This definition is not run while it has these rename warnings:",
            "one",
            "two",
            BLOCKING_ADVICE,
        ]
        assert blocking_explanation(["one", "two."]) == f"one; two. {BLOCKING_ADVICE}"


class TestRemovingOne:
    def test_it_goes_by_identity_not_by_message(self):
        same = "Field was renamed"
        first = d.rename_warning(same)
        second = d.rename_warning(same)
        definition = d.warned(d.staged(), first, second)

        assert remove_rename_warning(definition, second) is True

        assert definition[WARNINGS_KEY] == {LOCATION: [first]}
        assert definition[WARNINGS_KEY][LOCATION][0] is first

    def test_an_emptied_location_goes_and_the_others_stay(self):
        kept = d.rename_warning("kept")
        gone = d.rename_warning("gone")
        definition = d.staged()
        definition[WARNINGS_KEY] = {"a.value.text": [gone], "b.field": [kept]}

        remove_rename_warning(definition, gone)

        assert definition[WARNINGS_KEY] == {"b.field": [kept]}

    def test_the_store_goes_once_nothing_in_it_has_a_message(self):
        last = d.rename_warning("last")
        definition = d.staged()
        definition[WARNINGS_KEY] = {"a.value.text": [last], "b.field": [{"old": "Word"}]}

        remove_rename_warning(definition, last)

        assert WARNINGS_KEY not in definition

    def test_an_entry_not_stored_changes_nothing(self):
        stored = d.rename_warning("stored")
        definition = d.warned(d.staged(), stored)

        assert remove_rename_warning(definition, d.rename_warning("stored")) is False
        assert definition[WARNINGS_KEY] == {LOCATION: [stored]}


class TestTheWarningAfterANoteTypeOperation:
    def stale(self, message: str, blocks: bool) -> StaleName:
        return StaleName("g", "Def", "field", "Word", message=message, blocks_run=blocks)

    def test_it_lists_only_the_new_warnings_that_block(self):
        result = ReconcileResult(
            newly_marked=[self.stale("blocks", True), self.stale("warns", False)]
        )

        text = broken_definitions_warning(result)

        assert text is not None
        assert "blocks" in text and "warns" not in text

    def test_new_warnings_that_do_not_block_say_nothing(self):
        result = ReconcileResult(newly_marked=[self.stale("warns", False)])

        assert broken_definitions_warning(result) is None
