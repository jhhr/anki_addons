"""The deck run's report: what describe_result says and what the deck gear posts.

The report goes to the shared notification stack, where a card shows its title
and keeps its body shut until it is opened, so the outcome has to be in the
title on its own. Which window shows it is the notify package's business and is
tested there; here `post` is replaced with a recorder.
"""

from dataclasses import replace
from typing import Any

import pytest
from anki.decks import DeckId

from related_card_disperse import hooks_deck_browser
from related_card_disperse.bury import BuryRunResult, describe_result, result_level


def ran(**fields: Any) -> BuryRunResult:
    """A run that read a session of five and buried two in the deck it started from."""
    run = BuryRunResult(
        deck_name="Japanese::Vocab",
        block_name="Japanese",
        ordering="due date",
        session_cards=5,
        pool_size=5,
        buried=2,
        rule_runs=7,
        buried_by_deck={"Japanese::Vocab": 2},
    )
    return replace(run, **fields)


@pytest.fixture
def posts(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(hooks_deck_browser, "post", lambda **kwargs: recorded.append(kwargs))
    return recorded


def test_the_title_says_what_happened_to_the_deck_and_the_body_how():
    title, body = describe_result(ran())
    assert title == "Japanese::Vocab: buried 2 of 5 cards in today's session"
    assert body == "Ordered by due date (7 rule runs)"
    assert result_level(ran()) == "success"


def test_cards_buried_outside_the_tree_are_named_in_the_body():
    result = ran(buried=3, buried_by_deck={"Japanese::Vocab": 2, "Kanji": 1})
    title, body = describe_result(result)
    assert "buried 3 of 5" in title
    assert body.split("<br>")[1:] == ["Buried elsewhere: 1 card in Kanji"]


def test_a_rule_that_raised_marks_the_report():
    # The error is in the body, which stays shut: the mark is what says to open it.
    result = ran(rule_errors={"Reversible": ("boom", 3)})
    _, body = describe_result(result)
    assert body.endswith("Rule errors: Reversible (boom) on 3 cards")
    assert result_level(result) == "warning"


def test_nothing_to_bury_is_said_in_the_body():
    result = ran(buried=0, buried_by_deck={})
    title, body = describe_result(result)
    assert "buried 0 of 5" in title
    assert body.endswith("No two related cards were going to come up together")
    assert result_level(result) == "success"


@pytest.mark.parametrize(
    "result, title, level",
    [
        (BuryRunResult(error="no such deck"), "Could not disperse: no such deck", "error"),
        (
            ran(cancelled=True, buried=0, rule_errors={"R": ("boom", 1)}),
            "Cancelled; nothing was buried",
            "info",
        ),
        (ran(session_cards=1, buried=0), "Japanese::Vocab: nothing due to disperse", "info"),
    ],
)
def test_a_run_that_buried_nothing_is_one_line(result, title, level):
    assert describe_result(result) == (title, "")
    assert result_level(result) == level


def test_the_result_is_posted_keyed_to_its_deck(posts):
    hooks_deck_browser._show_result(DeckId(7), ran(rule_errors={"R": ("boom", 1)}))
    [post] = posts
    assert post["source"] == "Related Card Disperse"
    assert post["title"] == "Japanese::Vocab: buried 2 of 5 cards in today's session"
    assert post["body"].startswith("Ordered by due date")
    assert post["level"] == "warning"
    assert post["timeout_ms"] == 10000
    assert post["key"] == "deck:7"
    # Ungrouped: the "sync" group is for what a sync reports.
    assert "group" not in post


class FakeConfig:
    rules = True
    disperse_across_decks = True

    def load(self):
        pass

    def has_any_rules(self):
        return self.rules


def test_with_no_rules_the_deck_gear_says_so_and_runs_nothing(posts, monkeypatch):
    monkeypatch.setattr(FakeConfig, "rules", False)
    monkeypatch.setattr(hooks_deck_browser, "Config", FakeConfig)
    started: list[DeckId] = []
    monkeypatch.setattr(
        hooks_deck_browser,
        "run_deck_bury_disperse_in_background",
        lambda deck_id, *args, **kwargs: started.append(deck_id),
    )
    hooks_deck_browser.run_disperse_on_deck(DeckId(7))
    assert started == []
    [post] = posts
    assert (post["title"], post["level"]) == ("No rules configured", "warning")
    # The run's report replaces it once rules are set up and the deck is run again.
    assert post["key"] == "deck:7"


def test_the_deck_gear_reports_the_run_for_the_deck_it_was_opened_on(posts, monkeypatch):
    monkeypatch.setattr(hooks_deck_browser, "Config", FakeConfig)

    def run_now(deck_id, config, on_done, **kwargs):
        on_done(ran())

    monkeypatch.setattr(hooks_deck_browser, "run_deck_bury_disperse_in_background", run_now)
    hooks_deck_browser.run_disperse_on_deck(DeckId(9))
    [post] = posts
    assert post["key"] == "deck:9"
    assert post["level"] == "success"
