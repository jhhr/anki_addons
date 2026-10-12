from __future__ import annotations

from anki.cards import CardId
from anki.notes import NoteId
from aqt.browser import Browser
from aqt.gui_hooks import browser_will_show_context_menu
from aqt.qt import QAction, QMenu, qconnect

from .configuration import Config
from .logic import (
    NOTIFY_SOURCE,
    BrowserRunResult,
    describe_buried_decks,
    run_browser_disperse_in_background,
)
from .shared.notify import post

MENU_LABEL = "Disperse related cards"


def _summary(result: BrowserRunResult, notes_mode: bool) -> str:
    scope = (
        f"{result.notes} note{'' if result.notes == 1 else 's'}"
        if notes_mode
        else f"{result.anchor_cards} card{'' if result.anchor_cards == 1 else 's'}"
    )
    head = "Cancelled after" if result.cancelled else "Dispersed"
    return (
        f"{head} {result.updated} card{'' if result.updated == 1 else 's'}"
        f" over {scope} ({result.rule_runs} rule runs)"
    )


def _show_result(result: BrowserRunResult, browser: Browser, notes_mode: bool) -> None:
    # The table's due column is stale now that due dates have moved under it.
    browser.table.reset()
    # Posted straight away: the notify host holds its redraw until the progress
    # dialog has gone, which a tooltip raised from here had to be delayed for.
    post(
        source=NOTIFY_SOURCE,
        title=_summary(result, notes_mode),
        # A selection says which cards to disperse, never which decks may be
        # touched: the rules decide that, and the buried ones drop out of a
        # session the user did not ask about. Name those decks.
        body=describe_buried_decks(result.buried_by_deck),
        level="info" if result.cancelled else "success",
        timeout_ms=10000,
    )


def run_disperse_on_browser_selection(browser: Browser) -> None:
    """Disperse whatever the browser has selected, notes or cards.

    In notes mode the rows are notes, so the whole note is the unit of work and
    a rule that covers all of a note's cards in one run only runs once. In
    cards mode the selection names individual cards, and each gets its own run.
    """
    config = Config()
    config.load()

    note_ids: list[NoteId] = []
    card_ids: list[CardId] = []
    notes_mode = browser.table.is_notes_mode()
    if notes_mode:
        note_ids = list(browser.selected_notes())
    else:
        card_ids = list(browser.selected_cards())
    if not note_ids and not card_ids:
        post(source=NOTIFY_SOURCE, title="No cards selected", level="warning")
        return

    run_browser_disperse_in_background(
        config,
        lambda result: _show_result(result, browser, notes_mode),
        note_ids=note_ids,
        card_ids=card_ids,
        parent=browser,
    )


def on_browser_will_show_context_menu(browser: Browser, menu: QMenu) -> None:
    action = QAction(MENU_LABEL, browser)
    qconnect(action.triggered, lambda: run_disperse_on_browser_selection(browser))
    menu.addAction(action)


def init_browser_hook() -> None:
    browser_will_show_context_menu.append(on_browser_will_show_context_menu)
