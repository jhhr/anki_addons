from related_card_disperse.configuration import default_rule
from related_card_disperse.logic import get_applicable_rules


def make_rule(**overrides):
    rule = default_rule()
    rule.update(overrides)
    return rule


def test_no_card_targets_matches_every_card_type():
    """How every rule written before card type targeting existed behaves."""
    rule = make_rule(target_note_types='"Basic"')
    for card_type in ("Card 1", "Card 2", ""):
        assert get_applicable_rules([rule], "Basic", card_type) == [rule]


def test_card_targets_gate_the_rule():
    rule = make_rule(target_note_types='"Basic"', target_card_types='"Basic<::>Card 1"')
    assert get_applicable_rules([rule], "Basic", "Card 1") == [rule]
    assert get_applicable_rules([rule], "Basic", "Card 2") == []


def test_card_targets_are_qualified_by_note_type():
    """A card type name of the same spelling in another note type is not a match."""
    rule = make_rule(
        target_note_types='"Basic", "Reverse"',
        target_card_types='"Basic<::>Card 1"',
    )
    assert get_applicable_rules([rule], "Basic", "Card 1") == [rule]
    assert get_applicable_rules([rule], "Reverse", "Card 1") == []


def test_note_type_still_gates_first():
    rule = make_rule(target_note_types='"Basic"', target_card_types='"Other<::>Card 1"')
    assert get_applicable_rules([rule], "Other", "Card 1") == []


def test_disabled_and_trigger_flags_still_apply():
    disabled = make_rule(target_note_types='"Basic"', enabled=False)
    review_only = make_rule(target_note_types='"Basic"', on_sync=False)
    assert get_applicable_rules([disabled], "Basic", "Card 1") == []
    assert get_applicable_rules([review_only], "Basic", "Card 1", on_review=True) == [review_only]
    assert get_applicable_rules([review_only], "Basic", "Card 1", on_sync=True) == []


def test_sibling_rules_on_one_note_type_split_by_card_type():
    """The A-shaped way to give one note type two different queries."""
    front = make_rule(
        name="front",
        target_note_types='"Basic"',
        target_card_types='"Basic<::>Card 1"',
    )
    back = make_rule(
        name="back",
        target_note_types='"Basic"',
        target_card_types='"Basic<::>Card 2"',
    )
    assert get_applicable_rules([front, back], "Basic", "Card 2") == [back]
