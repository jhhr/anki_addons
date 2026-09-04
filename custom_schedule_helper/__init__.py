from typing import Callable

from aqt import mw
from aqt.browser import Browser
from aqt.gui_hooks import (
    deck_browser_will_show_options_menu,
    state_did_change,
    browser_will_show_context_menu,
)
from aqt.qt import QAction, qconnect, QMenu

from .configuration import Config, run_on_configuration_change
from .ease import init_ease_adjust_review_hook
from .ease.auto_ease_factor import adjust_ease
from .ease.fsrs_ops import adjust_fsrs_revlog
from .ease.export import export_ease_factors, import_ease_factors
from .schedule import init_schedule_review_hook
from .schedule.advance import advance
from .schedule.disperse_siblings import disperse_siblings
from .schedule.free_days import free_days
from .schedule.postpone import postpone
from .schedule.reschedule import reschedule
from .sync_hook import init_sync_hook

"""
Acknowledgement to Arthur Milchior, Carlos Duarte and oakkitten.
I learnt a lot from their add-ons.
https://github.com/Arthur-Milchior/Anki-postpone-reviews
https://github.com/cjdduarte/Free_Weekend_Load_Balancer
https://github.com/oakkitten/anki-delay-siblings
https://github.com/hgiesel/anki_straight_reward
"""

config = Config()
config.load()


# A tiny helper for menu items, since type checking is broken there
def checkable(title: str, on_click: Callable[[bool], None]) -> QAction:
    action = QAction(title, mw, checkable=True)  # noqa
    action.triggered.connect(on_click)  # noqa
    return action


def build_action(fun, text, shortcut=None):
    """fun -- without argument
    text -- the text in the menu
    """
    action = QAction(text)
    action.triggered.connect(lambda b, did=None: fun(did))
    if shortcut:
        action.setShortcut(shortcut)
    return action


def add_action_to_gear(fun, get_text):
    """fun -- takes an argument, the did
    text -- what's written in the gear."""

    def aux(m, did):
        config.load()
        # Use function to get so that it occurs after config is loaded
        # in case the text uses a config value
        # This the text gets updated when the config changes
        a = m.addAction(get_text())
        a.triggered.connect(lambda b, did=did: fun(did))

    deck_browser_will_show_options_menu.append(aux)


def add_separator_to_gear():
    """fun -- takes an argument, the did
    text -- what's written in the gear."""

    def aux(m, did):
        a = m.addSeparator()

    deck_browser_will_show_options_menu.append(aux)


def set_auto_reschedule_after_sync(checked):
    config.auto_reschedule_after_sync = checked


menu_auto_reschedule_after_sync = checkable(
    title="Auto reschedule cards reviewed on other devices after sync",
    on_click=set_auto_reschedule_after_sync,
)


def set_auto_disperse_after_sync(checked):
    config.auto_disperse_after_sync = checked


menu_auto_disperse_after_sync = checkable(
    title="Auto disperse siblings reviewed on other devices after sync",
    on_click=set_auto_disperse_after_sync,
)


def set_auto_disperse_when_review(checked):
    config.auto_disperse = checked


menu_auto_disperse = checkable(
    title="Auto disperse siblings when review", on_click=set_auto_disperse_when_review
)


def set_load_balance(checked):
    config.load_balance = checked


menu_load_balance = checkable(title="Load Balance when rescheduling", on_click=set_load_balance)


def reschedule_recent(did):
    reschedule(did, recent=True)


menu_reschedule = build_action(reschedule, "Reschedule all cards")
add_separator_to_gear()
add_action_to_gear(reschedule, lambda: "Reschedule all cards")

menu_reschedule_recent = build_action(
    reschedule_recent,
    f"Reschedule cards reviewed in the last {config.days_to_reschedule} days",
)
add_action_to_gear(
    reschedule_recent,
    lambda: f"Reschedule cards reviewed the last {config.days_to_reschedule} days",
)
menu_postpone = build_action(postpone, "Postpone cards in all decks")
add_action_to_gear(postpone, lambda: "Postpone cards")

menu_advance = build_action(advance, "Advance cards in all decks")
add_action_to_gear(advance, lambda: "Advance cards")

add_separator_to_gear()
add_action_to_gear(export_ease_factors, lambda: "Export ease factors")
add_action_to_gear(import_ease_factors, lambda: "Import ease factors")


def adjust_ease_recent(did):
    adjust_ease(did, recent=True)


menu_adjust_ease = build_action(adjust_ease, "Adjust ease factors in all decks")
menu_adjust_fsrs = build_action(adjust_fsrs_revlog, "Adjust fsrs revlog for all cards (FSRS only)")
add_action_to_gear(adjust_ease, lambda: "Adjust ease factor for all cards")
add_action_to_gear(adjust_fsrs_revlog, lambda: "Adjust fsrs revlog for all cards (FSRS only)")
menu_adjust_ease_recent = build_action(
    adjust_ease_recent,
    f"Adjust ease factors for cards reviewed in the last {config.days_to_reschedule} days",
)
add_action_to_gear(
    adjust_ease_recent,
    lambda: f"Adjust ease factors for cards reviewed in the last {config.days_to_reschedule} days",
)

menu_disperse_siblings = build_action(disperse_siblings, "Disperse all siblings")

menu_for_helper = mw.form.menuTools.addMenu("Custom Schedule Helper")
menu_for_helper.addAction(menu_auto_reschedule_after_sync)
menu_for_helper.addAction(menu_auto_disperse_after_sync)
menu_for_helper.addAction(menu_auto_disperse)
menu_for_helper.addAction(menu_load_balance)
menu_for_free_days = menu_for_helper.addMenu("No Anki on Free Days (requires Load Balancing)")
menu_for_helper.addSeparator()
menu_for_helper.addAction(menu_reschedule)
menu_for_helper.addAction(menu_reschedule_recent)
menu_for_helper.addAction(menu_postpone)
menu_for_helper.addAction(menu_advance)
menu_for_helper.addAction(menu_disperse_siblings)
menu_for_helper.addSeparator()
menu_for_helper.addAction(menu_adjust_ease)
menu_for_helper.addAction(menu_adjust_ease_recent)
menu_for_helper.addAction(menu_adjust_fsrs)

menu_apply_free_days = build_action(free_days, "Apply free days now")


def set_free_days(day, checked):
    config.free_days = (day, checked)


menu_for_free_0 = checkable(title="Free Mon", on_click=lambda x: set_free_days(0, x))
menu_for_free_1 = checkable(title="Free Tue", on_click=lambda x: set_free_days(1, x))
menu_for_free_2 = checkable(title="Free Wed", on_click=lambda x: set_free_days(2, x))
menu_for_free_3 = checkable(title="Free Thu", on_click=lambda x: set_free_days(3, x))
menu_for_free_4 = checkable(title="Free Fri", on_click=lambda x: set_free_days(4, x))
menu_for_free_5 = checkable(title="Free Sat", on_click=lambda x: set_free_days(5, x))
menu_for_free_6 = checkable(title="Free Sun", on_click=lambda x: set_free_days(6, x))
menu_for_free_days.addAction(menu_apply_free_days)
menu_for_free_days.addSeparator()
menu_for_free_days.addAction(menu_for_free_0)
menu_for_free_days.addAction(menu_for_free_1)
menu_for_free_days.addAction(menu_for_free_2)
menu_for_free_days.addAction(menu_for_free_3)
menu_for_free_days.addAction(menu_for_free_4)
menu_for_free_days.addAction(menu_for_free_5)
menu_for_free_days.addAction(menu_for_free_6)


def adjust_menu():
    if mw.col is not None:
        menu_reschedule_recent.setText(
            f"Reschedule cards reviewed in the last {config.days_to_reschedule} days"
        )
        menu_auto_reschedule_after_sync.setChecked(config.auto_reschedule_after_sync)
        menu_auto_disperse_after_sync.setChecked(config.auto_disperse_after_sync)
        menu_auto_disperse.setChecked(config.auto_disperse)
        menu_load_balance.setChecked(config.load_balance)
        menu_for_free_0.setChecked(0 in config.free_days)
        menu_for_free_1.setChecked(1 in config.free_days)
        menu_for_free_2.setChecked(2 in config.free_days)
        menu_for_free_3.setChecked(3 in config.free_days)
        menu_for_free_4.setChecked(4 in config.free_days)
        menu_for_free_5.setChecked(5 in config.free_days)
        menu_for_free_6.setChecked(6 in config.free_days)


def on_browser_will_show_context_menu(browser: Browser, menu: QMenu):
    custom_scheduler_menu = menu.addMenu("Custom Scheduler")
    postpone_action = QAction("Postpone", browser)
    adjust_ease_action = QAction("Adjust Ease", browser)
    adjust_fsrs_action = QAction("Adjust FSRS Revlog", browser)
    qconnect(
        postpone_action.triggered,
        lambda: postpone(card_ids=browser.selectedNotesAsCards(), parent=browser),
    )
    qconnect(
        adjust_ease_action.triggered,
        lambda: adjust_ease(card_ids=browser.selectedNotesAsCards(), parent=browser),
    )
    qconnect(
        adjust_fsrs_action.triggered,
        lambda: adjust_fsrs_revlog(card_ids=browser.selectedNotesAsCards(), parent=browser),
    )
    custom_scheduler_menu.addAction(postpone_action)
    custom_scheduler_menu.addAction(adjust_ease_action)
    custom_scheduler_menu.addAction(adjust_fsrs_action)


browser_will_show_context_menu.append(on_browser_will_show_context_menu)


@state_did_change.append
def state_did_change(_next_state, _previous_state):
    adjust_menu()


@run_on_configuration_change
def configuration_changed():
    config.load()
    adjust_menu()


init_sync_hook()
init_schedule_review_hook()
# init_ease_adjust_review_hook()
