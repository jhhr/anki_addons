"""What the editor, the picker and the run say about a name Anki no longer has.

The reconcile pass follows what it can (`test_rename_reconcile.py`) and reports the rest,
but its report goes into a log file the user has to have turned up to read. This is the
other half: the places a user already looks.

A structured reference that resolves to nothing is shown under the name it was written
with, marked, and warned about -- a definition naming a note type this collection does not
have runs on nothing, quietly, and the note type may still be on its way -- but it saves. A
name inside a query or a code block is only ever reported, here as an amber warning under
the query: query text is Anki's grammar, not this addon's, and a mechanical rewrite of one
term inside it would be a guess. The picker marks a definition that names something the
collection cannot resolve, refuses one a rename left broken, and shows a definition saved
from it as saved; a sort field no selected note has says so once per run rather than per
note.
"""

import copy
from typing import Optional

import pytest
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from note_types import CLOZE, DEFAULT_CONFIG, KANJI, ODD_TEMPLATE, VOCAB
from copy_anywhere.configuration import Config
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note
from copy_anywhere.logic.definition_schema import (
    STAGE_EDIT_NOTE,
    STAGE_NOTE_QUERY,
    new_definition,
    value_expression,
)
from copy_anywhere.logic.object_refs import (
    NOT_FOUND_SUFFIX,
    card_type_ref,
    normalize_card_type_ref,
    resolve_card_type,
)
from copy_anywhere.logic.query_terms import stale_search_terms
from copy_anywhere.logic.rename_reconcile import reconcile
from copy_anywhere.logic.rename_warnings import WARNINGS_KEY, blocking_messages, blocking_tooltip
from copy_anywhere.ui.stage_document import StageDocument, default_stage
from copy_anywhere.ui.stage_editor_context import (
    build_contexts,
    make_note_types_for,
    trigger_name_blockers,
    unresolved_reference_warnings,
)
from copy_anywhere.ui.stage_editors import StageEditorEnvironment, make_stage_editor
from copy_anywhere.ui.stage_list import StageTreeWidget
from copy_anywhere.ui.stage_triggers_editor import TriggersEditor, selected_names

ADDON_TAG = "copy_anywhere"

#: An id no collection this suite builds can have, so a reference carrying it resolves by
#: neither half.
GONE_ID = 999999

#: A location no stage of these definitions has, as a warning whose stage was deleted is.
ORPHAN_LOCATION = "definition"


def triggers_editor(col, widget_parent, **triggers):
    """A trigger editor over a definition with these trigger settings, as stored."""
    real_anki.add_note(col, VOCAB, {"Word": "neko"})
    definition = new_definition("d", "A definition")
    definition["triggers"].update(triggers)
    return TriggersEditor(widget_parent, definition), definition


def count_note_type_listings(col, monkeypatch) -> list:
    """One entry per `col.models.all()`, which is what a stale-search scan reads names from."""
    listings: list = []
    list_all = col.models.all

    def counted():
        listings.append(1)
        return list_all()

    monkeypatch.setattr(col.models, "all", counted)
    return listings


def every_note():
    """The picker's note source with no selection and an empty search: every note counts.

    What the picker counted over before the shared note-source buttons, when these tests
    passed no browser at all; the counts here are about note types and decks, not about
    which notes the browser offered.
    """
    from copy_anywhere.shared.ui.note_source_buttons import NoteSource

    return NoteSource([], "", use_selection=False)


def document_for(definition) -> StageDocument:
    """A document wired to the collection the way the dialog wires it."""
    return StageDocument(
        definition,
        unresolved_refs=unresolved_reference_warnings,
        trigger_names=trigger_name_blockers,
    )


def item_texts(box) -> list[str]:
    return [box.itemText(index) for index in range(box.count())]


def choose(box, *names) -> None:
    box.setCurrentText(", ".join(f'"{name}"' for name in names))


def card_actions_editor(col, stage):
    """The card actions editor of an Edit Note stage, with its staged load drained."""
    definition = new_definition("d", "A definition", stages=[stage])
    definition["triggers"]["note_types"] = [d.object_ref(VOCAB, col.models.id_for_name(VOCAB))]
    document = StageDocument(definition)
    environment = StageEditorEnvironment(
        make_note_types_for(document.definition), [definition], "d"
    )
    tree = StageTreeWidget(None, document, environment)
    editor = tree.rows[stage["guid"]].editor.card_actions
    editor.finish_loading_initial_actions()
    return tree, editor


# -- N6: a reference that resolves to nothing ------------------------------------------


class TestATriggerNoteTypeThatResolvesToNothing:
    """It is still shown, marked, and warned about, and the definition saves.

    Dropping it silently would be worse than any of those: the user would reopen a
    definition that has quietly stopped naming anything and see nothing wrong with it.
    Refusing the save would be worse too: at run time the reference just matches no note,
    and the note type may be one the user has not made yet.
    """

    def test_it_is_shown_under_its_stored_name(self, col, qapp, widget_parent):
        editor, _definition = triggers_editor(
            col, widget_parent, note_types=[d.object_ref("CA Ghost", GONE_ID)]
        )

        assert selected_names(editor.note_types_box) == ["CA Ghost" + NOT_FOUND_SUFFIX]

    def test_it_survives_being_opened_and_saved(self, col, qapp, widget_parent):
        editor, definition = triggers_editor(
            col, widget_parent, note_types=[d.object_ref("CA Ghost", GONE_ID)]
        )

        editor.apply()

        assert definition["triggers"]["note_types"] == [{"id": GONE_ID, "name": "CA Ghost"}]

    def test_it_warns_and_does_not_block_the_save(self, col, qapp, widget_parent):
        _editor, definition = triggers_editor(
            col, widget_parent, note_types=[d.object_ref("CA Ghost", GONE_ID)]
        )
        document = document_for(definition)

        assert document.warnings() == [
            "Note type 'CA Ghost' is not in this collection, so nothing triggers this"
            " definition on it until it is."
        ]
        assert document.save_blockers() == []

    def test_choosing_a_live_note_type_clears_it(self, col, qapp, widget_parent):
        editor, definition = triggers_editor(
            col, widget_parent, note_types=[d.object_ref("CA Ghost", GONE_ID)]
        )

        choose(editor.note_types_box, VOCAB)
        editor.apply()

        assert definition["triggers"]["note_types"] == [
            {"id": col.models.id_for_name(VOCAB), "name": VOCAB}
        ]
        assert document_for(definition).warnings() == []

    def test_a_null_id_whose_name_resolves_is_fine(self, col, qapp, widget_parent):
        _editor, definition = triggers_editor(
            col, widget_parent, note_types=[d.object_ref(VOCAB)]
        )

        assert document_for(definition).warnings() == []
        assert document_for(definition).save_blockers() == []

    def test_a_stale_cached_name_is_offered_once_under_the_live_name(
        self, col, qapp, widget_parent
    ):
        stored = d.object_ref("What it used to be called", col.models.id_for_name(VOCAB))
        editor, _definition = triggers_editor(col, widget_parent, note_types=[stored])

        assert selected_names(editor.note_types_box) == [VOCAB]
        assert item_texts(editor.note_types_box).count(f'"{VOCAB}"') == 1
        assert not any(NOT_FOUND_SUFFIX in text for text in item_texts(editor.note_types_box))


class TestATriggerDeckThatResolvesToNothing:
    def test_it_is_shown_under_its_stored_name(self, col, qapp, widget_parent):
        editor, _definition = triggers_editor(
            col,
            widget_parent,
            note_types=[d.object_ref(VOCAB)],
            deck_names=[d.object_ref("Gone deck", GONE_ID)],
        )

        assert selected_names(editor.decks_box) == ["Gone deck" + NOT_FOUND_SUFFIX]

    def test_the_only_deck_warns_that_no_note_passes(self, col, qapp, widget_parent):
        _editor, definition = triggers_editor(
            col,
            widget_parent,
            note_types=[d.object_ref(VOCAB)],
            deck_names=[d.object_ref("Gone deck", GONE_ID)],
        )
        document = document_for(definition)

        # A whitelist that is not empty but resolves to nothing lets no note through
        # (`note_passes_deck_whitelist`), which is worth saying in so many words.
        assert document.warnings() == [
            "Deck 'Gone deck' is not in this collection, and it is the only deck the"
            " whitelist names, so no note passes the whitelist until it is."
        ]
        assert document.save_blockers() == []

    def test_beside_a_live_deck_it_only_drops_out_of_the_whitelist(
        self, col, qapp, widget_parent
    ):
        _editor, definition = triggers_editor(
            col,
            widget_parent,
            note_types=[d.object_ref(VOCAB)],
            deck_names=[
                d.object_ref("Gone deck", GONE_ID),
                d.object_ref("Other", col.decks.id_for_name("Other")),
            ],
        )
        document = document_for(definition)

        assert document.warnings() == [
            "Deck 'Gone deck' is not in this collection, so the deck whitelist matches no"
            " deck for it until it is."
        ]
        assert document.save_blockers() == []

    def test_several_decks_none_of_which_resolve_say_so(self, col, qapp, widget_parent):
        _editor, definition = triggers_editor(
            col,
            widget_parent,
            note_types=[d.object_ref(VOCAB)],
            deck_names=[
                d.object_ref("Gone deck", GONE_ID),
                d.object_ref("Also gone", GONE_ID + 1),
            ],
        )

        assert document_for(definition).warnings() == [
            f"Deck '{name}' is not in this collection, and no other deck the whitelist"
            " names is either, so no note passes the whitelist until one is."
            for name in ("Gone deck", "Also gone")
        ]

    def test_unticking_it_clears_it(self, col, qapp, widget_parent):
        editor, definition = triggers_editor(
            col,
            widget_parent,
            note_types=[d.object_ref(VOCAB)],
            deck_names=[d.object_ref("Gone deck", GONE_ID)],
        )

        choose(editor.decks_box)
        editor.apply()

        assert definition["triggers"]["deck_names"] == []
        assert document_for(definition).warnings() == []


class TestACardTypeThatResolvesToNothing:
    def action_stage(self, col):
        stage = default_stage(STAGE_EDIT_NOTE, "e")
        stage["target"] = {"binding": "trigger"}
        stage["card_actions"] = [
            d.card_action(
                VOCAB,
                "Deleted card type",
                card_type={
                    "note_type_id": col.models.id_for_name(VOCAB),
                    "template_id": GONE_ID,
                    "name": f"{VOCAB}<::>Deleted card type",
                },
                set_flag=1,
            )
        ]
        del stage["card_actions"][0]["card_type_name"]
        return stage

    def test_the_card_actions_editor_shows_it_under_its_stored_name(self, col, qapp):
        _tree, editor = card_actions_editor(col, self.action_stage(col))

        assert list(editor.card_actions) == [
            f"{VOCAB}<::>Deleted card type" + NOT_FOUND_SUFFIX
        ]

    def test_relisting_the_card_types_does_not_drop_it(self, col, qapp):
        _tree, editor = card_actions_editor(col, self.action_stage(col))

        editor.relist_card_types()

        assert list(editor.card_actions) == [
            f"{VOCAB}<::>Deleted card type" + NOT_FOUND_SUFFIX
        ]

    def test_it_warns_and_does_not_block_the_save(self, col, qapp):
        definition = new_definition("d", "A definition", stages=[self.action_stage(col)])
        definition["triggers"]["note_types"] = [d.object_ref(VOCAB)]
        document = document_for(definition)

        assert document.warnings() == [
            f"Card type '{VOCAB}<::>Deleted card type' is not in this collection, so its"
            " card action is skipped until it is."
        ]
        assert document.save_blockers() == []


class TestTheDialogWarnsAndSaves:
    @pytest.fixture
    def dialog(self, col, qapp, widget_parent):
        from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

        definition = new_definition("d", "A definition")
        definition["triggers"]["note_types"] = [d.object_ref("CA Ghost", GONE_ID)]
        definition["triggers"]["deck_names"] = [d.object_ref("Q<b>&A", GONE_ID)]
        dialog = EditStagedDefinitionDialog(widget_parent, definition)
        yield dialog
        dialog._refresh_timer.stop()

    def test_a_definition_naming_a_gone_note_type_is_warned_about(self, dialog):
        assert "Worth knowing" in dialog.status_label.text()
        assert "CA Ghost" in dialog.status_label.text()
        assert "Cannot be saved yet" not in dialog.status_label.text()

    def test_it_saves(self, dialog):
        # Asserted first: a blocked save opens a message box, which would wait for a click.
        assert dialog.ok_button.isEnabled()
        dialog.ok_button.click()

        assert dialog.result()
        assert dialog.get_copy_definition()["triggers"]["note_types"] == [
            {"id": GONE_ID, "name": "CA Ghost"}
        ]

    def test_a_name_is_shown_as_text(self, dialog):
        from aqt.qt import QTextDocument

        # The status is rich text for its colours; a name holding `<` or `&` must not be
        # read as markup. A card type's own name, "Note<::>Card", would lose its `<::>`.
        rendered = QTextDocument()
        rendered.setHtml(dialog.status_label.text())

        assert "Deck 'Q<b>&A' is not in this collection" in rendered.toPlainText()


# -- N7: names inside queries and code --------------------------------------------------


class TestStaleSearchTerms:
    """What a query names that the collection does not have.

    Nothing rewrites these: `col.replace_in_search_node` swaps every term of a kind at
    once, so it cannot rename one deck inside a query naming two, and nothing else in the
    Python API parses a search into nodes. So the scan checks the exact-name case and
    leaves everything Anki's grammar would have to be reimplemented for alone.
    """

    def stale(self, col, query) -> list[tuple[str, str]]:
        return [(term.kind, term.name) for term in stale_search_terms(query, col)]

    def test_a_live_deck_is_not_reported(self, col):
        assert self.stale(col, "deck:Other") == []

    def test_a_deck_that_is_gone_is_reported(self, col):
        assert self.stale(col, "deck:Nowhere") == [("deck", "Nowhere")]

    def test_a_quoted_name_is_read(self, col):
        assert self.stale(col, 'deck:"JP vocab"') == []
        assert self.stale(col, 'deck:"No such deck"') == [("deck", "No such deck")]

    def test_the_whole_term_may_be_quoted(self, col):
        assert self.stale(col, '"deck:No such deck"') == [("deck", "No such deck")]

    def test_a_negated_term_is_read(self, col):
        assert self.stale(col, "-deck:Nowhere") == [("deck", "Nowhere")]

    def test_a_term_inside_parentheses_is_read(self, col):
        assert self.stale(col, "(deck:Other or deck:Nowhere)") == [("deck", "Nowhere")]

    def test_a_note_type_that_is_gone_is_reported(self, col):
        assert self.stale(col, f'note:"{VOCAB}" or note:Ghost') == [("note type", "Ghost")]

    def test_a_card_type_that_is_gone_is_reported(self, col):
        assert self.stale(col, "card:Recognition card:Nope") == [("card type", "Nope")]

    def test_a_card_ordinal_is_not_a_name(self, col):
        assert self.stale(col, "card:1") == []

    def test_a_name_anki_matches_in_any_case_is_not_stale(self, col):
        # Anki's `card:`, `deck:` and `note:` all match without regard to case, so a query
        # spelling a live name in another case works and must not be reported.
        assert self.stale(col, "card:recognition") == []
        assert self.stale(col, "deck:other") == []
        assert self.stale(col, f'note:"{VOCAB.lower()}"') == []

    def test_a_field_that_is_gone_is_reported(self, col):
        assert self.stale(col, "Word:neko Nonsuch:x") == [("field", "Nonsuch")]

    def test_ankis_own_search_keywords_are_not_fields(self, col):
        assert self.stale(col, "tag:x is:due prop:ivl>3 added:1 flag:1 nid:1") == []

    def test_a_term_with_a_wildcard_is_skipped(self, col):
        assert self.stale(col, "deck:Now*re") == []
        assert self.stale(col, "deck:Now_ere") == []

    def test_a_regex_term_is_skipped(self, col):
        assert self.stale(col, "deck:re:nowhere") == []
        assert self.stale(col, "re:nowhere") == []

    def test_a_term_holding_a_reference_is_skipped(self, col):
        assert self.stale(col, "deck:{{trigger.Word}}") == []

    def test_a_bare_word_is_not_a_term(self, col):
        assert self.stale(col, "neko or inu") == []

    def test_an_escaped_colon_is_not_a_field_search(self, col):
        # `foo\:bar` is a plain-text search in Anki: nothing before the escaped colon is a
        # key, so there is no field called `foo` to be missing.
        assert self.stale(col, r"foo\:bar") == []

    def test_a_field_search_splits_at_the_first_unescaped_colon(self, col):
        assert self.stale(col, r"Word:a\:b") == []
        assert self.stale(col, r"Nope:a\:b") == [("field", "Nope")]

class TestTheQueryEditorsWarning:
    def query_editor(self, col, widget_parent, query):
        stage = default_stage(STAGE_NOTE_QUERY, "s")
        stage["result"] = "found"
        stage["query"] = value_expression(text=query)
        definition = new_definition("d", "A definition", stages=[stage])
        definition["triggers"]["note_types"] = [d.object_ref(VOCAB)]
        document = StageDocument(definition)
        environment = StageEditorEnvironment(
            make_note_types_for(document.definition), [definition], "d"
        )
        return make_stage_editor(
            widget_parent, document.stage("s"), build_contexts(document)["s"], environment
        )

    def test_a_stale_deck_name_is_warned_about(self, col, qapp, widget_parent):
        editor = self.query_editor(col, widget_parent, "deck:Nowhere")

        assert "Nowhere" in editor.stale_terms_label.text()
        assert editor.stale_terms_label.isVisibleTo(editor)

    def test_a_query_that_names_only_live_things_says_nothing(
        self, col, qapp, widget_parent
    ):
        editor = self.query_editor(col, widget_parent, "deck:Other")

        assert editor.stale_terms_label.text() == ""

    def test_the_warning_goes_away_when_the_query_is_fixed(self, col, qapp, widget_parent):
        editor = self.query_editor(col, widget_parent, "deck:Nowhere")
        assert editor.stale_terms_label.text() != ""

        editor.query.text_layout.set_text("deck:Other")

        assert editor.stale_terms_label.text() == ""

    def test_a_term_is_shown_as_text(self, col, qapp, widget_parent):
        from aqt.qt import QTextDocument

        editor = self.query_editor(col, widget_parent, 'deck:"a<b>c&d" Word:neko')

        # The label is rich text for its colour; the term is whatever the user typed.
        rendered = QTextDocument()
        rendered.setHtml(editor.stale_terms_label.text())
        assert "deck 'a<b>c&d'" in rendered.toPlainText()

    def test_every_keystroke_reads_the_same_name_lists(
        self, col, qapp, widget_parent, monkeypatch
    ):
        """No note type can change while the editor is open, so one read of them serves."""
        listings = count_note_type_listings(col, monkeypatch)
        editor = self.query_editor(col, widget_parent, "Nonsuch:x card:Nope")
        read_at_start = len(listings)

        for query in ("Nonsuch:x card:Nope2", "Word:neko card:Recall", "Other:y"):
            editor.query.text_layout.set_text(query)

        assert "Other" in editor.stale_terms_label.text()
        assert read_at_start > 0 and len(listings) == read_at_start


# -- N8: the picker and the sort field ---------------------------------------------------


class TestThePickerMarksADefinition:
    """What the definition names that the collection cannot resolve, where the user picks
    a definition to run.

    The pass reports into an operation log nobody reads at the default level, so a
    definition that has stopped naming anything would otherwise look exactly like one that
    works. The row asks the collection itself, as it is drawn: a pass's report is gone by
    the next pass and with the process, and a marker read from it went with it.
    """

    @pytest.fixture
    def picker(self, col, qapp, widget_parent, stub_mw):
        """A stored config and a way to run the pass and build a row after it."""
        from copy_anywhere.ui.pick_copy_definition_dialog import DefinitionRow

        stub_mw.addonManager.configs[ADDON_TAG] = dict(DEFAULT_CONFIG)
        config = Config()
        config.load()

        def run(definition):
            config.data["copy_definitions"] = [definition]
            result = reconcile(config, mw.col)
            return DefinitionRow(widget_parent, definition, 0), result

        return config, run

    def test_a_deck_that_was_deleted_marks_the_row(self, col, picker):
        config, run = picker
        deck_id = col.decks.id_for_name("Other")
        definition = d.staged(
            "Marked", note_types=[VOCAB], deck_names=[d.object_ref("Other", deck_id)]
        )
        run(definition)
        col.decks.remove([deck_id])

        row, result = run(definition)

        # Gone rather than unresolved: the first pass snapshotted the deck, so the second
        # knows it was deleted. The row does not need to know which.
        assert [stale.name for stale in result.gone] == ["Other"]
        assert row.stale_marker.text() != ""
        assert "Other" in row.stale_marker.toolTip()

    def a_deleted_trigger_note_type(self, col, run):
        """A definition on VOCAB and a second note type, the second deleted after a pass."""
        other = real_anki.make_note_type(
            col, "CA Doomed", ["Word"], [("Card 1", "{{Word}}", "{{Word}}")]
        )
        definition = d.staged(
            "Marked",
            note_types=[d.object_ref(VOCAB), d.object_ref("CA Doomed", other["id"])],
        )
        run(definition)
        col.models.remove(other["id"])
        return definition

    def test_a_deleted_trigger_note_type_stays_marked_after_a_later_pass(self, col, picker):
        """Finding 2: the second pass after a deletion reports nothing, since its snapshot
        no longer has the note type; the marker must not go with the report."""
        _config, run = picker
        definition = self.a_deleted_trigger_note_type(col, run)
        _row, first = run(definition)
        assert [stale.name for stale in first.gone] == ["CA Doomed"]

        row, second = run(definition)

        assert second.gone == []
        assert "note type 'CA Doomed'" in row.stale_marker.toolTip()

    def test_a_deleted_trigger_note_type_is_marked_with_no_pass_since(
        self, col, picker, widget_parent
    ):
        """As on the next start of Anki: no pass has run over the deletion yet in this
        process, and no pass result is kept anywhere to consult."""
        from copy_anywhere.ui.pick_copy_definition_dialog import DefinitionRow

        _config, run = picker
        definition = self.a_deleted_trigger_note_type(col, run)

        row = DefinitionRow(widget_parent, definition, 0)

        assert "note type 'CA Doomed'" in row.stale_marker.toolTip()

    def test_a_definition_fixed_since_clears_on_refresh(self, col, picker):
        _config, run = picker
        definition = self.a_deleted_trigger_note_type(col, run)
        row, _result = run(definition)
        assert row.stale_marker.text() != ""
        fixed = copy.deepcopy(definition)
        fixed["triggers"]["note_types"] = [d.object_ref(VOCAB)]

        row.refresh(fixed)

        assert row.stale_marker.text() == "" and row.stale_marker.toolTip() == ""

    def test_a_name_in_the_tooltip_is_shown_as_text(self, col, picker, widget_parent):
        from aqt.qt import Qt
        from copy_anywhere.ui.pick_copy_definition_dialog import DefinitionRow

        _config, _run = picker
        definition = d.staged("Marked", note_types=[d.object_ref("<b>Q&A", GONE_ID)])

        tooltip = DefinitionRow(widget_parent, definition, 0).stale_marker.toolTip()

        # A tooltip is rich text only when Qt guesses so from its first line, which here is
        # fixed; so the name goes in as it is, and an escaped one would show its entities.
        assert "note type '<b>Q&A'" in tooltip
        assert not Qt.mightBeRichText(tooltip)

    def reading(self, value) -> dict:
        """A definition that reads its trigger's fields through this one expression."""
        return d.staged(
            "Marked",
            note_types=[VOCAB],
            stages=[d.edit_note("trigger", fields=[d.write("Meaning", value)])],
        )

    def delete_the_note_field(self, col, run, definition):
        """Delete VOCAB's "Note" field between two passes, so the second sees it deleted."""
        # The first pass is what snapshots the field ids; without one there is no old name
        # to miss.
        run(definition)
        model = col.models.by_name(VOCAB)
        col.models.remove_field(model, model["flds"][-1])
        return run(definition)

    def test_a_field_that_was_deleted_marks_the_row(self, col, picker):
        _config, run = picker
        definition = self.reading(d.text("{{trigger.Note}}"))

        row, result = self.delete_the_note_field(col, run, definition)

        # A mark, stored, rather than a report the next pass would no longer have: the row
        # shows it as broken, and the definition is not run.
        assert [stale.name for stale in result.newly_marked] == ["Note"]
        assert result.gone == []
        assert "Note" in row.broken_marker.toolTip()
        assert not row.checkbox.isEnabled()

    def test_a_field_mentioned_only_in_code_still_marks_the_row(self, col, picker):
        _config, run = picker
        definition = self.reading(d.code("return trigger['Note']"))

        row, _result = self.delete_the_note_field(col, run, definition)

        assert "Note" in row.broken_marker.toolTip()

    def test_a_deleted_field_the_definition_never_spelled_does_not_mark_it(
        self, col, picker
    ):
        _config, run = picker

        # The pass marks only the definitions that spell the deleted field, and this one
        # reads only "Word".
        row, result = self.delete_the_note_field(
            col, run, self.reading(d.text("{{trigger.Word}}"))
        )

        assert result.gone == [] and result.broken == []
        assert row.stale_marker.text() == "" and row.broken_marker.text() == ""

    @pytest.mark.parametrize("reads, marked", [("Recall", True), ("Recognition", False)])
    def test_a_deleted_template_marks_the_rows_that_read_its_card(
        self, col, picker, reads, marked
    ):
        _config, run = picker
        definition = self.reading(d.text(f"{{{{trigger.{reads}__Card_Interval}}}}"))
        run(definition)
        model = col.models.by_name(VOCAB)
        col.models.remove_template(model, model["tmpls"][1])
        col.models.update_dict(model)

        row, result = run(definition)

        assert result.gone == []
        assert ("Recall" in row.broken_marker.toolTip()) is marked

    def test_a_definition_fixed_since_the_last_pass_is_not_marked(
        self, col, picker, widget_parent
    ):
        from copy_anywhere.ui.pick_copy_definition_dialog import DefinitionRow

        _config, run = picker
        definition = d.staged("Marked", note_types=[d.object_ref("Nonsuch", GONE_ID)])
        _row, result = run(definition)
        assert [stale.name for stale in result.unresolved] == ["Nonsuch"]
        # What the editor's save writes: a note type this collection does have. No pass
        # runs over a config write, so only the collection can say it is fixed now.
        model = col.models.by_name(VOCAB)
        definition["triggers"]["note_types"] = [d.object_ref(VOCAB, model["id"])]

        row = DefinitionRow(widget_parent, definition, 0)

        assert row.stale_marker.text() == ""

    def test_a_definition_that_went_stale_since_the_last_pass_is_marked(
        self, col, picker, widget_parent
    ):
        from copy_anywhere.ui.pick_copy_definition_dialog import DefinitionRow

        _config, run = picker
        deck_id = col.decks.id_for_name("Other")
        definition = d.staged(
            "Marked", note_types=[VOCAB], deck_names=[d.object_ref("Other", deck_id)]
        )
        _row, result = run(definition)
        assert result.unresolved == [] and result.gone == []
        # The deck goes away with no pass behind it -- a deck operation is one of the two
        # things that would run one, but an import or a sync is not.
        col.decks.remove([deck_id])

        row = DefinitionRow(widget_parent, definition, 0)

        assert row.stale_marker.text() != ""
        assert "Other" in row.stale_marker.toolTip()

    def test_a_definition_the_pass_was_happy_with_is_not_marked(self, col, picker):
        _config, run = picker

        row, result = run(d.staged("Fine", note_types=[VOCAB]))

        assert result.unresolved == [] and result.gone == []
        assert row.stale_marker.text() == ""

    # -- After a save from the picker ------------------------------------------------------

    def dialog(self, widget_parent, config):
        """The picker over the stored definitions, as `show_copy_dialog` opens it."""
        from copy_anywhere.ui.pick_copy_definition_dialog import PickCopyDefinitionDialog

        return PickCopyDefinitionDialog(widget_parent, list(config.copy_definitions), every_note())

    def save_through(self, monkeypatch, dialog, definition, saved):
        """Edit `definition` in the picker, the editor handing back `saved`, and save it."""
        monkeypatch.setattr(dialog, "run_definition_editor", lambda _definition, _config: saved)
        assert dialog.edit_definition_by_guid(definition["guid"]) == 0

    def test_a_row_fixed_in_the_editor_is_unmarked(
        self, col, picker, widget_parent, monkeypatch
    ):
        config, run = picker
        definition = d.staged("Marked", note_types=[d.object_ref("Nonsuch", GONE_ID)])
        run(definition)
        dialog = self.dialog(widget_parent, config)
        row = dialog.definition_ui_components[definition["guid"]]["widget"]
        assert row.stale_marker.text() != ""
        fixed = copy.deepcopy(definition)
        fixed["triggers"]["note_types"] = [d.object_ref(VOCAB, col.models.id_for_name(VOCAB))]

        self.save_through(monkeypatch, dialog, definition, fixed)

        # The same row, not a rebuilt one, and it holds what the save stored.
        assert dialog.definition_ui_components[definition["guid"]]["widget"] is row
        assert row.definition is dialog.copy_definitions[0]
        assert row.stale_marker.text() == "" and row.stale_marker.toolTip() == ""

    def test_a_deleted_field_taken_out_and_dismissed_in_the_editor_unmarks_the_row(
        self, col, picker, widget_parent, monkeypatch
    ):
        config, run = picker
        definition = self.reading(d.text("{{trigger.Note}}"))
        self.delete_the_note_field(col, run, definition)
        dialog = self.dialog(widget_parent, config)
        row = dialog.definition_ui_components[definition["guid"]]["widget"]
        assert "Note" in row.broken_marker.toolTip()
        fixed = copy.deepcopy(definition)
        fixed["stages"][0]["fields"][0]["value"] = d.text("{{trigger.Word}}")
        # What dismissing the mark in the editor leaves: nothing re-derives it, so the fix
        # alone would keep it.
        del fixed[WARNINGS_KEY]

        self.save_through(monkeypatch, dialog, definition, fixed)

        assert row.broken_marker.text() == "" and row.stale_marker.text() == ""
        assert row.checkbox.isEnabled()

    def test_a_deleted_field_still_spelled_after_a_save_keeps_the_mark(
        self, col, picker, widget_parent, monkeypatch
    ):
        config, run = picker
        definition = self.reading(d.text("{{trigger.Note}}"))
        self.delete_the_note_field(col, run, definition)
        dialog = self.dialog(widget_parent, config)
        row = dialog.definition_ui_components[definition["guid"]]["widget"]
        renamed = copy.deepcopy(definition)
        renamed["definition_name"] = "Renamed"

        self.save_through(monkeypatch, dialog, definition, renamed)

        assert row.checkbox.text() == "Renamed"
        assert "Note" in row.broken_marker.toolTip()

    def test_a_mark_dismissed_in_the_real_editor_and_saved_unmarks_the_row(
        self, col, picker, widget_parent, monkeypatch
    ):
        from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

        config, run = picker
        definition = self.reading(d.code("return trigger['Note']"))
        self.delete_the_note_field(col, run, definition)
        dialog = self.dialog(widget_parent, config)
        row = dialog.definition_ui_components[definition["guid"]]["widget"]
        assert "Note" in row.broken_marker.toolTip()

        def dismiss_and_save(to_edit, _config):
            # What `run_definition_editor` does, with the user's clicks in place of `exec`.
            editor = EditStagedDefinitionDialog(widget_parent, to_edit)
            try:
                dismiss_button(editor.marks_banner, 0).click()
                editor.ok_button.click()
                return editor.get_copy_definition() if editor.result() else None
            finally:
                editor._refresh_timer.stop()

        monkeypatch.setattr(dialog, "run_definition_editor", dismiss_and_save)
        assert dialog.edit_definition_by_guid(definition["guid"]) == 0

        assert WARNINGS_KEY not in dialog.copy_definitions[0]
        assert row.broken_marker.text() == "" and row.checkbox.isEnabled()


def rename_mark(old: str, new: Optional[str], message: Optional[str] = None) -> dict:
    """A mark entry as the pass stores one for a field of VOCAB."""
    done = f'renamed to "{new}"' if new else "deleted"
    return {
        "kind": "field",
        "object_id": 2,
        "note_type_id": 1,
        "old": old,
        "new": new,
        "blocks_run": True,
        "message": message or f'Field "{old}" of note type "{VOCAB}" was {done}',
    }


def dismiss_button(banner, index: int):
    from aqt.qt import QPushButton

    return banner.rows[index][1].findChild(QPushButton)


class TestTheEditorShowsTheMarks:
    """The marks a rename left, at the top of the definition editor, each with Dismiss.

    Nothing re-derives a mark, so the editor is where one leaves (besides undoing the
    rename): the user updates the definition, dismisses the mark to say so, and saves.
    Cancel throws the dismissal away with the rest of the edit.
    """

    @pytest.fixture
    def open_editor(self, col, qapp, widget_parent):
        from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

        opened = []

        def open_editor(definition):
            dialog = EditStagedDefinitionDialog(widget_parent, definition)
            opened.append(dialog)
            return dialog

        yield open_editor
        for dialog in opened:
            dialog._refresh_timer.stop()

    def marked(self, *marks) -> dict:
        definition = d.staged(
            "Marked",
            note_types=[VOCAB],
            stages=[
                d.edit_note("trigger", fields=[d.write("Meaning", d.text("{{trigger.Word}}"))])
            ],
        )
        if marks:
            definition[WARNINGS_KEY] = {ORPHAN_LOCATION: list(marks)}
        return definition

    def save(self, dialog):
        # Asserted first: a blocked save opens a message box, which would wait for a click.
        assert dialog.ok_button.isEnabled()
        dialog.ok_button.click()
        assert dialog.result()
        return dialog.get_copy_definition()

    def test_the_banner_lists_each_marks_message(self, open_editor):
        first = rename_mark("Word", "Term")
        second = rename_mark("Note", None)
        dialog = open_editor(self.marked(first, second))

        banner = dialog.marks_banner
        assert not banner.isHidden()
        assert banner.messages() == [first["message"], second["message"]]
        assert not banner.dismiss_all_button.isHidden()

    def test_dismiss_removes_one_and_save_stores_the_definition_without_it(self, open_editor):
        first = rename_mark("Word", "Term")
        second = rename_mark("Note", None)
        dialog = open_editor(self.marked(first, second))

        dismiss_button(dialog.marks_banner, 0).click()

        assert dialog.marks_banner.messages() == [second["message"]]
        # One left: Dismiss does the same, so Dismiss all is not offered.
        assert dialog.marks_banner.dismiss_all_button.isHidden()
        assert self.save(dialog)[WARNINGS_KEY] == {ORPHAN_LOCATION: [second]}

    def test_two_marks_with_one_message_are_dismissed_one_at_a_time(self, open_editor):
        same = "Field was renamed"
        dialog = open_editor(
            self.marked(
                rename_mark("Word", "Term", message=same), rename_mark("Note", None, message=same)
            )
        )

        dismiss_button(dialog.marks_banner, 1).click()

        saved = self.save(dialog)[WARNINGS_KEY][ORPHAN_LOCATION]
        assert [entry["old"] for entry in saved] == ["Word"]

    def test_dismiss_all_takes_every_mark_off_and_the_banner_away(self, open_editor):
        # An entry with nothing to say is never shown; it goes with the rest.
        dialog = open_editor(
            self.marked(rename_mark("Word", "Term"), {"old": "Note"}, rename_mark("Note", None))
        )

        dialog.marks_banner.dismiss_all_button.click()

        assert dialog.marks_banner.isHidden()
        assert WARNINGS_KEY not in self.save(dialog)

    def test_dismissing_the_last_one_takes_the_banner_away_and_the_key_with_it(
        self, open_editor
    ):
        dialog = open_editor(self.marked(rename_mark("Word", "Term"), {"old": "Note"}))

        dismiss_button(dialog.marks_banner, 0).click()

        assert dialog.marks_banner.isHidden()
        assert WARNINGS_KEY not in self.save(dialog)

    def test_cancel_keeps_the_marks(self, open_editor):
        marks = [rename_mark("Word", "Term"), rename_mark("Note", None)]
        definition = self.marked(*marks)
        dialog = open_editor(definition)

        dialog.marks_banner.dismiss_all_button.click()
        dialog.close_button.click()

        # The dict handed in is the stored one (`run_definition_editor` passes the config's
        # own), and a cancelled editor hands nothing back.
        assert not dialog.result()
        assert definition[WARNINGS_KEY] == {ORPHAN_LOCATION: marks}

    def test_an_unmarked_definition_has_no_banner(self, open_editor):
        dialog = open_editor(self.marked())

        assert dialog.marks_banner.isHidden()
        assert dialog.marks_banner.messages() == []
        assert WARNINGS_KEY not in self.save(dialog)

    def test_dismissing_the_last_of_a_location_takes_only_that_location_away(
        self, open_editor
    ):
        first = rename_mark("Word", "Term")
        second = rename_mark("Note", None)
        definition = self.marked(first)
        definition[WARNINGS_KEY]["some-stage.value.text"] = [second]
        dialog = open_editor(definition)

        assert dialog.marks_banner.messages() == [first["message"], second["message"]]
        dismiss_button(dialog.marks_banner, 1).click()
        assert self.save(dialog)[WARNINGS_KEY] == {ORPHAN_LOCATION: [first]}

    def test_a_message_is_shown_as_text(self, open_editor):
        from aqt.qt import QLabel

        dialog = open_editor(self.marked(rename_mark("<b>Word</b>", "Term")))
        label = dialog.marks_banner.rows[0][1].findChild(QLabel)

        assert "&lt;b&gt;Word&lt;/b&gt;" in label.text()


class ADefinitionBrokenByARename:
    """The `broken` fixture and a picker over it, shared by the picker and menu tests."""

    OTHER = "CA Vocab B"

    @pytest.fixture
    def broken(self, col, stub_mw):
        """A definition triggering on two note types, marked by a rename in one of them."""
        real_anki.make_note_type(
            col, self.OTHER, ["Word", "Meaning"], [("Card 1", "{{Word}}", "{{Meaning}}")]
        )
        stub_mw.addonManager.configs[ADDON_TAG] = dict(DEFAULT_CONFIG)
        config = Config()
        config.load()
        definition = d.staged(
            "both",
            note_types=[VOCAB, self.OTHER],
            stages=[
                d.edit_note("trigger", fields=[d.write("Meaning", d.text("{{trigger.Word}}"))])
            ],
        )
        config.data["copy_definitions"] = [definition]
        reconcile(config, mw.col)
        model = col.models.by_name(VOCAB)
        model["flds"][0]["name"] = "Term"
        col.models.update_dict(model)
        reconcile(config, mw.col)
        assert WARNINGS_KEY in definition
        return config, definition

    def dialog(self, widget_parent, config, *more):
        from copy_anywhere.ui.pick_copy_definition_dialog import PickCopyDefinitionDialog

        config.data["copy_definitions"] += list(more)
        return PickCopyDefinitionDialog(widget_parent, list(config.copy_definitions), every_note())


class TestThePickerRefusesADefinitionBrokenByARename(ADefinitionBrokenByARename):
    """A definition a field rename left marked is shown broken and cannot be selected.

    It would not run anyway (`copy_for_single_trigger_note` refuses it and logs the stored
    message), so the picker says so where the user picks, and editing it whole from there
    gives the checkbox back.
    """

    def test_its_row_shows_the_icon_with_each_message(self, broken, qapp, widget_parent):
        config, definition = broken
        row = self.dialog(widget_parent, config).definition_ui_components["def-both"]["widget"]

        assert row.broken_marker.text() != ""
        for message in blocking_messages(definition):
            assert message in row.broken_marker.toolTip().splitlines()

    def test_its_checkbox_is_disabled_unticked_and_says_why(
        self, broken, qapp, widget_parent
    ):
        config, _definition = broken
        row = self.dialog(widget_parent, config).definition_ui_components["def-both"]["widget"]

        row.checkbox.click()

        assert not row.checkbox.isEnabled()
        assert not row.checkbox.isChecked()
        assert row.checkbox.toolTip() == row.broken_marker.toolTip()
        # Editing is one of the ways out, so the row's buttons stay.
        assert row.edit_button.isEnabled() and row.remove_button.isEnabled()

    def test_it_is_never_counted(self, col, broken, qapp, widget_parent):
        config, _definition = broken
        real_anki.add_note(col, self.OTHER, {"Word": "neko"})
        dialog = self.dialog(widget_parent, config)

        dialog.checkboxes[0].click()
        dialog.update_card_counts_for_all_cards()

        assert dialog.definition_note_ids == [[]]
        assert dialog.selected_definitions_applicable_notes == set()
        assert not dialog.apply_button.isEnabled()

    def test_an_unbroken_row_shows_neither(self, broken, qapp, widget_parent):
        config, _definition = broken
        dialog = self.dialog(widget_parent, config, d.staged("Fine", note_types=[VOCAB]))
        row = dialog.definition_ui_components["def-Fine"]["widget"]

        assert row.broken_marker.text() == "" and row.broken_marker.toolTip() == ""
        assert row.checkbox.isEnabled() and row.checkbox.toolTip() == ""

    def test_saving_it_reworked_clears_the_icon_and_gives_the_checkbox_back(
        self, col, broken, qapp, widget_parent, monkeypatch
    ):
        config, definition = broken
        real_anki.add_note(col, self.OTHER, {"Word": "neko"})
        dialog = self.dialog(widget_parent, config)
        row = dialog.definition_ui_components["def-both"]["widget"]
        # Triggering on the other note type alone, which still has "Word", and the mark
        # dismissed, as the editor leaves it: a save does not re-derive a mark.
        reworked = copy.deepcopy(definition)
        reworked["triggers"]["note_types"] = [d.object_ref(self.OTHER)]
        del reworked[WARNINGS_KEY]
        monkeypatch.setattr(dialog, "run_definition_editor", lambda _d, _c: reworked)

        dialog.edit_definition_by_guid("def-both")

        assert WARNINGS_KEY not in row.definition
        assert row.broken_marker.text() == "" and row.broken_marker.toolTip() == ""
        assert row.checkbox.isEnabled() and not row.checkbox.isChecked()
        assert row.checkbox.toolTip() == ""
        # And it counts again once ticked.
        row.checkbox.click()
        assert row.checkbox.text() == "both (1)"

    def test_saving_it_still_broken_keeps_it_refused(
        self, broken, qapp, widget_parent, monkeypatch
    ):
        config, definition = broken
        dialog = self.dialog(widget_parent, config)
        row = dialog.definition_ui_components["def-both"]["widget"]
        renamed = copy.deepcopy(definition)
        renamed["definition_name"] = "still both"
        monkeypatch.setattr(dialog, "run_definition_editor", lambda _d, _c: renamed)

        dialog.edit_definition_by_guid("def-both")

        assert row.checkbox.text() == "still both"
        assert row.broken_marker.text() != ""
        assert not row.checkbox.isEnabled()


class TestATriggerFieldSomeTriggerNoteTypesLack(ADefinitionBrokenByARename):
    """A definition on several note types can only use a field all of them have.

    It spells a trigger field once for every note type it triggers on, so a field one of
    them lacks makes it fail on that note type's notes. The analyser checks a
    `{{trigger.X}}` against the fields any trigger note type has, so the editor refuses
    the field some of them lack itself -- at every slot a rename is followed into.
    """

    def both(self, col, *stages, **triggers):
        real_anki.make_note_type(
            col, self.OTHER, ["Word", "Meaning", "Extra"], [("Card 1", "{{Word}}", "{{Meaning}}")]
        )
        return d.staged("both", note_types=[VOCAB, self.OTHER], stages=list(stages), **triggers)

    def blockers_for(self, definition):
        return [
            blocker
            for blocker in document_for(definition).save_blockers()
            if blocker.startswith("Field ")
        ]

    def test_a_definition_a_rename_left_broken_is_refused_in_the_editor(self, broken):
        _config, definition = broken

        assert self.blockers_for(definition) == [
            f'Field "Word" is not on note type "{VOCAB}", which this definition also'
            " triggers on; use a field all of them have, or rename it in the others too."
        ]

    def test_trading_the_old_name_for_the_new_one_is_refused_too(self, broken):
        _config, definition = broken
        half_fixed = copy.deepcopy(definition)
        half_fixed["stages"][0]["fields"][0]["value"]["text"] = "{{trigger.Term}}"

        assert self.blockers_for(half_fixed) == [
            f'Field "Term" is not on note type "{self.OTHER}", which this definition also'
            " triggers on; use a field all of them have, or rename it in the others too."
        ]

    def test_renaming_it_in_the_other_note_type_too_is_accepted(self, col, broken):
        _config, definition = broken
        model = col.models.by_name(self.OTHER)
        model["flds"][0]["name"] = "Term"
        col.models.update_dict(model)
        fixed = copy.deepcopy(definition)
        fixed["stages"][0]["fields"][0]["value"]["text"] = "{{trigger.Term}}"

        assert self.blockers_for(fixed) == []

    def test_a_field_write_on_the_trigger_is_checked(self, col):
        definition = self.both(col, d.edit_note("trigger", fields=[d.write("Extra", d.text("x"))]))

        assert self.blockers_for(definition)[0].startswith(
            f'Field "Extra" is not on note type "{VOCAB}"'
        )

    def test_the_unfocus_lists_are_checked(self, col):
        definition = self.both(col, on_unfocus={"edit_fields": ["Extra"], "add_fields": []})

        assert self.blockers_for(definition)[0].startswith(
            f'Field "Extra" is not on note type "{VOCAB}"'
        )

    def test_a_field_every_trigger_note_type_has_is_fine(self, col):
        definition = self.both(
            col,
            d.edit_note("trigger", fields=[d.write("Meaning", d.text("{{trigger.word}}"))]),
            on_unfocus={"edit_fields": ["Word"], "add_fields": []},
        )

        assert self.blockers_for(definition) == []

    def test_a_note_value_key_is_not_a_field(self, col):
        definition = self.both(
            col,
            d.edit_note(
                "trigger", fields=[d.write("Meaning", d.text("{{trigger.__Note_Type_ID}}"))]
            ),
        )

        assert self.blockers_for(definition) == []

    def test_a_field_none_of_them_has_is_left_to_the_analyser(self, col):
        definition = self.both(
            col, d.edit_note("trigger", fields=[d.write("Meaning", d.text("{{trigger.Nonsuch}}"))])
        )

        from copy_anywhere.ui.stage_editor_context import known_fields_for

        assert self.blockers_for(definition) == []
        # Wired as the dialog wires it, with the analyser's field list.
        document = StageDocument(
            definition,
            known_fields=known_fields_for,
            unresolved_refs=unresolved_reference_warnings,
            trigger_names=trigger_name_blockers,
        )
        assert any("Nonsuch" in blocker for blocker in document.save_blockers())

    def card_type_blockers_for(self, definition):
        return [
            blocker
            for blocker in document_for(definition).save_blockers()
            if blocker.startswith("Card type ")
        ]

    def test_a_card_type_some_of_them_lack_is_refused(self, col):
        definition = self.both(
            col,
            d.edit_note(
                "trigger",
                fields=[d.write("Meaning", d.text("{{trigger.Recognition__Card_Due}}"))],
            ),
        )

        assert self.card_type_blockers_for(definition) == [
            f'Card type "Recognition" is not on note type "{self.OTHER}", which this definition'
            " also triggers on; use a card type all of them have, or rename it in the others"
            " too."
        ]

    def test_a_card_type_every_trigger_note_type_has_is_fine(self, col):
        definition = self.both(
            col,
            d.edit_note(
                "trigger", fields=[d.write("Meaning", d.text("{{trigger.Card 1__Card_Due}}"))]
            ),
        )
        model = col.models.by_name(VOCAB)
        model["tmpls"][0]["name"] = "Card 1"
        col.models.update_dict(model)

        assert self.card_type_blockers_for(definition) == []

    def test_a_card_type_none_of_them_has_is_not_refused_here(self, col):
        definition = self.both(
            col,
            d.edit_note(
                "trigger", fields=[d.write("Meaning", d.text("{{trigger.Nonsuch__Card_Due}}"))]
            ),
        )

        assert self.card_type_blockers_for(definition) == []

    def test_one_trigger_note_type_is_not_checked_here(self, col):
        definition = d.staged(
            "one", stages=[d.edit_note("trigger", fields=[d.write("Meaning", d.text("x"))])]
        )
        definition["triggers"]["on_unfocus"] = {"edit_fields": ["Nonsuch"], "add_fields": []}

        assert self.blockers_for(definition) == []


class TestTheBrowserMenuRefusesADefinitionBrokenByARename(ADefinitionBrokenByARename):
    """The browser's "Copy anywhere" menu offers a marked definition as the picker does.

    Listed, so the user still finds it, but disabled, with the picker's tooltip: running it
    would only log the same message.
    """

    def menu_actions(self, stub_mw, config, widget_parent):
        from aqt.qt import QMenu

        from copy_anywhere.hooks.browser_hooks import on_browser_will_show_context_menu

        stub_mw.addonManager.configs[ADDON_TAG] = config.data
        menu = QMenu(widget_parent)
        on_browser_will_show_context_menu(widget_parent, menu)
        copy_menu = next(
            action.menu() for action in menu.actions() if action.text() == "Copy anywhere"
        )
        return copy_menu, {action.text(): action for action in copy_menu.actions()}

    def test_a_marked_definition_is_listed_disabled_and_says_why(
        self, broken, stub_mw, qapp, widget_parent
    ):
        config, definition = broken
        config.data["copy_definitions"].append(d.staged("whole", note_types=[VOCAB]))

        copy_menu, actions = self.menu_actions(stub_mw, config, widget_parent)

        assert not actions["both"].isEnabled()
        assert actions["both"].toolTip() == blocking_tooltip(blocking_messages(definition))
        assert actions["whole"].isEnabled()
        assert copy_menu.toolTipsVisible()

    def test_its_tooltip_is_the_pickers(self, broken, stub_mw, qapp, widget_parent):
        config, _definition = broken
        row = self.dialog(widget_parent, config).definition_ui_components["def-both"]["widget"]

        _copy_menu, actions = self.menu_actions(stub_mw, config, widget_parent)

        assert actions["both"].toolTip() == row.broken_marker.toolTip()


class TestThePickerMarksAStaleSearch:
    """A search naming something the collection lacks gets its own icon in the list.

    The query stage says the same under its search, but only once the definition is
    opened. The definition still runs -- the term just matches nothing -- so the mark is
    neither the refusal nor the unresolved-reference triangle.
    """

    def row(self, widget_parent, definition):
        from copy_anywhere.ui.pick_copy_definition_dialog import DefinitionRow

        return DefinitionRow(widget_parent, definition, 0)

    def searching(self, query, **extra):
        return d.staged(
            "searching",
            note_types=[VOCAB],
            stages=[d.note_query("found", query, **extra)],
        )

    def test_a_deck_the_collection_lacks_marks_the_row(self, col, qapp, widget_parent):
        row = self.row(widget_parent, self.searching('deck:"Gone deck" Word:neko'))

        assert row.search_marker.text() != ""
        lines = row.search_marker.toolTip().splitlines()
        assert "deck 'Gone deck'" in lines
        assert "still runs" in row.search_marker.toolTip()

    def test_it_is_neither_the_refusal_nor_the_triangle(self, col, qapp, widget_parent):
        row = self.row(widget_parent, self.searching("deck:Nonsuch"))

        assert row.checkbox.isEnabled()
        assert row.broken_marker.text() == ""
        assert row.stale_marker.text() == ""
        assert row.search_marker.text() not in ("", row.stale_marker.text())

    def test_a_name_in_the_tooltip_is_shown_as_text(self, col, qapp, widget_parent):
        from aqt.qt import Qt

        row = self.row(widget_parent, self.searching('deck:"<b>Q&A"'))

        # Plain text: Qt guesses rich text from the first line only, which here is fixed.
        assert "deck '<b>Q&A'" in row.search_marker.toolTip().splitlines()
        assert not Qt.mightBeRichText(row.search_marker.toolTip())

    def test_a_field_the_collection_lacks_marks_the_row(self, col, qapp, widget_parent):
        row = self.row(widget_parent, self.searching("Nonsuch:neko"))

        assert "field 'Nonsuch'" in row.search_marker.toolTip().splitlines()

    def test_a_search_naming_only_live_things_is_not_marked(self, col, qapp, widget_parent):
        row = self.row(widget_parent, self.searching('deck:"JP vocab" Word:neko'))

        assert row.search_marker.text() == ""
        assert row.search_marker.toolTip() == ""

    def test_a_condition_search_is_read_too(self, col, qapp, widget_parent):
        definition = d.staged(
            "condition",
            note_types=[VOCAB],
            stages=[
                d.condition(
                    d.text("deck:Nonsuch"),
                    [d.edit_note("trigger", fields=[d.write("Note", d.text("x"))])],
                    predicate_kind="note_query",
                )
            ],
        )

        row = self.row(widget_parent, definition)

        assert "deck 'Nonsuch'" in row.search_marker.toolTip().splitlines()

    def test_every_row_reads_the_same_name_lists(
        self, col, stub_mw, qapp, widget_parent, monkeypatch
    ):
        """The picker holds the Browser's window modal: no note type changes while it is open."""
        from copy_anywhere.ui.pick_copy_definition_dialog import PickCopyDefinitionDialog

        definitions = [self.searching("Nonsuch:neko card:Nope") for _ in range(3)]
        for index, definition in enumerate(definitions):
            definition["guid"] = f"searching-{index}"
        listings = count_note_type_listings(col, monkeypatch)

        dialog = PickCopyDefinitionDialog(widget_parent, definitions, every_note())

        rows = [
            dialog.definition_ui_components[definition["guid"]]["widget"]
            for definition in definitions
        ]
        assert all("field 'Nonsuch'" in row.search_marker.toolTip() for row in rows)
        # One listing for the field names and one for the card type names.
        assert len(listings) == 2

    def test_saving_a_fixed_search_through_the_picker_clears_it(
        self, col, stub_mw, qapp, widget_parent, monkeypatch
    ):
        from copy_anywhere.ui.pick_copy_definition_dialog import PickCopyDefinitionDialog

        stub_mw.addonManager.configs[ADDON_TAG] = dict(DEFAULT_CONFIG)
        config = Config()
        config.load()
        definition = self.searching("deck:Nonsuch")
        config.data["copy_definitions"] = [definition]
        dialog = PickCopyDefinitionDialog(widget_parent, list(config.copy_definitions), every_note())
        row = dialog.definition_ui_components[definition["guid"]]["widget"]
        assert row.search_marker.text() != ""
        fixed = copy.deepcopy(definition)
        fixed["stages"][0]["query"]["text"] = 'deck:"JP vocab"'
        monkeypatch.setattr(dialog, "run_definition_editor", lambda _d, _c: copy.deepcopy(fixed))

        dialog.edit_definition_by_guid(definition["guid"])

        assert dialog.definition_ui_components[definition["guid"]]["widget"] is row
        assert row.search_marker.text() == ""


class TestTheCardActionsEditorBindsACardTypeByTheResolver:
    """The reference an action is saved with is what the run's resolver finds for its name.

    One rule for finding a card type by name (`object_refs.resolve_card_type`), so what the
    editor binds cannot drift from what the pass and the run look up.
    """

    NAMES = [
        f"{VOCAB}<::>Recognition",
        f"{VOCAB.lower()}<::>Recognition",
        f"{VOCAB}<::>recognition",
        f"{VOCAB}<::>Nonsuch",
        "CA Nonsuch<::>Card 1",
        "Recognition",
        f"{CLOZE}<::>Cloze",
        f"{ODD_TEMPLATE}<::>Card__Front",
    ]

    @pytest.mark.parametrize("name", NAMES)
    def test_it_matches_the_resolver(self, col, name):
        from copy_anywhere.ui.card_actions_editor import CardActionsEditor

        model, template = resolve_card_type(normalize_card_type_ref(name), col)
        expected = (
            card_type_ref(model, template)
            if model is not None and template is not None
            else normalize_card_type_ref(name)
        )

        assert CardActionsEditor._card_type_ref_for(None, name, None) == expected

    def test_the_resolved_names_carry_ids(self, col):
        from copy_anywhere.ui.card_actions_editor import CardActionsEditor

        model = col.models.by_name(ODD_TEMPLATE)

        assert CardActionsEditor._card_type_ref_for(
            None, f"{ODD_TEMPLATE}<::>Card__Front", None
        ) == {
            "note_type_id": model["id"],
            "template_id": model["tmpls"][0]["id"],
            "name": f"{ODD_TEMPLATE}<::>Card__Front",
        }

    def test_a_name_that_resolves_to_nothing_keeps_the_actions_reference(self, col):
        from copy_anywhere.ui.card_actions_editor import CardActionsEditor

        stored = {
            "note_type_id": col.models.id_for_name(VOCAB),
            "template_id": GONE_ID,
            "name": f"{VOCAB}<::>Deleted card type",
        }

        assert (
            CardActionsEditor._card_type_ref_for(
                None, f"{VOCAB}<::>Deleted card type", {"card_type": stored}
            )
            == stored
        )


class TestThePickerCountsApplicableNotes:
    """The count beside a row is what the run would work on, not what the stored name finds.

    The run resolves a trigger note type or deck by its id, so a rename it has not caught up
    with changes nothing about which notes it visits. A count built from the stored name
    instead would read '(0)' and the picker would refuse to apply a definition that in fact
    has plenty to do.
    """

    def picker(self, widget_parent, definition):
        """The dialog over this one definition, with its row ticked so the count is made."""
        from copy_anywhere.ui.pick_copy_definition_dialog import PickCopyDefinitionDialog

        dialog = PickCopyDefinitionDialog(widget_parent, [definition], every_note())
        dialog.checkboxes[0].setChecked(True)
        return dialog

    def test_a_renamed_note_type_is_counted_by_its_id(self, col, qapp, widget_parent):
        real_anki.add_note(col, VOCAB, {"Word": "neko"})
        real_anki.add_note(col, VOCAB, {"Word": "inu"})
        model = col.models.by_name(VOCAB)
        definition = d.staged("Counted", note_types=[d.object_ref(VOCAB, model["id"])])
        # Renamed in Anki with the reconcile pass not yet run, so the definition still
        # carries the old name beside the id that is still good.
        model["name"] = "Vocabulary"
        col.models.update_dict(model)

        dialog = self.picker(widget_parent, definition)

        assert dialog.checkboxes[0].text() == "Counted (2)"
        assert len(dialog.selected_definitions_applicable_notes) == 2
        assert dialog.apply_button.isEnabled()

    def test_a_renamed_deck_is_counted_by_its_id(self, col, qapp, widget_parent):
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        deck_id = col.decks.id_for_name("Other")
        definition = d.staged("Counted", deck_names=[d.object_ref("Other", deck_id)])
        col.decks.rename(col.decks.get(deck_id), "Elsewhere")

        dialog = self.picker(widget_parent, definition)

        assert dialog.checkboxes[0].text() == "Counted (1)"

    def test_a_name_that_resolves_to_nothing_still_counts_nothing(
        self, col, qapp, widget_parent
    ):
        real_anki.add_note(col, VOCAB, {"Word": "neko"})
        definition = d.staged("Counted", note_types=[d.object_ref("Nonsuch", GONE_ID)])

        dialog = self.picker(widget_parent, definition)

        assert dialog.checkboxes[0].text() == "Counted (0)"
        assert not dialog.apply_button.isEnabled()


class TestTheSortFieldWarning:
    """Once per run, not once per note.

    `sort_by_field_value` stays silent per note on purpose -- a query legitimately mixes
    note types, and the characterization suites pin the empty-string fallback -- so a sort
    field nobody has is invisible without this.
    """

    def run_with_sort_field(self, col, trigger, sort_field):
        stage = d.note_query(
            "found",
            "tag:pool",
            selection={
                "strategy": "all",
                "count": None,
                "sort_field": sort_field,
                "sort_order": "descending",
            },
        )
        definition = d.staged(stages=[stage])
        return copy_for_single_trigger_note(definition, trigger, copied_into_notes=[])

    @pytest.fixture
    def trigger(self, col):
        real_anki.add_note(col, VOCAB, {"Word": "w1", "Freq": "1"}, tags=["pool"])
        real_anki.add_note(col, KANJI, {"Kanji": "k", "Keyword": "kw"}, tags=["pool"])
        return real_anki.add_note(col, VOCAB, {"Word": "trigger"})

    def test_it_fires_once_when_no_note_has_the_field(self, col, trigger, logger):
        self.run_with_sort_field(col, trigger, "Nonsuch")

        assert [
            message for message in logger.warnings if "Nonsuch" in message
        ] == [
            "Sorting on field 'Nonsuch', which none of the selected notes has:"
            " every note sorted as if it were empty"
        ]

    def test_it_stays_quiet_when_one_note_has_the_field(self, col, trigger, logger):
        self.run_with_sort_field(col, trigger, "Freq")

        assert [message for message in logger.warnings if "Freq" in message] == []
