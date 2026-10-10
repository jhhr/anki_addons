"""The shared interpolated text edit, built the way its own defaults allow.

`InterpolatedTextEditLayout` lives in `anki_shared`, whose suite has no Qt application; it is
built here, where the editor cases already run on the offscreen platform plugin.
"""

import pytest

from anki_shared.ui.interpolated_text_edit import InterpolatedTextEditLayout


@pytest.fixture
def qt_messages(qapp):
    """Everything Qt logs through its message handler while the case runs.

    `QLayout: Cannot add a null widget` is a Qt warning, not a Python exception, so it goes
    past `pytest.raises` and `capfd` alike; only the handler sees it.
    """
    from aqt.qt import qInstallMessageHandler

    logged: list[str] = []
    previous = qInstallMessageHandler(lambda _type, _context, message: logged.append(message))
    yield logged
    qInstallMessageHandler(previous)


def test_a_layout_without_a_label_adds_no_null_widget(widget_parent, qt_messages):
    # The default is no label, and `set_label` already guards against one; the constructor
    # is the half that still handed Qt a None.
    layout = InterpolatedTextEditLayout(widget_parent)

    assert qt_messages == []
    assert layout.main_label is None
    assert all(layout.itemAt(i).widget() is not None for i in range(layout.count()))
    layout.set_label("x")  # a no-op rather than an AttributeError
    assert layout.main_label is None


def test_a_layout_with_a_label_still_shows_it(widget_parent, qt_messages):
    layout = InterpolatedTextEditLayout(widget_parent, label="Caption")

    assert qt_messages == []
    assert layout.main_label is not None
    assert layout.main_label.text() == "Caption"
