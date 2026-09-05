from related_card_disperse.core import select_cards_to_bury


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

