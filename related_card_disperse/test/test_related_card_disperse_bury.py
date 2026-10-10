from related_card_disperse.core import (
    describe_rule_errors,
    merge_rule_error,
    order_session_blocks,
    select_backlog_cards_to_bury,
    select_cards_to_bury,
)


def relations(*pairs):
    """Build the symmetric neighbour map select_cards_to_bury expects."""
    out: dict[int, set[int]] = {}
    for a, b in pairs:
        out.setdefault(a, set()).add(b)
        out.setdefault(b, set()).add(a)
    return out


def test_unrelated_session_is_left_alone():
    kept, buried = select_cards_to_bury([1, 2, 3], {})
    assert kept == [1, 2, 3]
    assert buried == []


def test_the_card_shown_first_is_the_one_kept():
    kept, buried = select_cards_to_bury([1, 2], relations((1, 2)))
    assert kept == [1]
    assert buried == [2]


def test_a_group_of_three_keeps_only_its_earliest():
    pairs = relations((1, 2), (2, 3), (1, 3))
    kept, buried = select_cards_to_bury([1, 2, 3], pairs)
    assert kept == [1]
    assert buried == [2, 3]


def test_a_chain_keeps_both_ends_because_they_are_unrelated():
    """A relates to B and B to C, but A and C do not collide with each other."""
    kept, buried = select_cards_to_bury([1, 2, 3], relations((1, 2), (2, 3)))
    assert kept == [1, 3]
    assert buried == [2]


def test_running_again_on_what_survived_buries_nothing():
    pairs = relations((1, 2), (2, 3), (3, 4))
    kept, _ = select_cards_to_bury([1, 2, 3, 4], pairs)
    kept_again, buried_again = select_cards_to_bury(kept, pairs)
    assert kept_again == kept
    assert buried_again == []


def test_a_gap_lets_a_distant_pair_through():
    kept, buried = select_cards_to_bury([1, 2, 3, 4], relations((1, 4)), min_gap=3)
    assert kept == [1, 2, 3, 4]
    assert buried == []


def test_a_gap_still_buries_a_close_pair():
    kept, buried = select_cards_to_bury([1, 2, 3, 4], relations((1, 2)), min_gap=3)
    assert kept == [1, 3, 4]
    assert buried == [2]


def test_the_gap_is_measured_in_kept_cards_not_session_positions():
    """A buried card is never shown, so it cannot space anything out.

    2 is buried against 1, which leaves 3 only one *shown* card behind 1 -- so
    with a gap of 3 it collides too, even though it sat two positions away.
    """
    kept, buried = select_cards_to_bury([1, 2, 3, 4], relations((1, 2), (1, 3)), min_gap=3)
    assert kept == [1, 4]
    assert buried == [2, 3]


def test_zero_gap_means_the_whole_session():
    kept, buried = select_cards_to_bury(list(range(10)), relations((0, 9)), min_gap=0)
    assert buried == [9]
    assert kept == list(range(9))



def test_no_backlog_is_nothing_to_bury():
    assert select_backlog_cards_to_bury([]) == []


def test_the_anchor_keeps_the_day_when_it_is_itself_late():
    assert select_backlog_cards_to_bury([3, 1, 2], anchor_id=1) == [3, 2]


def test_the_longest_wait_keeps_the_day_when_the_anchor_is_not_late():
    """The anchor may be due in the future, or not in the group at all.

    ``past_due`` arrives most overdue first, so its head is the card that has
    been waiting longest.
    """
    assert select_backlog_cards_to_bury([3, 1, 2], anchor_id=9) == [1, 2]
    assert select_backlog_cards_to_bury([3, 1, 2]) == [1, 2]


def test_a_lone_backlogged_card_keeps_the_day():
    assert select_backlog_cards_to_bury([7], anchor_id=9) == []


def test_a_group_that_already_had_its_card_today_loses_the_whole_backlog():
    """The anchor was just answered, so it spent the day; nothing else may run.

    This is the case that generalises Anki's sibling burying: the card you
    answered takes the slot and every related card still due goes.
    """
    assert select_backlog_cards_to_bury([3, 1, 2], anchor_id=9, slot_taken=True) == [3, 1, 2]


def test_a_spent_day_beats_even_a_late_anchor():
    assert select_backlog_cards_to_bury([3, 1], anchor_id=1, slot_taken=True) == [3, 1]


def test_the_anchor_block_leads_even_when_its_name_sorts_last():
    blocks = [("Zoology", [1, 2]), ("Anatomy", [3, 4])]
    assert order_session_blocks(blocks, "Zoology") == [1, 2, 3, 4]


def test_the_other_blocks_follow_in_name_order():
    blocks = [("Zoology", [3]), ("Anatomy", [2]), ("Botany", [1])]
    assert order_session_blocks(blocks, "Botany") == [1, 2, 3]


def test_a_missing_anchor_still_orders_the_rest():
    """The deck the run started from may simply have nothing due today."""
    blocks = [("Zoology", [2]), ("Anatomy", [1])]
    assert order_session_blocks(blocks, "Geology") == [1, 2]


def test_a_card_in_two_blocks_is_kept_once_in_the_first():
    blocks = [("Anchor", [1, 2]), ("Other", [2, 3])]
    assert order_session_blocks(blocks, "Anchor") == [1, 2, 3]


def test_empty_blocks_are_dropped():
    blocks = [("Empty", []), ("Anchor", [1])]
    assert order_session_blocks(blocks, "Anchor") == [1]


def test_the_non_anchor_deck_is_the_one_buried():
    """The regression test for the whole cross-deck bug.

    Two top-level decks, one relation running between them. Before the session
    became the union of every deck's day, the relation was resolved and then
    dropped for pointing outside the anchor deck, and nothing was buried. Now it
    collides -- and because the anchor block leads, the card that goes is the
    one in the deck the user was *not* about to sit down with.
    """
    session = order_session_blocks([("JP vocab", [10]), ("JP write", [20])], "JP vocab")
    kept, buried = select_cards_to_bury(session, relations((10, 20)))
    assert kept == [10]
    assert buried == [20]


def test_a_rule_error_is_recorded_once_with_a_count():
    errors: dict[str, tuple[str, int]] = {}
    merge_rule_error(errors, "Reversible", "__import__ not found")
    merge_rule_error(errors, "Reversible", "__import__ not found")
    merge_rule_error(errors, "Other", "boom")
    assert errors == {"Reversible": ("__import__ not found", 2), "Other": ("boom", 1)}


def test_the_first_message_is_the_one_kept():
    errors: dict[str, tuple[str, int]] = {}
    merge_rule_error(errors, "Reversible", "first")
    merge_rule_error(errors, "Reversible", "second")
    assert errors["Reversible"] == ("first", 2)


def test_rule_errors_read_as_one_line_per_rule():
    text = describe_rule_errors({"Reversible": ("__import__ not found", 36)})
    assert text == "Rule errors: Reversible (__import__ not found) on 36 cards"


def test_a_single_failed_card_is_not_pluralised():
    assert describe_rule_errors({"R": ("boom", 1)}) == "Rule errors: R (boom) on 1 card"
