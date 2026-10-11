"""The one window every addon's entries land in.

The stack is a layout, not a set of independently positioned windows. That is
the whole trick: adding or removing a card reflows the rest automatically, so
there is no y-offset arithmetic to get wrong, nothing to recompute when an
entry is dismissed, and no way for two addons' messages to overlap.

The window is a native Qt.Tool frame parented to mw, so the window manager
handles moving, resizing and stacking, and it is deliberately focus-inert:
Anki sends review keystrokes to the reviewer webview, and a notification that
took focus would eat answers.

Reconciliation runs one way only. Callers mutate the Stack, then the widgets
are rebuilt to match Stack.visible(). There is no change-event protocol, so
the model and the widget tree cannot drift apart.

What other copies call is ToastHost, a plain object holding the window, not
the window itself. As a QWidget the contract shared Qt's namespace:
update(handle, fields) overrode QWidget.update(), the repaint slot, and
close(), show() and hide() are taken as well, so the contract could not grow
without colliding with Qt again.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from aqt.qt import (
    QFrame,
    QLabel,
    QPoint,
    QScrollArea,
    Qt,
    QVBoxLayout,
    QWidget,
)

from .card import ToastCard
from .policy import Stack

WIDTH = 380
MARGIN = 12

# Never take more than this share of the screen height; the rest scrolls.
MAX_HEIGHT_FRACTION = 0.6

# Several addons report within a few ms of each other after a sync. Coalescing
# means one rebuild for the lot rather than one per post.
COALESCE_MS = 60


class ToastHost:
    """post, update and dismiss: what the registry hands out, and nothing else.

    Every copy reaches the elected host by calling these by name, across addon
    versions, so they live where nothing else can claim the names.
    """

    def __init__(self) -> None:
        self.window = ToastWindow()

    def post(self, spec: Dict[str, Any]) -> str:
        return self.window.post_entry(spec)

    def update(self, handle: str, fields: Dict[str, Any]) -> bool:
        return self.window.update_entry(handle, fields)

    def dismiss(self, handle: str) -> bool:
        return self.window.dismiss_entry(handle)


class ToastWindow(QWidget):
    # Anki closes a window that says this as the profile closes. Any other still
    # up is reported as one that "should have been closed" and stays on screen
    # after the main window has gone. Its entries belong to the session that
    # posted them, so closing is the right end for them.
    silentlyClose = True

    def __init__(self) -> None:
        from aqt import mw

        if mw is None:
            raise RuntimeError("no main window to host notifications")

        super().__init__(mw, Qt.WindowType.Tool)
        self.setWindowTitle("Anki notifications")
        # show() must not activate the window, and nothing in it may take the
        # keyboard focus away from the reviewer.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._stack = Stack()
        self._cards: Dict[str, ToastCard] = {}
        self._headers: List[QLabel] = []
        self._reconcile_pending = False
        self._applying_size = False
        self._auto_size = True
        # Where _reposition last left the window's bottom-right corner, and how far
        # the user has moved it from where it would otherwise sit. None until the
        # window has first been placed.
        self._placed: Optional[QPoint] = None
        self._offset = QPoint(0, 0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self._scroll)

        # Kept rather than read back with self._scroll.widget(), which Qt types
        # as Optional although this widget is set once and never replaced.
        self._container = QWidget()
        self._container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._cards_layout = QVBoxLayout(self._container)
        self._cards_layout.setContentsMargins(8, 8, 8, 8)
        self._cards_layout.setSpacing(6)
        self._cards_layout.addStretch(1)
        self._scroll.setWidget(self._container)

        self.resize(WIDTH, 120)

        # Follow the main window around, and go away with it.
        mw.installEventFilter(self)

        from aqt import gui_hooks

        gui_hooks.theme_did_change.append(self._on_theme_did_change)

    # -- entries, as ToastHost hands them on --------------------------------

    def post_entry(self, spec: Dict[str, Any]) -> str:
        handle = self._stack.post(spec, self._now_ms())
        self._stop_countdown(handle)
        self._schedule_reconcile()
        return handle

    def update_entry(self, handle: str, fields: Dict[str, Any]) -> bool:
        changed = self._stack.update(handle, fields)
        if changed:
            self._stop_countdown(handle)
            self._schedule_reconcile()
        return changed

    def _stop_countdown(self, handle: str) -> None:
        """Stop the countdown of a card whose entry has just been replaced.

        The redraw that re-arms it for the new content waits out a progress
        dialog. The old countdown, held back by the same dialog, would otherwise
        get to end the entry first, and the new content with it.
        """
        card = self._cards.get(handle)
        if card is not None:
            card.stop_timer()

    def dismiss_entry(self, handle: str) -> bool:
        removed = self._stack.dismiss(handle)
        if removed:
            self._schedule_reconcile()
        return removed

    # -- reconciliation ----------------------------------------------------

    def _schedule_reconcile(self) -> None:
        """Rebuild soon, and never while a progress dialog is up.

        mw.progress.timer already refuses to fire under a progress window and
        retries shortly after, which is what keeps a post made as an operation
        finishes from being drawn into a dialog that is still tearing down.
        """
        if self._reconcile_pending:
            return
        self._reconcile_pending = True
        from aqt import mw

        mw.progress.timer(
            COALESCE_MS,
            self._run_reconcile,
            repeat=False,
            requiresCollection=False,
            parent=self,
        )

    def _run_reconcile(self) -> None:
        self._reconcile_pending = False
        try:
            self._reconcile()
        except RuntimeError:
            # Widgets torn down under us; the registry retires this host on the
            # next call and a surviving copy takes over.
            pass

    def _reconcile(self) -> None:
        entries = self._stack.visible()
        wanted = {entry["handle"] for entry in entries}

        for handle in list(self._cards):
            if handle not in wanted:
                stale = self._cards.pop(handle)
                stale.stop_timer()
                stale.setParent(None)
                stale.deleteLater()

        for entry in entries:
            handle = entry["handle"]
            card = self._cards.get(handle)
            if card is None:
                card = ToastCard(handle, entry["spec"], self.dismiss_entry, self._set_pinned)
                self._cards[handle] = card
                card.arm(self._stack.timeout_ms(entry))
            elif card.spec is not entry["spec"]:
                # A keyed repost or an update replaced the spec object.
                card.set_spec(entry["spec"])
                card.arm(self._stack.timeout_ms(entry))

        self._relayout(entries)

        if not entries:
            self.hide()
            return
        self._apply_size()
        if not self.isVisible():
            self.show()
        self._reposition()

    def _relayout(self, entries: List[Dict[str, Any]]) -> None:
        """Put the cards back in display order, with a header per named group."""
        while (item := self._cards_layout.takeAt(0)) is not None:
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        for header in self._headers:
            header.deleteLater()
        self._headers = []

        last_group = None
        for entry in entries:
            group_id = entry["group_id"]
            if group_id != last_group:
                name = self._stack.group_name(entry)
                if name:
                    header = self._make_header(name)
                    self._headers.append(header)
                    self._cards_layout.addWidget(header)
                last_group = group_id
            card = self._cards[entry["handle"]]
            card.setParent(self._container)
            card.show()
            self._cards_layout.addWidget(card)
        self._cards_layout.addStretch(1)

    def _make_header(self, name: str) -> QLabel:
        from aqt.theme import theme_manager

        header = QLabel(name.upper())
        header.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        colour = "#9aa5ad" if theme_manager.night_mode else "#6b7680"
        header.setStyleSheet(
            f"color: {colour}; font-size: 10px; font-weight: bold; background: transparent;"
        )
        return header

    def _set_pinned(self, handle: str, pinned: bool) -> None:
        self._stack.set_pinned(handle, pinned)
        self._schedule_reconcile()

    # -- geometry ----------------------------------------------------------

    def _apply_size(self) -> None:
        """Grow to fit the cards, unless the user has taken over the sizing."""
        if not self._auto_size:
            return
        wanted = self._container.sizeHint().height() + 4
        screen = self.screen()
        if screen is not None:
            wanted = min(wanted, int(screen.availableGeometry().height() * MAX_HEIGHT_FRACTION))
        before = self.geometry().bottomRight()
        self._applying_size = True
        try:
            self.resize(self.width(), max(wanted, 60))
        finally:
            self._applying_size = False
        if self._placed is not None:
            # Growing moves the bottom edge, which is not the user moving it.
            self._placed += self.geometry().bottomRight() - before

    def _reposition(self) -> None:
        """Anchor to the bottom-right of the main window, where the user left it.

        A corner that is not where this last left it was moved by someone else:
        the user dragging the window, or the window manager placing it. Put back on
        every post and every move of mw, a window dragged out of the way came
        straight back. The move is kept instead, as an offset from mw's corner, so
        the window still follows mw around.
        """
        from aqt import mw

        if mw is None:
            return
        if self._placed is not None:
            here = self.geometry().bottomRight()
            self._offset += here - self._placed
            self._placed = here
        try:
            corner = mw.mapToGlobal(QPoint(mw.width(), mw.height()))
        except RuntimeError:
            return
        self.move(
            corner.x() - self.width() - MARGIN + self._offset.x(),
            corner.y() - self.height() - MARGIN + self._offset.y(),
        )
        self._placed = self.geometry().bottomRight()

    def resizeEvent(self, event) -> None:
        # The user taking over the size arrives from the window system, which
        # Qt marks spontaneous. Its own resizes are not, and counting them was
        # wrong: the pending one delivered while show() runs switched auto-sizing
        # off the first time the window appeared. _applying_size still covers a
        # platform answering our own resize() synchronously.
        if event.spontaneous() and not self._applying_size:
            self._auto_size = False
        super().resizeEvent(event)

    def eventFilter(self, obj, event) -> bool:
        from aqt.qt import QEvent

        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
            if self.isVisible():
                self._reposition()
        elif event.type() == QEvent.Type.WindowStateChange:
            if obj.isMinimized():
                self.hide()
            elif self._cards:
                self.show()
                self._reposition()
        return False

    # -- lifecycle ---------------------------------------------------------

    def _on_theme_did_change(self) -> None:
        try:
            for card in self._cards.values():
                card.apply_theme()
            self._schedule_reconcile()
        except RuntimeError:
            pass

    def closeEvent(self, event) -> None:
        # Closing the window means "I am done with all of these" -- queued
        # entries included, or it would reopen itself as they promoted.
        for card in self._cards.values():
            card.stop_timer()
        self._stack.clear()
        self._cards.clear()
        self._relayout([])
        super().closeEvent(event)

    @staticmethod
    def _now_ms() -> int:
        import time

        return int(time.time() * 1000)
