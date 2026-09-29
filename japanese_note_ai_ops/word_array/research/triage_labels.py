"""The triage's labels: what the user decided for each vocab note they have judged, read off the
card's current state, which is taken as their final judgement.

    py -3.10 word_array/research/triage_labels.py

Checked in this order, the first that fits wins:

1. suspended, new or not, whatever its reviews or tags: `suspend` (some were reviewed first and
   then suspended when the user changed their mind);
2. the ignore tag (settings `ignore_tag`, which keeps manually scheduled cards out of the FSRS
   parameters): `schedule`, with the interval and date of its last set-due-date entry (a
   manual revlog row) before its first real review;
3. reviewed at least once without that tag: `learn`, with the first answer's button and time.

Cards that fit none of these but are not plain new cards (reviewed-looking without reviews, or
new with the ignore tag) are listed, not labelled. The scheduled cards that have come due and
been reviewed since are the user's own check on their judgement: their pass rate is reported by
the interval given. Writes `labels.jsonl` and `reports/labels.txt`.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

import triage_data as td

SUSPEND, SCHEDULE, LEARN = "suspend", "schedule", "learn"
INTERVAL_BINS = [(0, 180), (180, 540), (540, 900), (900, 1260), (1260, 1620), (1620, 99999)]


def day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def interval_bin(days: int) -> str:
    for lo, hi in INTERVAL_BINS:
        if lo <= days < hi:
            return f"{lo}-{hi - 1}" if hi < 99999 else f"{lo}+"
    return "?"


def label_note(note: td.VocabNote, reviews: list[td.Review], ignore_tag: str, mod_s: int) -> dict:
    """The note's label and what goes with it; `label` None for an unlabelled card."""
    card = note.card
    assert card is not None
    real = [r for r in reviews if r.type in (td.REVLOG_LEARN, td.REVLOG_REVIEW, td.REVLOG_RELEARN)
            and r.ease > 0]
    manual = [r for r in reviews if r.type in (td.REVLOG_MANUAL, td.REVLOG_RESCHED)]
    row: dict = {"nid": note.nid, "cid": card.cid, "type": card.type, "queue": card.queue,
                 "reps": card.reps, "lapses": card.lapses, "ivl": card.ivl,
                 "reviews": len(real), "manual_entries": len(manual),
                 "ignore_tag": note.has_tag(ignore_tag), "mod": day(mod_s * 1000)}
    if card.suspended:
        row.update(label=SUSPEND, judged=row["mod"], after_reviews=len(real))
        return row
    if note.has_tag(ignore_tag):
        before_review = [m for m in manual if not real or m.id < real[0].id]
        entry = (before_review or manual or [None])[-1]
        if entry is None:
            row.update(label=None, why=f"{ignore_tag} but no set-due-date entry")
            return row
        row.update(label=SCHEDULE, interval=entry.ivl, judged=day(entry.id),
                   first_interval=manual[0].ivl, reschedules=len(manual) - 1)
        after = [r for r in real if r.id > entry.id]
        if after:
            first = after[0]
            row.update(checked=day(first.id), check_ease=first.ease,
                       check_passed=first.ease > 1,
                       check_days=round((first.id - entry.id) / td.DAY_MS))
        return row
    if real:
        first = real[0]
        row.update(label=LEARN, first_ease=first.ease, first_time_ms=first.time_ms,
                   judged=day(first.id), first_type=first.type)
        return row
    if card.is_new:
        row.update(label=None, why="new" if not note.has_tag(ignore_tag) else "")
        return row
    row.update(label=None, why="not new, never reviewed, not suspended, no "
               f"{ignore_tag}: scheduled but never tagged?")
    return row


def report(rows: list[dict], words: dict[int, dict], ignore_tag: str) -> list[str]:
    labelled = [r for r in rows if r.get("label")]
    counts = Counter(r["label"] for r in labelled)
    lines = [f"{len(labelled)} labelled notes: " + ", ".join(f"{k} {v}" for k, v in
                                                             counts.most_common()), ""]
    odd = [r for r in rows if not r.get("label") and r.get("why") != "new"]
    lines.append(f"--- {len(odd)} cards that fit no rule ---")
    for r in odd:
        w = words.get(r["nid"], {})
        lines.append(f"  nid {r['nid']} {w.get('key', '')} [{w.get('deck', '')}] type {r['type']}"
                     f" queue {r['queue']} ivl {r['ivl']} reps {r['reps']} manual entries"
                     f" {r['manual_entries']}: {r.get('why') or ignore_tag + ' on a new card'}")
    lines.append("")

    sus = [r for r in labelled if r["label"] == SUSPEND]
    lines.append("--- suspend ---")
    lines.append(f"  {len(sus)}: {sum(r['type'] == 0 for r in sus)} still new,"
                 f" {sum(r['after_reviews'] > 0 for r in sus)} reviewed before being suspended")
    by_deck = Counter(words.get(r["nid"], {}).get("deck", "") for r in sus)
    lines.append("  by deck: " + ", ".join(f"{d} {n}" for d, n in by_deck.most_common()))
    lines.append("")

    sch = [r for r in labelled if r["label"] == SCHEDULE]
    lines.append("--- schedule: the interval given (last set-due-date before any review) ---")
    bins = Counter(interval_bin(r["interval"]) for r in sch)
    for lo, hi in INTERVAL_BINS:
        name = interval_bin(lo)
        lines.append(f"  {name:>10} days: {bins.get(name, 0)}")
    resched = [r for r in sch if r["reschedules"]]
    lines.append(f"  {len(resched)} were set more than once; first vs last interval, a sample:")
    for r in resched[:15]:
        lines.append(f"    nid {r['nid']} {words.get(r['nid'], {}).get('key', '')}:"
                     f" {r['first_interval']} -> {r['interval']} ({r['manual_entries']} entries)")
    checked = [r for r in sch if "check_ease" in r]
    lines.append("")
    lines.append(f"--- the {len(checked)} scheduled cards reviewed since: pass rate by interval ---")
    by_bin: dict = defaultdict(list)
    for r in checked:
        by_bin[interval_bin(r["interval"])].append(r["check_passed"])
    for lo, hi in INTERVAL_BINS:
        name = interval_bin(lo)
        got = by_bin.get(name, [])
        if got:
            lines.append(f"  {name:>10} days: {sum(got)}/{len(got)} passed"
                         f" ({100 * sum(got) / len(got):.0f}%)")
    if checked:
        lines.append(f"  all: {sum(r['check_passed'] for r in checked)}/{len(checked)}")
        waited = [r["check_days"] for r in checked]
        lines.append(f"  days from scheduling to that review: median {statistics.median(waited)}"
                     f", range {min(waited)}-{max(waited)}")
        for r in checked:
            w = words.get(r["nid"], {})
            lines.append(f"    {'PASS' if r['check_passed'] else 'FAIL'} ease {r['check_ease']}"
                         f" ivl {r['interval']} after {r['check_days']}d: {w.get('key', '')}"
                         f" ({w.get('reading', '')}) {w.get('meaning', '')[:60]}")
    lines.append("")

    learn = [r for r in labelled if r["label"] == LEARN]
    lines.append("--- learn: the first answer ---")
    eases = Counter(r["first_ease"] for r in learn)
    lines.append("  buttons: " + ", ".join(f"{b} {eases.get(b, 0)}" for b in (1, 2, 3, 4)))
    for ease in (1, 2, 3, 4):
        times = [r["first_time_ms"] / 1000 for r in learn if r["first_ease"] == ease]
        if times:
            lines.append(f"  button {ease}: median {statistics.median(times):.1f}s to answer")
    lines.append("")

    lines.append("--- consistency over time: the label mix by month judged ---")
    by_month: dict = defaultdict(Counter)
    for r in labelled:
        by_month[r["judged"][:7]][r["label"]] += 1
    lines.append(f"  {'month':8} {'n':>5} {'suspend':>8} {'schedule':>9} {'learn':>6}"
                 f"  median interval  ease-1 share of learn")
    for month in sorted(by_month):
        c = by_month[month]
        n = sum(c.values())
        ivls = [r["interval"] for r in sch if r["judged"][:7] == month]
        ln = [r for r in learn if r["judged"][:7] == month]
        again = sum(r["first_ease"] == 1 for r in ln)
        lines.append(
            f"  {month:8} {n:>5} {100 * c[SUSPEND] / n:>7.0f}% {100 * c[SCHEDULE] / n:>8.0f}%"
            f" {100 * c[LEARN] / n:>5.0f}%  {statistics.median(ivls) if ivls else '-':>15}"
            f"  {f'{again}/{len(ln)}' if ln else '-'}"
        )
    lines.append("  (a suspended card's month is its card's last modification, the only date it has)")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    td.add_settings_args(parser)
    args = parser.parse_args()
    settings = td.settings(args)
    ignore_tag = settings["ignore_tag"]
    words = {w["nid"]: w for w in td.read_jsonl(td.data_file("words.jsonl"))}
    col = td.open_collection(settings["collection"])
    try:
        notes = td.load_vocab_notes(col, settings["note_type"])
        tagged = {nid: n for nid, n in notes.items() if n.has_tag(td.NEW_WORD_TAG) and n.card}
        cids = [n.card.cid for n in tagged.values() if n.card]
        revlog = td.load_revlog(col, cids)
        mods = dict(col.db.all("select id, mod from cards where id in (%s)" % ",".join(
            map(str, cids))))
    finally:
        col.close()
    rows = []
    for nid, note in sorted(tagged.items()):
        assert note.card is not None
        rows.append(label_note(note, revlog.get(note.card.cid, []), ignore_tag,
                               mods.get(note.card.cid, 0)))
    labelled = [r for r in rows if r.get("label")]
    td.write_jsonl(td.data_file("labels.jsonl"), labelled)
    lines = report(rows, words, ignore_tag)
    path = td.report_file("labels.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[0])
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
