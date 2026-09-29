"""Which kanji the user knows, from their kanji drawing notes.

    py -3.10 word_array/research/triage_kanji.py

The kanji note type (settings `kanji_note_type`) has one note per kanji and one card, drawing it
from its keyword. A kanji the user can draw they can read; one they have not studied they may
still read, which the triage model has to learn from the labels rather than be told. Per kanji:
whether it was studied, its recall today (FSRS's, and recalibrated on the kanji cards' own
reviews of the last `--years`, as triage_retention.py does for the vocab), its lapses, and the
kanji's own newspaper frequency rank from the note. Writes `kanji_recall.jsonl` and
`reports/kanji.txt`, with how much of the triage words' kanji the studied set covers.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter

import triage_data as td
from triage_retention import calibrated, fit_calibration, histories, predictions

KANJI_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--years", type=float, default=2.0)
    td.add_settings_args(parser)
    args = parser.parse_args()
    settings = td.settings(args)
    now = time.time()
    since = now - args.years * 365.25 * 86400

    col = td.open_collection(settings["collection"])
    try:
        model = col.models.by_name(settings["kanji_note_type"])
        if model is None:
            sys.exit(f"no note type {settings['kanji_note_type']!r}")
        names = [f["name"] for f in model["flds"]]
        kanji_notes = {nid: dict(zip(names, flds.split("\x1f")))
                       for nid, flds in col.db.all("select id, flds from notes where mid = ?",
                                                   model["id"])}
        cards = td.load_cards(col, kanji_notes)
        reviewed = [c for cs in cards.values() for c in cs if c.reps > 0]
        entries = histories(col, [c.cid for c in reviewed])
        words = td.read_jsonl(td.data_file("words.jsonl"))
    finally:
        col.close()

    decay = {c.cid: c.decay for c in reviewed}
    preds = [p for cid, es in entries.items() for p in predictions(cid, es, decay[cid])[0]]
    recent = [p for p in preds if p.t >= since]
    cal = fit_calibration(recent) if len(recent) > 200 else None

    rows = []
    for nid, fields in kanji_notes.items():
        char = td.plain(fields.get("Kanji", ""))
        if len(char) != 1:
            continue
        freq = td.plain(fields.get("Frequency", ""))
        card = (cards.get(nid) or [None])[0]
        row = {"kanji": char, "nid": nid, "freq_rank": int(freq) if freq.isdigit() else None,
               "studied": bool(card and card.reps > 0), "suspended": bool(card and card.suspended),
               "r": None, "r_cal": None, "lapses": card.lapses if card else 0,
               "s": card.stability if card else None}
        if card is not None and card.reps > 0:
            r = td.retrievability(card, now)
            if r is not None and card.last_review_s:
                elapsed = (now - card.last_review_s) / 86400
                row["r"] = round(r, 4)
                row["r_cal"] = round(calibrated(cal, r, elapsed / max(card.ivl, 1)), 4) \
                    if cal else row["r"]
        rows.append(row)
    td.write_jsonl(td.data_file("kanji_recall.jsonl"), rows)

    by_char = {r["kanji"]: r for r in rows}
    studied = [r for r in rows if r["studied"]]
    lines = [f"{len(rows)} kanji notes, {len(studied)} studied"
             f" ({sum(r['suspended'] for r in studied)} of them suspended now)", ""]
    if cal:
        lines.append(f"kanji reviews in the last {args.years:g} years: {len(recent)};"
                     f" FSRS predicted {100 * sum(p.r for p in recent) / len(recent):.1f}%,"
                     f" passed {100 * sum(p.passed for p in recent) / len(recent):.1f}%")
        lines.append(f"  recalibration: {cal['intercept']:.3f} + {cal['logit_r']:.3f} logit(R)"
                     f" + {cal['log_overdue']:.3f} log(overdue)")
    known = [r for r in studied if r["r_cal"] is not None]
    lines.append(f"expected drawable today: FSRS {sum(r['r'] for r in known):.0f},"
                 f" recalibrated {sum(r['r_cal'] for r in known):.0f} of {len(known)}")
    lines.append("")
    lines.append("--- studied share and recall by the kanji's frequency rank ---")
    bands = [(1, 500), (501, 1000), (1001, 1500), (1501, 2000), (2001, 2500), (2501, 99999)]
    for lo, hi in bands:
        got = [r for r in rows if r["freq_rank"] and lo <= r["freq_rank"] <= hi]
        st = [r for r in got if r["r_cal"] is not None]
        if got:
            lines.append(f"  rank {lo:>4}-{hi if hi < 99999 else '':<5} {len(got):>5} kanji,"
                         f" {100 * len(st) / len(got):>5.1f}% studied, mean recall of studied"
                         f" {100 * sum(r['r_cal'] for r in st) / max(len(st), 1):.0f}%")
    no_rank = [r for r in rows if not r["freq_rank"]]
    lines.append(f"  no rank     {len(no_rank):>5} kanji,"
                 f" {100 * sum(r['studied'] for r in no_rank) / max(len(no_rank), 1):.1f}% studied")
    lines.append("")

    lines.append("--- the triage words' kanji ---")
    cover = Counter()
    unseen: Counter = Counter()
    for w in words:
        chars = KANJI_RE.findall(w["kanjified"] if not w["ignore_kanjified"] else w["word"])
        if not chars:
            cover["no kanji"] += 1
            continue
        state = [by_char.get(c) for c in chars]
        if all(s and s["studied"] for s in state):
            cover["every kanji studied"] += 1
        elif any(s and s["studied"] for s in state):
            cover["some studied"] += 1
        else:
            cover["none studied"] += 1
        for c, s in zip(chars, state):
            if not (s and s["studied"]):
                unseen[c] += 1
    for k, v in cover.most_common():
        lines.append(f"  {k:22} {v:>6}")
    lines.append(f"  unstudied kanji in them: {len(unseen)}; the commonest: " +
                 " ".join(f"{c}{n}" for c, n in unseen.most_common(40)))
    path = td.report_file("kanji.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:4]))
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
