"""Word frequency ranks from outside lists, and the chance a word is known against its rank.

    py -3.10 word_array/research/triage_frequency.py [--freq-dir DIR]

The lists are downloaded by hand into `--freq-dir` (default: `frequency/` beside the working
copy of the collection), never into a repo:

- `jiten_global.csv` from jiten.moe (CC BY-SA 4.0; https://jiten.moe/other): ranks over anime,
  drama, novels, visual novels and manga, one row per JMdict spelling and reading, so
  homographs (生物 せいぶつ/なまもの) have their own;
  https://api.jiten.moe/api/frequency-list/download?downloadType=csv
- `BCCWJ_frequencylist_suw_ver1_0.zip` from NINJAL (free for research and teaching; CC BY-NC-ND
  3.0): short-unit lemmas with readings over 104M words of balanced written Japanese;
  https://repository.ninjal.ac.jp/record/3234/files/BCCWJ_frequencylist_suw_ver1_0.zip
- `tubelex-ja-310-lemma-pos.tsv.xz` from TUBELEX-ja (BSD-3-Clause): lemmas without readings over
  YouTube subtitles, with the number of channels using each;
  https://github.com/naist-nlp/tubelex

A note is matched by its spelling (`vocab-kanjified`, then `vocab`) with its reading, a kana
word by the reading alone; TUBELEX has no readings, so a spelling there stands for all of them.
Every vocab note gets a row in `frequency.jsonl`, for the model and for the curves below.

The curves (analysis 4), by Jiten rank:

- (a) from the studied cards: the share of Jiten's words at each rank the user has studied,
  times their recall today (triage_recall.py's recalibrated figure): what the studying alone
  accounts for, counting every unstudied word as unknown;
- (b) from the labels: the chance a judged word was known (scheduled or suspended, not left to
  learn), fitted as a logistic curve in log rank;
- combined: (a), plus the unstudied share times (b).

Where each crosses 50% is an effective vocabulary size. Writes `reports/frequency.txt`.
"""

from __future__ import annotations

import argparse
import csv
import io
import lzma
import math
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

import triage_data as td
from _bootstrap import load_root

to_hiragana = load_root("kana_conv").to_hiragana

JITEN = "jiten_global.csv"
BCCWJ = "BCCWJ_frequencylist_suw_ver1_0.zip"
TUBELEX = "tubelex-ja-310-lemma-pos.tsv.xz"
BINS = [1, 1000, 2000, 3000, 5000, 7500, 10000, 15000, 20000, 30000, 50000, 75000, 100000,
        150000, 400000]


def default_dir() -> Path:
    collection = td.read_settings().get("collection")
    return Path(collection).parent / "frequency" if collection else td.triage_dir() / "frequency"


def load_jiten(path: Path) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        next(reader)
        for word, form, rank in reader:
            key = (word, to_hiragana(form))
            r = int(rank)
            if key not in out or r < out[key]:
                out[key] = r
    return out


def load_bccwj(path: Path) -> dict[tuple[str, str], tuple[int, float]]:
    """(lemma, hiragana reading) -> (rank, per-million frequency), the commonest POS's."""
    out: dict[tuple[str, str], tuple[int, float]] = {}
    with zipfile.ZipFile(path) as z, z.open(z.namelist()[0]) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"), delimiter="\t")
        header = next(reader)
        col = {name: i for i, name in enumerate(header)}
        for row in reader:
            key = (row[col["lemma"]], to_hiragana(row[col["lForm"]]))
            rank, pmw = int(row[col["rank"]]), float(row[col["pmw"]])
            if key not in out or rank < out[key][0]:
                out[key] = (rank, pmw)
    return out


def load_tubelex(path: Path) -> dict[str, tuple[int, int]]:
    """lemma -> (rank by count, channels)."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    with lzma.open(path, "rt", encoding="utf-8") as fh:
        header = next(fh).rstrip("\n").split("\t")
        col = {name: i for i, name in enumerate(header)}
        for line in fh:
            row = line.rstrip("\n").split("\t")
            c = counts[row[col["word"]]]
            c[0] += int(row[col["count"]])
            c[1] = max(c[1], int(row[col["channels"]]))
    ordered = sorted(counts.items(), key=lambda kv: -kv[1][0])
    return {w: (i + 1, c[1]) for i, (w, c) in enumerate(ordered)}


def note_keys(note: td.VocabNote) -> list[tuple[str, str]]:
    reading = to_hiragana(note.reading)
    forms = [f for f in (note.kanjified, td.plain(note.get("vocab"))) if f]
    keys = [(f, reading) for f in dict.fromkeys(forms)]
    keys += [(to_hiragana(f), reading) for f in forms if to_hiragana(f) == reading]
    return list(dict.fromkeys(keys))


def ranks_for(note: td.VocabNote, jiten, bccwj, tubelex) -> dict:
    keys = note_keys(note)
    row: dict = {"nid": note.nid}
    j = [(jiten[k], k) for k in keys if k in jiten]
    if j:
        rank, key = min(j)
        row["freq_jiten_rank"] = rank
        row["jiten_key"] = list(key)
    b = [bccwj[k] for k in keys if k in bccwj]
    if b:
        row["freq_bccwj_rank"] = min(b)[0]
    t = [tubelex[f] for f, _ in keys if f in tubelex]
    if t:
        best = min(t)
        row["freq_tubelex_rank"] = best[0]
        row["tubelex_channels"] = best[1]
    return row


def logistic_fit(x: list[float], y: list[float], w: Optional[list[float]] = None):
    """A 1-D logistic regression, `p = sigmoid(a + b x)`, with soft targets allowed."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    xs, ys, ws = [], [], []
    for xi, yi, wi in zip(x, y, w or [1.0] * len(x)):
        xs += [[xi], [xi]]
        ys += [1, 0]
        ws += [wi * yi, wi * (1 - yi)]
    model = LogisticRegression(C=1e4).fit(np.array(xs), np.array(ys), sample_weight=np.array(ws))
    return float(model.intercept_[0]), float(model.coef_[0][0])


def crossing(a: float, b: float) -> Optional[float]:
    """The rank where `sigmoid(a + b log10 rank)` is 50%."""
    return 10 ** (-a / b) if b else None


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--freq-dir", type=Path, default=None)
    td.add_settings_args(parser)
    args = parser.parse_args()
    settings = td.settings(args)
    folder = args.freq_dir or default_dir()
    missing = [n for n in (JITEN, BCCWJ, TUBELEX) if not (folder / n).exists()]
    if missing:
        sys.exit(f"missing in {folder}: {', '.join(missing)} (see this script's docstring)")
    jiten = load_jiten(folder / JITEN)
    bccwj = load_bccwj(folder / BCCWJ)
    tubelex = load_tubelex(folder / TUBELEX)

    col = td.open_collection(settings["collection"])
    try:
        notes = td.load_vocab_notes(col, settings["note_type"])
    finally:
        col.close()
    rows = [ranks_for(note, jiten, bccwj, tubelex) for _, note in sorted(notes.items())]
    td.write_jsonl(td.data_file("frequency.jsonl"), rows)
    by_nid = {r["nid"]: r for r in rows}
    tagged = [by_nid[n] for n, note in notes.items() if note.has_tag(td.NEW_WORD_TAG)]

    lines = [f"lists: Jiten {len(jiten)} spellings, BCCWJ {len(bccwj)} lemmas,"
             f" TUBELEX {len(tubelex)} lemmas", ""]
    for name in ("jiten", "bccwj", "tubelex"):
        lines.append(f"  {name:8} ranks {sum(f'freq_{name}_rank' in r for r in rows):>6} of"
                     f" {len(rows)} vocab notes, {sum(f'freq_{name}_rank' in r for r in tagged):>6}"
                     f" of {len(tagged)} triage notes")
    lines.append("")

    # (a) studied share times recall, per Jiten rank bin
    recall = {r["nid"]: r for r in td.read_jsonl(td.data_file("recall_now.jsonl"))
              if not r["suspended"]}
    studied_keys: dict[tuple, float] = {}
    for nid, r in recall.items():
        key = by_nid.get(nid, {}).get("jiten_key")
        if key:
            k = tuple(key)
            studied_keys[k] = max(studied_keys.get(k, 0.0), r["r_cal"])
    labels = {r["nid"]: r["label"] for r in td.read_jsonl(td.data_file("labels.jsonl"))}
    lab_x, lab_y = [], []
    for nid, label in labels.items():
        rank = by_nid.get(nid, {}).get("freq_jiten_rank")
        if rank:
            lab_x.append(math.log10(rank))
            lab_y.append(0.0 if label == "learn" else 1.0)
    a, b = logistic_fit(lab_x, lab_y)
    bins: dict[int, list[float]] = defaultdict(lambda: [0, 0, 0.0])  # entries, studied, recall sum
    for key, rank in jiten.items():
        for lo, hi in zip(BINS, BINS[1:]):
            if lo <= rank < hi:
                cell = bins[lo]
                cell[0] += 1
                if key in studied_keys:
                    cell[1] += 1
                    cell[2] += studied_keys[key]
                break
    lines.append("--- the chance a word is known, by Jiten rank ---")
    lines.append(f"  {'rank':>15} {'words':>7} {'studied':>8} {'(a) studied':>12}"
                 f" {'(b) labels':>11} {'combined':>9}")
    curve_a, curve_c, cumulative = [], [], [0.0, 0.0, 0.0]
    for lo, hi in zip(BINS, BINS[1:]):
        entries, studied, rsum = bins[lo]
        if not entries:
            continue
        pa = rsum / entries
        mid = math.sqrt(lo * hi)
        pb = sigmoid(a + b * math.log10(mid))
        pc = pa + (1 - studied / entries) * pb
        curve_a.append((lo, hi, pa))
        curve_c.append((lo, hi, pc))
        cumulative[0] += rsum
        cumulative[1] += pc * entries
        cumulative[2] += entries
        lines.append(f"  {lo:>7}-{hi - 1:<7} {entries:>7} {100 * studied / entries:>7.1f}%"
                     f" {100 * pa:>11.1f}% {100 * pb:>10.1f}% {100 * pc:>8.1f}%"
                     f"   known so far: studied {cumulative[0]:>7.0f}, all {cumulative[1]:>7.0f}")
    lines.append("")

    def first_below(curve: list) -> str:
        for lo, hi, p in curve:
            if p < 0.5:
                return f"in the {lo}-{hi - 1} band"
        return "not within the list"

    cross_b = crossing(a, b)
    lines.append("--- where each crosses 50% ---")
    lines.append(f"  (a) studied cards' recall:  {first_below(curve_a)}")
    lines.append(f"  (b) labels, fitted: p = sigmoid({a:.2f} + {b:.2f} log10 rank), 50% at rank"
                 f" {cross_b:,.0f}" if cross_b else "  (b) labels: no crossing")
    lines.append(f"  combined:                   {first_below(curve_c)}")
    lines.append(f"  labelled notes with a Jiten rank: {len(lab_x)} of {len(labels)}")
    lines.append("")
    lines.append("Read (a) as a floor: it counts only what was studied and is recalled. (b) is"
                 " fitted on words the user met in their own sentences and judged, which are not"
                 " a random draw of words at their rank, and on few 'learn' labels: its crossing"
                 " is an extrapolation when it falls past the labelled words' ranks.")
    path = td.report_file("frequency.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
