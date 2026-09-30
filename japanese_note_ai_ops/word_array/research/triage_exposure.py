"""How often the user has met each triage word in sentences they have reviewed.

    py -3.10 word_array/research/triage_exposure.py

Every vocab note's word array links each word of its sentence to the vocab note it was matched
to (`[id]` or `[id, quality]` in the element's match data), so the notes whose arrays link a
triage note are the sentences it appears in; the note's own `sentence-nids` adds those the
matching recorded there. Per triage note: how many such sentences, how many of them the user has
reviewed, how many passing answers those reviews were, how long since the first and the last,
and the best recall among them today. Meeting a word in a studied sentence does not prove it is
known (the card tests the sentence's own target word), which is why these are features for the
model rather than a rule. Writes `exposure.jsonl` and `reports/exposure.txt`, the latter with the
labelled notes' exposure by label.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from collections import defaultdict

import triage_data as td
from _bootstrap import load

match_flags = load("match_flags")

ARRAY_FIELD = "sentence-vocab-list"
# About 1,200 arrays hold a compound whose own match data was written as nothing (`"noun", ,
# [`), which is no JSON; read as `[]` here, counted, and left for a repair of their own
EMPTY_SLOT_RE = re.compile(r'(",\s*),(\s*\[)')


def read_array(field: str, problems: dict) -> list:
    arr, _ = match_flags.read_word_array(field)
    if arr is None:
        arr, _ = match_flags.read_word_array(EMPTY_SLOT_RE.sub(r"\1[],\2", field))
        problems["empty match slot" if arr is not None else "unreadable"] += 1
    return arr or []


def links(notes: dict[int, td.VocabNote], problems: dict) -> dict[int, dict[int, int]]:
    """target note id -> {sentence note id: best match quality (0 when unrated)}."""
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for nid, note in notes.items():
        field = note.get(ARRAY_FIELD)
        if not field.strip():
            continue
        arr = read_array(field, problems)
        for _, element in match_flags.iter_words(arr):
            if len(element) < 5 or not element[4] or not isinstance(element[4][0], int):
                continue
            target = element[4][0]
            if target <= 0 or target == nid:
                continue
            quality = element[4][1] if len(element[4]) > 1 and isinstance(element[4][1], int) else 0
            out[target][nid] = max(out[target].get(nid, 0), quality)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    td.add_settings_args(parser)
    args = parser.parse_args()
    settings = td.settings(args)
    now = time.time()
    col = td.open_collection(settings["collection"])
    try:
        notes = td.load_vocab_notes(col, settings["note_type"])
        cids = [c.cid for n in notes.values() for c in n.cards if c.reps > 0]
        revlog = td.load_revlog(col, cids)
    finally:
        col.close()
    problems: dict = defaultdict(int)
    linked = links(notes, problems)
    labels = {r["nid"]: r["label"] for r in td.read_jsonl(td.data_file("labels.jsonl"))}

    rows: list[dict] = []
    for nid, note in sorted(notes.items()):
        if not note.has_tag(td.NEW_WORD_TAG):
            continue
        sentences = dict(linked.get(nid, {}))
        for other in (int(x) for x in note.get("sentence-nids").split(",") if x.strip().isdigit()):
            if other in notes and other != nid:
                sentences.setdefault(other, 0)
        reviewed, passes, first, last, best_r = 0, 0, None, None, 0.0
        for sid in sentences:
            for card in notes[sid].cards:
                if card.reps == 0:
                    continue
                reviewed += 1
                answers = [r for r in revlog.get(card.cid, [])
                           if r.ease > 0 and r.type <= td.REVLOG_FILTERED]
                passes += sum(r.ease > 1 for r in answers)
                if answers:
                    first = min(first or answers[0].id, answers[0].id)
                    last = max(last or answers[-1].id, answers[-1].id)
                r = td.retrievability(card, now)
                if r is not None:
                    best_r = max(best_r, r)
        rows.append({
            "nid": nid,
            "exp_sentences": len(sentences),
            "exp_reviewed": reviewed,
            "exp_passes": passes,
            "exp_first_days": round((now - first / 1000) / 86400) if first else None,
            "exp_last_days": round((now - last / 1000) / 86400) if last else None,
            "exp_best_r": round(best_r, 3),
            "exp_quality": max(sentences.values()) if sentences else 0,
        })
    td.write_jsonl(td.data_file("exposure.jsonl"), rows)

    lines = [f"{len(rows)} triage notes; {sum(r['exp_sentences'] > 0 for r in rows)} appear in a"
             f" sentence of another note, {sum(r['exp_reviewed'] > 0 for r in rows)} in one the"
             " user has reviewed",
             "broken word arrays: " + (", ".join(f"{k} {v}" for k, v in problems.items())
                                        or "none"), ""]
    lines.append("--- reviewed sentences containing the word ---")
    buckets = [(0, 0), (1, 1), (2, 2), (3, 5), (6, 10), (11, 10**9)]
    for lo, hi in buckets:
        n = sum(lo <= r["exp_reviewed"] <= hi for r in rows)
        lines.append(f"  {lo}-{hi if hi < 10**9 else '':<3} {n:>6}")
    lines.append("")
    lines.append("--- by label: share met in a reviewed sentence, median passing answers there ---")
    for label in ("suspend", "schedule", "learn", None):
        got = [r for r in rows if labels.get(r["nid"]) == label]
        if not got:
            continue
        seen = [r for r in got if r["exp_reviewed"]]
        lines.append(f"  {label or 'unjudged':9} {len(got):>6} notes: {100 * len(seen) / len(got):>5.1f}%"
                     f" met; median passes {statistics.median([r['exp_passes'] for r in got]):.0f},"
                     f" mean {sum(r['exp_passes'] for r in got) / len(got):.1f}")
    path = td.report_file("exposure.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
