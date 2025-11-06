from aqt.gui_hooks import sync_will_start, media_sync_did_start_or_stop

from aqt import mw
from aqt.qt import QAction
from aqt.utils import showInfo
from .menu_actions import (
    save_configs_menu_action,
    read_configs_menu_action,
    show_missing_addons,
)
from .messages import get_read_configs_message, get_save_configs_message
from .sync_actions import (
    save_configs_on_sync,
    read_configs_on_sync,
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
missingAction = build_action(show_missing_addons, "Show Missing Addons")

menu_for_helper = mw.form.menuTools.addMenu("Sync Addon Configs")
menu_for_helper.addAction(saveAction)
menu_for_helper.addAction(readAction)
menu_for_helper.addSeparator()
menu_for_helper.addAction(missingAction)


sync_saved_addons = []
sync_disabled_addons = []
sync_skipped_addons = []

sync_loaded_addons = []
sync_missing_addons = []


def sync_on_save():
    """Save configs on sync, if the option is enabled."""
    # Reset lists
    sync_saved_addons.clear()
    sync_disabled_addons.clear()
    sync_skipped_addons.clear()

    config = mw.addonManager.getConfig(__name__)

    if config.get("run_on_sync", True):
        save_configs_on_sync(
            sync_saved_addons,
            sync_disabled_addons,
            sync_skipped_addons,
        )


def read_on_sync(media_sync_status: bool):
    """Read configs on sync, if the option is enabled."""
    # Reset lists, we use the same disabled addons list for both save and read
    sync_loaded_addons.clear()
    sync_missing_addons.clear()

    config = mw.addonManager.getConfig(__name__)

    if config.get("run_on_sync", True):

        # Because the media sync hook can run multiple times as it waits for media sync to finish,
        # we only want to show the summary dialog once all sync operations are complete.
        # read_configs_on_sync handles this by accepting a callback function that it will
        # call once it actually finishes reading configs.
        def on_finish_callback():
            if config.get("show_summary_on_sync", True) and (
                sync_loaded_addons
                or sync_saved_addons
                or sync_missing_addons
                or sync_disabled_addons
            ):
                # Show summary dialog
                message = "<b>Auto-Sync Configs Complete!</b><br><br>"
                message += get_save_configs_message(
                    saved_addons=sync_saved_addons,
                    disabled_addons=sync_disabled_addons,
                    # Don't need to track skipped addons on sync save, as the same message would
                    # just get shown every time
                    skipped_addons=[],
                )
                message += get_read_configs_message(
                    loaded_addons=sync_loaded_addons,
                    disabled_addons=sync_disabled_addons,
                    missing_addons=sync_missing_addons,
                )
                showInfo(message, title="Addon Config Sync", textFormat="rich")
            else:
                # log to console when not showing in UI
                print("Auto-Sync configs done, summary lists:")
                print("  Loaded addons:", sync_loaded_addons)
                print("  Disabled addons:", sync_disabled_addons)
                print("  Missing addons:", sync_missing_addons)
                print("  Saved addons:", sync_saved_addons)
                print("  Skipped addons:", sync_skipped_addons)

        read_configs_on_sync(
            sync_loaded_addons,
            sync_disabled_addons,
            sync_missing_addons,
            on_finish_callback,
            media_sync_status,
        )


# Register hooks for auto-sync
# Since the funcs check this addon's config when they run, the hooks can always be active and
# editing the config doesn't require restarting Anki.
sync_will_start.append(sync_on_save)
media_sync_did_start_or_stop.append(read_on_sync)
