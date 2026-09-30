"""What the triage applier must refuse, what it writes, and what a revert may take back.

The decisions were made from a copy of the collection, so the risk is writing over a card that
has moved on since: every refusal below is a way a card can change between the copy and the
apply. Nothing here talks to Anki: a fake AnkiConnect answers from a dict of cards and records
the actions it was sent.
"""

import json
import sys
from pathlib import Path

import pytest

# See the note in test_vocab_morphology.py: the suite already has a `conftest` of its own.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anki_connect  # noqa: E402
import triage_apply as ta  # noqa: E402

SETTINGS = {"processing_deck": "Processing", "learn_deck": "Learn::Deck",
            "ignore_tag": "fsrs_ignore"}


def card(cid, nid, key="語", reading="ご", deck="Processing", type_=0, queue=0, due=5, reps=0):
    return {"cardId": cid, "note": nid, "deckName": deck, "type": type_, "queue": queue,
            "due": due, "reps": reps,
            "fields": {"vocab-key": {"value": key}, "vocab-kana": {"value": reading}}}


def decision(cid, nid, action, key="語", reading="ご", **rest):
    return {"cid": cid, "nid": nid, "key": key, "reading": reading, "action": action,
            "interval": rest.get("interval"), "learn_order": rest.get("learn_order"),
            "probability": rest.get("probability", 0.9), "wave": rest.get("wave", 1)}


class FakeAnki:
    def __init__(self, cards, tags=None, learn_deck_new=(), fail=None):
        self.cards = {c["cardId"]: c for c in cards}
        self.tags = tags or {}
        self.learn_deck_new = {c["cardId"]: c for c in learn_deck_new}
        self.fail = fail
        self.sent: list[dict] = []

    def invoke(self, action, **params):
        if action == "cardsInfo":
            every = {**self.cards, **self.learn_deck_new}
            return [every.get(c, {}) for c in params["cards"]]
        if action == "notesInfo":
            return [{"noteId": n, "tags": self.tags.get(n, [])} for n in params["notes"]]
        if action == "findCards":
            return list(self.learn_deck_new)
        if action == "multi":
            self.sent += params["actions"]
            return [{"result": None, "error": self.fail if a["action"] == self.fail_on else None}
                    for a in params["actions"]]
        raise AssertionError(action)

    fail_on = ""

    def notes_info(self, nids):
        return self.invoke("notesInfo", notes=nids)


def planned(fake, decisions):
    live = ta.cards_info(fake, [d["cid"] for d in decisions])
    tags = ta.note_tags(fake, [d["nid"] for d in decisions])
    start = ta.first_free_position(fake, SETTINGS["learn_deck"])
    return ta.plan(decisions, live, tags, SETTINGS, start)


# --- refusals: a card that has moved on since the copy ------------------------------------


@pytest.mark.parametrize("changed, why", [
    (dict(type_=2, queue=2), "no longer new"),
    (dict(queue=-1), "no longer new"),
    (dict(deck="Elsewhere"), "not the processing deck"),
    (dict(key="別"), "its key is now"),
    (dict(reading="べつ"), "its reading is now"),
])
def test_a_card_changed_since_the_copy_is_refused(changed, why):
    fake = FakeAnki([card(1, 10, **changed)])
    writes, refused = planned(fake, [decision(1, 10, "schedule", interval=100)])
    assert writes == []
    assert why in refused[0][1]


def test_a_card_already_tagged_is_refused():
    fake = FakeAnki([card(1, 10)], tags={10: ["FSRS_ignore"]})
    writes, refused = planned(fake, [decision(1, 10, "schedule", interval=100)])
    assert writes == [] and "already tagged" in refused[0][1]


def test_a_missing_card_is_refused_and_the_rest_still_planned():
    fake = FakeAnki([card(2, 20)])
    writes, refused = planned(fake, [decision(1, 10, "learn", learn_order=1),
                                     decision(2, 20, "learn", learn_order=2)])
    assert [w["cid"] for w in writes] == [2]
    assert refused[0][1] == "no such card"


# --- what gets written ---------------------------------------------------------------------


def test_learn_cards_go_after_the_learn_decks_last_new_card_in_learn_order():
    fake = FakeAnki([card(1, 10), card(2, 20), card(3, 30)],
                    learn_deck_new=[card(90, 900, deck="Learn::Deck", due=41),
                                    card(91, 910, deck="Learn::Deck", due=7)])
    writes, _ = planned(fake, [decision(1, 10, "learn", learn_order=3),
                               decision(2, 20, "learn", learn_order=1),
                               decision(3, 30, "schedule", interval=400)])
    learn = [(w["cid"], w["after"]["due"]) for w in writes if w["action"] == "learn"]
    assert learn == [(2, 42), (1, 43)]


def test_each_action_sends_what_it_should_with_a_version():
    fake = FakeAnki([card(1, 10), card(2, 20), card(3, 30)])
    writes, _ = planned(fake, [decision(1, 10, "schedule", interval=365),
                               decision(2, 20, "learn", learn_order=1),
                               decision(3, 30, "suspend")])
    by_cid = {w["cid"]: ta.write_actions(w) for w in writes}
    assert [a["action"] for a in by_cid[1]] == ["setDueDate", "addTags"]
    assert by_cid[1][0]["params"]["days"] == "365!"
    assert [a["action"] for a in by_cid[2]] == ["changeDeck", "setSpecificValueOfCard"]
    assert by_cid[2][0]["params"]["deck"] == "Learn::Deck"
    assert [a["action"] for a in by_cid[3]] == ["changeDeck", "suspend"]
    ta.multi(fake, by_cid[1])
    assert all(a["version"] == anki_connect.VERSION for a in fake.sent)


def test_the_undo_log_is_written_before_the_writes(tmp_path):
    fake = FakeAnki([card(1, 10)])
    writes, _ = planned(fake, [decision(1, 10, "schedule", interval=365)])
    undo = tmp_path / "undo.jsonl"
    fake.fail_on, fake.fail = "setDueDate", "boom"
    with pytest.raises(anki_connect.AnkiConnectError, match="setDueDate: boom"):
        ta.apply(fake, writes, undo, log=lambda _: None)
    logged = [json.loads(line) for line in undo.read_text(encoding="utf-8").splitlines()]
    assert logged[0]["cid"] == 1 and logged[0]["before"]["type"] == 0


def test_selection_takes_the_surest_waves_first():
    rows = [decision(1, 10, "learn", wave=2, probability=0.8),
            decision(2, 20, "schedule", wave=1, probability=0.95),
            decision(3, 30, "schedule", wave=1, probability=0.99),
            decision(4, 40, "suspend", wave=3, probability=0.6)]
    assert [d["cid"] for d in ta.select(rows, 2, "", 0)] == [3, 2, 1]
    assert [d["cid"] for d in ta.select(rows, 0, "schedule", 1)] == [3]


# --- revert ----------------------------------------------------------------------------------


def test_revert_takes_back_only_what_is_still_as_applied(tmp_path):
    undo = tmp_path / "undo.jsonl"
    entries = [
        {"cid": 1, "nid": 10, "key": "a", "action": "schedule",
         "before": {"deck": "Processing", "type": 0, "queue": 0, "due": 5, "reps": 0},
         "after": {"deck": "Processing", "interval": 365, "tag": "fsrs_ignore"}},
        {"cid": 2, "nid": 20, "key": "b", "action": "schedule",
         "before": {"deck": "Processing", "type": 0, "queue": 0, "due": 6, "reps": 0},
         "after": {"deck": "Processing", "interval": 400, "tag": "fsrs_ignore"}},
        {"cid": 3, "nid": 30, "key": "c", "action": "learn",
         "before": {"deck": "Processing", "type": 0, "queue": 0, "due": 7, "reps": 0},
         "after": {"deck": "Learn::Deck", "due": 50}},
    ]
    undo.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    fake = FakeAnki([card(1, 10, type_=2, queue=2, reps=0),
                     card(2, 20, type_=2, queue=2, reps=1),  # reviewed since: keep it
                     card(3, 30, deck="Learn::Deck", due=50)])
    reverted, refused = ta.revert(fake, undo)
    assert reverted == 2
    assert len(refused) == 1 and "cid 2" in refused[0]
    kept = [json.loads(line) for line in undo.read_text(encoding="utf-8").splitlines()]
    assert [e["cid"] for e in kept] == [2]
    sent = [(a["action"], a["params"].get("cards") or a["params"].get("card")) for a in fake.sent]
    assert ("forgetCards", [1]) in sent and ("changeDeck", [3]) in sent


def test_a_held_decision_is_left_out_unless_asked_for():
    rows = [decision(1, 10, "schedule", interval=100), decision(2, 20, "learn", learn_order=1)]
    rows[0]["hold"] = "the reading does not fit the sense"
    assert [d["cid"] for d in ta.select(rows, 0, "", 0)] == [2]
    assert sorted(d["cid"] for d in ta.select(rows, 0, "", 0, include_held=True)) == [1, 2]
