"""What every run posts: the deck gear's, the browser's and an answer's report.

Each goes to the shared notification stack, where a card shows its title and
keeps its body shut until it is opened, so the outcome has to be in the title on
its own. Which window shows it is the notify package's business and is tested
there; here `post` is replaced with a recorder. The sync's report is
sync_hook's, and needs a sync.
"""

from dataclasses import replace
from typing import Any

import pytest
from anki.decks import DeckId

from related_card_disperse import hooks_browser, hooks_deck_browser, hooks_review
from related_card_disperse.bury import BuryRunResult, describe_result, result_level
from related_card_disperse.logic import BrowserRunResult, RuleOutcome


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
    for module in (hooks_deck_browser, hooks_browser, hooks_review):
        monkeypatch.setattr(module, "post", lambda **kwargs: recorded.append(kwargs))
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


# The browser's run ------------------------------------------------------------------------


class StandInTable:
    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def is_notes_mode(self) -> bool:
        return True


class StandInBrowser:
    def __init__(self, notes: tuple = ()) -> None:
        self.table = StandInTable()
        self.notes = list(notes)

    def selected_notes(self) -> list:
        return self.notes


def test_the_browser_run_reports_its_summary_and_names_the_decks_it_reached(posts):
    browser = StandInBrowser()
    result = BrowserRunResult(updated=3, rule_runs=4, notes=2, buried_by_deck={"Kanji": 1})
    hooks_browser._show_result(result, browser, notes_mode=True)
    [post] = posts
    assert post["source"] == "Related Card Disperse"
    assert post["title"] == "Dispersed 3 cards over 2 notes (4 rule runs)"
    assert post["body"] == "Buried: 1 card in Kanji"
    assert (post["level"], post["timeout_ms"]) == ("success", 10000)
    # Each run is a report of its own.
    assert "key" not in post and "group" not in post
    assert browser.table.resets == 1, "the due column has moved under the table"


def test_a_cancelled_browser_run_is_not_marked_a_success(posts):
    result = BrowserRunResult(updated=1, rule_runs=1, notes=1, cancelled=True)
    hooks_browser._show_result(result, StandInBrowser(), notes_mode=True)
    [post] = posts
    assert post["title"].startswith("Cancelled after 1 card")
    assert post["level"] == "info"


def test_with_nothing_selected_the_browser_says_so_and_runs_nothing(posts, monkeypatch):
    monkeypatch.setattr(hooks_browser, "Config", FakeConfig)
    started: list[object] = []
    monkeypatch.setattr(
        hooks_browser, "run_browser_disperse_in_background", lambda *a, **k: started.append(a)
    )
    hooks_browser.run_disperse_on_browser_selection(StandInBrowser())
    assert started == []
    [post] = posts
    assert (post["title"], post["level"]) == ("No cards selected", "warning")


# An answer's report -----------------------------------------------------------------------


def test_an_answers_report_is_titled_by_what_it_dispersed(posts):
    outcomes = [RuleOutcome("Siblings: updated=2", 2), RuleOutcome("Kanji: updated=1", 1)]
    hooks_review.post_review_report(
        outcomes, outcomes, "Buried elsewhere: 1 card in Kanji", details=True
    )
    [post] = posts
    assert post["title"] == "Dispersed 3 cards (2 rule runs)"
    assert post["body"].split("<br><br>") == [
        "Siblings: updated=2",
        "Kanji: updated=1",
        "Buried elsewhere: 1 card in Kanji",
    ]
    assert (post["level"], post["timeout_ms"]) == ("success", 7000)


def test_the_compact_style_keeps_only_the_decks_reached_behind_the_title(posts):
    outcomes = [RuleOutcome("Siblings: updated=2", 2)]
    hooks_review.post_review_report(outcomes, outcomes, "", details=False)
    hooks_review.post_review_report(outcomes, outcomes, "Buried elsewhere: 1", details=False)
    assert [post["body"] for post in posts] == ["", "Buried elsewhere: 1"]


def test_every_rule_that_ran_is_counted_and_only_the_reportable_ones_are_listed(posts):
    moved, unmoved = RuleOutcome("Siblings: updated=2", 2), RuleOutcome("Kanji: updated=0", 0)
    hooks_review.post_review_report([moved, unmoved], [moved], "", details=True)
    [post] = posts
    assert post["title"] == "Dispersed 2 cards (2 rule runs)"
    assert post["body"] == "Siblings: updated=2"


def test_an_answer_that_moved_nothing_is_not_marked_a_success(posts):
    outcomes = [RuleOutcome("Siblings: updated=0", 0)]
    hooks_review.post_review_report(outcomes, outcomes, "", details=False)
    [post] = posts
    assert (post["title"], post["level"]) == ("Dispersed 0 cards (1 rule run)", "info")


def test_every_answer_reports_into_the_same_entry(posts):
    # Kept apart, a review session's worth of reports would queue up behind the cards on
    # show; one key makes each answer's report replace the last, as the tooltip did.
    outcomes = [RuleOutcome("Siblings: updated=1", 1)]
    hooks_review.post_review_report(outcomes, outcomes, "", details=False)
    hooks_review.post_review_report(outcomes, outcomes, "", details=False)
    assert [post["key"] for post in posts] == ["review", "review"]
