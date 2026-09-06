"""Drive the Qt half of anki_shared.notify against real PyQt6, headless.

Run by test_notify_host_qt.py in a subprocess, because it installs a fake
`aqt` into sys.modules and the rest of the suite stubs aqt differently -- two
incompatible fakes in one interpreter would depend on import order.

Exits non-zero on the first failed assertion. Everything Anki-specific is
faked here: aqt.qt re-exports PyQt6, mw is a plain QMainWindow, and
mw.progress reproduces the one behaviour the host leans on -- timers that
refuse to fire while a progress dialog is up.
"""

import sys
import types

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCloseEvent


def install_fake_aqt():
    aqt = types.ModuleType("aqt")
    qt = types.ModuleType("aqt.qt")
    for module in (QtCore, QtGui, QtWidgets):
        for name in dir(module):
            if not name.startswith("_"):
                setattr(qt, name, getattr(module, name))
    qt.qconnect = lambda signal, func: signal.connect(func)

    theme = types.ModuleType("aqt.theme")
    theme.theme_manager = type("ThemeManager", (), {"night_mode": False})()

    hooks = types.ModuleType("aqt.gui_hooks")

    class Hook(list):
        def __call__(self):
            for fn in list(self):
                fn()

    hooks.theme_did_change = Hook()

    utils = types.ModuleType("aqt.utils")
    utils.tooltip = lambda *a, **k: None
    utils.closeTooltip = lambda *a, **k: None

    class Progress:
        def __init__(self):
            self.busy_flag = False
            self._keep = []

        def _guard(self, func):
            def fire():
                if self.busy_flag:
                    QTimer.singleShot(50, fire)
                    return
                func()

            return fire

        def timer(self, ms, func, repeat, requiresCollection=True, parent=None):
            t = QTimer(parent)
            t.setSingleShot(not repeat)
            t.timeout.connect(self._guard(func))
            t.start(ms)
            self._keep.append(t)
            return t

        def single_shot(self, ms, func, requires_collection=True):
            QTimer.singleShot(ms, self._guard(func))

        def busy(self):
            return self.busy_flag

    aqt.qt, aqt.theme, aqt.gui_hooks, aqt.utils, aqt.mw = qt, theme, hooks, utils, None
    aqt.Progress = Progress
    for name, module in [
        ("aqt", aqt), ("aqt.qt", qt), ("aqt.theme", theme),
        ("aqt.gui_hooks", hooks), ("aqt.utils", utils),
    ]:
        sys.modules[name] = module
    return aqt


def main() -> int:
    aqt = install_fake_aqt()
    app = QtWidgets.QApplication(["harness"])
    mw = QtWidgets.QMainWindow()
    mw.resize(900, 700)
    mw.progress = aqt.Progress()
    aqt.mw = mw
    mw.show()

    from anki_shared.notify.host import ToastHost
    from anki_shared.notify.spec import STICKY, make_spec

    # The offscreen platform closes any shown Qt.Tool window -- a bare QWidget
    # with the same flags does it too -- so neuter the handler and call the real
    # one directly where close behaviour is what is under test.
    real_close = ToastHost.closeEvent
    ToastHost.closeEvent = lambda self, event: event.accept()

    def pump(ms):
        quit_timer = QTimer()
        quit_timer.setSingleShot(True)
        quit_timer.timeout.connect(app.quit)
        quit_timer.start(ms)
        app.exec()

    host = ToastHost()

    # -- focus safety: the reviewer must keep the keyboard ------------------
    assert host.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
    assert host.windowFlags() & QtCore.Qt.WindowType.Tool
    assert host.testAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)

    # -- stacking and grouping ---------------------------------------------
    h1 = host.post(make_spec(source="Copy Anywhere", title="Copied fields",
                             body="12 notes", level="success", group="sync",
                             key="sync", timeout_ms=STICKY))
    host.post(make_spec(source="Related Card Disperse", title="Dispersed 4",
                        body="deck A: 2", group="sync", timeout_ms=STICKY))
    host.post(make_spec(source="Custom Schedule Helper", title="Rescheduled 30",
                        timeout_ms=STICKY))
    pump(250)
    assert len(host._cards) == 3, host._cards
    assert [h.text() for h in host._headers] == ["SYNC"]

    # Anchored inside the main window's bottom-right corner.
    assert host.geometry().right() <= mw.frameGeometry().right()
    assert host.geometry().bottom() <= mw.frameGeometry().bottom()

    # -- disclosure state is tracked, not read back off the widget ----------
    card = host._cards[h1]
    assert card.expanded is False
    card.toggle_details()
    pump(50)
    assert card.expanded is True
    assert card.details.isVisibleTo(card) is True
    assert card.pinned is True, "expanding should pin"
    card.toggle_details()
    pump(50)
    assert card.expanded is False
    assert card.disclosure.text() == "▸"

    # -- a keyed repost replaces in place ----------------------------------
    host.post(make_spec(source="Copy Anywhere", title="Copied local and remote",
                        body="12 + 3", level="success", group="sync",
                        key="sync", timeout_ms=STICKY))
    pump(250)
    assert len(host._cards) == 3, "keyed repost must not add a card"
    assert "Copied local and remote" in host._cards[h1].summary.text()

    # -- overflow queues ---------------------------------------------------
    for i in range(5):
        host.post(make_spec(source=f"Addon {i}", title=f"m{i}", timeout_ms=STICKY))
    pump(250)
    assert len(host._cards) == host._stack.max_visible
    assert len(host._stack.queued()) == 8 - host._stack.max_visible

    # -- close clears everything, queue included ---------------------------
    real_close(host, QCloseEvent())
    assert host._cards == {}
    assert host._stack.ordered() == []
    assert host._headers == []

    # -- expiry, and the hover re-arm --------------------------------------
    h = host.post(make_spec(source="A", title="short lived", timeout_ms=200))
    pump(120)
    assert len(host._cards) == 1
    pump(400)
    assert len(host._cards) == 0, "should have expired"

    h = host.post(make_spec(source="A", title="hovered", timeout_ms=200))
    pump(120)
    card = host._cards[h]
    card.underMouse = lambda: True
    pump(600)
    assert len(host._cards) == 1, "a hovered card must not close under the cursor"
    card.underMouse = lambda: False
    pump(600)
    assert len(host._cards) == 0, "and must expire once the cursor leaves"

    # -- pinning suspends expiry -------------------------------------------
    h = host.post(make_spec(source="A", title="pinned", timeout_ms=200))
    pump(120)
    host._cards[h].toggle_pin()
    pump(600)
    assert len(host._cards) == 1, "a pinned card never expires"
    host.dismiss(h)
    pump(200)

    # -- nothing is drawn while a progress dialog is up --------------------
    mw.progress.busy_flag = True
    host.post(make_spec(source="A", title="posted under progress", timeout_ms=STICKY))
    pump(400)
    assert len(host._cards) == 0, "reconcile must wait for the progress dialog"
    assert len(host._stack.ordered()) == 1, "but the model records it immediately"
    mw.progress.busy_flag = False
    pump(400)
    assert len(host._cards) == 1, "and it appears once the dialog is gone"

    # -- theme changes restyle without error -------------------------------
    aqt.theme.theme_manager.night_mode = True
    aqt.gui_hooks.theme_did_change()
    pump(250)

    # -- an action runs, and acting on a notification dismisses it ---------
    real_close(host, QCloseEvent())
    fired = []
    h = host.post(make_spec(source="Addon Config Sync", title="Loaded config",
                            timeout_ms=STICKY,
                            actions=[("Open config manager…", lambda: fired.append(1))]))
    pump(250)
    button = host._cards[h].actions_row.itemAt(0).widget()
    assert button.text() == "Open config manager…"
    assert button.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
    button.click()
    pump(250)
    assert fired == [1], "the action callback should have run"
    assert len(host._cards) == 0, "acting on a notification dismisses it"

    # -- controls work when clicked, not just when called ------------------
    # clicked emits a bool; letting it reach the slot turned pin into "unpin".
    h = host.post(make_spec(source="A", title="clicked controls",
                            body="detail", timeout_ms=250))
    pump(150)
    card = host._cards[h]
    assert card.pinned is False
    card.pin_button.click()
    pump(100)
    assert card.pinned is True, "clicking pin must toggle, not set False"
    pump(600)
    assert len(host._cards) == 1, "a card pinned by click must not expire"

    card.disclosure.click()
    pump(100)
    assert card.expanded is True, "clicking the arrow must expand"

    card.close_button.click()
    pump(200)
    assert len(host._cards) == 0, "clicking close must dismiss"

    result = check_cross_addon_election()
    print(f"cross-addon election: {result}")

    print("qt host harness: all assertions passed")
    return 0


def check_cross_addon_election() -> str:
    """Two addons' vendored copies must converge on one window.

    This is the claim the whole package exists to make, and it can only be
    checked against the real vendored layout, which build.py link materialises.
    Skipped rather than failed when those links are absent, since a bare
    checkout has none.
    """
    import importlib
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    addons = ("copy_anywhere", "related_card_disperse")
    for addon in addons:
        if not os.path.isdir(os.path.join(root, addon, "shared", "notify")):
            return f"skipped: {addon}/shared/notify missing (run build.py link)"

    modules = []
    for addon in addons:
        # Register the addon and its shared/ dir as stub packages, exactly as
        # conftest does: their real __init__ touches mw.addonManager.
        for name, path in [
            (addon, os.path.join(root, addon)),
            (f"{addon}.shared", os.path.join(root, addon, "shared")),
        ]:
            stub = types.ModuleType(name)
            stub.__path__ = [path]
            stub.__package__ = name
            sys.modules[name] = stub
        modules.append(importlib.import_module(f"{addon}.shared.notify"))

    first, second = modules
    assert first is not second, "vendoring should produce distinct module objects"

    anchor = sys.modules["aqt"].mw
    host_a = first.registry.host(anchor)
    host_b = second.registry.host(anchor)
    assert host_a is host_b, "each copy elected its own host"

    before = len(host_a._stack.ordered())
    first.post(source="Copy Anywhere", title="from copy A", timeout_ms=-1)
    second.post(source="Related Card Disperse", title="from copy B", timeout_ms=-1)
    after = len(host_a._stack.ordered())
    assert after == before + 2, f"both posts should land in one stack ({before} -> {after})"
    return "two distinct copies, one host, both posts in it"


if __name__ == "__main__":
    sys.exit(main())
