"""A triage word's family in the word arrays: the linked words above it and below it, each
sentence counted once, and each relative's card state read as triage_labels.py reads a judged
note's. Nothing here reads the user's data: the arrays and notes are made up."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import triage_data as td  # noqa: E402
import triage_family as tfam  # noqa: E402
import triage_features as tf  # noqa: E402
import triage_judge as tj  # noqa: E402


def element(text, nid=None, parts=()):
    return [text, "noun", text, "よみ", [nid, 5] if nid else ["dontmatch"], list(parts)]


# 様に成る (200) over 様に (201) over 様 (202) and に; 成る (203) beside 様に; then 雨 (204)
SENTENCE = [
    element("様に成る", 200, [element("様に", 201, [element("様", 202), element("に")]),
                            element("成る", 203)]),
    ["。"],
    element("雨", 204),
]


def test_parents_and_parts_reach_every_level_with_the_fewest_levels_between():
    found = tfam.families([SENTENCE])
    top = found[200]
    assert {n: r.gap for n, r in top.parts.items()} == {201: 1, 202: 2, 203: 1}
    assert not top.parents and len(top.top) == 1
    leaf = found[202]
    assert {n: r.gap for n, r in leaf.parents.items()} == {201: 1, 200: 2}
    assert not leaf.parts and not leaf.top
    assert not found[204].parents and not found[204].parts


def test_the_same_sentence_in_two_notes_counts_once_and_others_add_up():
    other = [element("様", 202), element("だ")]
    found = tfam.families([SENTENCE, SENTENCE, other])
    assert len(found[202].sentences) == 2
    assert len(found[202].top) == 1  # only `other` has it at the top
    assert len(found[202].parents[201].sentences) == 1


def card(new=False, suspended=False):
    return td.Card(1, 1, 1, 0 if new else 2, -1 if suspended else (0 if new else 2), 0, 0,
                   0 if new else 3, 0, 0, {})


def note(nid, tags=(), *cards):
    return td.VocabNote(nid, 1, list(tags), {"vocab-key": f"w{nid}", "vocab-kana": "よみ",
                                             "vocab-translation": "a <b>word</b>"}, list(cards))


def test_a_relatives_state_is_read_as_the_labels_read_a_judged_note():
    assert tfam.state(note(1, (), card(suspended=True)), "fsrs_ignore") == tfam.SUSPENDED
    assert tfam.state(note(1, ("FSRS_ignore",), card()), "fsrs_ignore") == tfam.SCHEDULED
    assert tfam.state(note(1, (), card()), "fsrs_ignore") == tfam.STUDIED
    assert tfam.state(note(1, (), card(new=True)), "fsrs_ignore") == tfam.NEW


def test_rows_describe_each_relative_and_skip_links_to_no_note():
    notes = {200: note(200, (), card()),
             201: note(201, (td.NEW_WORD_TAG,), card(new=True)),
             202: note(202, (td.NEW_WORD_TAG,), card(new=True))}
    rows = {r["nid"]: r for r in tfam.family_rows(notes, tfam.families([SENTENCE]), "fsrs_ignore")}
    assert set(rows) == {201, 202}  # the tagged ones
    parents = rows[202]["parents"]
    assert [(p["nid"], p["state"], p["gap"]) for p in parents] == [(201, "new", 1),
                                                                    (200, "studied", 2)]
    assert parents[1]["meaning"] == "a word" and parents[0]["tagged"]
    # 203 is linked below 201's parent, not below 201; 202 is; a link to no note is left out
    assert [p["nid"] for p in rows[201]["parts"]] == [202]
    assert [p["nid"] for p in rows[201]["parents"]] == [200]


def relative(nid, state, tagged=False):
    return {"nid": nid, "key": f"w{nid}", "reading": "よみ", "meaning": "m" * 100,
            "state": state, "tagged": tagged, "sentences": 1, "gap": 1}


def test_features_count_the_known_relatives_and_take_their_best_recall():
    family = {"nid": 1, "sentences": 4, "top_level": 3,
              "parts": [relative(10, "studied"), relative(11, "scheduled")],
              "parents": [relative(12, "new", tagged=True)]}
    recall = {10: {"r_cal": 0.7}, 11: {"r_cal": 0.95}}
    row = tf.family_features(family, recall)
    assert row["fam_parts"] == 2 and row["fam_parts_known"] == 2
    assert row["fam_parts_all_known"] == 1.0 and row["fam_parts_known_share"] == 1.0
    assert row["fam_parts_best_recall"] == 0.95
    assert row["fam_top_share"] == 0.75
    assert row["fam_parents"] == 1 and row["fam_parent_known"] == 0.0
    assert math.isnan(row["fam_parent_best_recall"])
    missing = tf.family_features(None, recall)
    assert missing["fam_missing"] == 1.0 and math.isnan(missing["fam_parts"])
    assert set(missing) == set(row)


def test_the_page_lists_the_nearest_relatives_with_their_state():
    word = {"nid": 1, "key": "様", "markers": [], "word": "様", "kanjified": "様",
            "reading": "よう", "pos": "Noun", "meaning": "m", "meaning_jp": "", "sentence": "",
            "sentence_translation": "", "siblings": []}
    family = {"parents": [relative(n, "studied") for n in range(10, 20)], "parts": []}
    shown = tj.item(word, "random", None, family)
    assert len(shown["parents"]) == tj.FAMILY_SHOWN and shown["parts"] == []
    assert shown["parents"][0] == {"key": "w10", "reading": "よみ", "meaning": "m" * 80,
                                   "state": "studied", "tagged": False}
    assert tj.item(word, "random")["parents"] == []
