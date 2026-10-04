"""Each triage word's family in the sentence word arrays: the words it is part of, and the words
it is made of.

    py -3.10 word_array/research/triage_family.py

A word array nests a word's parts under it (飛行機 -> 飛行 + 機, 様に成る -> 様に + 成る, 様に -> 様 +
に), and an element matched to a vocab note names it in its match data. So across every vocab
note's array, a triage note's linked elements give the notes linked above it, up to the top-level
word (its parents), and below it, all the way down (its parts). The user's observation: a
top-level word whose parts they have studied is likely known, and so is a part of a top-level word
they have studied. Each relative is described as the judging page shows it, with its card's state
read the way triage_labels.py reads a judged note's:

- `suspended`: every card suspended;
- `scheduled`: not new and carrying the ignore tag, set due as known without being studied;
- `studied`: not new, without the tag;
- `new`.

`tagged` says the relative is a triage note itself. Sentences are counted by their text, since a
note made from a word keeps the sentence it came from: the same sentence in several notes is one.

Reads the working copy (settings.json, as the other triage scripts), so it describes the notes
`words.jsonl` was written from when run on the same copy. Writes `family.jsonl`, per triage note:
how many sentences link it, in how many it is a top-level word, and its parents and parts (with
the sentences linking each and the fewest levels between), and `reports/family.txt`, the Anki-
judged notes' families by label.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import triage_data as td
from triage_exposure import ARRAY_FIELD, read_array
from _bootstrap import load

match_flags = load("match_flags")

SUSPENDED, SCHEDULED, STUDIED, NEW = "suspended", "scheduled", "studied", "new"
KNOWN = (SCHEDULED, STUDIED)


@dataclass
class Relative:
    sentences: set[str] = field(default_factory=set)
    gap: int = 99  # the fewest array levels between the two


@dataclass
class Family:
    sentences: set[str] = field(default_factory=set)
    top: set[str] = field(default_factory=set)
    parents: dict[int, Relative] = field(default_factory=lambda: defaultdict(Relative))
    parts: dict[int, Relative] = field(default_factory=lambda: defaultdict(Relative))


def linked_note(element: list) -> Optional[int]:
    data = element[4] if len(element) > 4 else None
    first = data[0] if data else None
    if isinstance(first, bool) or not isinstance(first, int) or first <= 0:
        return None
    return first


def _relate(relatives: dict[int, Relative], nid: int, sentence: str, gap: int) -> None:
    relative = relatives[nid]
    relative.sentences.add(sentence)
    relative.gap = min(relative.gap, gap)


def _visit(arr: list, above: list[tuple[int, int]], depth: int, sentence: str,
           out: dict[int, Family]) -> list[tuple[int, int]]:
    """Records every linked element of `arr`; returns the (note, depth) linked in it."""
    found: list[tuple[int, int]] = []
    for element in arr:
        if len(element) <= 1:  # a tag or punctuation
            continue
        target = linked_note(element)
        chain = above + [(target, depth)] if target else above
        below = _visit(element[5] if len(element) > 5 else [], chain, depth + 1, sentence, out)
        if target:
            family = out[target]
            family.sentences.add(sentence)
            if depth == 0:
                family.top.add(sentence)
            for nid, d in above:
                if nid != target:
                    _relate(family.parents, nid, sentence, depth - d)
            for nid, d in below:
                if nid != target:
                    _relate(family.parts, nid, sentence, d - depth)
            found.append((target, depth))
        found.extend(below)
    return found


def families(arrays: Iterable[list]) -> dict[int, Family]:
    """Note id -> what the arrays link above and below it."""
    out: dict[int, Family] = defaultdict(Family)
    for arr in arrays:
        sentence = "".join(match_flags.plain_text(e[0]) for e in arr if e)
        _visit(arr, [], 0, sentence, out)
    return out


def state(note: td.VocabNote, ignore_tag: str) -> str:
    if not note.cards:
        return NEW
    if all(c.suspended for c in note.cards):
        return SUSPENDED
    if any(not c.is_new for c in note.cards):
        return SCHEDULED if note.has_tag(ignore_tag) else STUDIED
    return NEW


def relatives(found: dict[int, Relative], notes: dict[int, td.VocabNote],
              ignore_tag: str) -> list[dict]:
    rows: list[dict[str, Any]] = []
    for nid, relative in found.items():
        note = notes.get(nid)
        if note is None:
            continue
        rows.append({
            "nid": nid,
            "key": note.key,
            "reading": note.reading,
            "meaning": td.plain(note.get("vocab-translation")),
            "state": state(note, ignore_tag),
            "tagged": note.has_tag(td.NEW_WORD_TAG),
            "sentences": len(relative.sentences),
            "gap": relative.gap,
        })
    rows.sort(key=lambda r: (r["gap"], -r["sentences"], r["nid"]))
    return rows


def family_rows(notes: dict[int, td.VocabNote], found: dict[int, Family],
                ignore_tag: str) -> list[dict]:
    rows = []
    for nid, note in sorted(notes.items()):
        if not note.has_tag(td.NEW_WORD_TAG) or note.card is None:
            continue
        family = found.get(nid) or Family()
        rows.append({
            "nid": nid,
            "sentences": len(family.sentences),
            "top_level": len(family.top),
            "parents": relatives(family.parents, notes, ignore_tag),
            "parts": relatives(family.parts, notes, ignore_tag),
        })
    return rows


def known_share(rels: list[dict]) -> Optional[float]:
    return sum(r["state"] in KNOWN for r in rels) / len(rels) if rels else None


def report(rows: list[dict], labels: dict[int, str], problems: Counter, missing: int) -> list[str]:
    lines = [
        f"{len(rows)} triage notes: {sum(r['sentences'] > 0 for r in rows)} linked in a word"
        f" array, {sum(r['top_level'] > 0 for r in rows)} as a top-level word somewhere,"
        f" {sum(bool(r['parts']) for r in rows)} with linked parts,"
        f" {sum(bool(r['parents']) for r in rows)} with a linked parent",
        "broken word arrays: " + (", ".join(f"{k} {v}" for k, v in problems.items()) or "none")
        + f"; links to no vocab note: {missing}",
        "",
        "--- the notes judged in Anki, by label: their parts and parents ---",
        "  a relative is known when studied or scheduled",
        f"  {'label':9} {'notes':>6}  {'w/ parts':>8} {'parts all known':>15}"
        f"  {'w/ parent':>9} {'a parent known':>14}",
    ]
    for label in ("suspend", "schedule", "learn", None):
        got = [r for r in rows if labels.get(r["nid"]) == label]
        if not got:
            continue
        with_parts = [r for r in got if r["parts"]]
        all_known = sum(known_share(r["parts"]) == 1.0 for r in with_parts)
        with_parent = [r for r in got if r["parents"]]
        parent_known = sum(any(p["state"] in KNOWN for p in r["parents"]) for r in with_parent)

        def pct(n: int, of: list) -> str:
            return f"{100 * n / len(of):5.1f}%" if of else "    -"

        lines.append(f"  {label or 'unjudged':9} {len(got):>6}  {len(with_parts):>8}"
                     f" {pct(all_known, with_parts):>15}  {len(with_parent):>9}"
                     f" {pct(parent_known, with_parent):>14}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    td.add_settings_args(parser)
    args = parser.parse_args()
    settings = td.settings(args)
    col = td.open_collection(settings["collection"])
    try:
        notes = td.load_vocab_notes(col, settings["note_type"])
    finally:
        col.close()
    problems: Counter = Counter()
    arrays = (read_array(n.get(ARRAY_FIELD), problems) for n in notes.values()
              if n.get(ARRAY_FIELD).strip())
    found = families(arrays)
    missing = sum(1 for nid in {p for f in found.values() for p in (*f.parents, *f.parts)}
                  if nid not in notes)
    rows = family_rows(notes, found, settings["ignore_tag"])
    td.write_jsonl(td.data_file("family.jsonl"), rows)

    words = {r["nid"] for r in td.read_jsonl(td.data_file("words.jsonl"))}
    labels = {r["nid"]: r["label"] for r in td.read_jsonl(td.data_file("labels.jsonl"))}
    lines = report(rows, labels, problems, missing)
    unmatched = len({r["nid"] for r in rows} ^ words)
    if unmatched:
        lines.insert(0, f"NOTE: {unmatched} notes differ from words.jsonl's: run this on the copy"
                        " triage_extract.py words read, or refresh the rest too")
    path = td.report_file("family.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"{len(rows)} notes -> {td.data_file('family.jsonl')}; report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
