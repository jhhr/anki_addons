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


class MainWindow(QtWidgets.QMainWindow):
    """mw: a plain main window carrying the progress manager the host leans on."""

    def __init__(self) -> None:
        super().__init__()
        self.progress = Progress()


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

    aqt.qt, aqt.theme, aqt.gui_hooks, aqt.utils, aqt.mw = qt, theme, hooks, utils, None
    for name, module in [
        ("aqt", aqt), ("aqt.qt", qt), ("aqt.theme", theme),
        ("aqt.gui_hooks", hooks), ("aqt.utils", utils),
    ]:
        sys.modules[name] = module
    return aqt


def main() -> int:
    aqt = install_fake_aqt()
    app = QtWidgets.QApplication(["harness"])
    mw = MainWindow()
    mw.resize(900, 700)
    aqt.mw = mw
    mw.show()

    from anki_shared.notify.host import ToastHost
    from anki_shared.notify.spec import STICKY, make_spec

    def pump(ms):
        # A local loop, not app.exec() and app.quit(): under Qt 6 quit() closes
        # every top-level window on its way out, so each pump closed the host,
        # whose closeEvent clears the stack, and no step ever saw the window
        # stay up between posts.
        loop = QtCore.QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    host = ToastHost()
    # The contract is called through host; everything Qt is on its window.
    window = host.window

    # -- focus safety: the reviewer must keep the keyboard ------------------
    assert window.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
    assert window.windowFlags() & QtCore.Qt.WindowType.Tool
    assert window.testAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
    # Qt owns a widget's namespace (update, close, show...); the contract keeps out of it.
    assert not isinstance(host, QtWidgets.QWidget)

    # -- stacking and grouping ---------------------------------------------
    h1 = host.post(make_spec(source="Copy Anywhere", title="Copied fields",
                             body="12 notes", level="success", group="sync",
                             key="sync", timeout_ms=STICKY))
    host.post(make_spec(source="Related Card Disperse", title="Dispersed 4",
                        body="deck A: 2", group="sync", timeout_ms=STICKY))
    host.post(make_spec(source="Custom Schedule Helper", title="Rescheduled 30",
                        timeout_ms=STICKY))
    pump(250)
    assert len(window._cards) == 3, window._cards
    assert [h.text() for h in window._headers] == ["SYNC"]

    # Anchored inside the main window's bottom-right corner.
    assert window.geometry().right() <= mw.frameGeometry().right()
    assert window.geometry().bottom() <= mw.frameGeometry().bottom()

    # -- disclosure state is tracked, not read back off the widget ----------
    card = window._cards[h1]
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
    assert len(window._cards) == 3, "keyed repost must not add a card"
    assert "Copied local and remote" in window._cards[h1].summary.text()

    # -- overflow queues ---------------------------------------------------
    for i in range(5):
        host.post(make_spec(source=f"Addon {i}", title=f"m{i}", timeout_ms=STICKY))
    pump(250)
    assert len(window._cards) == window._stack.max_visible
    assert len(window._stack.queued()) == 8 - window._stack.max_visible

    # -- close clears everything, queue included ---------------------------
    window.close()
    assert window._cards == {}
    assert window._stack.ordered() == []
    assert window._headers == []

    # -- the window keeps fitting its cards after it first appears ---------
    # Qt delivers a pending resize while show() runs. Taken for the user's, it
    # switched auto-sizing off the first time the window appeared.
    host.post(make_spec(source="A", title="one", timeout_ms=STICKY))
    pump(150)
    one_card = window.height()
    host.post(make_spec(source="B", title="two", timeout_ms=STICKY))
    host.post(make_spec(source="C", title="three", timeout_ms=STICKY))
    pump(150)
    assert window.height() > one_card, "the window must grow with its cards"

    # A resize through the native window arrives spontaneous, as a user's drag
    # does, and hands the size over to them.
    native = window.windowHandle()
    assert native is not None
    native.resize(window.width(), one_card)
    pump(100)
    host.post(make_spec(source="D", title="four", timeout_ms=STICKY))
    pump(150)
    assert window.height() == one_card, "a size the user chose must stick"
    window.close()

    # -- expiry, and the hover re-arm --------------------------------------
    h = host.post(make_spec(source="A", title="short lived", timeout_ms=200))
    pump(120)
    assert len(window._cards) == 1
    pump(400)
    assert len(window._cards) == 0, "should have expired"

    h = host.post(make_spec(source="A", title="hovered", timeout_ms=200))
    pump(120)
    card = window._cards[h]
    card.underMouse = lambda: True
    pump(600)
    assert len(window._cards) == 1, "a hovered card must not close under the cursor"
    card.underMouse = lambda: False
    pump(600)
    assert len(window._cards) == 0, "and must expire once the cursor leaves"

    # -- pinning suspends expiry -------------------------------------------
    h = host.post(make_spec(source="A", title="pinned", timeout_ms=200))
    pump(120)
    window._cards[h].toggle_pin()
    pump(600)
    assert len(window._cards) == 1, "a pinned card never expires"
    host.dismiss(h)
    pump(200)

    # -- a card reposted while pinned expires again once unpinned ----------
    # The repost re-armed the card with the pinned entry's "never expires",
    # which it kept as its lifetime, so unpinning left it up for good.
    h = host.post(make_spec(source="A", title="first", key="k", timeout_ms=200))
    pump(120)
    card = window._cards[h]
    card.pin_button.click()
    host.post(make_spec(source="A", title="second", key="k", timeout_ms=200))
    pump(150)
    assert h in window._cards, "still pinned, so still up"
    card.pin_button.click()
    pump(600)
    assert h not in window._cards, "unpinned, it must expire again"

    # -- nothing is drawn while a progress dialog is up --------------------
    mw.progress.busy_flag = True
    host.post(make_spec(source="A", title="posted under progress", timeout_ms=STICKY))
    pump(400)
    assert len(window._cards) == 0, "reconcile must wait for the progress dialog"
    assert len(window._stack.ordered()) == 1, "but the model records it immediately"
    mw.progress.busy_flag = False
    pump(400)
    assert len(window._cards) == 1, "and it appears once the dialog is gone"

    # -- theme changes restyle without error -------------------------------
    aqt.theme.theme_manager.night_mode = True
    aqt.gui_hooks.theme_did_change()
    pump(250)

    # -- an action runs, and acting on a notification dismisses it ---------
    window.close()
    fired = []
    h = host.post(make_spec(source="Addon Config Sync", title="Loaded config",
                            timeout_ms=STICKY,
                            actions=[("Open config manager…", lambda: fired.append(1))]))
    pump(250)
    button = window._cards[h].actions_row.itemAt(0).widget()
    assert button.text() == "Open config manager…"
    assert button.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
    button.click()
    pump(250)
    assert fired == [1], "the action callback should have run"
    assert len(window._cards) == 0, "acting on a notification dismisses it"

    # -- controls work when clicked, not just when called ------------------
    # clicked emits a bool; letting it reach the slot turned pin into "unpin".
    h = host.post(make_spec(source="A", title="clicked controls",
                            body="detail", timeout_ms=250))
    pump(150)
    card = window._cards[h]
    assert card.pinned is False
    card.pin_button.click()
    pump(100)
    assert card.pinned is True, "clicking pin must toggle, not set False"
    pump(600)
    assert len(window._cards) == 1, "a card pinned by click must not expire"

    card.disclosure.click()
    pump(100)
    assert card.expanded is True, "clicking the arrow must expand"

    card.close_button.click()
    pump(200)
    assert len(window._cards) == 0, "clicking close must dismiss"

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

    before = len(host_a.window._stack.ordered())
    first.post(source="Copy Anywhere", title="from copy A", timeout_ms=-1)
    second.post(source="Related Card Disperse", title="from copy B", timeout_ms=-1)
    after = len(host_a.window._stack.ordered())
    assert after == before + 2, f"both posts should land in one stack ({before} -> {after})"
    return "two distinct copies, one host, both posts in it"


if __name__ == "__main__":
    sys.exit(main())
