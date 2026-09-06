"""The startup wiring: who gets asked, who does not, and what a refusal costs.

Anki is not running here, so aqt is stubbed. What is actually under test is the decision
sequence - health, then whether the question is due, then whether a rebuild is even possible -
because getting that order wrong is how an addon ends up asking at every single startup on a
machine that can never do anything about it.
"""

import sys
from unittest.mock import MagicMock

import pytest

for _name in ("aqt", "aqt.qt", "aqt.utils"):
    sys.modules.setdefault(_name, MagicMock())

from anki_shared.utils import vendor_rebuild_ui  # noqa: E402


@pytest.fixture
def ui(monkeypatch):
    """The module with every side effect replaced by a recorder."""
    calls = {"asked": [], "rebuilt": [], "recorded": [], "warned": []}
    monkeypatch.setattr(vendor_rebuild_ui, "_add_menu_action", lambda *a: None)
    monkeypatch.setattr(vendor_rebuild_ui, "can_rebuild", lambda: None)
    monkeypatch.setattr(vendor_rebuild_ui, "prompt_is_due", lambda _d: True)
    monkeypatch.setattr(
        vendor_rebuild_ui, "askUser",
        lambda text, **kw: calls["asked"].append(text) or True,
    )
    monkeypatch.setattr(
        vendor_rebuild_ui, "_run_rebuild", lambda *a: calls["rebuilt"].append(a)
    )
    monkeypatch.setattr(
        vendor_rebuild_ui, "record_attempt",
        lambda _d, outcome: calls["recorded"].append(outcome),
    )
    monkeypatch.setattr(
        vendor_rebuild_ui, "showWarning", lambda text, **kw: calls["warned"].append(text)
    )
    monkeypatch.setattr(vendor_rebuild_ui, "gui_hooks", MagicMock())
    return calls


def start_up(health):
    """Run install_rebuild_ui and fire the hook it registered."""
    hooks = []
    vendor_rebuild_ui.gui_hooks.main_window_did_init.append = hooks.append
    vendor_rebuild_ui.install_rebuild_ui("/addon", "Test Addon", health)
    assert len(hooks) == 1
    hooks[0]()


class TestStartupOffer:
    def test_a_healthy_install_is_never_asked(self, ui):
        start_up(None)
        assert ui["asked"] == [] and ui["rebuilt"] == []

    def test_an_unhealthy_install_is_asked_and_told_why(self, ui):
        start_up("built for Python 3.12")
        assert len(ui["asked"]) == 1
        assert "built for Python 3.12" in ui["asked"][0]
        assert ui["rebuilt"]

    def test_declining_is_recorded_and_nothing_is_rebuilt(self, ui, monkeypatch):
        monkeypatch.setattr(vendor_rebuild_ui, "askUser", lambda *a, **k: False)
        start_up("built for Python 3.12")
        assert ui["recorded"] == ["declined"] and ui["rebuilt"] == []

    def test_a_previous_answer_is_not_asked_for_again(self, ui, monkeypatch):
        monkeypatch.setattr(vendor_rebuild_ui, "prompt_is_due", lambda _d: False)
        start_up("built for Python 3.12")
        assert ui["asked"] == [] and ui["rebuilt"] == []

    def test_an_impossible_rebuild_is_recorded_rather_than_offered(self, ui, monkeypatch):
        """Offering what cannot be done, at every startup, is the nagging this avoids."""
        monkeypatch.setattr(vendor_rebuild_ui, "can_rebuild", lambda: "anki.exe is not a Python")
        start_up("built for Python 3.12")
        assert ui["asked"] == [] and ui["rebuilt"] == []
        assert ui["recorded"] == ["unavailable: anki.exe is not a Python"]

    def test_the_menu_action_is_added_whatever_the_verdict(self, ui, monkeypatch):
        added = []
        monkeypatch.setattr(vendor_rebuild_ui, "_add_menu_action", lambda *a: added.append(a))
        start_up(None)
        assert added == [("/addon", "Test Addon")]


class TestLogging:
    def test_the_module_logger_has_a_handler_of_its_own(self):
        """Without one, logging's last resort writes to stderr and Anki reports a crash.

        Everything this module logs is a condition it has already put in a dialog, so the
        stderr copy was a second and scarier report of something that had been handled.
        """
        import logging

        assert any(
            isinstance(handler, logging.NullHandler)
            for handler in vendor_rebuild_ui.logger.handlers
        )


class TestOnDemand:
    def test_a_healthy_install_can_still_ask_for_the_compiled_half(self, ui):
        vendor_rebuild_ui._rebuild_on_demand("/addon", "Test Addon")
        assert len(ui["asked"]) == 1
        assert "faster" in ui["asked"][0]
        assert ui["rebuilt"]

    def test_an_obstacle_is_said_out_loud_when_the_user_asked(self, ui, monkeypatch):
        monkeypatch.setattr(vendor_rebuild_ui, "can_rebuild", lambda: "there is no pip")
        vendor_rebuild_ui._rebuild_on_demand("/addon", "Test Addon")
        assert ui["asked"] == [] and ui["rebuilt"] == []
        assert "there is no pip" in ui["warned"][0]

    def test_declining_on_demand_records_nothing(self, ui, monkeypatch):
        """The startup offer is what needs suppressing; a menu action the user can repeat."""
        monkeypatch.setattr(vendor_rebuild_ui, "askUser", lambda *a, **k: False)
        vendor_rebuild_ui._rebuild_on_demand("/addon", "Test Addon")
        assert ui["recorded"] == [] and ui["rebuilt"] == []
