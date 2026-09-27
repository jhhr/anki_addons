"""The Replace button beside a rename indicator and the diff dialog it opens.

Replace is the one place the editor rewrites a search, a piece of code or a template for a
rename, and only after the user has seen the result, so the tests pin what the dialog shows
against what Apply then writes, that Close writes nothing, and that a text box takes the
change through its undo stack. Each kind of part is driven through the real dialog, as in
`test_rename_indicators.py`, whose cases and helpers these reuse.
"""

from __future__ import annotations

import html
from typing import Optional

import pytest
from aqt import mw

import definitions as d
import test_rename_indicators as indicators
from anki_shared.testing import real_anki
from note_types import DEFAULT_CONFIG, VOCAB
from copy_anywhere.configuration import Config
from copy_anywhere.logic.rename_locations import (
    card_action_key,
    field_write_key,
    stage_key,
    trigger_key,
)
from copy_anywhere.logic.rename_reconcile import reconcile
from copy_anywhere.logic.rename_scan import (
    READ_AS_CODE,
    READ_AS_QUERY,
    READ_AS_TEXT,
    READ_AS_TRIGGER_SLOT,
    Rename,
    apply,
    find_at,
)
from copy_anywhere.logic.rename_warnings import WARNINGS_KEY
from copy_anywhere.ui.rename_indicator import RenameIndicator
from copy_anywhere.ui.rename_replace_dialog import (
    NEW,
    OLD,
    SAME,
    diff_html,
    diff_pieces,
    plan_replacement,
)

PARTS = indicators.PARTS
open_editor = indicators.open_editor
editor = indicators.editor
save = indicators.save
shown_indicators = indicators.shown_indicators
with_warnings = indicators.with_warnings
field_warning = indicators.field_warning
deck_warning = indicators.deck_warning
a_write = indicators.a_write
an_action = indicators.an_action

ADDON_TAG = "copy_anywhere"


def note_type_warning(old: str = VOCAB, new: Optional[str] = "CA Words") -> dict:
    return d.rename_warning(
        f'Note type "{old}" was renamed to "{new}"',
        old=old,
        new=new,
        kind="note type",
        note_type_id=None,
    )


def one_warning(dialog) -> RenameIndicator:
    shown = shown_indicators(dialog)
    assert len(shown) == 1
    return shown[0]


def removed_struck_out(pieces) -> str:
    """The text a diff reads as once its struck-through runs are taken out."""
    return "".join(text for kind, text in pieces if kind != OLD)


# -- Every kind of part ---------------------------------------------------------------------


class TestEveryPart:
    """The same chain for each part: the button shows, Apply puts the new name in, the
    indicator hides since the old name is gone, and Save drops the warning with it.

    The whole chain through the Save button, with warnings the pass itself filed, is
    `TestAfterARealRename`."""

    @pytest.mark.parametrize("name", list(PARTS))
    def test_apply_fixes_it_and_save_drops_the_warning(self, open_editor, name):
        part = PARTS[name]
        dialog = open_editor(part.definition())
        indicator = part.indicator(dialog)
        assert indicator is not None
        assert not indicator.replace_button.isHidden()

        indicator.replace_dialog().apply()

        assert indicator.isHidden()
        assert shown_indicators(dialog) == []
        # What Save stores, without Save's other checks: some of these cases name fields
        # the test note type does not have, which the analyser would rightly refuse.
        assert WARNINGS_KEY not in dialog.get_copy_definition()

    @pytest.mark.parametrize("name", list(PARTS))
    def test_close_changes_nothing(self, open_editor, name):
        part = PARTS[name]
        dialog = open_editor(part.definition())
        indicator = part.indicator(dialog)
        assert indicator is not None

        replace = indicator.replace_dialog()
        replace.close_button.click()

        assert not replace.result()
        assert not indicator.isHidden()
        assert dialog.get_copy_definition()[WARNINGS_KEY] == {part.key: [part.warning]}

    def test_the_button_opens_the_dialog_and_its_apply_is_what_changes_the_part(
        self, open_editor, monkeypatch
    ):
        from copy_anywhere.ui.rename_replace_dialog import RenameReplaceDialog

        part = PARTS["query"]
        dialog = open_editor(part.definition())
        indicator = part.indicator(dialog)
        assert indicator is not None
        opened = []
        monkeypatch.setattr(RenameReplaceDialog, "exec", lambda self: opened.append(self) or 0)

        indicator.replace_button.click()

        assert len(opened) == 1
        assert editor(dialog, "q").query.text_layout.get_text() == "deck:Old"
        opened[0].apply_button.click()
        assert editor(dialog, "q").query.text_layout.get_text() == "deck:New"


# -- When the button shows ------------------------------------------------------------------


class TestWhenTheButtonShows:
    def variable(self, code: str) -> dict:
        return d.staged("W", stages=[d.variable("x", d.code(code), guid="v")])

    def open_code(self, open_editor, code: str, *warnings: dict):
        definition = self.variable(code)
        dialog = open_editor(
            with_warnings(definition, {stage_key("v", "value.code"): list(warnings)})
        )
        return dialog, one_warning(dialog)

    def test_not_for_an_f_string_the_name_is_spelled_in(self, open_editor):
        _dialog, indicator = self.open_code(
            open_editor, "return f\"{trigger['Word']}!\"", field_warning()
        )

        assert indicator.replace_button.isHidden()
        assert indicator.replacements == []

    def test_not_for_a_deletion(self, open_editor):
        _dialog, indicator = self.open_code(
            open_editor, "return trigger['Word']", field_warning(new=None)
        )

        assert indicator.replace_button.isHidden()

    def test_when_some_spellings_can_be_replaced_the_rest_are_listed_to_fix_by_hand(
        self, open_editor
    ):
        code = "a = trigger['Word']\nreturn f\"{trigger['Word']}!\" + a"
        dialog, indicator = self.open_code(open_editor, code, field_warning())
        assert not indicator.replace_button.isHidden()

        replace = indicator.replace_dialog()

        assert replace.left_label is not None
        left = html.unescape(replace.left_label.text())
        assert "line 2: “f\"{trigger['Word']}!\"”: this spelling cannot be rewritten" in left
        replace.apply()
        code_layout = editor(dialog, "v").value.code_layout
        assert code_layout is not None
        assert code_layout.get_text() == "a = trigger['Term']\nreturn f\"{trigger['Word']}!\" + a"
        # The f-string still spells it, so the warning stays, and nothing is left to replace.
        assert not indicator.isHidden()
        assert indicator.replace_button.isHidden()

    def test_not_while_the_part_has_no_way_to_take_a_value(self, qapp, widget_parent):
        from copy_anywhere.ui.rename_indicator import LiveLocation
        from copy_anywhere.ui.stage_document import StageDocument

        key = stage_key("q", "query.text")
        definition = with_warnings(
            d.staged("W", stages=[d.note_query("found", "deck:Old", guid="q")]),
            {key: [deck_warning()]},
        )
        indicator = RenameIndicator(
            widget_parent,
            StageDocument(definition),
            lambda: [LiveLocation(key, READ_AS_QUERY, "deck:Old")],
        )

        assert not indicator.isHidden()
        assert indicator.replace_button.isHidden()


# -- What the dialog shows ------------------------------------------------------------------


class TestTheDiff:
    def test_a_search_gets_a_quoted_deck_name(self):
        warning = deck_warning("Old", "Old Vocab")

        replacement = plan_replacement(READ_AS_QUERY, "deck:Old is:new", [warning])

        assert diff_pieces(replacement) == [
            (OLD, "deck:Old"),
            (NEW, '"deck:Old Vocab"'),
            (SAME, " is:new"),
        ]
        assert replacement.after == '"deck:Old Vocab" is:new'

    def test_a_code_literal_keeps_its_quotes(self):
        replacement = plan_replacement(
            READ_AS_CODE, "return trigger['Word'] + \"x\"", [field_warning()]
        )

        assert diff_pieces(replacement) == [
            (SAME, "return trigger["),
            (OLD, "'Word'"),
            (NEW, "'Term'"),
            (SAME, '] + "x"'),
        ]

    def test_a_template_token(self):
        replacement = plan_replacement(
            READ_AS_TEXT, "{{trigger.Word}} ({{trigger.Reading}})", [field_warning()]
        )

        pieces = diff_pieces(replacement)
        assert [kind for kind, _text in pieces] == [OLD, NEW, SAME]
        assert "Word" in pieces[0][1] and "Term" in pieces[1][1]
        assert removed_struck_out(pieces) == "{{trigger.Term}} ({{trigger.Reading}})"

    def test_every_warning_at_the_location_is_applied_in_one_go(self):
        text = f'deck:Old "note:{VOCAB}"'

        replacement = plan_replacement(
            READ_AS_QUERY, text, [deck_warning(), note_type_warning()]
        )

        assert replacement.after == 'deck:New "note:CA Words"'
        assert [kind for kind, _text in diff_pieces(replacement)] == [OLD, NEW, SAME, OLD, NEW]
        assert replacement.left == []

    def test_what_it_shows_is_what_apply_writes_when_two_hits_overlap(self):
        # The same field, renamed differently in two note types: both find one spelling.
        one = field_warning()
        other = dict(field_warning(new="Vocab"), object_id=2, note_type_id=2)
        code = "return trigger['Word']"
        renames = [Rename.from_entry(entry) for entry in (one, other)]
        hits = [
            hit
            for rename in renames
            if rename is not None
            for hit in find_at(READ_AS_CODE, code, rename)
        ]

        replacement = plan_replacement(READ_AS_CODE, code, [one, other])

        assert replacement.after == apply(code, hits) == "return trigger['Term']"
        assert removed_struck_out(diff_pieces(replacement)) == replacement.after
        assert len(replacement.left) == 1 and "overlaps" in replacement.left[0]

    def test_the_users_text_is_escaped_and_the_colours_come_from_the_palette(self, qapp):
        from aqt.qt import QColor, QPalette

        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#123456"))
        replacement = plan_replacement(READ_AS_TEXT, "<b>{{trigger.Word}}</b>", [field_warning()])

        shown = diff_html(replacement, palette)

        assert "&lt;b&gt;" in shown and "<b>" not in shown
        assert "background-color: #123456" in shown
        assert "line-through" in shown and "font-weight: bold" in shown

    def test_a_slot_shows_old_and_new_names(self):
        replacement = plan_replacement(
            READ_AS_TRIGGER_SLOT, ["Note", "Word"], [field_warning(), field_warning("Freq", None)]
        )

        assert replacement.after == ["Note", "Term"]
        assert replacement.swapped == [("Word", "Term")]

    def test_a_list_that_holds_the_new_name_already_does_not_hold_it_twice(self):
        replacement = plan_replacement(READ_AS_TRIGGER_SLOT, ["Word", "Term"], [field_warning()])

        assert replacement.after == ["Term"]


# -- Text parts and undo ---------------------------------------------------------------------


class TestATextPart:
    def open_query(self, open_editor, text: str = "deck:Old is:new"):
        definition = d.staged("W", stages=[d.note_query("found", text, guid="q")])
        return open_editor(
            with_warnings(definition, {stage_key("q", "query.text"): [deck_warning()]})
        )

    def test_the_dialog_shows_the_change_it_will_make(self, open_editor):
        dialog = self.open_query(open_editor)

        replace = one_warning(dialog).replace_dialog()

        from aqt.qt import QTextBrowser

        [view] = replace.views
        assert isinstance(view, QTextBrowser)
        assert view.toPlainText() == "deck:Olddeck:New is:new"

    def test_undo_brings_the_old_text_back_and_the_warning_with_it(self, open_editor):
        dialog = self.open_query(open_editor)
        indicator = one_warning(dialog)
        text_edit = editor(dialog, "q").query.text_layout.text_edit

        indicator.replace_dialog().apply()
        assert text_edit.toPlainText() == "deck:New is:new"
        assert indicator.isHidden()

        text_edit.undo()

        assert text_edit.toPlainText() == "deck:Old is:new"
        assert not indicator.isHidden()

    def test_the_definition_takes_the_new_text(self, open_editor):
        dialog = self.open_query(open_editor)

        one_warning(dialog).replace_dialog().apply()
        dialog.apply_editors()

        assert dialog.document.definition["stages"][0]["query"]["text"] == "deck:New is:new"

    def test_a_card_actions_code_is_undoable_too(self, open_editor):
        part = PARTS["card action code"]
        dialog = open_editor(part.definition())
        indicator = part.indicator(dialog)
        code_edit = editor(dialog, "ec").card_actions.action_ui_components["a1"]["code_editor"]

        indicator.replace_dialog().apply()
        assert code_edit.get_text() == "return {'deck': 'New'}"
        code_edit.text_edit.undo()

        assert code_edit.get_text() == "return {'deck': 'Old'}"


# -- Pickers ----------------------------------------------------------------------------------


class TestAPicker:
    def test_the_dialog_shows_old_and_new_on_one_line(self, open_editor):
        dialog = open_editor(PARTS["sort field"].definition())

        replace = one_warning(dialog).replace_dialog()

        from aqt.qt import QLabel

        [view] = replace.views
        assert isinstance(view, QLabel)
        assert view.text() == "“Word” &rarr; <b>“Term”</b>"

    def test_the_sort_field_selects_the_new_name(self, open_editor):
        dialog = open_editor(PARTS["sort field"].definition())

        one_warning(dialog).replace_dialog().apply()

        assert editor(dialog, "q").sort_field.currentText() == "Term"
        assert save(dialog)["stages"][0]["selection"]["sort_field"] == "Term"

    def test_the_card_actions_deck_selects_the_new_name(self, col, open_editor):
        col.decks.id("New")
        dialog = open_editor(PARTS["card action deck"].definition())
        combo = editor(dialog, "ec").card_actions.action_ui_components["a1"]["deck_combo"]
        count = combo.count()

        one_warning(dialog).replace_dialog().apply()

        assert combo.currentText() == "New"
        # The deck was on offer already, so the list did not grow for it.
        assert combo.count() == count

    def test_an_unfocus_gate_swaps_the_chosen_name_and_keeps_the_rest(self, open_editor):
        dialog = open_editor(PARTS["field write unfocus gate"].definition())

        one_warning(dialog).replace_dialog().apply()

        write = save(dialog)["stages"][0]["fields"][0]
        assert sorted(write["unfocus_trigger_fields"]) == ["Note", "Term"]

    def test_a_trigger_unfocus_list_swaps_the_chosen_name(self, open_editor):
        definition = PARTS["trigger unfocus list"].definition()
        definition["triggers"]["on_unfocus"]["add_fields"] = ["Word", "Meaning"]
        dialog = open_editor(definition)

        one_warning(dialog).replace_dialog().apply()

        unfocus = save(dialog)["triggers"]["on_unfocus"]
        assert sorted(unfocus["add_fields"]) == ["Meaning", "Term"]
        assert unfocus["edit_fields"] == []


class TestAFieldPickerKeepsAWarnedNameNoNoteTypeHas:
    """A field picker lists the fields its note types have, and used to blank any other
    name. A renamed field is exactly such a name, so the part showed no warning and a save
    dropped the field and its warning with it; while a warning is about it, it stays."""

    def sort_definition(self, sort_field: str, *warnings: dict) -> dict:
        definition = d.staged(
            "W",
            stages=[
                d.note_query(
                    "found",
                    "deck:Default",
                    guid="q",
                    selection={"strategy": "all", "count": None, "sort_field": sort_field},
                )
            ],
            note_types=[VOCAB],
        )
        if warnings:
            with_warnings(definition, {stage_key("q", "selection.sort_field"): list(warnings)})
        return definition

    def write_definition(self, field: str, *warnings: dict) -> dict:
        definition = d.staged(
            "W",
            stages=[d.edit_note("trigger", fields=[a_write(field, d.text("x"))], guid="e")],
            note_types=[VOCAB],
        )
        if warnings:
            with_warnings(definition, {field_write_key("w1", "field"): list(warnings)})
        return definition

    def test_an_untouched_sort_field_is_shown_warned_and_saved_as_it_was(self, open_editor):
        warning = field_warning("Headword", "Word", blocks_run=False)
        dialog = open_editor(self.sort_definition("Headword", warning))

        assert editor(dialog, "q").sort_field.currentText() == "Headword"
        assert one_warning(dialog) is editor(dialog, "q").sort_field_indicator
        saved = save(dialog)
        assert saved["stages"][0]["selection"]["sort_field"] == "Headword"
        assert saved[WARNINGS_KEY] == {stage_key("q", "selection.sort_field"): [warning]}

    def test_an_untouched_write_target_is_shown_warned_and_saved_as_it_was(self, open_editor):
        warning = field_warning("Headword", "Word")
        dialog = open_editor(self.write_definition("Headword", warning))

        assert editor(dialog, "e").field_rows[0].field.currentText() == "Headword"
        assert one_warning(dialog) is editor(dialog, "e").field_rows[0].field_indicator
        saved = save(dialog)
        assert saved["stages"][0]["fields"][0]["field"] == "Headword"
        assert saved[WARNINGS_KEY] == {field_write_key("w1", "field"): [warning]}

    def test_replace_selects_the_new_name(self, open_editor):
        dialog = open_editor(self.write_definition("Headword", field_warning("Headword", "Word")))

        one_warning(dialog).replace_dialog().apply()

        assert shown_indicators(dialog) == []
        saved = save(dialog)
        assert saved["stages"][0]["fields"][0]["field"] == "Word"
        assert WARNINGS_KEY not in saved

    def test_the_name_survives_the_pickers_being_relisted(self, open_editor):
        dialog = open_editor(self.sort_definition("Headword", field_warning("Headword", "Word")))

        dialog.refresh_status()

        assert editor(dialog, "q").sort_field.currentText() == "Headword"

    def test_a_name_no_warning_is_about_is_still_blanked(self, open_editor):
        dialog = open_editor(self.sort_definition("Headword"))

        assert editor(dialog, "q").sort_field.currentText() == ""


# -- With the real pass ------------------------------------------------------------------------


@pytest.fixture
def config(col, stub_mw):
    stub_mw.addonManager.configs[ADDON_TAG] = dict(DEFAULT_CONFIG)
    config = Config()
    config.load()
    return config


def renamed(config, definition: dict, rename) -> dict:
    """The definition as the pass leaves it after `rename` changes the collection."""
    config.data["copy_definitions"] = [definition]
    reconcile(config, mw.col)
    rename()
    reconcile(config, mw.col)
    assert WARNINGS_KEY in definition
    return definition


class TestAfterARealRename:
    def test_a_renamed_deck_in_a_search_is_replaced_and_the_warning_goes_on_save(
        self, col, config, open_editor
    ):
        deck_id = col.decks.id("Old")
        note = col.new_note(col.models.by_name(VOCAB))
        note["Word"] = "猫"
        col.add_note(note, deck_id)
        definition = d.staged(
            "W", stages=[d.note_query("found", "deck:Old is:new", guid="q")], note_types=[VOCAB]
        )

        def rename_deck():
            deck = col.decks.get(deck_id)
            deck["name"] = "Old Vocab"
            col.decks.save(deck)

        dialog = open_editor(renamed(config, definition, rename_deck))
        indicator = one_warning(dialog)

        indicator.replace_dialog().apply()

        assert indicator.isHidden()
        assert editor(dialog, "q").query.text_layout.get_text() == '"deck:Old Vocab" is:new'
        saved = save(dialog)
        assert WARNINGS_KEY not in saved
        assert list(col.find_notes(saved["stages"][0]["query"]["text"])) == [note.id]

    def test_the_trigger_field_blocker_still_speaks_after_replacing_in_one_note_type(
        self, col, config, open_editor
    ):
        other = "CA Vocab B"
        real_anki.make_note_type(
            col, other, ["Word", "Meaning"], [("Card 1", "{{Word}}", "{{Meaning}}")]
        )
        definition = d.staged(
            "both",
            note_types=[VOCAB, other],
            stages=[
                d.edit_note("trigger", fields=[d.write("Meaning", d.text("{{trigger.Word}}"))])
            ],
        )

        def rename_field():
            model = col.models.by_name(VOCAB)
            model["flds"][0]["name"] = "Term"
            col.models.update_dict(model)

        dialog = open_editor(renamed(config, definition, rename_field))
        indicator = one_warning(dialog)
        assert not indicator.replace_button.isHidden()

        indicator.replace_dialog().apply()
        dialog.refresh_status()

        assert indicator.isHidden()
        assert not dialog.ok_button.isEnabled()
        assert f'Field "Term" is not on note type "{other}"' in dialog.status_label.text()
