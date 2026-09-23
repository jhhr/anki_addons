"""The note source two dialogs share: what each mode resolves to, and the toggle pair.

copy_anywhere counts its definitions' notes from `browser_query()` and japanese_note_ai_ops
runs its ops on `note_ids()`, so a wrong answer here is either a wrong count or an op run
over notes the user did not choose.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from anki_shared.ui.note_source_buttons import (
    ACTIVE_BUTTON_STYLE,
    NoteSource,
    NoteSourceButtons,
    browser_note_source,
)


class RecordingFind:
    """A `find_notes` stand-in that records what it was asked, so a test can tell whether the
    search was consulted at all."""

    def __init__(self, result):
        self.result = result
        self.searches: list[str] = []

    def __call__(self, search):
        self.searches.append(search)
        return self.result


class TestNoteSource:
    def test_selection_mode_queries_and_returns_the_selected_ids(self):
        source = NoteSource([3, 1, 2], "deck:x", use_selection=True)
        find = RecordingFind([9])
        assert source.browser_query() == "nid:3,1,2"
        assert source.note_ids(find) == [3, 1, 2]
        assert find.searches == []

    def test_search_mode_queries_and_finds_by_the_search(self):
        source = NoteSource([3, 1], "deck:x", use_selection=False)
        find = RecordingFind((7, 8))
        assert source.browser_query() == "deck:x"
        assert source.note_ids(find) == [7, 8]
        assert find.searches == ["deck:x"]

    def test_the_mode_defaults_to_selection_when_something_is_selected(self):
        assert NoteSource([5], "deck:x").use_selection is True

    def test_nothing_selected_starts_in_search_mode(self):
        source = NoteSource([], "deck:x")
        assert source.use_selection is False
        assert source.browser_query() == "deck:x"
        assert source.note_ids(RecordingFind([4])) == [4]

    def test_forced_selection_mode_with_nothing_selected_never_widens_to_the_search(self):
        # The query keeps copy_anywhere's historical fallback (an empty `nid:` is a syntax
        # error), but the ids an op would run on stay empty.
        source = NoteSource([], "deck:x", use_selection=True)
        find = RecordingFind([4])
        assert source.browser_query() == "deck:x"
        assert source.note_ids(find) == []
        assert find.searches == []

    def test_an_empty_search_is_passed_on_as_it_is(self):
        source = NoteSource([], "")
        find = RecordingFind([1, 2])
        assert source.browser_query() == ""
        assert source.note_ids(find) == [1, 2]
        assert find.searches == [""]

    def test_the_selection_is_a_copy_the_browser_cannot_change_later(self):
        selected = [1, 2]
        source = NoteSource(selected, "")
        selected.append(3)
        assert source.selected_nids == [1, 2]


class TestBrowserNoteSource:
    def test_it_captures_selection_and_search(self):
        browser = SimpleNamespace(
            selected_notes=lambda: (10, 11), current_search=lambda: "tag:a"
        )
        source = browser_note_source(browser)
        assert source.selected_nids == [10, 11]
        assert source.search == "tag:a"
        assert source.use_selection is True

    def test_nothing_selected_means_search_mode(self):
        browser = SimpleNamespace(selected_notes=lambda: [], current_search=lambda: "tag:a")
        assert browser_note_source(browser).use_selection is False

    def test_no_browser_is_an_empty_source_not_an_error(self):
        source = browser_note_source(None)
        assert source.selected_nids == []
        assert source.search == ""
        assert source.use_selection is False


needs_qt = pytest.mark.skipif(
    importlib.util.find_spec("pytestqt") is None, reason="needs pytest-qt and real Qt"
)


@needs_qt
class TestNoteSourceButtons:
    def make(self, qtbot, source):
        changes: list[bool] = []
        widget = NoteSourceButtons(
            source, on_change=lambda: changes.append(source.use_selection)
        )
        qtbot.addWidget(widget)
        return widget, changes

    def test_labels_and_initial_highlight_follow_the_source(self, qtbot):
        widget, changes = self.make(qtbot, NoteSource([1, 2, 3], "deck:x"))
        assert widget.selection_button.text() == "Use selected notes (3)"
        assert widget.search_button.text() == "Use all notes from current search"
        assert widget.selection_button.isEnabled()
        assert widget.selection_button.styleSheet() == ACTIVE_BUTTON_STYLE
        assert widget.search_button.styleSheet() == ""
        assert changes == []

    def test_clicking_toggles_mode_and_highlight_and_calls_back(self, qtbot):
        source = NoteSource([1, 2], "deck:x")
        widget, changes = self.make(qtbot, source)

        widget.search_button.click()
        assert source.use_selection is False
        assert widget.search_button.styleSheet() == ACTIVE_BUTTON_STYLE
        assert widget.selection_button.styleSheet() == ""
        assert changes == [False]

        widget.selection_button.click()
        assert source.use_selection is True
        assert widget.selection_button.styleSheet() == ACTIVE_BUTTON_STYLE
        assert widget.search_button.styleSheet() == ""
        assert changes == [False, True]

    def test_clicking_the_active_button_changes_nothing(self, qtbot):
        widget, changes = self.make(qtbot, NoteSource([1], "deck:x"))
        widget.selection_button.click()
        assert changes == []

    def test_nothing_selected_disables_selection_and_highlights_search(self, qtbot):
        source = NoteSource([], "deck:x", use_selection=True)
        widget, changes = self.make(qtbot, source)
        assert widget.selection_button.text() == "Use selected notes (0)"
        assert not widget.selection_button.isEnabled()
        assert source.use_selection is False
        assert widget.search_button.styleSheet() == ACTIVE_BUTTON_STYLE

        widget.set_use_selection(True)
        assert source.use_selection is False
        assert changes == []
