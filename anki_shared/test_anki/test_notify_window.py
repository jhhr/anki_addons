"""The notification window, against a running Anki.

What the window shows -- order, groups, dedup, the queue, lifetimes -- is
`policy.Stack`'s to decide, and test/test_notify_stack.py tests that without Qt.
What is here is the widgets carrying it out, on what only a running Anki has:
`mw.progress.timer`, which every redraw and every countdown goes through and
which holds a timer back while a progress dialog is up; a real main window to
parent the window to and follow; and `gui_hooks.theme_did_change`.

Each test builds a `ToastHost` of its own on an `AnkiQt` of its own. The registry
only comes in at the end, where the copies two addons vendor have to find one
window.
"""

import importlib
from typing import Any, Iterator

import aqt.utils
import pytest
from aqt.gui_hooks import theme_did_change
from aqt.qt import QApplication, QEvent, QMouseEvent, QPoint, QPointF, Qt, QWidget
from aqt.theme import Theme, theme_manager

from anki_shared.notify.card import ACCENTS, ToastCard
from anki_shared.notify.host import ToastHost
from anki_shared.notify.spec import STICKY, make_spec
from anki_shared.testing import running_anki

# For what should happen: a redraw is due 60ms after a post, and the shortest
# countdown here is 200ms. qtbot.wait() is for what must not.
WAIT = 3000


@pytest.fixture(autouse=True)
def restore_stub_mw() -> Iterator[Any]:
    # Every window appends its restyle handler to theme_did_change, which outlives
    # the test's Anki.
    with running_anki.stub_mw_restored(["anki_shared"], [theme_did_change]) as stub:
        yield stub


@pytest.fixture(autouse=True)
def nothing_hovered(monkeypatch) -> None:
    """No card is under the cursor unless a test says so.

    The offscreen platform marks every window it shows as entered, wherever the
    cursor is, so every card would report underMouse() and none would expire.
    """
    monkeypatch.setattr(ToastCard, "underMouse", lambda self: False)


@pytest.fixture
def real_mw(anki_session, restore_stub_mw) -> Iterator[Any]:
    with running_anki.main_window(anki_session, ["anki_shared"]) as mw:
        yield mw


@pytest.fixture
def host(real_mw) -> ToastHost:
    return ToastHost()


@pytest.fixture
def set_theme(real_mw, monkeypatch) -> Iterator[Any]:
    """set_theme(night) switches this Anki's theme the way its preferences do.

    Put back to light afterwards: the palette and theme_manager are the
    process's, not the test's Anki's.
    """

    def set_theme(night: bool) -> None:
        theme = Theme.DARK if night else Theme.LIGHT
        monkeypatch.setattr(real_mw.pm, "theme", lambda: theme)
        theme_manager.apply_style()

    yield set_theme
    set_theme(False)


def tooltip_colours(real_mw):
    """The frame and background of Anki's own tooltip, as it draws them now."""
    aqt.utils.tooltip("for comparison", parent=real_mw, period=10000)
    try:
        image = aqt.utils._tooltipLabel.grab().toImage()
        return image.pixelColor(0, 0).name(), image.pixelColor(image.width() // 2, 5).name()
    finally:
        aqt.utils.closeTooltip()


def card_colours(card):
    """A card's frame, taken at the top right clear of any accent, and background."""
    image = card.grab().toImage()
    return (
        image.pixelColor(image.width() - 1, 0).name(),
        image.pixelColor(image.width() // 2, 5).name(),
    )


def send_mouse(widget, kind, at, button, buttons):
    event = QMouseEvent(
        kind,
        QPointF(at),
        QPointF(widget.mapToGlobal(at)),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def test_the_window_never_takes_the_keyboard(host, qtbot):
    # Anki sends review keystrokes to the reviewer webview; a notification that
    # took the focus would eat answers.
    window = host.window
    handle = host.post(make_spec(source="A", title="done", body="detail", timeout_ms=STICKY,
                                 actions=[("Open", lambda: None)]))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    assert window.windowFlags() & Qt.WindowType.Tool
    assert window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    takes_focus = [
        widget
        for widget in [window, *window.findChildren(QWidget)]
        if widget.focusPolicy() != Qt.FocusPolicy.NoFocus
    ]
    assert takes_focus == []
    # Qt owns a widget's namespace (update, close, show...); the contract every
    # copy calls by name keeps out of it.
    assert not isinstance(host, QWidget)


def test_posts_stack_with_a_label_per_named_group(host, real_mw, qtbot):
    window = host.window
    host.post(make_spec(source="Copy Anywhere", title="Copied fields", body="12 notes",
                        group="sync", key="sync", timeout_ms=STICKY))
    host.post(make_spec(source="Related Card Disperse", title="Dispersed 4", group="sync",
                        timeout_ms=STICKY))
    host.post(make_spec(source="Custom Schedule Helper", title="Rescheduled 30",
                        timeout_ms=STICKY))
    qtbot.waitUntil(lambda: len(window._cards) == 3, timeout=WAIT)
    assert [label.text() for label in window._group_labels] == ["SYNC"]
    # Anchored inside the main window's bottom-right corner.
    assert window.geometry().right() <= real_mw.frameGeometry().right()
    assert window.geometry().bottom() <= real_mw.frameGeometry().bottom()


def test_the_disclosure_is_tracked_and_reading_the_detail_pins(host, qtbot):
    window = host.window
    handle = host.post(make_spec(source="A", title="done", body="detail", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    card = window._cards[handle]
    card.disclosure.click()
    assert card.expanded is True
    assert card.details.isVisibleTo(card)
    assert card.pinned is True, "reading the detail is a request to keep it"
    card.disclosure.click()
    assert card.expanded is False
    assert card.disclosure.text() == "▸"


def test_a_keyed_repost_replaces_its_card(host, qtbot):
    window = host.window
    first = host.post(make_spec(source="Copy Anywhere", title="Copied local", group="sync",
                                key="sync", timeout_ms=STICKY))
    host.post(make_spec(source="Other", title="other", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: len(window._cards) == 2, timeout=WAIT)
    host.post(make_spec(source="Copy Anywhere", title="Copied local and remote",
                        group="sync", key="sync", timeout_ms=STICKY))
    qtbot.waitUntil(
        lambda: "Copied local and remote" in window._cards[first].summary.text(), timeout=WAIT
    )
    assert len(window._cards) == 2


def test_overflow_queues_and_closing_clears_the_queue_too(host, qtbot):
    window = host.window
    for i in range(8):
        host.post(make_spec(source=f"Addon {i}", title=f"m{i}", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: len(window._cards) == window._stack.max_visible, timeout=WAIT)
    assert len(window._stack.queued()) == 8 - window._stack.max_visible
    # Closing means done with all of them: left queued, they would reopen the window
    # as they were promoted.
    window.close()
    assert window._cards == {}
    assert window._stack.ordered() == []
    assert window._group_labels == []


def test_the_window_keeps_fitting_its_cards_after_it_first_appears(host, qtbot):
    # Qt delivers a hidden window's held-back resize while show() runs. Taken for
    # the user's, it switched auto-sizing off the first time the window appeared.
    window = host.window
    host.post(make_spec(source="A", title="one", timeout_ms=STICKY))
    qtbot.waitUntil(window.isVisible, timeout=WAIT)
    one_card = window.height()
    host.post(make_spec(source="B", title="two", timeout_ms=STICKY))
    host.post(make_spec(source="C", title="three", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: window.height() > one_card, timeout=WAIT)


def test_a_size_the_user_sets_sticks(host, qtbot):
    window = host.window
    host.post(make_spec(source="A", title="one", timeout_ms=STICKY))
    qtbot.waitUntil(window.isVisible, timeout=WAIT)
    chosen = window.height() + 7
    # Through the native window, so the resize arrives spontaneous, as a drag's does.
    native = window.windowHandle()
    assert native is not None
    native.resize(window.width(), chosen)
    qtbot.waitUntil(lambda: window.height() == chosen, timeout=WAIT)
    host.post(make_spec(source="B", title="two", timeout_ms=STICKY))
    host.post(make_spec(source="C", title="three", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: len(window._cards) == 3, timeout=WAIT)
    assert window.height() == chosen


def test_a_window_the_user_moved_stays_where_they_put_it(host, real_mw, qtbot):
    # Put back in mw's corner on every post and every move of mw, a window dragged
    # out of the way came straight back.
    window = host.window
    host.post(make_spec(source="A", title="one", timeout_ms=STICKY))
    qtbot.waitUntil(window.isVisible, timeout=WAIT)
    corner = window.geometry().bottomRight()
    drag = QPoint(-150, -100)
    # Through the native window, as the window system moves it for a drag.
    native = window.windowHandle()
    assert native is not None
    native.setPosition(native.position() + drag)
    qtbot.waitUntil(lambda: window.geometry().bottomRight() == corner + drag, timeout=WAIT)
    host.post(make_spec(source="B", title="two", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: len(window._cards) == 2, timeout=WAIT)
    assert window.geometry().bottomRight() == corner + drag, "the drag must stick"
    # It still follows mw around, keeping the user's offset from its corner.
    nudge = QPoint(30, 20)
    real_mw.move(real_mw.pos() + nudge)
    qtbot.waitUntil(
        lambda: window.geometry().bottomRight() == corner + drag + nudge, timeout=WAIT
    )


def test_a_card_expires(host, qtbot):
    window = host.window
    handle = host.post(make_spec(source="A", title="short lived", timeout_ms=200))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    qtbot.waitUntil(lambda: handle not in window._cards, timeout=WAIT)


def test_a_card_under_the_cursor_waits_for_it_to_leave(host, qtbot):
    window = host.window
    handle = host.post(make_spec(source="A", title="hovered", timeout_ms=200))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    card = window._cards[handle]
    card.underMouse = lambda: True
    qtbot.wait(600)
    assert handle in window._cards, "a hovered card must not close under the cursor"
    card.underMouse = lambda: False
    qtbot.waitUntil(lambda: handle not in window._cards, timeout=WAIT)


def test_a_card_pinned_by_click_does_not_expire(host, qtbot):
    # clicked emits a `checked` bool; let through to toggle_pin(), it set the card
    # unpinned rather than toggling it.
    window = host.window
    handle = host.post(make_spec(source="A", title="pinned", timeout_ms=200))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    card = window._cards[handle]
    card.pin_button.click()
    assert card.pinned is True, "clicking pin must toggle, not set False"
    qtbot.wait(600)
    assert handle in window._cards
    card.close_button.click()
    qtbot.waitUntil(lambda: handle not in window._cards, timeout=WAIT)


def test_a_card_reposted_while_pinned_expires_once_unpinned(host, qtbot):
    # The repost re-armed the card with the pinned entry's "never", which it kept
    # as its lifetime, so unpinning left it up for good.
    window = host.window
    handle = host.post(make_spec(source="A", title="first", key="k", timeout_ms=200))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    card = window._cards[handle]
    card.pin_button.click()
    host.post(make_spec(source="A", title="second", key="k", timeout_ms=200))
    qtbot.waitUntil(lambda: "second" in card.summary.text(), timeout=WAIT)
    card.pin_button.click()
    qtbot.waitUntil(lambda: handle not in window._cards, timeout=WAIT)


def test_nothing_is_drawn_while_a_progress_dialog_is_up(host, real_mw, qtbot):
    # A post made as an operation finishes would otherwise be drawn into a dialog
    # that is still tearing down.
    window = host.window
    real_mw.progress.start()
    try:
        host.post(make_spec(source="A", title="posted under progress", timeout_ms=STICKY))
        qtbot.wait(400)
        assert window._cards == {}, "the redraw waits for the progress dialog"
        assert len(window._stack.ordered()) == 1, "but the entry is recorded at once"
    finally:
        real_mw.progress.finish()
    qtbot.waitUntil(lambda: len(window._cards) == 1, timeout=WAIT)


def test_a_countdown_held_back_by_a_progress_dialog_does_not_end_a_repost(
    host, real_mw, qtbot
):
    # Anki retries a timer that fires under a progress dialog from a single_shot of
    # its own, out of reach of the card's stop_timer(). When the dialog closed, that
    # retry ended the entry a repost had just replaced, and the new report with it.
    window = host.window
    handle = host.post(make_spec(source="A", title="first", key="k", timeout_ms=300))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    real_mw.progress.start()
    try:
        qtbot.wait(450)  # the countdown runs out under the dialog
        host.post(make_spec(source="A", title="second", key="k", timeout_ms=300))
    finally:
        real_mw.progress.finish()
    qtbot.wait(150)  # past the retry, which comes within 100ms of the dialog closing
    assert handle in window._cards, "the repost has a countdown of its own"
    qtbot.waitUntil(
        lambda: handle in window._cards and "second" in window._cards[handle].summary.text(),
        timeout=WAIT,
    )
    qtbot.waitUntil(lambda: handle not in window._cards, timeout=WAIT)


def test_a_card_is_drawn_as_ankis_tooltip(host, real_mw, qtbot):
    # Next to the tooltips Anki still shows itself, a card styled as a widget of
    # its own was jarring.
    window = host.window
    handle = host.post(make_spec(source="A", title="done", level="success", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    assert card_colours(window._cards[handle]) == tooltip_colours(real_mw)


def test_a_theme_change_redraws_the_cards_as_the_night_tooltip(
    host, real_mw, qtbot, set_theme
):
    window = host.window
    handle = host.post(make_spec(source="A", title="done", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    card = window._cards[handle]
    light = card_colours(card)
    set_theme(night=True)
    qtbot.waitUntil(lambda: card_colours(card) != light, timeout=WAIT)
    assert card_colours(card) == tooltip_colours(real_mw)


def test_a_warning_or_an_error_is_marked_on_its_left_edge(host, qtbot):
    # Drawn as a tooltip, it must still not pass for an info or a success.
    window = host.window
    plain = host.post(make_spec(source="A", title="done", level="success", timeout_ms=STICKY))
    failed = host.post(make_spec(source="B", title="failed", level="error", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: len(window._cards) == 2, timeout=WAIT)

    def edges(handle):
        image = window._cards[handle].grab().toImage()
        middle = image.height() // 2
        return (
            image.pixelColor(1, middle).name(),
            image.pixelColor(image.width() - 2, middle).name(),
        )

    left, right = edges(plain)
    assert left == right, "a success is a plain tooltip"
    left, right = edges(failed)
    assert left == ACCENTS[False]["error"] != right


def test_only_the_header_is_drawn_solid(host, qtbot):
    # Below it the window fades, down and out to the sides, so the cards read as
    # tooltips floating on their own rather than the contents of a panel.
    window = host.window
    handles = [
        host.post(make_spec(source=f"A{i}", title="done", timeout_ms=STICKY)) for i in range(3)
    ]
    qtbot.waitUntil(lambda: len(window._cards) == 3, timeout=WAIT)
    image = window.grab().toImage()

    def alpha(x, y):
        return image.pixelColor(x, y).alpha()

    middle = image.width() // 2
    header = window._header.geometry()
    solid = alpha(middle, header.center().y())
    under_header = alpha(middle, header.bottom() + 3)
    assert solid >= 230
    assert 0 < under_header < solid
    assert alpha(middle, image.height() - 2) < under_header, "fading towards the bottom"
    assert alpha(1, header.bottom() + 3) < under_header, "and out to the sides"
    card = window._cards[handles[0]]
    centre = card.mapTo(window, card.rect().center())
    assert alpha(centre.x(), centre.y()) == 255, "the cards themselves are solid"


def test_the_window_ends_just_below_its_last_card(host, qtbot):
    # Sized from the cards' sizeHint(), which wrapping text answers for a narrower
    # width than they get, the window left a band of empty backdrop under them.
    window = host.window
    handles = [
        host.post(make_spec(source=f"A{i}", title="Copied fields for local and remote changes",
                            timeout_ms=STICKY))
        for i in range(3)
    ]
    qtbot.waitUntil(lambda: len(window._cards) == 3, timeout=WAIT)
    last = window._cards[handles[-1]]
    below = window.height() - last.mapTo(window, QPoint(0, last.height())).y()
    assert below <= window._cards_layout.contentsMargins().bottom() + 4


def test_expanding_a_pinned_card_grows_the_window_to_fit(host, qtbot):
    # Only the pin that expanding sets refitted the window, so a card pinned first
    # and expanded after was cut off at the window's edge behind a scrollbar.
    window = host.window
    host.post(make_spec(source="A", title="one", body="detail", timeout_ms=STICKY))
    handle = host.post(make_spec(source="B", title="two", timeout_ms=STICKY,
                                 body="<br>".join(f"line {i}" for i in range(6))))
    qtbot.waitUntil(lambda: len(window._cards) == 2, timeout=WAIT)
    card = window._cards[handle]
    card.pin_button.click()
    qtbot.wait(150)
    collapsed = window.height()
    card.disclosure.click()
    qtbot.waitUntil(lambda: window.height() > collapsed, timeout=WAIT)
    scrollbar = window._scroll.verticalScrollBar()
    assert scrollbar is not None and not scrollbar.isVisible(), "nothing is cut off"
    card.disclosure.click()
    qtbot.waitUntil(lambda: window.height() == collapsed, timeout=WAIT)


def test_the_header_drags_the_window(host, qtbot):
    # With no title bar, the header is what moves the window. The window system
    # takes the drag where it can; offscreen it cannot, so this is the drag
    # followed by hand.
    window = host.window
    host.post(make_spec(source="A", title="one", timeout_ms=STICKY))
    qtbot.waitUntil(window.isVisible, timeout=WAIT)
    header = window._header
    start = window.pos()
    grip = QPoint(8, header.height() // 2)
    drag = QPoint(-120, -80)
    left = Qt.MouseButton.LeftButton
    none = Qt.MouseButton.NoButton
    send_mouse(header, QEvent.Type.MouseButtonPress, grip, left, left)
    send_mouse(header, QEvent.Type.MouseMove, grip + drag, none, left)
    send_mouse(header, QEvent.Type.MouseButtonRelease, grip + drag, left, none)
    assert window.pos() == start + drag
    corner = window.geometry().bottomRight()
    host.post(make_spec(source="B", title="two", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: len(window._cards) == 2, timeout=WAIT)
    assert window.geometry().bottomRight() == corner, "and it stays where it was dragged"


def test_the_header_dismisses_everything(host, qtbot):
    window = host.window
    for i in range(6):
        host.post(make_spec(source=f"A{i}", title="done", timeout_ms=STICKY))
    qtbot.waitUntil(lambda: len(window._cards) == window._stack.max_visible, timeout=WAIT)
    window._header.dismiss_all.click()
    assert window._stack.ordered() == [], "queued ones included"
    assert not window.isVisible()


def test_an_action_runs_and_dismisses_its_card(host, qtbot):
    window = host.window
    fired = []
    handle = host.post(make_spec(source="Addon Config Sync", title="Loaded config",
                                 timeout_ms=STICKY,
                                 actions=[("Open config manager…", lambda: fired.append(1))]))
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    button = window._cards[handle].actions_row.itemAt(0).widget()
    assert button.text() == "Open config manager…"
    assert button.focusPolicy() == Qt.FocusPolicy.NoFocus
    button.click()
    assert fired == [1]
    # Acting on a notification is done reading it.
    qtbot.waitUntil(lambda: handle not in window._cards, timeout=WAIT)


def test_a_closing_profile_closes_the_window_with_it(anki_session, capsys):
    # Anki closes a window marked silentlyClose as the profile closes, and reports
    # any other still up as one that "should have been closed", leaving it there.
    with running_anki.main_window(anki_session, ["anki_shared"]):
        host = ToastHost()
        host.post(make_spec(source="A", title="up as the profile closes", timeout_ms=STICKY))
        anki_session.qtbot.waitUntil(host.window.isVisible, timeout=WAIT)
    # Leaving the block unloads the profile.
    assert not host.window.isVisible()
    assert host.window._stack.ordered() == []
    reported = [
        line for line in capsys.readouterr().out.splitlines() if "should have been closed" in line
    ]
    assert not [line for line in reported if "ToastWindow" in line]


def test_the_copies_two_addons_vendor_post_into_one_window(real_mw, qtbot):
    # The claim the package exists to make. Vendored, the package is a different
    # module object per addon, and they share nothing but mw.
    copies = [
        importlib.import_module(f"{addon}.shared.notify")
        for addon in ("copy_anywhere", "related_card_disperse")
    ]
    first, second = copies
    assert first is not second
    # Each copy registers itself as it is imported, against whichever mw existed
    # then: under the suite, possibly the stub. Register them with this Anki, as its
    # loading the addons would have.
    for vendored in copies:
        vendored.registry.register(real_mw, vendored.__name__, vendored._make_host)
    elected = first.registry.host(real_mw)
    assert elected is second.registry.host(real_mw), "each copy elected its own window"
    first.post(source="Copy Anywhere", title="from copy A", timeout_ms=STICKY)
    second.post(source="Related Card Disperse", title="from copy B", timeout_ms=STICKY)
    qtbot.waitUntil(lambda: len(elected.window._cards) == 2, timeout=WAIT)


def test_a_malformed_update_is_corrected_before_the_window_draws_it(real_mw, qtbot):
    # Passed on as given, it was accepted and then raised in the redraw, from a Qt
    # timer: here pytest-qt would fail the test, in Anki the user got an error dialog.
    notify = importlib.import_module("anki_shared.notify")
    notify.registry.register(real_mw, notify.__name__, notify._make_host)
    window = notify.registry.host(real_mw).window
    handle = notify.post(source="A", title="to update", timeout_ms=STICKY)
    qtbot.waitUntil(lambda: handle in window._cards, timeout=WAIT)
    card = window._cards[handle]
    assert notify.update(handle, actions=[("no callback",)], timeout_ms="soon", body=5)
    qtbot.waitUntil(lambda: card.details.text() == "5", timeout=WAIT)
    qtbot.wait(100)
    assert card.actions_row.count() == 1, "only the stretch: no button for the action"
