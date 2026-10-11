from __future__ import annotations

from anki.decks import DeckId
from aqt.gui_hooks import deck_browser_will_show_options_menu
from aqt.qt import QAction, QMenu, qconnect

from .bury import (
    BuryRunResult,
    describe_result,
    result_level,
    run_deck_bury_disperse_in_background,
)
from .configuration import Config
from .shared.notify import post

MENU_LABEL = "Disperse due cards"
SOURCE = "Related Card Disperse"
RESULT_TIMEOUT_MS = 10000


def _result_key(deck_id: DeckId) -> str:
    """One entry per deck.

    A rerun reports on the session as it is now, so it replaces what the last
    run said rather than stacking beside it.
    """
    return f"deck:{deck_id}"


def _show_result(deck_id: DeckId, result: BuryRunResult) -> None:
    # Posted straight away: the notify host holds its redraw until the progress
    # dialog has gone, which a tooltip raised from here had to be delayed for.
    title, body = describe_result(result)
    post(
        source=SOURCE,
        title=title,
        body=body,
        level=result_level(result),
        timeout_ms=RESULT_TIMEOUT_MS,
        key=_result_key(deck_id),
    )


def run_disperse_on_deck(deck_id: DeckId) -> None:
    """Bury whatever would collide in this deck's session today.

    Meant to be run against the deck you are about to study -- for a backlog,
    that is usually a filtered deck holding the batch you carved out -- and
    again after you rebuild it. Rebuilding leaves the buried cards behind
    (Anki appends ``-is:buried`` to a filtered deck's search), so a rebuilt
    deck is a clean session to disperse afresh.

    By default the session is the whole day across every deck, not this deck's
    tree: rules point wherever their queries do, and a one-tree session throws
    away every relation that leaves it. The deck named here still leads, so a
    collision between it and another deck buries the other deck's card.
    """
    config = Config()
    config.load()
    if not config.has_any_rules():
        post(
            source=SOURCE,
            title="No rules configured",
            level="warning",
            key=_result_key(deck_id),
        )
        return
    run_deck_bury_disperse_in_background(
        deck_id,
        config,
        lambda result: _show_result(deck_id, result),
        across_decks=config.disperse_across_decks,
    )


def on_deck_browser_will_show_options_menu(menu: QMenu, deck_id: int) -> None:
    # The hook hands over a plain int; everything below speaks Anki's deck id.
    action = QAction(MENU_LABEL, menu)
    qconnect(action.triggered, lambda: run_disperse_on_deck(DeckId(deck_id)))
    menu.addAction(action)


def init_deck_browser_hook() -> None:
    deck_browser_will_show_options_menu.append(on_deck_browser_will_show_options_menu)
