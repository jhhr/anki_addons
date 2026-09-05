from __future__ import annotations

from aqt import mw
from aqt.gui_hooks import deck_browser_will_show_options_menu
from aqt.qt import QAction, QMenu, qconnect
from aqt.utils import tooltip

from .bury import BuryRunResult, describe_result, run_deck_bury_disperse_in_background
from .configuration import Config

MENU_LABEL = "Disperse due cards"


def _show_result(result: BuryRunResult) -> None:
    # As in the browser run: a tooltip raised while the progress dialog is still
    # tearing down gets closed along with it.
    mw.progress.single_shot(100, lambda: tooltip(describe_result(result), period=10000))


def run_disperse_on_deck(deck_id: int) -> None:
    """Bury whatever would collide in this deck's session today.

    Meant to be run against the deck you are about to study -- for a backlog,
    that is usually a filtered deck holding the batch you carved out -- and
    again after you rebuild it. Rebuilding leaves the buried cards behind
    (Anki appends ``-is:buried`` to a filtered deck's search), so a rebuilt
    deck is a clean session to disperse afresh.
    """
    config = Config()
    config.load()
    if not config.rules:
        tooltip("No rules configured")
        return
    run_deck_bury_disperse_in_background(deck_id, config, _show_result)


def on_deck_browser_will_show_options_menu(menu: QMenu, deck_id: int) -> None:
    action = QAction(MENU_LABEL, menu)
    qconnect(action.triggered, lambda: run_disperse_on_deck(deck_id))
    menu.addAction(action)


def init_deck_browser_hook() -> None:
    deck_browser_will_show_options_menu.append(on_deck_browser_will_show_options_menu)
