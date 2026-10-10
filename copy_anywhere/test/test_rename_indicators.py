"""Rename warnings where they are spelled: each editor part's indicator, the banner grouped
by location, and the warnings a save drops once their text no longer spells the old name.

The pass files a warning under a location key built by `rename_locations`, and every
editor part builds the key of what it holds the same way. A part that spelled its key
differently would show nothing and fail no other test, so each kind of part is checked
here through the real dialog with a warning filed under the key the pass would use.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import pytest

import definitions as d
from note_types import VOCAB
from copy_anywhere.logic.rename_locations import (
    card_action_key,
    field_write_key,
    stage_key,
    trigger_key,
)
from copy_anywhere.logic.rename_reconcile import drop_cleared_warnings
from copy_anywhere.logic.rename_warnings import INACTIVE_KEY, WARNINGS_KEY, blocks_run
from copy_anywhere.ui.rename_indicator import (
    BLOCKING_HEADER,
    BLOCKING_ICON,
    RenameIndicator,
    WARNING_HEADER,
    WARNING_ICON,
)
from copy_anywhere.ui.rename_marks_banner import ORPHANED

#: A location whose anchor no stage, write or card action of these definitions has.
ORPHAN = stage_key("deleted-stage", "value.text")


def field_warning(old: str = "Word", new: Optional[str] = "Term", blocks_run: bool = True):
    done = f'renamed to "{new}"' if new else "deleted"
    return d.rename_warning(
        f'Field "{old}" of note type "{VOCAB}" was {done}', old=old, new=new, blocks_run=blocks_run
    )


def deck_warning(old: str = "Old", new: Optional[str] = "New", blocks_run: bool = True):
    done = f'renamed to "{new}"' if new else "deleted"
    return d.rename_warning(
        f'Deck "{old}" was {done}',
        old=old,
        new=new,
        kind="deck",
        note_type_id=None,
        blocks_run=blocks_run,
    )


def with_warnings(definition: dict, warnings: dict[str, list[dict]]) -> dict:
    definition[WARNINGS_KEY] = {key: list(entries) for key, entries in warnings.items()}
    return definition


def a_write(field: str, value: dict, guid: str = "w1", **extra: Any) -> dict:
    write = d.write(field, value)
    write["guid"] = guid
    write.update(extra)
    return write


def an_action(guid: str = "a1", **extra: Any) -> dict:
    action = d.card_action(VOCAB, "Recognition", **extra)
    action["guid"] = guid
    return action


@pytest.fixture
def open_editor(col, qapp, widget_parent):
    from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

    opened = []

    def open_editor(definition):
        dialog = EditStagedDefinitionDialog(widget_parent, definition)
        opened.append(dialog)
        # Card actions arrive in batches on a timer; the tests read their rows at once.
        for row in dialog.stage_tree.rows.values():
            card_actions = getattr(row.editor, "card_actions", None)
            if card_actions is not None:
                card_actions.finish_loading_initial_actions()
        dialog.refresh_rename_indicators()
        return dialog

    yield open_editor
    for dialog in opened:
        dialog._refresh_timer.stop()


def shown_indicators(dialog) -> list[RenameIndicator]:
    return [
        indicator for indicator in dialog.findChildren(RenameIndicator) if not indicator.isHidden()
    ]


def save(dialog) -> dict:
    # Asserted first: a blocked save opens a message box, which would wait for a click.
    assert dialog.ok_button.isEnabled(), dialog.status_label.text()
    dialog.ok_button.click()
    assert dialog.result()
    return dialog.get_copy_definition()


def editor(dialog, guid: str):
    return dialog.stage_tree.rows[guid].editor


# -- One case per kind of editor part ------------------------------------------------------


class Part:
    """A definition with one warning at one part, and how to find that part's indicator."""

    def __init__(
        self,
        stages: list,
        key: str,
        warning: dict,
        indicator: Callable[[Any], Optional[RenameIndicator]],
        **triggers: Any,
    ) -> None:
        self.stages = stages
        self.key = key
        self.warning = warning
        self.indicator = indicator
        self.triggers = triggers

    def definition(self) -> dict:
        definition = d.staged(
            "Warned", stages=self.stages, note_types=[VOCAB], **self.triggers
        )
        return with_warnings(definition, {self.key: [self.warning]})


def _parts() -> dict[str, Part]:
    def card_action_indicator(which: str):
        def find(dialog):
            actions = editor(dialog, "ec").card_actions
            return actions.action_ui_components["a1"][which]

        return find

    return {
        "query": Part(
            [d.note_query("found", "deck:Old", guid="q")],
            stage_key("q", "query.text"),
            deck_warning(),
            lambda dialog: editor(dialog, "q").query.rename_indicator,
        ),
        "sort field": Part(
            [
                d.note_query(
                    "found",
                    "deck:Default",
                    guid="q",
                    selection={"strategy": "all", "count": None, "sort_field": "Word"},
                )
            ],
            stage_key("q", "selection.sort_field"),
            field_warning(blocks_run=False),
            lambda dialog: editor(dialog, "q").sort_field_indicator,
        ),
        "value code": Part(
            [d.variable("x", d.code("return trigger['Word']"), guid="v")],
            stage_key("v", "value.code"),
            field_warning(),
            lambda dialog: editor(dialog, "v").value.rename_indicator,
        ),
        "select index": Part(
            [
                d.note_query("found", "deck:Default", guid="q"),
                d.select_note("found", "one", d.text("{{trigger.Word}}"), guid="s"),
            ],
            stage_key("s", "index.text"),
            field_warning(),
            lambda dialog: editor(dialog, "s").index.rename_indicator,
        ),
        "file name": Part(
            [d.read_file("contents", "{{trigger.Word}}.txt", guid="r")],
            stage_key("r", "filename.text"),
            field_warning(),
            lambda dialog: editor(dialog, "r").filename.rename_indicator,
        ),
        "file content": Part(
            [d.write_file("out.txt", d.code("return trigger['Word']"), guid="wf")],
            stage_key("wf", "content.code"),
            field_warning(),
            lambda dialog: editor(dialog, "wf").content.rename_indicator,
        ),
        "condition as a search": Part(
            [
                d.condition(
                    d.text("deck:Old"),
                    then=[],
                    guid="c",
                    predicate_kind="note_query",
                    predicate_target={"binding": "trigger"},
                )
            ],
            stage_key("c", "predicate.text"),
            deck_warning(),
            lambda dialog: editor(dialog, "c").predicate.rename_indicator,
        ),
        "field write target": Part(
            [d.edit_note("trigger", fields=[a_write("Word", d.text("x"))], guid="e")],
            field_write_key("w1", "field"),
            field_warning(),
            lambda dialog: editor(dialog, "e").field_rows[0].field_indicator,
        ),
        "field write value": Part(
            [
                d.edit_note(
                    "trigger", fields=[a_write("Meaning", d.text("{{trigger.Word}}"))], guid="e"
                )
            ],
            field_write_key("w1", "value.text"),
            field_warning(),
            lambda dialog: editor(dialog, "e").field_rows[0].value.rename_indicator,
        ),
        "field write unfocus gate": Part(
            [
                d.edit_note(
                    "trigger",
                    fields=[
                        a_write("Meaning", d.text("x"), unfocus_trigger_fields=["Word", "Note"])
                    ],
                    guid="e",
                )
            ],
            field_write_key("w1", "unfocus_trigger_fields"),
            field_warning(),
            lambda dialog: editor(dialog, "e").field_rows[0].unfocus_indicator,
        ),
        "stage unfocus gate": Part(
            [
                {
                    **d.variable("x", d.text("{{trigger.Meaning}}"), guid="g"),
                    "unfocus_trigger_fields": ["Word", "Note"],
                }
            ],
            stage_key("g", "unfocus_trigger_fields"),
            field_warning(),
            lambda dialog: editor(dialog, "g").gate_fields_indicator,
        ),
        "stage write-if field": Part(
            [
                {
                    **d.variable("x", d.text("{{trigger.Meaning}}"), guid="g"),
                    "write_if": "empty",
                    "write_if_field": "Word",
                }
            ],
            stage_key("g", "write_if_field"),
            field_warning(),
            lambda dialog: editor(dialog, "g").gate_write_if_indicator,
        ),
        "card action code": Part(
            [
                d.edit_card(
                    "",
                    card_actions=[an_action(use_code=True, action_code="return {'deck': 'Old'}")],
                    guid="ec",
                )
            ],
            card_action_key("a1", "action_code"),
            deck_warning(),
            card_action_indicator("code_indicator"),
        ),
        "card action deck": Part(
            [d.edit_card("", card_actions=[an_action(change_deck="Old")], guid="ec")],
            card_action_key("a1", "change_deck"),
            deck_warning(),
            card_action_indicator("deck_indicator"),
        ),
        "trigger unfocus list": Part(
            [],
            trigger_key("on_unfocus.add_fields"),
            field_warning(),
            lambda dialog: dialog.triggers_editor.rename_indicators[1],
            on_unfocus={"edit_fields": [], "add_fields": ["Word"]},
        ),
    }


PARTS = _parts()


class TestEachPartShowsTheWarningFiledUnderItsKey:
    @pytest.mark.parametrize("name", list(PARTS))
    def test_only_that_part_shows_it(self, open_editor, name):
        part = PARTS[name]
        dialog = open_editor(part.definition())

        indicator = part.indicator(dialog)
        assert indicator is not None
        assert shown_indicators(dialog) == [indicator]
        assert indicator.messages() == [part.warning["message"]]

    @pytest.mark.parametrize("name", list(PARTS))
    def test_a_warning_under_another_key_shows_nowhere(self, open_editor, name):
        part = PARTS[name]
        definition = part.definition()
        definition[WARNINGS_KEY] = {part.key + "x": [part.warning]}

        assert shown_indicators(open_editor(definition)) == []


class TestTheIndicator:
    def query(self, text: str = "deck:Old") -> list:
        return [d.note_query("found", text, guid="q")]

    def indicator(self, dialog) -> RenameIndicator:
        found = editor(dialog, "q").query.rename_indicator
        assert found is not None
        return found

    def test_a_blocking_warning_shows_the_cross_and_says_the_definition_is_not_run(
        self, open_editor
    ):
        definition = d.staged("W", stages=self.query())
        dialog = open_editor(
            with_warnings(definition, {stage_key("q", "query.text"): [deck_warning()]})
        )

        indicator = self.indicator(dialog)
        assert indicator.icon.text() == BLOCKING_ICON and "#c0392b" in BLOCKING_ICON
        assert indicator.icon.toolTip().splitlines() == [
            BLOCKING_HEADER,
            'Deck "Old" was renamed to "New"',
        ]

    def test_only_warn_only_ones_show_the_info_mark(self, open_editor):
        definition = d.staged("W", stages=self.query())
        dialog = open_editor(
            with_warnings(
                definition, {stage_key("q", "query.text"): [deck_warning(blocks_run=False)]}
            )
        )

        indicator = self.indicator(dialog)
        assert indicator.icon.text() == WARNING_ICON and "#2471a3" in WARNING_ICON
        assert indicator.icon.toolTip().splitlines()[0] == WARNING_HEADER

    def test_one_blocking_among_warn_only_ones_is_enough_for_the_cross(self, open_editor):
        definition = d.staged("W", stages=self.query("deck:Old deck:Older"))
        dialog = open_editor(
            with_warnings(
                definition,
                {
                    stage_key("q", "query.text"): [
                        deck_warning(blocks_run=False),
                        deck_warning("Older", blocks_run=True),
                    ]
                },
            )
        )

        assert self.indicator(dialog).icon.text() == BLOCKING_ICON

    def test_it_hides_as_the_name_is_typed_away_and_comes_back_with_it(self, open_editor):
        definition = d.staged("W", stages=self.query())
        dialog = open_editor(
            with_warnings(definition, {stage_key("q", "query.text"): [deck_warning()]})
        )
        indicator = self.indicator(dialog)
        text_edit = editor(dialog, "q").query.text_layout.text_edit

        text_edit.setPlainText("deck:New")
        assert indicator.isHidden()
        text_edit.setPlainText("deck:Old is:due")
        assert not indicator.isHidden()

    def test_it_hides_when_the_expression_switches_to_its_other_side(self, open_editor):
        definition = d.staged("W", stages=[d.variable("x", d.code("return 'Word'"), guid="v")])
        dialog = open_editor(
            with_warnings(definition, {stage_key("v", "value.code"): [field_warning()]})
        )
        value = editor(dialog, "v").value
        assert value.rename_indicator is not None and not value.rename_indicator.isHidden()

        assert value.use_code_toggle is not None
        value.use_code_toggle.setChecked(False)

        assert value.rename_indicator.isHidden()

    def test_code_that_cannot_be_read_keeps_it_shown(self, open_editor):
        definition = d.staged("W", stages=[d.variable("x", d.code("return 'Word'"), guid="v")])
        dialog = open_editor(
            with_warnings(definition, {stage_key("v", "value.code"): [field_warning()]})
        )
        value = editor(dialog, "v").value
        assert value.code_layout is not None and value.rename_indicator is not None

        value.code_layout.text_edit.setPlainText("return 'Wo")

        assert not value.rename_indicator.isHidden()

    def test_a_condition_switched_from_a_search_stops_reading_it_as_one(self, open_editor):
        definition = d.staged(
            "W",
            stages=[
                d.condition(
                    d.text("deck:Old"),
                    then=[],
                    guid="c",
                    predicate_kind="note_query",
                    predicate_target={"binding": "trigger"},
                )
            ],
        )
        dialog = open_editor(
            with_warnings(definition, {stage_key("c", "predicate.text"): [deck_warning()]})
        )
        condition = editor(dialog, "c")
        assert condition.predicate.rename_indicator is not None
        assert not condition.predicate.rename_indicator.isHidden()

        condition.match_as_search.setChecked(False)

        assert condition.predicate.rename_indicator.isHidden()

    def test_a_card_actions_deck_is_not_in_effect_once_code_decides(self, open_editor):
        action = an_action(change_deck="Old", action_code="return {}")
        definition = d.staged("W", stages=[d.edit_card("", card_actions=[action], guid="ec")])
        dialog = open_editor(
            with_warnings(definition, {card_action_key("a1", "change_deck"): [deck_warning()]})
        )
        ui = editor(dialog, "ec").card_actions.action_ui_components["a1"]
        assert not ui["deck_indicator"].isHidden()
        # A deck the collection no longer has is still the one chosen, not "-".
        assert ui["deck_combo"].currentText() == "Old"

        ui["use_code_toggle"].setChecked(True)

        assert ui["deck_indicator"].isHidden()

    def test_dismissing_it_in_the_banner_hides_it(self, open_editor):
        definition = d.staged("W", stages=self.query())
        dialog = open_editor(
            with_warnings(definition, {stage_key("q", "query.text"): [deck_warning()]})
        )

        dialog.marks_banner.dismiss_all_button.setVisible(True)
        dialog.marks_banner.dismiss_all_button.click()

        assert self.indicator(dialog).isHidden()


# -- Save drops what the text no longer spells ---------------------------------------------


class TestDropClearedWarnings:
    """The rule a save applies, without the dialog."""

    def definition(self, value: dict, warnings: dict[str, list[dict]]) -> dict:
        definition = d.staged(
            "W", stages=[d.edit_note("trigger", fields=[a_write("Meaning", value)], guid="e")]
        )
        return with_warnings(definition, warnings)

    def test_a_warning_whose_text_still_spells_the_name_is_kept(self):
        warning = field_warning()
        definition = self.definition(
            d.text("{{trigger.Word}}"), {field_write_key("w1", "value.text"): [warning]}
        )

        assert drop_cleared_warnings(definition)[WARNINGS_KEY] == {
            field_write_key("w1", "value.text"): [warning]
        }

    def test_one_whose_text_no_longer_does_is_dropped_and_the_store_with_it(self):
        definition = self.definition(
            d.text("{{trigger.Term}}"), {field_write_key("w1", "value.text"): [field_warning()]}
        )

        assert WARNINGS_KEY not in drop_cleared_warnings(definition)

    def test_only_the_cleared_one_of_a_location_goes(self):
        kept = field_warning("Note", None)
        definition = self.definition(
            d.text("{{trigger.Term}} {{trigger.Note}}"),
            {field_write_key("w1", "value.text"): [field_warning(), kept]},
        )

        assert drop_cleared_warnings(definition)[WARNINGS_KEY] == {
            field_write_key("w1", "value.text"): [kept]
        }

    def test_code_that_cannot_be_read_keeps_it(self):
        warning = field_warning()
        definition = self.definition(
            d.code("return trigger['Term"), {field_write_key("w1", "value.code"): [warning]}
        )

        assert drop_cleared_warnings(definition)[WARNINGS_KEY] == {
            field_write_key("w1", "value.code"): [warning]
        }

    def test_the_side_of_an_expression_that_does_not_run_loses_it_once_it_is_fixed(self):
        definition = self.definition(
            d.text("x"), {field_write_key("w1", "value.code"): [field_warning()]}
        )

        assert WARNINGS_KEY not in drop_cleared_warnings(definition)

    def test_the_side_that_does_not_run_keeps_it_without_blocking_until_switched_back(self):
        # Saved in code mode with the text still spelling the name, then switched back:
        # dropping it on the first save let the second one run the old name unwarned.
        value = d.text("{{trigger.Word}}")
        value["mode"], value["code"] = "code", "return 'x'"
        definition = self.definition(
            value, {field_write_key("w1", "value.text"): [field_warning()]}
        )

        drop_cleared_warnings(definition)
        [entry] = definition[WARNINGS_KEY][field_write_key("w1", "value.text")]
        assert entry[INACTIVE_KEY] is True
        assert not blocks_run(definition)

        value["mode"] = "text"
        drop_cleared_warnings(definition)
        assert INACTIVE_KEY not in entry
        assert blocks_run(definition)

    def test_a_switched_off_stage_keeps_it_without_blocking(self):
        definition = self.definition(
            d.text("{{trigger.Word}}"), {field_write_key("w1", "value.text"): [field_warning()]}
        )
        definition["stages"][0]["enabled"] = False

        drop_cleared_warnings(definition)

        assert definition[WARNINGS_KEY][field_write_key("w1", "value.text")][0][INACTIVE_KEY]
        assert not blocks_run(definition)

    def test_an_entry_a_replace_answered_goes_unless_the_text_was_put_back(self):
        # A swap: after Replace, the text spells both old names again, so only the record
        # of the Replace can say the entries were answered.
        before = "{{note.A}} {{note.B}}"
        key = field_write_key("w1", "value.text")
        a_to_b, b_to_a = field_warning("A", "B", False), field_warning("B", "A", False)

        swapped = self.definition(d.text("{{note.B}} {{note.A}}"), {key: [a_to_b, b_to_a]})
        drop_cleared_warnings(swapped, [(key, before, a_to_b), (key, before, b_to_a)])
        assert WARNINGS_KEY not in swapped

        undone = self.definition(d.text(before), {key: [a_to_b, b_to_a]})
        drop_cleared_warnings(undone, [(key, before, a_to_b), (key, before, b_to_a)])
        assert undone[WARNINGS_KEY] == {key: [a_to_b, b_to_a]}

    def test_an_orphaned_location_is_dropped(self):
        definition = self.definition(d.text("{{trigger.Word}}"), {ORPHAN: [field_warning()]})

        assert WARNINGS_KEY not in drop_cleared_warnings(definition)

    def test_an_entry_that_does_not_say_which_rename_it_is_about_is_kept(self):
        unreadable = {"message": "Something was renamed"}
        definition = self.definition(
            d.text("x"), {field_write_key("w1", "value.text"): [unreadable]}
        )

        assert drop_cleared_warnings(definition)[WARNINGS_KEY] == {
            field_write_key("w1", "value.text"): [unreadable]
        }


class TestADuplicatedStageKeepsItsWarnings:
    def test_the_copy_gets_the_originals_warnings_under_its_own_guids(self):
        from copy_anywhere.ui.stage_document import StageDocument

        warning = deck_warning()
        definition = d.staged(
            "W",
            stages=[
                d.note_query("found", "deck:Old", guid="q"),
                d.edit_note(
                    "trigger",
                    fields=[a_write("Meaning", d.text("{{trigger.Word}}"))],
                    card_actions=[an_action(change_deck="Old")],
                    guid="e",
                ),
            ],
        )
        with_warnings(
            definition,
            {
                stage_key("q", "query.text"): [warning],
                field_write_key("w1", "value.text"): [field_warning()],
                card_action_key("a1", "change_deck"): [deck_warning()],
            },
        )
        document = StageDocument(definition)

        query_copy = document.duplicate_stage("q")
        edit_copy = document.duplicate_stage("e")

        assert query_copy is not None and edit_copy is not None
        write_guid = edit_copy["fields"][0]["guid"]
        action_guid = edit_copy["card_actions"][0]["guid"]
        assert document.rename_marks_at(stage_key(query_copy["guid"], "query.text")) == [warning]
        assert document.rename_marks_at(field_write_key(write_guid, "value.text"))
        assert document.rename_marks_at(card_action_key(action_guid, "change_deck"))
        # Copies, so dismissing one leaves the other.
        [copied] = document.rename_marks_at(stage_key(query_copy["guid"], "query.text"))
        document.dismiss_rename_mark(copied)
        assert document.rename_marks_at(stage_key("q", "query.text")) == [warning]

        # Fixing only the original and saving leaves the copy's warning, so it still blocks.
        document.stage("q")["query"]["text"] = "deck:New"
        saved = drop_cleared_warnings(document.to_definition())
        assert blocks_run(saved)


class TestTheDialogSaves:
    def test_save_drops_a_cleared_warning_and_keeps_an_unreadable_code_one(self, open_editor):
        cleared, unreadable = field_warning(), field_warning("Note", None)
        definition = d.staged(
            "W",
            stages=[
                d.variable("x", d.text("{{trigger.Word}}"), guid="v"),
                d.variable("y", d.code("return trigger['Note']"), guid="u"),
            ],
        )
        dialog = open_editor(
            with_warnings(
                definition,
                {
                    stage_key("v", "value.text"): [cleared],
                    stage_key("u", "value.code"): [unreadable],
                },
            )
        )
        editor(dialog, "v").value.text_layout.text_edit.setPlainText("{{trigger.Meaning}}")
        code_layout = editor(dialog, "u").value.code_layout
        assert code_layout is not None
        code_layout.text_edit.setPlainText("return trigger['Note")

        saved = save(dialog)

        assert saved[WARNINGS_KEY] == {stage_key("u", "value.code"): [unreadable]}

    def test_cancel_keeps_every_warning_however_the_text_was_left(self, open_editor):
        warning = field_warning()
        definition = d.staged("W", stages=[d.variable("x", d.text("{{trigger.Word}}"), guid="v")])
        with_warnings(definition, {stage_key("v", "value.text"): [warning]})
        dialog = open_editor(definition)
        editor(dialog, "v").value.text_layout.text_edit.setPlainText("{{trigger.Term}}")

        dialog.close_button.click()

        assert not dialog.result()
        assert definition[WARNINGS_KEY] == {stage_key("v", "value.text"): [warning]}


# -- The banner, grouped by location -------------------------------------------------------


class TestTheBanner:
    def nested(self) -> dict:
        inner = d.variable("x", d.text("{{trigger.Word}}"), guid="inner", name="Inner value")
        return d.staged(
            "W",
            stages=[
                d.condition(d.code("return True"), then=[inner], guid="outer"),
                d.edit_note(
                    "trigger",
                    fields=[a_write("Meaning", d.text("{{trigger.Word}}"))],
                    guid="e",
                    name="Edit Note",
                ),
            ],
            on_unfocus={"edit_fields": ["Word"], "add_fields": []},
        )

    def test_warnings_are_grouped_under_a_readable_name_for_their_location(self, open_editor):
        first, second = field_warning(), field_warning("Note", None)
        definition = with_warnings(
            self.nested(),
            {
                field_write_key("w1", "value.text"): [first, second],
                trigger_key("on_unfocus.edit_fields"): [field_warning()],
                ORPHAN: [field_warning()],
            },
        )
        dialog = open_editor(definition)

        labels = dialog.marks_banner.group_labels()
        assert list(labels) == [
            field_write_key("w1", "value.text"),
            trigger_key("on_unfocus.edit_fields"),
            ORPHAN,
        ]
        assert "Edit Note → field write &#x27;Meaning&#x27; → value" in labels[
            field_write_key("w1", "value.text")
        ]
        assert "Triggers → unfocus fields (editing a note)" in labels[
            trigger_key("on_unfocus.edit_fields")
        ]
        assert ORPHANED in labels[ORPHAN]
        assert dialog.marks_banner.messages()[:2] == [first["message"], second["message"]]

    def test_the_text_says_an_info_mark_does_not_stop_the_run(self, open_editor):
        definition = with_warnings(
            self.nested(), {stage_key("inner", "value.text"): [field_warning(blocks_run=False)]}
        )
        dialog = open_editor(definition)

        header = dialog.marks_banner.header.text()
        assert header.startswith(WARNING_ICON)
        assert "does not stop it" in header

    def test_an_orphaned_location_is_dismissable(self, open_editor):
        kept = field_warning()
        definition = with_warnings(
            self.nested(),
            {ORPHAN: [field_warning("Note", None)], stage_key("inner", "value.text"): [kept]},
        )
        dialog = open_editor(definition)
        assert ORPHAN in dialog.marks_banner.group_labels()

        from aqt.qt import QPushButton

        dialog.marks_banner.rows[0][1].findChild(QPushButton).click()

        assert ORPHAN not in dialog.marks_banner.group_labels()
        assert save(dialog)[WARNINGS_KEY] == {stage_key("inner", "value.text"): [kept]}

    def test_clicking_a_location_opens_its_stage_and_the_ones_around_it(self, open_editor):
        definition = with_warnings(
            self.nested(), {stage_key("inner", "value.text"): [field_warning()]}
        )
        dialog = open_editor(definition)
        rows = dialog.stage_tree.rows
        assert rows["inner"].body.isHidden() and rows["outer"].body.isHidden()

        _group, label, _rows = dialog.marks_banner.groups[stage_key("inner", "value.text")]
        label.linkActivated.emit("#")

        assert not rows["inner"].body.isHidden()
        assert not rows["outer"].body.isHidden()

    def test_clicking_a_field_write_opens_the_stage_that_holds_it(self, open_editor):
        definition = with_warnings(
            self.nested(), {field_write_key("w1", "value.text"): [field_warning()]}
        )
        dialog = open_editor(definition)

        _group, label, _rows = dialog.marks_banner.groups[field_write_key("w1", "value.text")]
        label.linkActivated.emit("#")

        assert not dialog.stage_tree.rows["e"].body.isHidden()
