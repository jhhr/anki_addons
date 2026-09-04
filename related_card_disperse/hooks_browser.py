from __future__ import annotations

from aqt import mw
from aqt.browser import Browser
from aqt.gui_hooks import browser_will_show_context_menu
from aqt.qt import QAction, QMenu, qconnect
from aqt.utils import tooltip

from .configuration import Config
from .logic import BrowserRunResult, run_browser_disperse_in_background

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
    lines = [_summary(result, notes_mode)]
    # The table's due column is stale now that due dates have moved under it.
    browser.table.reset()
    # Showing the tooltip right as the op finishes gets it closed again by the
    # progress dialog still tearing down, so let that settle first.
    mw.progress.single_shot(
        100,
        lambda: tooltip("<br><br>".join(lines), parent=browser, period=10000),
    )


def run_disperse_on_browser_selection(browser: Browser) -> None:
    """Disperse whatever the browser has selected, notes or cards.

    In notes mode the rows are notes, so the whole note is the unit of work and
    a rule that covers all of a note's cards in one run only runs once. In
    cards mode the selection names individual cards, and each gets its own run.
    """
    config = Config()
    config.load()

    note_ids: list[int] = []
    card_ids: list[int] = []
    notes_mode = browser.table.is_notes_mode()
    if notes_mode:
        note_ids = list(browser.selected_notes())
    else:
        card_ids = list(browser.selected_cards())
    if not note_ids and not card_ids:
        tooltip("No cards selected", parent=browser)
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
