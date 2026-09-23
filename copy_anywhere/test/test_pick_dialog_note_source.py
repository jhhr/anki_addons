"""The picker dialog's note counts under the shared note-source buttons.

The counts are what decides which notes `show_copy_dialog` passes to `copy_fields`: each
checked definition's ids are found by `note type + decks + browser_query()`. A regression
here runs a definition over the whole search when the user picked a selection, or the
reverse, so it is pinned on a real collection.
"""

import importlib.util

import pytest

import definitions as d
from anki_shared.testing import real_anki
from copy_anywhere.shared.ui.note_source_buttons import NoteSource
from copy_anywhere.ui.pick_copy_definition_dialog import PickCopyDefinitionDialog

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pytestqt") is None, reason="needs pytest-qt and real Qt"
)


@pytest.fixture
def notes(col):
    vocab = [real_anki.add_note(col, d.VOCAB, {"Word": w}) for w in ("a", "b", "c")]
    # Anki's stock type, so the definition's note-type filter has something to exclude.
    real_anki.add_note(col, "Basic", {"Front": "s"})
    return vocab


def open_dialog_on(qtbot, definitions, note_source):
    dialog = PickCopyDefinitionDialog(None, definitions, note_source)
    qtbot.addWidget(dialog)
    dialog.checkboxes[0].setChecked(True)
    return dialog


def open_dialog(qtbot, note_source):
    return open_dialog_on(qtbot, [d.within_note(note_types=[d.VOCAB])], note_source)


def test_selection_mode_counts_only_the_selected_notes(qtbot, notes):
    dialog = open_dialog(qtbot, NoteSource([notes[0].id], ""))
    assert list(dialog.definition_note_ids[0]) == [notes[0].id]
    assert dialog.apply_button.isEnabled()


def test_switching_to_the_search_recounts_over_the_search(qtbot, notes):
    dialog = open_dialog(qtbot, NoteSource([notes[0].id], f"nid:{notes[0].id},{notes[1].id}"))
    dialog.note_source_buttons.search_button.click()
    assert sorted(dialog.definition_note_ids[0]) == sorted([notes[0].id, notes[1].id])

    dialog.note_source_buttons.selection_button.click()
    assert list(dialog.definition_note_ids[0]) == [notes[0].id]


def test_nothing_selected_counts_nothing_until_the_search_is_clicked(qtbot, notes):
    # A stray Enter must not apply to the whole search: it has to be picked by a click
    dialog = open_dialog(qtbot, NoteSource([], ""))
    assert dialog.note_source_buttons.selection_button.isEnabled()
    assert list(dialog.definition_note_ids[0]) == []
    assert not dialog.apply_button.isEnabled()

    dialog.note_source_buttons.search_button.click()
    # An empty search box matches the whole collection; the note type narrows it.
    assert sorted(dialog.definition_note_ids[0]) == sorted(n.id for n in notes)
    assert dialog.apply_button.isEnabled()


def test_a_definition_without_note_types_counts_without_a_type_filter(qtbot, notes):
    # The editor saves a definition with no note types; checking it raised UnboundLocalError
    untyped = d.within_note("untyped")
    untyped["copy_into_note_types"] = ""
    dialog = open_dialog_on(qtbot, [untyped], NoteSource([notes[0].id], ""))
    assert list(dialog.definition_note_ids[0]) == [notes[0].id]
    assert dialog.applicable_note_type_names == []
