from related_card_disperse.configuration import default_rule
from related_card_disperse.core import remaining_note_cards
from related_card_disperse.logic import plan_note_rule_runs


def make_rule(**overrides):
    rule = default_rule()
    rule.update(overrides)
    return rule


def test_remaining_drops_the_anchor_and_everything_the_query_found():
    assert remaining_note_cards([1, 2, 3, 4], 1, [1, 2, 3]) == [4]


def test_remaining_drops_the_anchor_even_when_the_query_missed_it():
    """Without this the queue never shrinks and the caller loops forever."""
    assert remaining_note_cards([1, 2, 3], 1, [7, 8]) == [2, 3]


def test_remaining_ignores_covered_ids_from_other_notes():
    assert remaining_note_cards([1, 2], 1, [1, 99]) == [2]


def test_sibling_query_covers_the_note_in_one_run():
    remaining = [1, 2, 3]
    runs = []
    while remaining:
        anchor = remaining[0]
        runs.append(anchor)
        # A `nid:` query finds every card of the note from any of them.
        remaining = remaining_note_cards(remaining, anchor, [1, 2, 3])
    assert runs == [1]


def test_query_covering_only_some_cards_recurses_over_the_rest():
    covered_by = {1: [1, 2], 3: [3, 4]}
    remaining = [1, 2, 3, 4]
    runs = []
    while remaining:
        anchor = remaining[0]
        runs.append(anchor)
        remaining = remaining_note_cards(remaining, anchor, covered_by.get(anchor, []))
    assert runs == [1, 3]


def test_query_covering_nothing_still_terminates_one_card_at_a_time():
    remaining = [1, 2, 3]
    runs = []
    while remaining:
        anchor = remaining[0]
        runs.append(anchor)
        remaining = remaining_note_cards(remaining, anchor, [])
    assert runs == [1, 2, 3]


def test_plan_gives_an_untargeted_rule_the_whole_note_as_one_queue():
    rule = make_rule(target_note_types='"Basic"')
    runs = plan_note_rule_runs([rule], "Basic", {1: "Card 1", 2: "Card 2"}, [1, 2])
    assert len(runs) == 1
    assert runs[0].card_ids == [1, 2]
    assert runs[0].per_card is False


def test_plan_runs_a_card_targeted_rule_per_targeted_card():
    """User error to point two card types at one group, but honour it anyway."""
    rule = make_rule(
        target_note_types='"Basic"',
        target_card_types='"Basic<::>Card 1", "Basic<::>Card 2"',
    )
    runs = plan_note_rule_runs([rule], "Basic", {1: "Card 1", 2: "Card 2", 3: "Card 3"}, [1, 2, 3])
    assert len(runs) == 1
    assert runs[0].card_ids == [1, 2]
    assert runs[0].per_card is True


def test_plan_skips_a_rule_no_card_of_the_note_matches():
    rule = make_rule(target_note_types='"Basic"', target_card_types='"Basic<::>Card 9"')
    assert plan_note_rule_runs([rule], "Basic", {1: "Card 1"}, [1]) == []


def test_plan_skips_rules_for_other_note_types_and_disabled_ones():
    other = make_rule(target_note_types='"Other"')
    disabled = make_rule(target_note_types='"Basic"', enabled=False)
    assert plan_note_rule_runs([other, disabled], "Basic", {1: "Card 1"}, [1]) == []


def test_plan_ignores_the_automatic_trigger_flags():
    """An explicit browser run is neither a review nor a sync."""
    rule = make_rule(target_note_types='"Basic"', on_review=False, on_sync=False)
    runs = plan_note_rule_runs([rule], "Basic", {1: "Card 1"}, [1])
    assert [run.rule for run in runs] == [rule]


def test_plan_keeps_rule_order_and_card_order():
    first = make_rule(name="first", target_note_types='"Basic"')
    second = make_rule(
        name="second",
        target_note_types='"Basic"',
        target_card_types='"Basic<::>Card 2"',
    )
    runs = plan_note_rule_runs([first, second], "Basic", {1: "Card 1", 2: "Card 2"}, [1, 2])
    assert [run.rule["name"] for run in runs] == ["first", "second"]
    assert runs[0].card_ids == [1, 2]
    assert runs[1].card_ids == [2]


class FakeNote:
    def note_type(self):
        return {"name": "Basic"}


class FakeCard:
    def __init__(self, cid, card_type_name):
        self.id = cid
        self.card_type_name = card_type_name

    def note(self):
        return FakeNote()


def run_note(monkeypatch, cards, rules, covered_by, show_unchanged=True):
    """Drive _disperse_browser_note over a fake note, returning the anchors used.

    ``covered_by`` maps an anchor card id to the ids its rule's query returned.
    """
    from related_card_disperse import logic

    by_id = {card.id: card for card in cards}
    monkeypatch.setattr(logic.mw.col, "card_ids_of_note", lambda nid: list(by_id))
    monkeypatch.setattr(logic.mw.col, "get_card", lambda cid: by_id[cid])
    monkeypatch.setattr(logic, "card_type_name_for", lambda card: card.card_type_name)

    anchors = []

    def fake_run(rule, card, _config, _stats, _undo, _processed):
        anchors.append((rule["name"], card.id))
        return logic.RuleOutcome("ran", 1, list(covered_by.get(card.id, [])))

    monkeypatch.setattr(logic, "run_rule_for_reviewed_card", fake_run)

    class FakeConfig:
        show_unchanged_outcome = show_unchanged

        def __init__(self, rules):
            self.rules = rules

        def rules_for_model(self, _model):
            # The default sibling toggle is off here: only the rules given run.
            return self.rules

    result = logic.BrowserRunResult()
    logic._disperse_browser_note(1, FakeConfig(rules), {}, 0, set(), result)
    return anchors, result


def test_note_run_anchors_once_when_the_query_covers_every_card(monkeypatch):
    cards = [FakeCard(1, "Card 1"), FakeCard(2, "Card 2"), FakeCard(3, "Card 3")]
    rule = make_rule(name="all", target_note_types='"Basic"')
    anchors, result = run_note(monkeypatch, cards, [rule], {1: [1, 2, 3]})
    assert anchors == [("all", 1)]
    assert result.notes == 1
    assert result.rule_runs == 1


def test_note_run_recurses_over_the_cards_the_query_left_out(monkeypatch):
    cards = [FakeCard(1, "Card 1"), FakeCard(2, "Card 2"), FakeCard(3, "Card 3")]
    rule = make_rule(name="all", target_note_types='"Basic"')
    anchors, _ = run_note(monkeypatch, cards, [rule], {1: [1, 2], 3: [3]})
    assert anchors == [("all", 1), ("all", 3)]


def test_note_run_anchors_every_card_of_a_card_targeted_rule(monkeypatch):
    """Even a query that covers both: the config asked for a run per card type."""
    cards = [FakeCard(1, "Card 1"), FakeCard(2, "Card 2"), FakeCard(3, "Card 3")]
    rule = make_rule(
        name="targeted",
        target_note_types='"Basic"',
        target_card_types='"Basic<::>Card 1", "Basic<::>Card 2"',
    )
    anchors, _ = run_note(monkeypatch, cards, [rule], {1: [1, 2, 3], 2: [1, 2, 3]})
    assert anchors == [("targeted", 1), ("targeted", 2)]


def test_note_run_keeps_rules_independent_of_each_other(monkeypatch):
    """One rule covering the note says nothing about what another rule covers."""
    cards = [FakeCard(1, "Card 1"), FakeCard(2, "Card 2")]
    broad = make_rule(name="broad", target_note_types='"Basic"')
    narrow = make_rule(name="narrow", target_note_types='"Basic"')
    anchors, _ = run_note(monkeypatch, cards, [broad, narrow], {1: [1, 2]})
    assert anchors == [("broad", 1), ("narrow", 1)]


def test_note_run_does_nothing_when_no_rule_matches(monkeypatch):
    cards = [FakeCard(1, "Card 1")]
    rule = make_rule(name="other", target_note_types='"Other"')
    anchors, result = run_note(monkeypatch, cards, [rule], {})
    assert anchors == []
    assert result.notes == 0
