from aqt import mw
from aqt.qt import QAction

from .funcs import readConfigs, saveConfigs, showMissingAddons


def build_action(fun, text, shortcut=None):
    """fun -- without argument
    text -- the text in the menu
    """
    action = QAction(text)
    action.triggered.connect(lambda b: fun())
    if shortcut:
        action.setShortcut(shortcut)
    return action


saveAction = build_action(saveConfigs, "Save Configs")
readAction = build_action(readConfigs, "Read Configs")
missingAction = build_action(showMissingAddons, "Show Missing Addons")

menu_for_helper = mw.form.menuTools.addMenu("Sync Addon Configs")
menu_for_helper.addAction(saveAction)
menu_for_helper.addAction(readAction)
menu_for_helper.addSeparator()
menu_for_helper.addAction(missingAction)
