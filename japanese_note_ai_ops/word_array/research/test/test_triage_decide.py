"""The triage's decision rule: a hand label wins over the model, a schedule comes due at a flat
rate with the least sure soonest, and the words to learn go commonest first. Nothing here reads
the user's data.
"""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import triage_decide as tdc  # noqa: E402


def args(**over):
    base = dict(suspend_min=0.8, learn_line=0.5, min_days=30, max_days=1600, waves=[0.9, 0.75])
    base.update(over)
    return argparse.Namespace(**base)


def word(nid, deck="Processing", type_=0, queue=0):
    return {"nid": nid, "cid": nid * 10, "key": f"w{nid}", "reading": "よみ", "deck": deck,
            "type": type_, "queue": queue}


def pred(nid, p):
    return {"nid": nid, "p": p, "p_known": 1 - p[0], "p_suspend": p[3], "for": [], "against": []}


def test_spread_is_flat_and_inside_the_bounds():
    days = tdc.spread(1571, 30, 1600)
    assert min(days) >= 30 and max(days) <= 1600
    assert len(set(days)) == len(days)  # one per day: as many due each day
    assert tdc.spread(0, 30, 1600) == []


def test_hand_labels_win_and_the_least_sure_schedule_comes_due_first():
    words = [word(1), word(2), word(3), word(4), word(5, deck="Elsewhere")]
    preds = {n: pred(n, p) for n, p in [
        (1, [0.05, 0.05, 0.3, 0.6]),   # very likely known
        (2, [0.3, 0.4, 0.25, 0.05]),   # less sure
        (3, [0.9, 0.05, 0.03, 0.02]),  # the model would learn it...
        (4, [0.02, 0.03, 0.05, 0.9]),  # sure suspend
        (5, [0.5, 0.2, 0.2, 0.1]),
    ]}
    hand = {3: {"nid": 3, "label": "schedule", "kind": "random"}}  # ...but the user knows it
    rows, out = tdc.decide(words, preds, hand, {}, {}, {"processing_deck": "Processing"}, args())
    by = {r["nid"]: r for r in rows}
    assert out["not in the processing deck"] == 1 and 5 not in by
    assert by[3]["action"] == "schedule" and by[3]["source"] == "hand" and by[3]["wave"] == 1
    assert by[4]["action"] == "suspend"
    assert by[2]["interval"] < by[1]["interval"]


def test_learn_order_follows_jiten_then_the_other_lists():
    words = [word(1), word(2), word(3)]
    preds = {n: pred(n, [0.9, 0.05, 0.03, 0.02]) for n in (1, 2, 3)}
    freq = {1: {"freq_tubelex_rank": 10}, 2: {"freq_jiten_rank": 5000}, 3: {}}
    rows, _ = tdc.decide(words, preds, {}, {}, freq, {"processing_deck": "Processing"}, args())
    assert sorted((r["learn_order"], r["nid"]) for r in rows) == [(1, 2), (2, 1), (3, 3)]


@pytest.mark.parametrize("p, bounds, expected", [
    ([0.1, 0.2, 0.3, 0.4], None, 2.0),
    ([0.5, 0.25, 0.25, 0.0], (1, 2), 1.5),
])
def test_expected_level(p, bounds, expected):
    assert tdc.expected_level(p, bounds) == pytest.approx(expected)


def test_wrong_data_gets_no_decision_and_a_reported_error_holds_one():
    words = [word(1), word(2), word(3)]
    preds = {n: pred(n, [0.1, 0.3, 0.4, 0.2]) for n in (1, 2, 3)}
    wrong = {1: {"nid": 1, "label": "invalid", "note": "reading should be ご"}}
    issues = {2: ["the reading とばす does not fit 'to kill'"]}
    rows, out = tdc.decide(words, preds, {}, {}, {}, {"processing_deck": "Processing"}, args(),
                           wrong, issues)
    by = {r["nid"]: r for r in rows}
    assert 1 not in by and out["marked wrong data"] == 1
    assert by[2]["hold"] and not by[3]["hold"]


def test_a_marked_note_gone_from_the_collection_is_listed_apart():
    words = {1: word(1)}
    wrong = {1: {"nid": 1, "label": "invalid", "note": "reading is wrong"},
             2: {"nid": 2, "label": "invalid", "note": "not a word (bad split)"}}
    lines = tdc.wrong_data_lines(words, wrong, {})
    assert lines[0].startswith("1 notes marked Wrong data by hand")
    gone = lines.index("--- marked, but the note is gone from the collection (1) ---")
    assert lines[gone + 1] == "  nid 2 ? (?): not a word (bad split)"
