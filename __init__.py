from aqt.gui_hooks import sync_will_start, media_sync_did_start_or_stop

from aqt import mw
from aqt.qt import QAction

from .funcs import (
    read_configs_menu_action,
    save_configs_menu_action,
    save_configs_on_sync,
    read_configs_on_sync,
    showMissingAddons,
)


def build_action(fun, text, shortcut=None):
    """fun -- without argument
    text -- the text in the menu
    """
    action = QAction(text)
    action.triggered.connect(lambda b: fun())
    if shortcut:
        action.setShortcut(shortcut)
    return action


saveAction = build_action(save_configs_menu_action, "Save Configs")
readAction = build_action(read_configs_menu_action, "Read Configs")
missingAction = build_action(showMissingAddons, "Show Missing Addons")

menu_for_helper = mw.form.menuTools.addMenu("Sync Addon Configs")
menu_for_helper.addAction(saveAction)
menu_for_helper.addAction(readAction)
menu_for_helper.addSeparator()
menu_for_helper.addAction(missingAction)


def sync_on_save():
    """Save configs on sync, if the option is enabled."""
    if mw.addonManager.getConfig(__name__).get("run_on_sync", True):
        save_configs_on_sync()


def read_on_sync(media_sync_status: bool):
    """Read configs on sync, if the option is enabled."""
    if mw.addonManager.getConfig(__name__).get("run_on_sync", True):
        read_configs_on_sync(media_sync_status)


# Register hooks for auto-sync
# Since the funcs checks this addon's config when the run, the hooks can always be active and
# editing the config doesn't require restarting Anki.
sync_will_start.append(sync_on_save)
media_sync_did_start_or_stop.append(read_on_sync)
