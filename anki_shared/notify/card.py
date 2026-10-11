"""One entry in the stack: a summary line, optional detail, and its controls.

A card never positions itself. It is a plain widget in the host's layout, so
adding and removing one reflows the rest for free -- which is the reason this
package does not reproduce the y-offset arithmetic that floating-toast
libraries need.

Everything here is focus-inert. Anki routes review keystrokes to the reviewer
webview, and a notification that quietly took focus would swallow answers, so
the card and every control on it are NoFocus and the labels do not take text
interaction.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from aqt.qt import (
    QApplication,
    QColor,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPalette,
    Qt,
    QTimer,
    QToolButton,
    QVBoxLayout,
    QWidget,
    qconnect,
)

# A card is drawn as Anki's own tooltip (aqt.utils.tooltip) with controls, so
# that a notification looks like the thing it replaced rather than a widget of
# its own. In light mode the tooltip sets these on its palette.
TOOLTIP_LIGHT = ("#feffc4", "#000000")  # background; text and frame

# The tooltip's frame, and the padding its table gives the text inside it.
FRAME_WIDTH = 2
PADDING = 14

# Info and success are plain tooltips. A warning or an error must not pass for
# one, so it carries a wider left edge in its own colour, and nothing else.
ACCENTS = {
    False: {"warning": "#b26a00", "error": "#c62828"},
    True: {"warning": "#e8a33d", "error": "#ef6b6b"},
}
ACCENT_WIDTH = 6


def tooltip_colours(night: bool) -> Tuple[str, str]:
    """Background, and text-and-frame colour, as Anki's tooltip has them.

    In night mode the tooltip sets no colours and so takes the app palette's
    window colours, which theme_manager.apply_style() has switched by the time
    theme_did_change fires.
    """
    if not night:
        return TOOLTIP_LIGHT
    palette = QApplication.palette()
    return (
        palette.color(QPalette.ColorRole.Window).name(),
        palette.color(QPalette.ColorRole.WindowText).name(),
    )


def tool_button(text: str, tooltip: str, on_click: Callable[[], object]) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tooltip)
    button.setAutoRaise(True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    # Never let a control here become the keyboard focus.
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    # clicked emits a `checked` bool. Swallow it: every slot here takes no
    # arguments, and letting it through binds False over the first parameter --
    # which silently turned toggle_pin() into "set unpinned".
    qconnect(button.clicked, lambda *_: on_click())
    return button


class ToastCard(QFrame):
    def __init__(
        self,
        handle: str,
        spec: Dict[str, Any],
        on_dismiss: Callable[[str], object],
        on_pin: Callable[[str, bool], None],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.handle = handle
        self.spec = spec
        self._on_dismiss = on_dismiss
        self._on_pin = on_pin
        self._timer: Optional[QTimer] = None
        self._timeout_ms: Optional[int] = None
        # Bumped by every stop_timer(), and checked by a countdown before it acts.
        # Anki retries a timer that fires under a progress dialog from a single_shot
        # of its own, which stopping self._timer does not reach.
        self._countdown = 0
        self.pinned = False
        # Tracked rather than read back off the widget: isVisible() is False
        # for every child while the host window is hidden or minimised, which
        # would desync the disclosure arrow from the actual state.
        self.expanded = False

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        # The controls sit in the right-hand padding, so it is the one side kept
        # narrower than the tooltip's.
        outer.setContentsMargins(PADDING, PADDING - 2, 6, PADDING - 2)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(4)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.summary.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header.addWidget(self.summary, 1)

        self.disclosure = tool_button("▸", "Show details", self.toggle_details)
        self.pin_button = tool_button("○", "Keep this open", self.toggle_pin)
        self.close_button = tool_button("✕", "Dismiss", lambda: self._on_dismiss(self.handle))
        for button in (self.disclosure, self.pin_button, self.close_button):
            header.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.details.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.details.hide()
        outer.addWidget(self.details)

        self.actions_row = QHBoxLayout()
        self.actions_row.setSpacing(6)
        outer.addLayout(self.actions_row)

        self.set_spec(spec)

    # -- content -----------------------------------------------------------

    def set_spec(self, spec: Dict[str, Any]) -> None:
        self.spec = spec
        source = spec.get("source", "")
        title = spec.get("title", "")
        self.summary.setText(f"<b>{source}</b><br>{title}" if source else title)

        body = spec.get("body", "")
        self.details.setText(body)
        self.disclosure.setVisible(bool(body))
        if not body:
            self.expanded = False
        self._apply_disclosure()

        self._rebuild_actions(spec.get("actions", ()))
        self.apply_theme()

    def _rebuild_actions(self, actions) -> None:
        while (item := self.actions_row.takeAt(0)) is not None:
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for label, callback in actions:
            self.actions_row.addWidget(tool_button(label, label, self._runner(callback)))
        self.actions_row.addStretch(1)

    def _runner(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run an action, then take the card down: acting on a notification
        implies you are done reading it. A closure factory rather than a
        default argument, so nothing can be bound over the callback."""

        def run() -> None:
            callback()
            self._on_dismiss(self.handle)

        return run

    def apply_theme(self) -> None:
        from aqt.theme import theme_manager

        night = bool(theme_manager.night_mode)
        background, foreground = tooltip_colours(night)
        accent = ACCENTS[night].get(self.spec.get("level", "info"))
        edge = f" border-left: {ACCENT_WIDTH}px solid {accent};" if accent else ""
        # The controls are quieter than the message, which is what a tooltip is.
        quiet = QColor(foreground)
        quiet.setAlpha(150)
        quiet_css = f"rgba({quiet.red()}, {quiet.green()}, {quiet.blue()}, {quiet.alpha()})"
        self.setStyleSheet(
            f"ToastCard {{ background: {background};"
            f" border: {FRAME_WIDTH}px solid {foreground};{edge} }}"
            f" QLabel {{ color: {foreground}; background: transparent; }}"
            f" QToolButton {{ color: {quiet_css}; background: transparent;"
            f" border: none; padding: 1px 4px; }}"
            f" QToolButton:hover {{ color: {foreground}; }}"
        )

    # -- controls ----------------------------------------------------------

    def toggle_details(self) -> None:
        self.expanded = not self.expanded
        self._apply_disclosure()
        # Reading the detail is a request to keep it around.
        if self.expanded and not self.pinned:
            self.toggle_pin()

    def _apply_disclosure(self) -> None:
        has_body = bool(self.spec.get("body", ""))
        self.details.setVisible(self.expanded and has_body)
        self.disclosure.setText("▾" if self.expanded else "▸")

    def toggle_pin(self, pinned: Optional[bool] = None) -> None:
        self.pinned = (not self.pinned) if pinned is None else pinned
        self.pin_button.setText("●" if self.pinned else "○")
        self.pin_button.setToolTip("Let this close again" if self.pinned else "Keep this open")
        if self.pinned:
            self.stop_timer()
        else:
            self.restart_timer()
        self._on_pin(self.handle, self.pinned)

    # -- expiry ------------------------------------------------------------

    def arm(self, timeout_ms: Optional[int]) -> None:
        """Set how long this card lives. None means it stays until dismissed."""
        self._timeout_ms = timeout_ms
        self.restart_timer()

    def restart_timer(self) -> None:
        from aqt import mw

        self.stop_timer()
        if self._timeout_ms is None or self.pinned:
            return
        countdown = self._countdown
        # Parented to the card so the timer dies with it; single_shot would
        # outlive the widget and fire into a deleted object. Anki's retry of it
        # under a progress dialog is a single_shot regardless, hence _countdown.
        self._timer = mw.progress.timer(
            self._timeout_ms,
            lambda: self._expire(countdown),
            repeat=False,
            requiresCollection=False,
            parent=self,
        )

    def _expire(self, countdown: int) -> None:
        """Close, unless somebody is reading it.

        The hover check happens here rather than by stopping the timer on
        enterEvent, because enter and leave do not reliably pair up: a card
        that appears under a stationary cursor gets an Enter and no Leave, and
        stopping the timer there would strand it on screen forever. Re-arming
        on a hover instead is self-correcting -- a missed event costs one extra
        countdown, not the card's whole lifetime.

        A countdown stopped since it started does nothing, and finds that out
        before it touches the widget, which may be gone.
        """
        if countdown != self._countdown:
            return
        if self.pinned:
            return
        if self.underMouse():
            self.restart_timer()
            return
        self._on_dismiss(self.handle)

    def stop_timer(self) -> None:
        self._countdown += 1
        if self._timer is not None:
            try:
                self._timer.stop()
                self._timer.deleteLater()
            except RuntimeError:
                pass
            self._timer = None

