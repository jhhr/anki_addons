"""How many of the studied words would be recalled today.

    py -3.10 word_array/research/triage_recall.py

Over the vocab note type's reviewed cards without the ignore tag: each card's recall today from
its FSRS memory state (stability, difficulty, last review, the preset's decay), then corrected
by triage_retention.py's recalibration (`retention_calibration.json`), which found how FSRS's
predictions and the reviews that followed them disagreed over the last years. The sum of that
over the studied notes is the number of words expected to be recalled; the naive count is the
notes with more than one passing answer. Their ratio is the multiplier that turns "words I have
studied" into "words I know".

The correction is fitted on the reviews the user chose to do while the backlog grew, and the
backlog's oldest cards sit at predictions it saw few of: both are reported, so the figure can be
read with them. Writes `recall_now.jsonl` (per note, the triage model's sibling features) and
`reports/recall.txt`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict

import triage_data as td
from triage_retention import calibrated

R_BINS = [0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    td.add_settings_args(parser)
    args = parser.parse_args()
    settings = td.settings(args)
    ignore_tag = settings["ignore_tag"]
    cal_path = td.data_file("retention_calibration.json")
    if not cal_path.exists():
        sys.exit("no retention_calibration.json: run triage_retention.py first")
    cal = json.loads(cal_path.read_text())
    now = time.time()

    col = td.open_collection(settings["collection"])
    try:
        notes = td.load_vocab_notes(col, settings["note_type"])
        decks = {d.id: d.name for d in col.decks.all_names_and_ids()}
        cids = [c.cid for n in notes.values() for c in n.cards if c.reps > 0]
        revlog = td.load_revlog(col, cids)
    finally:
        col.close()

    rows = []
    for nid, note in sorted(notes.items()):
        if note.has_tag(ignore_tag) or not note.reviewed:
            continue
        best = None
        for card in note.cards:
            r = td.retrievability(card, now)
            if r is None or card.last_review_s is None:
                continue
            elapsed = (now - card.last_review_s) / 86400
            overdue = elapsed / card.ivl if card.ivl > 0 else 1.0
            reviews = revlog.get(card.cid, [])
            passes = sum(1 for rv in reviews if rv.ease > 1 and rv.type <= td.REVLOG_FILTERED)
            review_passes = sum(1 for rv in reviews if rv.ease > 1 and rv.type == td.REVLOG_REVIEW)
            row = {
                "nid": nid, "cid": card.cid, "r": round(r, 4),
                "r_cal": round(calibrated(cal, r, overdue), 4),
                "s": card.stability, "d": card.difficulty, "elapsed": round(elapsed, 1),
                "ivl": card.ivl, "overdue": round(overdue, 2), "reps": card.reps,
                "lapses": card.lapses, "passes": passes, "review_passes": review_passes,
                "suspended": card.suspended, "deck": decks.get(card.odid or card.did, ""),
                "tagged": note.has_tag(td.NEW_WORD_TAG),
            }
            if best is None or row["r"] > best["r"]:
                best = row
        if best is not None:
            rows.append(best)
    td.write_jsonl(td.data_file("recall_now.jsonl"), rows)

    active = [r for r in rows if not r["suspended"]]
    naive = [r for r in active if r["passes"] > 1]
    naive_review = [r for r in active if r["review_passes"] >= 1]
    fsrs_sum = sum(r["r"] for r in active)
    cal_sum = sum(r["r_cal"] for r in active)
    lines = [
        f"{len(rows)} studied vocab notes without {ignore_tag} ({len(rows) - len(active)} of"
        f" them suspended, left out below)", "",
        "--- the count ---",
        f"  naive: notes with more than one passing answer            {len(naive):>7}",
        f"         (with at least one passed review-card review:      {len(naive_review):>7})",
        f"  expected recalled today, FSRS as it stands                {fsrs_sum:>9.0f}",
        f"  expected recalled today, recalibrated                     {cal_sum:>9.0f}",
        f"  multiplier (recalibrated / naive):                         "
        f"{cal_sum / max(len(naive), 1):.2f}",
        f"  multiplier (FSRS / naive):                                 "
        f"{fsrs_sum / max(len(naive), 1):.2f}",
        "",
        "--- recall today, FSRS's figure, how the studied notes spread ---",
        f"  {'R':>11} {'notes':>6} {'mean R':>7} {'recalibrated':>12} {'median overdue':>15}",
    ]
    for lo, hi in zip(R_BINS, R_BINS[1:]):
        got = [r for r in active if lo <= r["r"] < hi]
        if got:
            over = sorted(r["overdue"] for r in got)[len(got) // 2]
            lines.append(f"  {lo:>4.1f}-{min(hi, 1):<4.1f}  {len(got):>6}"
                         f" {100 * sum(r['r'] for r in got) / len(got):>6.1f}%"
                         f" {100 * sum(r['r_cal'] for r in got) / len(got):>11.1f}%"
                         f" {over:>15.1f}")
    below = sum(r["r"] < 0.6 for r in active)
    lines += [
        f"  {below} notes sit below the lowest predictions the recalibration was fitted on"
        f" (0.6 and up had nearly all its reviews): their corrected figure is an extrapolation.",
        "",
        "--- by deck ---",
    ]
    by_deck: dict[str, list] = defaultdict(list)
    for row in active:
        by_deck[row["deck"]].append(row)
    for deck in sorted(by_deck):
        got = by_deck[deck]
        lines.append(f"  {deck:30} {len(got):>6} notes, FSRS {sum(r['r'] for r in got):>7.0f}"
                     f", recalibrated {sum(r['r_cal'] for r in got):>7.0f}")
    path = td.report_file("recall.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[3:9]))
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
