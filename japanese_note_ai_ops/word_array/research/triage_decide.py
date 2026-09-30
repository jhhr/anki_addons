"""What to do with each untriaged vocab note: suspend, schedule far out, or leave new to learn.

    py -3.10 word_array/research/triage_decide.py [--suspend-min 0.8] [--learn-line 0.5]

Reads the model's scores (`predictions.jsonl`, triage_model.py) and the notes judged on the
judging page (`hand_labels.jsonl`, triage_judge.py). In scope is every note still untouched: its
card new and in the processing deck, and not judged in Anki (`labels.jsonl`).

- A note judged by hand gets that action: the user's call, not the model's. One the user marked
  Wrong data on the judging page (label `invalid`) gets no decision: its note is wrong and waits
  for a fix. So does every proper noun (Opus's `kind`, triage_features.proper_noun_nids): the
  user's rule is that the match op should not have matched one.
- A note an Opus subagent reported as wrong (`note_issues.jsonl`: a reading that does not fit
  the sense, a sense that belongs to another word) is decided, but held: triage_apply.py leaves
  it alone unless asked to, since a wrong note scheduled years out stays wrong that long.
- Otherwise the model decides. Suspend only when very sure (P(suspend) at least
  `--suspend-min`), since a suspended word is never seen again; learn when P(known) is under
  `--learn-line`; schedule the rest.
- A scheduled note's interval says how sure the decision is, as the user's own intervals did:
  the scheduled notes are ranked by their expected level on the scale (learn 0 .. suspend 3; for a
  hand-scheduled note, the level given that it is short or long) and spread evenly from
  `--min-days` (least sure) to `--max-days` (most sure), so that as many come due each day.
- The notes to learn are ordered by Jiten rank, the commonest first; the ones Jiten lacks follow,
  by TUBELEX rank, then BCCWJ rank.

Each decision has a probability of being right (P(suspend) for a suspend, P(known) for a
schedule, P(learn) for a learn, 1 for a hand label) and an apply wave by it (`--waves`), so the
surest can go in first and the rest wait for more hand labels. When the random holdout of the
judging page has been judged, the model's rule is also run on it, as if its notes had no hand
label, and compared with what the user said.

Writes `decisions.jsonl` (per note: the action, interval or learn order, probability, expected
level, wave, hold and reasons), `reports/decisions.txt` (the counts, the daily load the schedule
adds and what the notes scheduled in Anki already bring, the expected errors, the waves) and
`reports/wrong_data.txt`: every note marked or reported wrong, with what is wrong, for fixing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

import triage_data as td
import triage_features as tf

SUSPEND_MIN = 0.8
LEARN_LINE = 0.5
MIN_DAYS = 30
MAX_DAYS = 1600
WAVES = (0.9, 0.75)
LEVELS = ("learn", "schedule_short", "schedule_long", "suspend")


def expected_level(p: list[float], bounds: Optional[tuple[int, int]] = None) -> float:
    """The mean level, over the label's range of levels when there is a label."""
    lo, hi = bounds if bounds else (0, 3)
    mass = sum(p[lo : hi + 1])
    if mass <= 0:
        return (lo + hi) / 2
    return sum(k * p[k] for k in range(lo, hi + 1)) / mass


def model_action(pred: dict, suspend_min: float, learn_line: float) -> str:
    if pred["p_suspend"] >= suspend_min:
        return "suspend"
    return "schedule" if pred["p_known"] >= learn_line else "learn"


def probability(action: str, pred: dict) -> float:
    """The chance the action is what the user would pick: a schedule is wrong only for a word
    they don't know, so a suspend-level word scheduled counts as right."""
    if action == "suspend":
        return pred["p_suspend"]
    if action == "schedule":
        return pred["p_known"]
    return 1 - pred["p_known"]


def spread(n: int, lo: int, hi: int) -> list[int]:
    """`n` intervals from `lo` to `hi`, evenly: the i-th of n at the middle of its share."""
    if n == 0:
        return []
    return [round(lo + (hi - lo) * (i + 0.5) / n) for i in range(n)]


def learn_key(freq: Optional[dict], nid: int) -> tuple:
    f = freq or {}
    for tier, name in enumerate(("freq_jiten_rank", "freq_tubelex_rank", "freq_bccwj_rank")):
        if f.get(name):
            return (tier, f[name], nid)
    return (3, 0, nid)


def wave_of(prob: float, waves: tuple[float, ...]) -> int:
    for i, line in enumerate(waves, 1):
        if prob >= line:
            return i
    return len(waves) + 1


def in_scope(w: dict, processing_deck: str, anki_labels: dict) -> str:
    """'' for a note to decide, else why not."""
    if w["nid"] in anki_labels:
        return "judged in Anki"
    if w["type"] != 0:
        return "card no longer new"
    if w["queue"] != 0:
        return "card suspended or buried"
    if processing_deck and w["deck"] != processing_deck:
        return "not in the processing deck"
    return ""


def decide(words, preds, hand, anki_labels, freq, settings, args, wrong=None, issues=None):
    """`(decisions, out_of_scope Counter)`. `wrong`: the notes marked Wrong data by hand;
    `issues`: the notes reported wrong by an agent, each a list of what it said."""
    wrong = wrong or {}
    issues = issues or {}
    processing_deck = settings.get("processing_deck", "")
    out: Counter = Counter()
    rows = []
    for w in words:
        why = in_scope(w, processing_deck, anki_labels)
        if not why and w["nid"] in wrong:
            why = "marked wrong data"
        if why:
            out[why] += 1
            continue
        pred = preds.get(w["nid"])
        if pred is None:
            out["no model score"] += 1
            continue
        h = hand.get(w["nid"])
        if h is not None:
            action, prob, source = h["label"], 1.0, "hand"
            bounds = {"learn": (0, 0), "schedule": (1, 2), "suspend": (3, 3)}[action]
        else:
            action = model_action(pred, args.suspend_min, args.learn_line)
            prob, source, bounds = probability(action, pred), "model", None
        reasons = []
        if source == "hand":
            reasons.append(f"judged {action} by hand ({h.get('kind', '')})")
        else:
            reasons.append(f"P(known) {pred['p_known']:.2f}, P(suspend) {pred['p_suspend']:.2f}")
            if pred.get("stacked"):
                reasons.append("with the agents' judgement")
        reasons += [f"+ {f}" for f in pred.get("for", [])] + [f"- {f}" for f in pred.get("against", [])]
        rows.append({
            "nid": w["nid"], "cid": w["cid"], "key": w["key"], "reading": w["reading"],
            "action": action, "interval": None, "learn_order": None,
            "probability": round(prob, 4),
            "confidence": round(expected_level(pred["p"], bounds), 4),
            "p": pred["p"], "source": source,
            "wave": 1 if source == "hand" else wave_of(prob, tuple(args.waves)),
            "hold": "; ".join(issues.get(w["nid"], [])),
            "reasons": reasons,
        })

    scheduled = sorted((r for r in rows if r["action"] == "schedule"),
                       key=lambda r: (r["confidence"], r["probability"], r["nid"]))
    for r, days in zip(scheduled, spread(len(scheduled), args.min_days, args.max_days)):
        r["interval"] = days
    learn = sorted((r for r in rows if r["action"] == "learn"),
                   key=lambda r: learn_key(freq.get(r["nid"]), r["nid"]))
    for i, r in enumerate(learn, 1):
        r["learn_order"] = i
    return rows, out


def holdout_check(preds, hand, args) -> list[str]:
    """The model's rule on the judged random holdout, against the user's labels."""
    held = [(hand[n], preds[n]) for n in hand
            if hand[n].get("kind") == "random" and n in preds]
    if not held:
        return ["--- the random holdout: not judged yet ---", ""]
    actions = ("suspend", "schedule", "learn")
    lines = [f"--- the model's rule on the {len(held)} judged random holdout notes ---"]
    grid: Counter = Counter()
    for h, p in held:
        grid[(h["label"], model_action(p, args.suspend_min, args.learn_line))] += 1
    right = sum(grid[(a, a)] for a in actions)
    lines.append(f"  agrees with the user on {right}/{len(held)} ({right / len(held):.0%})")
    lines.append("  judged \\ decided " + "".join(f"{a:>10}" for a in actions))
    for a in actions:
        lines.append(f"  {a:>16} " + "".join(f"{grid[(a, b)]:>10}" for b in actions))
    unknown_scheduled = grid[("learn", "schedule")] + grid[("learn", "suspend")]
    decided_known = sum(grid[(a, b)] for a in actions for b in ("schedule", "suspend"))
    if decided_known:
        lines.append(f"  words the user does not know among those decided known:"
                     f" {unknown_scheduled}/{decided_known} ({unknown_scheduled / decided_known:.0%})")
    # Where to draw the learn line: of the words the user would learn, how many a line catches,
    # and how many of the ones it sends to learn the user would learn
    truly = [p for h, p in held if h["label"] == "learn"]
    lines.append(f"  the learn line, against the {len(truly)} the user would learn:")
    for line in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        sent = [(h, p) for h, p in held if p["p_known"] < line]
        hit = sum(h["label"] == "learn" for h, _ in sent)
        caught = f"{hit}/{len(truly)}" if truly else "-"
        lines.append(f"    P(known) < {line}: sends {len(sent):>3} to learn, {hit} of them rightly;"
                     f" catches {caught}")
    lines.append("")
    return lines


def load_lines(rows, anki_labels, start: date) -> list[str]:
    """Reviews per day, by month, from this schedule and from the Anki-scheduled cards not yet
    reviewed."""
    ours: Counter = Counter()
    for r in rows:
        if r["action"] == "schedule":
            ours[(start + timedelta(days=r["interval"])).strftime("%Y-%m")] += 1
    theirs: Counter = Counter()
    for lab in anki_labels.values():
        if lab["label"] != "schedule" or lab.get("checked"):
            continue
        due = datetime.strptime(lab["judged"], "%Y-%m-%d").date() + timedelta(days=lab["interval"])
        if due >= start:
            theirs[due.strftime("%Y-%m")] += 1
    months = sorted(set(ours) | set(theirs))
    lines = ["--- the daily load: cards coming due per day, by month ---",
             "  month     new schedule   already scheduled in Anki"]
    for m in months:
        y, mo = map(int, m.split("-"))
        days = ((date(y + mo // 12, mo % 12 + 1, 1)) - date(y, mo, 1)).days
        lines.append(f"  {m}   {ours[m] / days:>10.1f}   {theirs[m] / days:>10.1f}")
    lines.append("")
    return lines


def report(rows, out, preds, hand, anki_labels, args) -> list[str]:
    by = defaultdict(list)
    for r in rows:
        by[r["action"]].append(r)
    lines = [f"{len(rows)} notes decided: " + ", ".join(
        f"{a} {len(by[a])}" for a in ("suspend", "schedule", "learn")),
        f"  by hand: {sum(r['source'] == 'hand' for r in rows)}; by the model:"
        f" {sum(r['source'] == 'model' for r in rows)}; held for a reported error in the note:"
        f" {sum(bool(r['hold']) for r in rows)}",
        f"  rule: suspend when P(suspend) >= {args.suspend_min}, learn when P(known) <"
        f" {args.learn_line}, else schedule over {args.min_days}-{args.max_days} days",
        "  not decided: " + (", ".join(f"{k} {v}" for k, v in out.most_common()) or "none"),
        ""]

    lines.append("--- expected errors (the model's own probabilities; hand labels count as right) ---")
    for a, what in (("suspend", "that the user would not suspend"),
                    ("schedule", "that the user does not know"),
                    ("learn", "that the user already knows")):
        rs = by[a]
        if rs:
            wrong = sum(1 - r["probability"] for r in rs)
            lines.append(f"  {a:8}: {wrong:7.0f} of {len(rs)} ({wrong / len(rs):.0%}) {what}")
    lines.append("")

    lines.append("--- how the model's decisions change with the lines ---")
    model = [preds[r["nid"]] for r in rows if r["source"] == "model"]
    unknown = sum(1 - p["p_known"] for p in model)
    lines.append(f"  the model expects {unknown:.0f} of its {len(model)} notes to be words the user"
                 " does not know; by its own probabilities, each learn line:")
    for ll in (0.5, 0.6, 0.7, 0.8, 0.9):
        sent = [p for p in model if p["p_known"] < ll]
        caught = sum(1 - p["p_known"] for p in sent)
        lines.append(f"    learn below {ll}: {len(sent):>5} to learn, about {caught:.0f} of them"
                     f" unknown ({caught / max(len(sent), 1):.0%}), {caught / max(unknown, 1):.0%}"
                     " of all the unknown ones caught")
    for sm in (0.6, 0.7, 0.8, 0.9):
        for ll in (0.4, 0.5, 0.6, 0.7):
            c = Counter(model_action(p, sm, ll) for p in model)
            lines.append(f"  suspend-min {sm}, learn-line {ll}: suspend {c['suspend']:>5},"
                         f" schedule {c['schedule']:>5}, learn {c['learn']:>5}")
    lines.append("")

    lines += holdout_check(preds, hand, args)

    sched = by["schedule"]
    if sched:
        per_day = len(sched) / max(args.max_days - args.min_days, 1)
        lines.append(f"--- the schedule: {len(sched)} cards over {args.min_days}-{args.max_days}"
                     f" days, {per_day:.1f} due per day ---")
        for lo, hi in ((30, 180), (180, 540), (540, 900), (900, 1260), (1260, 1601)):
            rs = [r for r in sched if lo <= r["interval"] < hi]
            if rs:
                lines.append(f"  {lo:>4}-{hi - 1:<4} days: {len(rs):>5} cards, mean P(known)"
                             f" {sum(r['probability'] for r in rs) / len(rs):.2f}")
        lines.append("")
    lines += load_lines(rows, anki_labels, args.start)

    lines.append("--- apply waves, surest first ---")
    for wave in sorted({r["wave"] for r in rows}):
        rs = [r for r in rows if r["wave"] == wave]
        c = Counter(r["action"] for r in rs)
        wrong = sum(1 - r["probability"] for r in rs)
        line = (f">= {args.waves[wave - 1]}" if wave <= len(args.waves) else "the rest")
        lines.append(f"  wave {wave} ({line}{', hand labels too' if wave == 1 else ''}):"
                     f" {len(rs)} notes (suspend {c['suspend']}, schedule {c['schedule']}, learn"
                     f" {c['learn']}), about {wrong:.0f} expected wrong")
    lines.append("")

    lines.append("--- the first 30 to learn ---")
    for r in sorted(by["learn"], key=lambda r: r["learn_order"])[:30]:
        lines.append(f"  {r['learn_order']:>5}. {r['key']} ({r['reading']})"
                     f" P(known) {1 - r['probability'] if r['source'] == 'model' else 0:.2f}"
                     f" [{r['source']}]")
    lines.append("")
    lines.append("--- the 20 surest suspends and the 20 least sure schedules ---")
    for r in sorted(by["suspend"], key=lambda r: -r["probability"])[:20]:
        lines.append(f"  suspend {r['probability']:.2f} {r['key']} ({r['reading']}) [{r['source']}]")
    for r in sorted(sched, key=lambda r: r["interval"])[:20]:
        lines.append(f"  schedule {r['interval']:>4}d P(known) {r['probability']:.2f} {r['key']}"
                     f" ({r['reading']}) [{r['source']}]")
    return lines


def wrong_data_lines(words: dict, wrong: dict, issues: dict) -> list[str]:
    lines = [f"{len(wrong)} notes marked Wrong data by hand, {len(issues)} reported wrong by an"
             " agent. Fix the note (its reading, meaning, sentence furigana or word array), then"
             " judge it again.", ""]
    for title, found in (("--- marked by hand ---", {n: [r.get("note") or ""] for n, r in wrong.items()}),
                         ("--- reported by an agent ---", issues)):
        lines.append(title)
        for nid in sorted(found, key=lambda n: words.get(n, {}).get("key", "")):
            w = words.get(nid, {})
            lines.append(f"  nid {nid} {w.get('key', '?')} ({w.get('reading', '?')}):"
                         f" {'; '.join(t for t in found[nid] if t) or '(no note)'}")
        lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--suspend-min", type=float, default=SUSPEND_MIN)
    parser.add_argument("--learn-line", type=float, default=LEARN_LINE)
    parser.add_argument("--min-days", type=int, default=MIN_DAYS)
    parser.add_argument("--max-days", type=int, default=MAX_DAYS)
    parser.add_argument("--waves", type=float, nargs="+", default=list(WAVES),
                        help="the probability lines between apply waves, highest first")
    parser.add_argument("--start", type=date.fromisoformat, default=date.today(),
                        help="the day the schedule is applied (for the load report)")
    args = parser.parse_args()
    args.waves = sorted(args.waves, reverse=True)

    settings = td.read_settings()
    words = td.read_jsonl(td.data_file("words.jsonl"))
    preds = {r["nid"]: r for r in td.read_jsonl(td.data_file("predictions.jsonl"))}
    if not preds:
        sys.exit("no predictions.jsonl: run triage_model.py first")
    hand_rows = td.read_jsonl(td.data_file("hand_labels.jsonl"))
    hand = {r["nid"]: r for r in hand_rows if r.get("label") in ("suspend", "schedule", "learn")}
    wrong = {r["nid"]: r for r in hand_rows if r.get("label") == "invalid"}
    issues: dict[int, list[str]] = defaultdict(list)
    minor: dict[int, list[str]] = defaultdict(list)
    for r in td.read_jsonl(td.data_file("note_issues.jsonl")):
        # An odd but readable spelling is listed for fixing and holds nothing back
        (minor if r.get("severity") == "minor" else issues)[r["nid"]].append(r["issue"])
    anki_labels = {r["nid"]: r for r in td.read_jsonl(td.data_file("labels.jsonl"))}
    for nid in tf.proper_noun_nids(words):
        if nid not in anki_labels:
            wrong.setdefault(nid, {"nid": nid, "label": "invalid", "note": tf.PROPER_NOUN_NOTE,
                                   "by": "rule"})
    freq = {r["nid"]: r for r in td.read_jsonl(td.data_file("frequency.jsonl"))}

    rows, out = decide(words, preds, hand, anki_labels, freq, settings, args, wrong, issues)
    td.write_jsonl(td.data_file("decisions.jsonl"), rows)
    by_nid = {w["nid"]: w for w in words}
    listed = {n: issues.get(n, []) + [f"(minor) {t}" for t in minor.get(n, [])]
              for n in set(issues) | set(minor)}
    td.report_file("wrong_data.txt").write_text(
        "\n".join(wrong_data_lines(by_nid, wrong, listed)) + "\n", encoding="utf-8")
    lines = report(rows, out, preds, hand, anki_labels, args)
    td.report_file("decisions.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
