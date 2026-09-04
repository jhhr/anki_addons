from __future__ import annotations

from aqt import mw
from aqt.qt import QAction

from .hooks_browser import init_browser_hook
from .hooks_review import init_review_hook
from .sync_hook import init_sync_hook
from .ui import show_config_dialog


def init_addon() -> None:
    init_review_hook()
    init_sync_hook()
    init_browser_hook()

    configure_action = QAction("Related Card Disperse", mw)
    configure_action.triggered.connect(lambda: show_config_dialog(mw))
    mw.form.menuTools.addAction(configure_action)
