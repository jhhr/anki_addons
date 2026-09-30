"""JMdict's frequency marks and usage notes for every vocab note, as triage features.

    py -3.10 word_array/research/triage_jmdict.py [--jmdict JMdict_e.gz]

The addon's index (word_array/jmdict_index.py) keeps only whether a spelling is common, so this
reads JMdict_e.gz itself: each spelling's and reading's priority tags (`nfXX`, the 500-word band
of its rank in a newspaper corpus, 01 the commonest; `news1/2`, `ichi1/2`, `spec1/2`, `gai1/2`),
its spelling notes (rarely used, irregular, outdated or search-only kanji) and its senses' usage
notes (archaic, obsolete, rare, colloquial, slang, honorific, idiomatic, a specialist field...).

A note is matched by its `vocab-kanjified` or `vocab` spelling together with its reading, a
kana spelling by the reading alone, and a `〜する` verb by its noun when JMdict has no entry for
the verb itself. Where several entries match, the commonest mark wins. Every vocab note gets a
row, the studied ones too, for triage_frequency.py's curves. Writes `jmdict_features.jsonl`.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import triage_data as td
from _bootstrap import ADDON_ROOT, data_paths, load_root

to_hiragana = load_root("kana_conv").to_hiragana

ENTITY_RE = re.compile(r"&([\w.-]+);")
NF_RE = re.compile(r"nf(\d\d)")
PRIORITY_KINDS = ("news", "ichi", "spec", "gai")
# The sense notes kept as features; the rest are rare enough to be noise
MISC = ("arch", "obs", "rare", "obsc", "uk", "col", "sl", "vulg", "derog", "hon", "hum", "pol",
        "fam", "male", "fem", "id", "yoji", "on-mim", "abbr", "poet", "form", "chn", "joc", "net-sl",
        "sens", "dated", "hist", "lit")
SPELLING_NOTES = ("rK", "iK", "oK", "sK", "ateji", "io")


def default_jmdict() -> Path:
    """The addon's download, here or in the main checkout when this is a linked worktree."""
    here = ADDON_ROOT / "user_files" / "jmdict" / "JMdict_e.gz"
    if here.exists():
        return here
    main = data_paths().main_checkout(ADDON_ROOT.parent)
    if main is not None:
        there = main / ADDON_ROOT.name / "user_files" / "jmdict" / "JMdict_e.gz"
        if there.exists():
            return there
    return here


class Mark:
    """What JMdict says of one spelling with one reading."""

    def __init__(self) -> None:
        self.nf: Optional[int] = None
        self.levels: dict[str, int] = {}  # "news": 1 or 2 (1 the commoner)
        self.spelling_notes: set[str] = set()
        self.misc_first: set[str] = set()
        self.misc_any: set[str] = set()
        self.fields: set[str] = set()
        self.senses = 0
        self.entries = 0

    def add_priority(self, tags: list[str]) -> None:
        for tag in tags:
            m = NF_RE.fullmatch(tag)
            if m:
                band = int(m.group(1))
                self.nf = band if self.nf is None else min(self.nf, band)
                continue
            for kind in PRIORITY_KINDS:
                if tag.startswith(kind) and tag[len(kind):].isdigit():
                    level = int(tag[len(kind):])
                    self.levels[kind] = min(self.levels.get(kind, 9), level)

    def merge(self, other: "Mark") -> None:
        if other.nf is not None:
            self.nf = other.nf if self.nf is None else min(self.nf, other.nf)
        for kind, level in other.levels.items():
            self.levels[kind] = min(self.levels.get(kind, 9), level)
        self.spelling_notes |= other.spelling_notes
        self.misc_first |= other.misc_first
        self.misc_any |= other.misc_any
        self.fields |= other.fields
        self.senses = max(self.senses, other.senses)
        self.entries += other.entries

    def row(self) -> dict:
        row: dict = {"jm_found": True, "jm_nf": self.nf, "jm_senses": self.senses,
                     "jm_entries": self.entries, "jm_common": bool(self.nf or self.levels)}
        for kind in PRIORITY_KINDS:
            row[f"jm_{kind}"] = {1: 2, 2: 1}.get(self.levels.get(kind, 0), 0)
        for note in SPELLING_NOTES:
            row[f"jm_k_{note}"] = note in self.spelling_notes
        for misc in MISC:
            row[f"jm_first_{misc}"] = misc in self.misc_first
            row[f"jm_any_{misc}"] = misc in self.misc_any
        row["jm_field"] = bool(self.fields)
        # Only what is set: every note's false flags made the file 40 MB. A reader takes a
        # missing key for false or 0 (triage_features.jmdict_features)
        return {k: v for k, v in row.items() if v or k in ("jm_found", "jm_nf")}


def parse(path: Path) -> dict[tuple[str, str], Mark]:
    """(spelling, hiragana reading) -> Mark, kana spellings under (reading, reading)."""
    marks: dict[tuple[str, str], Mark] = defaultdict(Mark)
    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=("end",))
    in_body = False
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not in_body:
                start = line.find("<JMdict>")
                if start < 0:
                    continue
                line, in_body = line[start:], True
            parser.feed(ENTITY_RE.sub(r"\1", line))
            # Only "end" events are asked for, each ("end", element), but the stub's event type
            # covers every kind's shape
            for event in parser.read_events():
                el = event[-1]
                if not isinstance(el, ET.Element) or el.tag != "entry":
                    continue
                add_entry(el, marks)
                el.clear()
    parser.close()
    return marks


def add_entry(el: ET.Element, marks: dict) -> None:
    kanji = []
    for k in el.iter("k_ele"):
        keb = k.findtext("keb")
        if keb:
            kanji.append((keb, [p.text for p in k.iter("ke_pri") if p.text],
                          {i.text for i in k.iter("ke_inf") if i.text}))
    senses = list(el.iter("sense"))
    misc_first = {m.text for m in senses[0].iter("misc") if m.text} if senses else set()
    misc_any = {m.text for s in senses for m in s.iter("misc") if m.text}
    fields = {f.text for s in senses for f in s.iter("field") if f.text}
    for r in el.iter("r_ele"):
        reb = r.findtext("reb")
        if not reb:
            continue
        reading = to_hiragana(reb)
        re_pri = [p.text for p in r.iter("re_pri") if p.text]
        restr = {x.text for x in r.iter("re_restr") if x.text}
        nokanji = r.find("re_nokanji") is not None
        pairs = [] if nokanji else [(keb, pri, inf) for keb, pri, inf in kanji
                                   if not restr or keb in restr]
        pairs.append((reading, re_pri, set()))
        for form, pri, inf in pairs:
            mark = Mark()
            # A reading's own marks count for its kana spelling; a kanji spelling's for it
            mark.add_priority(pri if form != reading or not kanji else pri + re_pri)
            mark.spelling_notes = set(inf)
            mark.misc_first, mark.misc_any, mark.fields = set(misc_first), set(misc_any), fields
            mark.senses, mark.entries = len(senses), 1
            marks[(form, reading)].merge(mark)


def match(note: td.VocabNote, marks: dict) -> tuple[Optional[Mark], str]:
    reading = to_hiragana(note.reading)
    forms = list(dict.fromkeys(f for f in (note.kanjified, td.plain(note.get("vocab"))) if f))
    for how, candidates in (
        ("spelling", [(f, reading) for f in forms]),
        ("kana", [(to_hiragana(f), reading) for f in forms]),
        ("suru", [(f[:-2], reading[:-2]) for f in forms
                  if f.endswith("する") and reading.endswith("する")]),
    ):
        found = [marks[c] for c in candidates if c in marks]
        if found:
            best = Mark()
            for m in found:
                best.merge(m)
            return best, how
    return None, "none"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jmdict", type=Path, default=None)
    td.add_settings_args(parser)
    args = parser.parse_args()
    settings = td.settings(args)
    path = args.jmdict or default_jmdict()
    if not path.exists():
        sys.exit(f"no JMdict at {path}: give --jmdict")
    marks = parse(path)
    col = td.open_collection(settings["collection"])
    try:
        notes = td.load_vocab_notes(col, settings["note_type"])
    finally:
        col.close()
    rows: list[dict] = []
    how_counts: Counter[str] = Counter()
    for nid, note in sorted(notes.items()):
        mark, how = match(note, marks)
        how_counts[how] += 1
        row = {"nid": nid, "jm_match": how}
        row.update(mark.row() if mark else {"jm_found": False})
        rows.append(row)
    td.write_jsonl(td.data_file("jmdict_features.jsonl"), rows)
    tagged = {n for n, note in notes.items() if note.has_tag(td.NEW_WORD_TAG)}
    tagged_rows = [r for r in rows if r["nid"] in tagged]
    print(f"{len(marks)} JMdict spellings; {len(rows)} notes: " +
          ", ".join(f"{k} {v}" for k, v in how_counts.most_common()))
    print(f"tagged notes: {sum(r['jm_found'] for r in tagged_rows)}/{len(tagged_rows)} found,"
          f" {sum(bool(r.get('jm_nf')) for r in tagged_rows)} with an nf band")
    return 0


if __name__ == "__main__":
    sys.exit(main())
