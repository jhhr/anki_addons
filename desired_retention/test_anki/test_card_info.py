"""Characterization tests for desired_retention against a running Anki 26.x."""

from __future__ import annotations

from typing import Any

from anki_shared.testing import real_anki

from .conftest import NOTE_TYPE, load_addon

WAIT = 15000


def _js(anki_session, web, expr: str) -> Any:
    result: list[Any] = []
    web.evalWithCallback(expr, lambda value: result.append(value))
    anki_session.qtbot.waitUntil(lambda: bool(result), timeout=WAIT)
    return result[0]


def _wait_for(anki_session, web, expr: str) -> Any:
    for _ in range(200):
        value = _js(anki_session, web, expr)
        if value:
            return value
        anki_session.qtbot.wait(50)
    raise AssertionError(expr)


def _close_dialog(anki_session, dialog) -> None:
    if dialog.web is not None:
        dialog.reject()
    dialog.deleteLater()
    anki_session.qtbot.wait(50)


def _make_card(mw, front: str, desired_retention: float | None = None):
    note = real_anki.add_note(mw.col, NOTE_TYPE, {"Front": front, "Back": front})
    card = note.cards()[0]
    if desired_retention is not None:
        card.desired_retention = desired_retention
        mw.col.update_card(card)
        card = mw.col.get_card(card.id)
    return card


def _assert_current_card_view(anki_session, dialog, card_id: int) -> None:
    assert dialog.web is not None
    _wait_for(anki_session, dialog.web, "Boolean(document.querySelector('.stats-table'))")
    body = _js(anki_session, dialog.web, "document.body.innerText")
    assert "Card ID" in body
    assert str(card_id) in body


def test_plain_current_card_view_is_not_empty_in_anki_26_9_2(real_mw, anki_session):
    from aqt.browser.card_info import CardInfoDialog

    card = _make_card(real_mw, "plain")
    dialog = CardInfoDialog(None, real_mw, card)
    try:
        _assert_current_card_view(anki_session, dialog, card.id)
    finally:
        _close_dialog(anki_session, dialog)


def test_browser_and_reviewer_current_card_views_stay_populated_with_the_addon(
    real_mw, anki_session
):
    from aqt.browser.card_info import BrowserCardInfo, ReviewerCardInfo

    load_addon()
    for manager_cls in (BrowserCardInfo, ReviewerCardInfo):
        manager = manager_cls(real_mw)
        first = _make_card(real_mw, "first", desired_retention=0.91)
        second = _make_card(real_mw, "second", desired_retention=0.88)
        manager.set_card(first)
        manager.show()
        dialog = manager._dialog
        assert dialog is not None
        try:
            _assert_current_card_view(anki_session, dialog, first.id)
            row = _wait_for(
                anki_session,
                dialog.web,
                "document.getElementById('anki-dr-row')?.innerText",
            )
            assert "Desired Retention" in row
            assert "91%" in row

            manager.set_card(second)
            _wait_for(
                anki_session,
                dialog.web,
                f"document.body.innerText.includes('{second.id}')",
            )
            _assert_current_card_view(anki_session, dialog, second.id)
            updated_row = _wait_for(
                anki_session,
                dialog.web,
                "document.getElementById('anki-dr-row')?.innerText",
            )
            assert "88%" in updated_row
        finally:
            _close_dialog(anki_session, dialog)
