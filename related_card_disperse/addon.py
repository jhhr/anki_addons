from __future__ import annotations

from aqt import mw
from aqt.qt import QAction

from .hooks_review import init_review_hook
from .sync_hook import init_sync_hook
from .ui import show_config_dialog


def init_addon() -> None:
    init_review_hook()
    init_sync_hook()

    menu = mw.form.menuTools.addMenu("Related Card Disperse")
    configure_action = QAction("Configure rules", mw)
    configure_action.triggered.connect(lambda: show_config_dialog(mw))
    menu.addAction(configure_action)
