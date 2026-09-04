from related_card_disperse.core import (
    cap_card_ids,
    dedupe_preserve_order,
    group_overlapping_sets,
    normalize_card_id_result,
    split_note_type_names,
    summarize_outcome,
)


def test_normalize_card_id_result_int_and_digit_string():
    assert normalize_card_id_result(123) == [123]
    assert normalize_card_id_result("456") == [456]


def test_normalize_card_id_result_iterable_of_ids():
    assert normalize_card_id_result([1, "2", 3]) == [1, 2, 3]


def test_dedupe_preserve_order():
    assert dedupe_preserve_order([3, 2, 3, 1, 2]) == [3, 2, 1]


def test_split_note_type_names():
    assert split_note_type_names('"Basic", "Cloze"') == ["Basic", "Cloze"]
    assert split_note_type_names("") == []


def test_cap_card_ids():
    assert cap_card_ids([1, 2, 3], 2) == ([1, 2], 1)
    assert cap_card_ids([1], 5) == ([1], 0)


def test_group_overlapping_sets_merges_transitively():
    groups = [{1, 2}, {2, 3}, {9}, {10, 11}, {11, 12}]
    merged = group_overlapping_sets(groups)
    assert {frozenset(g) for g in merged} == {
        frozenset({1, 2, 3}),
        frozenset({9}),
        frozenset({10, 11, 12}),
    }


def test_summarize_outcome_shape():
    text = summarize_outcome("Rule A", 10, 3, 2, 5, "dispersed")
    assert text == "Rule A: candidates=10, filtered=3, capped=2, updated=5, outcome=dispersed"
