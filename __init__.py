from typing import Callable, Optional
from aqt.gui_hooks import sync_will_start, media_sync_did_start_or_stop

from aqt import mw
from aqt.qt import QAction
from . import sync_actions
from .config_manager_dialog import open_addon_config_manager
from .sync_actions import (
    get_ignored_addons_from_config,
    save_configs_on_sync,
    read_configs_on_sync,
)
from .utils import get_main_config


def build_action(fun: Callable[[], None], text: str, shortcut: Optional[str] = None) -> QAction:
    """fun -- without argument
    text -- the text in the menu
    """
    action = QAction(text)
    action.triggered.connect(lambda b: fun())
    if shortcut:
        action.setShortcut(shortcut)
    return action


manageAction = build_action(
    lambda: open_addon_config_manager(blocking=True),
    "Manage Addon Configs",
)

mw.form.menuTools.addAction(manageAction)


sync_saved_addons = []
sync_disabled_addons = []
sync_skipped_addons = []

sync_loaded_addons = []
sync_missing_addons = []


def sync_on_save() -> None:
    """Save configs on sync, if the option is enabled."""
    # Reset lists
    sync_saved_addons.clear()
    sync_disabled_addons.clear()
    sync_skipped_addons.clear()

    config = get_main_config()
    ignored_addons = get_ignored_addons_from_config(config)

    if sync_actions.SUPPRESS_AUTO_SYNC_ACTIONS:
        return

    if config.get("run_on_sync", True) or config.get("ask_on_sync", False):
        save_configs_on_sync(
            sync_saved_addons,
            sync_disabled_addons,
            sync_skipped_addons,
            ignored_addons=ignored_addons,
        )


def read_on_sync(media_sync_status: bool) -> None:
    """Read configs on sync, if the option is enabled."""
    # Reset lists, we use the same disabled addons list for both save and read
    sync_loaded_addons.clear()
    sync_missing_addons.clear()

    if sync_actions.SUPPRESS_AUTO_SYNC_ACTIONS:
        if media_sync_status is False:
            for callback in sync_actions.SUPPRESS_SYNC_FINISH_CALLBACKS[:]:
                callback()
            sync_actions.SUPPRESS_SYNC_FINISH_CALLBACKS.clear()
        return

    config = get_main_config()
    ignored_addons = get_ignored_addons_from_config(config)
    run_on_sync = bool(config.get("run_on_sync", True))
    show_summary_on_sync = bool(config.get("show_summary_on_sync", False))
    ask_on_sync = bool(config.get("ask_on_sync", False))

    if run_on_sync or ask_on_sync:

        # Because the media sync hook can run multiple times as it waits for media sync to finish,
        # we only want to show the summary dialog once all sync operations are complete.
        # read_configs_on_sync handles this by accepting a callback function that it will
        # call once it actually finishes reading configs.
        def on_finish_callback():
            if ask_on_sync and (sync_loaded_addons or sync_missing_addons):
                open_addon_config_manager(
                    blocking=False,
                    prefilter_changes_or_missing=True,
                )
            elif show_summary_on_sync and sync_loaded_addons:
                open_addon_config_manager(blocking=False)

        read_configs_on_sync(
            sync_loaded_addons,
            sync_disabled_addons,
            sync_missing_addons,
            on_finish_callback,
            media_sync_status,
            ignored_addons=ignored_addons,
            apply_to_addons=run_on_sync and not ask_on_sync,
        )


# Register hooks for auto-sync
# Since the sync actions in sync_actions.py check this addon's config when they run, the hooks
# can always be active and editing the config doesn't require restarting Anki.
sync_will_start.append(sync_on_save)
media_sync_did_start_or_stop.append(read_on_sync)
