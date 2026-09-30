"""Applies the triage's decisions to the collection, through AnkiConnect, with Anki running.

    py -3.10 word_array/research/triage_apply.py [--wave N] [--action A] [--limit N]   # list
    py -3.10 word_array/research/triage_apply.py [--wave N] ... --apply
    py -3.10 word_array/research/triage_apply.py --revert

Reads `decisions.jsonl` (triage_decide.py). Without `--apply` it only lists what it would write,
in `reports/apply.txt`. `--wave N` takes the waves up to N (1 is the surest), `--action` one
action, `--limit` the first so many, surest first: the decisions can go in a slice at a time.
A decision held for a reported error in its note (triage_decide.py) is left out and counted,
unless `--include-held`.

What each action writes:

- schedule: a set due date `N!` (due in N days, the interval set to N, as the user's own
  scheduling did) and the ignore tag (settings `ignore_tag`), which keeps the card out of the
  FSRS parameters. The card stays in its deck: the user's CopyAnywhere definition moves it.
- learn: the card moves to the learn deck (settings `learn_deck`), still new, placed after the
  deck's last new card, in the decisions' learn order (Jiten rank).
- suspend: the card moves to the learn deck and is suspended.

A decision was made from a copy of the collection, and the collection has moved on since. So
immediately before writing, every card is read again and refused, named in the report, unless it
is still new and unsuspended, still in the processing deck, not yet tagged, and its note still
holds the key and reading the decision was made for.

`--apply` appends each card's state before the write to `apply_undo.jsonl` beside the decisions,
first, since AnkiConnect's writes bypass Anki's undo. `--revert` puts the cards back, newest
first, where each is still as the apply left it (a scheduled card not reviewed since, a moved
card still in the learn deck at its position), and keeps the entries it could not revert.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Callable, Iterable, Optional

import anki_connect
import triage_data as td

UNDO = "apply_undo.jsonl"
KEY_FIELD = "vocab-key"
READING_FIELD = "vocab-kana"
CHUNK = 200


def chunked(items: list, n: int = CHUNK) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def multi(client, actions: list[dict]) -> list:
    """Many actions in one request each CHUNK; raises on the first that failed. An action inside
    `multi` without a version is answered as version 4, which drops its error and answers None,
    so each carries the client's."""
    results = []
    for part in chunked([{**a, "version": anki_connect.VERSION} for a in actions]):
        for action, reply in zip(part, client.invoke("multi", actions=part)):
            if isinstance(reply, dict) and set(reply) == {"result", "error"}:
                if reply["error"] is not None:
                    raise anki_connect.AnkiConnectError(f"{action['action']}: {reply['error']}")
                reply = reply["result"]
            results.append(reply)
    return results


def cards_info(client, cids: list[int]) -> dict[int, dict]:
    out = {}
    for part in chunked(cids, 500):
        for info in client.invoke("cardsInfo", cards=part):
            if info and "cardId" in info:
                out[info["cardId"]] = info
    return out


def note_tags(client, nids: list[int]) -> dict[int, list[str]]:
    out = {}
    for part in chunked(nids, 500):
        for info in client.notes_info(part):
            if info:
                out[info["noteId"]] = info.get("tags", [])
    return out


def field(info: dict, name: str) -> str:
    return td.plain((info.get("fields", {}).get(name) or {}).get("value", ""))


def select(decisions: list[dict], wave: int, action: str, limit: int,
           include_held: bool = False) -> list[dict]:
    rows = [d for d in decisions if (not wave or d["wave"] <= wave)
            and (not action or d["action"] == action)
            and (include_held or not d.get("hold"))]
    rows.sort(key=lambda d: (d["wave"], -d["probability"], d["nid"]))
    return rows[:limit] if limit else rows


def refusal(d: dict, info: Optional[dict], tags: list[str], settings: dict) -> str:
    """'' when the card is as the decision found it, else what has changed."""
    if info is None:
        return "no such card"
    if info.get("note") != d["nid"]:
        return f"the card belongs to note {info.get('note')}"
    if info.get("type") != 0 or info.get("queue") != 0:
        return f"no longer new and unsuspended (type {info.get('type')}, queue {info.get('queue')})"
    if info.get("deckName") != settings["processing_deck"]:
        return f"in {info.get('deckName')!r}, not the processing deck"
    ignore = settings["ignore_tag"].lower()
    if any(t.lower() == ignore for t in tags):
        return f"already tagged {settings['ignore_tag']}"
    if field(info, KEY_FIELD) != d["key"]:
        return f"its key is now {field(info, KEY_FIELD)!r}"
    if field(info, READING_FIELD) != d["reading"]:
        return f"its reading is now {field(info, READING_FIELD)!r}"
    return ""


def first_free_position(client, deck: str) -> int:
    """One past the last new card's position in the deck."""
    cids = client.invoke("findCards", query=f'"deck:{deck}" is:new')
    infos = cards_info(client, cids) if cids else {}
    return max((i["due"] for i in infos.values() if i.get("type") == 0), default=0) + 1


def plan(decisions, live, tags, settings, start_position: int):
    """`(writes, refused)`: each write is the decision with the card's state before and after."""
    writes, refused = [], []
    position = start_position
    for d in sorted(decisions, key=lambda d: (d["action"] != "learn", d.get("learn_order") or 0)):
        info = live.get(d["cid"])
        why = refusal(d, info, tags.get(d["nid"], []), settings)
        if why:
            refused.append((d, why))
            continue
        assert info is not None
        before = {"deck": info["deckName"], "type": info["type"], "queue": info["queue"],
                  "due": info["due"], "reps": info.get("reps", 0)}
        if d["action"] == "schedule":
            after = {"deck": info["deckName"], "interval": d["interval"],
                     "tag": settings["ignore_tag"]}
        elif d["action"] == "learn":
            after = {"deck": settings["learn_deck"], "due": position}
            position += 1
        else:
            after = {"deck": settings["learn_deck"], "suspended": True}
        writes.append({"cid": d["cid"], "nid": d["nid"], "key": d["key"], "action": d["action"],
                       "before": before, "after": after})
    return writes, refused


def write_actions(w: dict) -> list[dict]:
    """The AnkiConnect actions of one card's write."""
    cid, after = w["cid"], w["after"]
    if w["action"] == "schedule":
        return [{"action": "setDueDate", "params": {"cards": [cid], "days": f"{after['interval']}!"}},
                {"action": "addTags", "params": {"notes": [w["nid"]], "tags": after["tag"]}}]
    moves = [{"action": "changeDeck", "params": {"cards": [cid], "deck": after["deck"]}}]
    if w["action"] == "learn":
        return moves + [{"action": "setSpecificValueOfCard",
                         "params": {"card": cid, "keys": ["due"], "newValues": [after["due"]],
                                    "warning_check": True}}]
    return moves + [{"action": "suspend", "params": {"cards": [cid]}}]


def apply(client, writes: list[dict], undo, log: Callable[[str], None] = print) -> int:
    """Each chunk's undo entries are on disk before any of its writes is sent."""
    done = 0
    undo.parent.mkdir(parents=True, exist_ok=True)
    with undo.open("a", encoding="utf-8") as out:
        for part in chunked(writes, 50):
            for w in part:
                out.write(json.dumps(w, ensure_ascii=False) + "\n")
            out.flush()
            multi(client, [a for w in part for a in write_actions(w)])
            done += len(part)
            log(f"  {done}/{len(writes)} written")
    return done


def still_as_applied(w: dict, info: Optional[dict], tags: list[str]) -> str:
    """'' when the card is as the apply left it, else what has changed."""
    if info is None:
        return "no such card"
    after = w["after"]
    if w["action"] == "schedule":
        if info.get("type") != 2 or info.get("reps", 0) != w["before"]["reps"]:
            return "reviewed or rescheduled since"
        return ""
    if info.get("deckName") != after["deck"]:
        return f"moved to {info.get('deckName')!r} since"
    if w["action"] == "learn" and (info.get("type") != 0 or info.get("due") != after["due"]):
        return "studied or repositioned since"
    if w["action"] == "suspend" and info.get("queue") != -1:
        return "unsuspended since"
    return ""


def revert_actions(w: dict) -> list[dict]:
    cid, before = w["cid"], w["before"]
    back = {"action": "changeDeck", "params": {"cards": [cid], "deck": before["deck"]}}
    due = {"action": "setSpecificValueOfCard",
           "params": {"card": cid, "keys": ["due"], "newValues": [before["due"]],
                      "warning_check": True}}
    if w["action"] == "schedule":
        # The user's CopyAnywhere definition may have moved it out of the processing deck
        return [{"action": "forgetCards", "params": {"cards": [cid]}}, due, back,
                {"action": "removeTags", "params": {"notes": [w["nid"]], "tags": w["after"]["tag"]}}]
    if w["action"] == "learn":
        return [back, due]
    return [{"action": "unsuspend", "params": {"cards": [cid]}}, back]


def revert(client, undo) -> tuple[int, list[str]]:
    entries = td.read_jsonl(undo)
    if not entries:
        return 0, []
    live = cards_info(client, [e["cid"] for e in entries])
    tags = note_tags(client, sorted({e["nid"] for e in entries}))
    reverted, refused, kept = 0, [], []
    for w in reversed(entries):
        why = still_as_applied(w, live.get(w["cid"]), tags.get(w["nid"], []))
        if why:
            refused.append(f"cid {w['cid']} {w['key']} ({w['action']}): {why}, left as is")
            kept.append(w)
            continue
        multi(client, revert_actions(w))
        reverted += 1
    td.write_jsonl(undo, reversed(kept))
    return reverted, refused


def report(writes, refused, settings, start_position) -> list[str]:
    counts = Counter(w["action"] for w in writes)
    lines = [f"{len(writes)} cards to write ({', '.join(f'{a} {n}' for a, n in counts.items())}),"
             f" {len(refused)} refused", ""]
    if refused:
        lines.append("--- refused: the card is not as the decision found it ---")
        for d, why in refused:
            lines.append(f"  cid {d['cid']} {d['key']} ({d['action']}): {why}")
        lines.append("")
    lines.append(f"schedule: set due date N! and tag {settings['ignore_tag']}, deck unchanged")
    lines.append(f"learn: move to {settings['learn_deck']!r}, new, from position {start_position}")
    lines.append(f"suspend: move to {settings['learn_deck']!r} and suspend")
    lines.append("")
    lines.append("--- the writes ---")
    for w in writes:
        a = w["after"]
        what = (f"due in {a['interval']} days" if w["action"] == "schedule"
                else f"position {a['due']}" if w["action"] == "learn" else "suspended")
        lines.append(f"  {w['action']:8} cid {w['cid']} {w['key']}: {what}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wave", type=int, default=0, help="waves up to this one; 0 for all")
    parser.add_argument("--action", choices=["suspend", "schedule", "learn"], default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-held", action="store_true",
                        help="also the decisions held for a reported error in the note")
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", action="store_true")
    args = parser.parse_args()

    settings = td.settings(required=("processing_deck", "learn_deck", "ignore_tag"))
    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    undo = td.data_file(UNDO)
    try:
        if args.revert:
            reverted, refused = revert(client, undo)
            print("\n".join(refused + [f"reverted {reverted} cards"]))
            return 0
        every = td.read_jsonl(td.data_file("decisions.jsonl"))
        decisions = select(every, args.wave, args.action, args.limit, args.include_held)
        held = len(select(every, args.wave, args.action, 0, True)) - len(
            select(every, args.wave, args.action, 0, False))
        if held and not args.include_held:
            print(f"{held} decisions held for a reported error in the note are left out"
                  " (reports/wrong_data.txt; --include-held to apply them anyway)")
        if not decisions:
            print("no decisions to apply: run triage_decide.py, or widen --wave/--action")
            return 1
        live = cards_info(client, [d["cid"] for d in decisions])
        tags = note_tags(client, sorted({d["nid"] for d in decisions}))
        start = first_free_position(client, settings["learn_deck"])
        writes, refused = plan(decisions, live, tags, settings, start)
        lines = report(writes, refused, settings, start)
        path = td.report_file("apply.txt")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(lines[0])
        if not args.apply:
            print(f"report in {path}")
            print("check it, then rerun with --apply")
            return 0
        done = apply(client, writes, undo)
        print(f"wrote {done} cards; undo with --revert (log in {undo})")
    except anki_connect.AnkiConnectError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
