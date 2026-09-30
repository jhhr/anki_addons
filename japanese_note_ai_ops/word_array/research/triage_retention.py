"""How well the studied vocab has been remembered, and whether FSRS's predictions held.

    py -3.10 word_array/research/triage_retention.py [--years 2]

Over the vocab note type's cards, leaving out every card with the ignore tag (their reviews
after an interval set by hand are not normal reviews, and the user keeps them out of the FSRS
parameters for that reason):

- per month: the reviews of review cards and their pass rate, the new cards started, and the
  backlog at the month's end (cards past their due date, and by how much);
- FSRS's predicted recall at each review against what happened, grouped by the prediction and by
  how overdue the card was (days since the last review over the interval it was given).

The predictions are Anki's own: `card_stats_data` gives the FSRS memory state after each review
as the collection's current parameters compute it, and the recall before the next review follows
from that state and the days in between. A card's first review has no state before it and is not
a prediction; neither is a same-day repeat (a learning step).

A logistic recalibration, `logit(actual) ~ logit(predicted) + log(overdue ratio)`, fitted on the
last `--years` of reviews, goes to `retention_calibration.json` for triage_recall.py.
Writes `reports/retention.txt`.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import triage_data as td

KIND_LEARN, KIND_REVIEW, KIND_RELEARN, KIND_FILTERED, KIND_MANUAL, KIND_RESCHED = range(6)
MIN_ELAPSED_DAYS = 0.75
R_BINS = [0, 0.3, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0001]
OVERDUE_BINS = [0, 0.8, 1.2, 2, 4, 8, 1e9]
EPS = 1e-4


@dataclass
class Prediction:
    t: int  # seconds
    kind: int
    passed: bool
    r: float
    elapsed: float  # days
    scheduled: float  # days: the interval the card had been given
    stability: float
    cid: int

    @property
    def overdue(self) -> float:
        return self.elapsed / self.scheduled if self.scheduled > 0 else float("inf")


def month(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m")


def logit(p: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def histories(col, cids: list[int]) -> dict[int, list]:
    """Each card's revlog as `card_stats_data` gives it, oldest first."""
    out = {}
    for cid in cids:
        entries = list(col.card_stats_data(cid).revlog)
        entries.sort(key=lambda e: e.time)
        out[cid] = entries
    return out


def predictions(cid: int, entries: list, decay: float) -> tuple[list[Prediction], list[tuple]]:
    """The card's predicted reviews, and its due dates over time as `(from_t, due_t)`: when each
    review or set-due-date made the card due next."""
    preds: list[Prediction] = []
    dues: list[tuple] = []
    state = None
    last_review: Optional[int] = None
    scheduled = 0.0
    for e in entries:
        if e.review_kind in (KIND_MANUAL, KIND_RESCHED) or e.button_chosen == 0:
            if e.interval > 0:
                dues.append((e.time, e.time + e.interval))
                scheduled = e.interval / 86400 + ((e.time - last_review) / 86400
                                                  if last_review else 0)
            continue
        if state is not None and last_review is not None:
            elapsed = (e.time - last_review) / 86400
            if elapsed >= MIN_ELAPSED_DAYS and state.stability > 0:
                preds.append(Prediction(
                    t=e.time, kind=e.review_kind, passed=e.button_chosen > 1,
                    r=td.forgetting_curve(elapsed, state.stability, decay), elapsed=elapsed,
                    scheduled=scheduled, stability=state.stability, cid=cid))
        if e.HasField("memory_state"):
            state = e.memory_state
        last_review = e.time
        scheduled = e.interval / 86400 if e.interval > 0 else 0.0
        if e.interval > 0:
            dues.append((e.time, e.time + e.interval))
    return preds, dues


def binned(rows: list[Prediction], value, bins: list[float]) -> list[tuple]:
    out = []
    for lo, hi in zip(bins, bins[1:]):
        got = [p for p in rows if lo <= value(p) < hi]
        if got:
            out.append((lo, hi, len(got), sum(p.r for p in got) / len(got),
                        sum(p.passed for p in got) / len(got)))
    return out


def fit_calibration(rows: list[Prediction]) -> dict:
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    x = np.array([[logit(p.r), math.log(max(min(p.overdue, 100.0), 0.05))] for p in rows])
    y = np.array([p.passed for p in rows])
    model = LogisticRegression(C=100.0).fit(x, y)
    return {"intercept": float(model.intercept_[0]), "logit_r": float(model.coef_[0][0]),
            "log_overdue": float(model.coef_[0][1]), "n": len(rows)}


def calibrated(cal: dict, r: float, overdue: float) -> float:
    z = (cal["intercept"] + cal["logit_r"] * logit(r)
         + cal["log_overdue"] * math.log(max(min(overdue, 100.0), 0.05)))
    return 1 / (1 + math.exp(-z))


def log_loss(rows: list[Prediction], prob) -> float:
    total = 0.0
    for p in rows:
        q = min(max(prob(p), EPS), 1 - EPS)
        total -= math.log(q if p.passed else 1 - q)
    return total / max(len(rows), 1)


def backlog_at(dues_by_card: dict[int, list[tuple]], t: float) -> tuple[int, float]:
    """Cards whose due date, as of `t`, had passed, and their median days overdue."""
    overdue = []
    for dues in dues_by_card.values():
        current = None
        for start, due in dues:
            if start > t:
                break
            current = due
        if current is not None and current < t:
            overdue.append((t - current) / 86400)
    return len(overdue), statistics.median(overdue) if overdue else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--years", type=float, default=2.0)
    td.add_settings_args(parser)
    args = parser.parse_args()
    settings = td.settings(args)
    ignore_tag = settings["ignore_tag"]
    now = time.time()
    since = now - args.years * 365.25 * 86400

    col = td.open_collection(settings["collection"])
    try:
        notes = td.load_vocab_notes(col, settings["note_type"])
        cards = [c for n in notes.values() if not n.has_tag(ignore_tag) for c in n.cards
                 if c.reps > 0]
        entries = histories(col, [c.cid for c in cards])
        new_by_month: dict[str, int] = defaultdict(int)
        for c in cards:
            first = next((e for e in entries[c.cid] if e.button_chosen > 0), None)
            if first is not None:
                new_by_month[month(first.time)] += 1
    finally:
        col.close()

    decay = {c.cid: c.decay for c in cards}
    preds: list[Prediction] = []
    dues: dict[int, list[tuple]] = {}
    for cid, es in entries.items():
        card_preds, card_dues = predictions(cid, es, decay[cid])
        preds.extend(card_preds)
        dues[cid] = card_dues
    recent = [p for p in preds if p.t >= since]

    lines = [f"{len(cards)} reviewed vocab cards without {ignore_tag}; {len(preds)} predicted"
             f" reviews, {len(recent)} of them in the last {args.years:g} years", ""]
    lines.append("--- by month: review-card reviews, pass rate, FSRS's mean prediction, new cards"
                 " started, backlog at month end ---")
    lines.append(f"  {'month':8} {'reviews':>7} {'passed':>7} {'predicted':>9} {'new':>5}"
                 f" {'overdue cards':>13} {'median days over':>16}")
    by_month: dict[str, list[Prediction]] = defaultdict(list)
    for p in preds:
        if p.kind == KIND_REVIEW:
            by_month[month(p.t)].append(p)
    months = sorted(set(by_month) | set(new_by_month))
    for m in months:
        if m < month(since - 31 * 86400):
            continue
        got = by_month.get(m, [])
        y, mo = map(int, m.split("-"))
        end = datetime(y + mo // 12, mo % 12 + 1, 1).timestamp()
        n_over, med_over = backlog_at(dues, min(end, now))
        passed = f"{100 * sum(p.passed for p in got) / len(got):.1f}%" if got else "-"
        predicted = f"{100 * sum(p.r for p in got) / len(got):.1f}%" if got else "-"
        lines.append(f"  {m:8} {len(got):>7} {passed:>7} {predicted:>9} {new_by_month.get(m, 0):>5}"
                     f" {n_over:>13} {med_over:>16.0f}")
    lines.append("")

    for title, rows in ((f"last {args.years:g} years", recent), ("all time", preds)):
        lines.append(f"--- calibration, {title}: predicted recall vs passed ---")
        lines.append(f"  {'predicted':>13} {'reviews':>8} {'mean pred':>9} {'passed':>7}")
        for lo, hi, n, mean_r, rate in binned(rows, lambda p: p.r, R_BINS):
            lines.append(f"  {lo:>5.2f}-{min(hi, 1):<5.2f}  {n:>8} {100 * mean_r:>8.1f}%"
                         f" {100 * rate:>6.1f}%")
        lines.append("")
        lines.append(f"--- calibration, {title}: by how overdue (elapsed / interval given) ---")
        lines.append(f"  {'overdue':>13} {'reviews':>8} {'mean pred':>9} {'passed':>7}")
        for lo, hi, n, mean_r, rate in binned(rows, lambda p: p.overdue, OVERDUE_BINS):
            label = f"{lo:g}-{hi:g}" if hi < 1e9 else f"{lo:g}+"
            lines.append(f"  {label:>13} {n:>8} {100 * mean_r:>8.1f}%  {100 * rate:>6.1f}%")
        lines.append("")

    lines.append(f"--- last {args.years:g} years: predicted vs passed, by overdue band and"
                 " prediction ---")
    for lo, hi in zip(OVERDUE_BINS, OVERDUE_BINS[1:]):
        band = [p for p in recent if lo <= p.overdue < hi]
        if len(band) < 50:
            continue
        label = f"{lo:g}-{hi:g}" if hi < 1e9 else f"{lo:g}+"
        cells = []
        for rlo, rhi, n, mean_r, rate in binned(band, lambda p: p.r, R_BINS):
            if n >= 20:
                cells.append(f"{100 * mean_r:.0f}->{100 * rate:.0f} (n{n})")
        lines.append(f"  overdue {label:>7}: " + ", ".join(cells))
    lines.append("")

    cal = fit_calibration(recent)
    raw = log_loss(recent, lambda p: p.r)
    fitted = log_loss(recent, lambda p: calibrated(cal, p.r, p.overdue))
    lines.append("--- recalibration fitted on the last years' reviews ---")
    lines.append(f"  logit(passed) = {cal['intercept']:.3f} + {cal['logit_r']:.3f} logit(R)"
                 f" + {cal['log_overdue']:.3f} log(overdue ratio)")
    lines.append(f"  log loss: FSRS {raw:.4f}, recalibrated {fitted:.4f} (in-sample)")
    for r in (0.5, 0.7, 0.8, 0.9, 0.95):
        row = ", ".join(f"overdue {o:g}: {100 * calibrated(cal, r, o):.0f}%"
                        for o in (1, 2, 4, 8))
        lines.append(f"  FSRS says {100 * r:.0f}% -> {row}")
    cal["fitted_on"] = f"reviews since {datetime.fromtimestamp(since):%Y-%m-%d}"
    td.data_file("retention_calibration.json").write_text(json.dumps(cal, indent=2) + "\n")

    path = td.report_file("retention.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:1]))
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
