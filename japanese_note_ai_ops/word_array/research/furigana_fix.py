"""Writes the kanjify golden set's furigana fix list into the notes' furigana sentence, through
AnkiConnect.

`kanjify_golden_collate.py` leaves `collated/furigana_fixes.jsonl` in the test data checkout, one
row per sentence with a furigana problem: its note ids, the sentence as dumped, what a program
found (`program`; for a missing space before a group and katakana written inside a kanji
word's group, the repaired sentence is in `fixed`) and what the step 2 labellers found
(`labeller`: a group as the input writes it, its corrected text and a confidence).

The fixes go into the config's `furigana_sentence_field`, the field the user edits:
`kanjified_sentence_field` is generated from it by the kanjify op, and a repair made only there
is undone the next time it is generated. The golden set labels a fixed sentence again, and its
label reaches the kanjified field through `kanjify_fix.py`.

What a sentence gets:
- the program's repairs, always: they change no word and no reading, only where a group starts
  and which kana its reading covers (`kanjify_golden.fix_groups`);
- with `--labeller`, the labellers' fixes too, each replacing its group where the sentence holds
  it exactly once, at `--min-confidence` or more, none where two labellers fixed one group
  differently. These change readings and okurigana, and some fix a typo (listed as
  `labeller, text`), so the list is for checking them first.
- with `--readings`, the reading pass's sentence where the row has one (`reading`, from
  `kanjify_golden_render.py readings`): an agent shown the row's findings wrote the whole
  sentence with its furigana fixed, so it replaces the other fixes. It is used at
  `--min-confidence` or more, when its text is the input's (or the agent says it fixed a typo,
  listed as `reading, text`) and the program finds nothing wrong with it; otherwise the row
  falls back to the others and lists why.
A sentence with nothing any of them can fix (a kanji with no reading, several readings in one
group) is listed as left for the user, never written.

By default the script only lists: `output/furigana_fix_list.txt` and a count per kind; `--check`
also reads the notes and counts what `--apply` would write and refuse. `--apply`
writes each note whose field is still the sentence as dumped (`kanjify_fix.plan_writes`),
appending the old value to `output/furigana_fix_undo.jsonl` before each write, since Anki can't
undo `updateNoteFields`; `--revert` puts them back.

`--redo OLD_FIXES` plans from the undo log instead: for each note a run from the fix list
OLD_FIXES wrote, and this version would have fixed otherwise, it writes this version's fix over
what that run wrote (the same modes list, check and write it).

    py -3.10 word_array/research/furigana_fix.py [--labeller] [--readings]
        [--min-confidence 0.8] [--redo OLD_FIXES] [-n COUNT] [--check | --apply | --revert]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from _bootstrap import ADDON_ROOT

import anki_connect
import kanjify_fix
import kanjify_golden as golden

OUTPUT = ADDON_ROOT / "output"
FIXES = golden.COLLATED / "furigana_fixes.jsonl"
UNDO = OUTPUT / "furigana_fix_undo.jsonl"
LIST = OUTPUT / "furigana_fix_list.txt"
FIELD_KEY = "furigana_sentence_field"


def without_context(fix: str, before: str, after: str) -> str:
    """The fix less the sentence text it repeats from either side of its group.

    A labeller sometimes wrote a group's fix with the words around it, up to the whole
    sentence. Put in place of the group, those words were doubled (人[ひと]にに), and the
    first run with --labeller wrote 19 such sentences into notes.
    """
    lead = next((k for k in range(min(len(before), len(fix)), 0, -1)
                 if before.endswith(fix[:k])), 0)  # fmt: skip
    fix = fix[lead:]
    tail = next((k for k in range(min(len(after), len(fix)), 0, -1)
                 if after.startswith(fix[len(fix) - k :])), 0)  # fmt: skip
    return fix[: len(fix) - tail]


def labeller_fixes(row: dict, min_confidence: float) -> tuple[list[tuple[str, str]], list[str]]:
    """The labellers' fixes to use, as (group, fix), and why the others are left out."""
    by_group: dict[str, set[str]] = {}
    left = []
    for f in row.get("labeller") or []:
        if f.get("confidence", 0) < min_confidence:
            left.append(f"confidence {f.get('confidence')}: {f['group']} -> {f['fix']}")
        elif f["group"] != f["fix"]:
            by_group.setdefault(f["group"], set()).add(f["fix"])
    use = []
    for group, fixes in by_group.items():
        if len(fixes) > 1:
            left.append(f"labellers disagree: {group} -> {' / '.join(sorted(fixes))}")
        else:
            use.append((group, fixes.pop()))
    return use, left


def reading_problem(row: dict) -> str | None:
    """Why the reading pass's sentence for the row can't be written, or None."""
    reading = row["reading"]
    fixed = golden.fix_groups(reading["fixed"])
    if not fixed.strip():
        return "no sentence"
    text_changed = golden.plain_text(fixed) != golden.plain_text(row["sentence"])
    if text_changed and not reading["text_changed"]:
        return "the text changed, and the agent doesn't say it fixed a typo"
    still = golden.furigana_suspects(fixed)
    return "still " + "; ".join(still) if still else None


def fix_row(row: dict, labeller: bool, min_confidence: float,
            readings: bool = False) -> tuple[str, list[str], list[str]]:  # fmt: skip
    """The sentence with the fixes applied, the fixes made and what was left unfixed."""
    text = row["sentence"]
    made: list[str] = []
    left: list[str] = []
    if readings and row.get("reading"):
        reading = row["reading"]
        problem = reading_problem(row)
        if problem is None and reading["confidence"] < min_confidence:
            problem = f"confidence {reading['confidence']}"
        if problem is None:
            # the agent was shown the program's and the labellers' findings and decided each,
            # so its sentence replaces their fixes rather than adding to them
            fixed = golden.fix_groups(reading["fixed"])
            text_changed = golden.plain_text(fixed) != golden.plain_text(text)
            kind = "reading, text" if text_changed else "reading"
            made = [f"{kind}: {c['was']} -> {c['now']} ({c['why']})" for c in reading["changes"]]
            unsure = [f"reading pass unsure: {reading['unsure']}"] if reading["unsure"] else []
            return fixed, made, unsure
        left.append(f"reading pass not used: {problem}: {reading['fixed']}")
    if labeller:
        use, dropped = labeller_fixes(row, min_confidence)
        left += dropped
        for group, fix in use:
            if text.count(group) != 1:
                left.append(f"not in the sentence once: {group}")
                continue
            at = text.index(group)
            before, after = text[:at], text[at + len(group) :]
            if golden.plain_text(fix) != golden.plain_text(group):
                # only a fix that changes the text can hold its context: in one that changes
                # readings alone, a match with the text beside it is the group's own kana
                fix = without_context(fix, before, after)
            if not fix.strip():
                left.append(f"nothing left of the fix: {group}")
                continue
            text = before + fix + after
            same = golden.plain_text(fix) == golden.plain_text(group)
            made.append(f"{'labeller' if same else 'labeller, text'}: {group} -> {fix}")
    fixed = golden.fix_groups(text)
    found = [p for p in row.get("program", []) if p.startswith(golden.REPAIRED)]
    if fixed != text:
        # a labeller's fix can join kana into a group (まだ五 分[ふん] -> まだ五分[ごぶ]) and so
        # leave it with no space before it, which no check of the dumped sentence saw
        made.append("program: " + ("; ".join(found) or "a space before a group a fix made"))
    left += [p for p in row.get("program", []) if not p.startswith(golden.REPAIRED)]
    return fixed, made, left


def plan(rows: list[dict], labeller: bool, min_confidence: float,
         readings: bool = False) -> tuple[list[dict], list[str]]:  # fmt: skip
    """`kanjify_fix` rows for every sentence something fixes, and the listing of every row."""
    out, lines = [], []
    for row in rows:
        after, made, left = fix_row(row, labeller, min_confidence, readings)
        nids = ",".join(map(str, row["nids"]))
        lines.append(f"[{row['sid']}] nid:{nids}")
        lines += [f"    fix    {m}" for m in made] + [f"    left   {x}" for x in left]
        if after != row["sentence"]:
            out.append({"row": row["sid"], "nids": row["nids"], "before": row["sentence"],
                        "after": after})  # fmt: skip
            lines += [f"    before {row['sentence']}", f"    after  {after}"]
    return out, lines


def redo(rows: list[dict], undo: list[dict], min_confidence: float) -> tuple[list[dict], list[str]]:
    """`kanjify_fix` rows that rewrite what an older version of this script wrote from `rows`
    (the fix list that run read) where this version fixes the sentence otherwise, and their
    listing.

    A write is redone when it is neither this version's fix of its sentence with the labellers'
    fixes nor without them, so the flags that run had need not be known. It is redone from what
    was written, which `kanjify_fix.plan_writes` checks the note still holds.
    """
    by_nid = {nid: row for row in rows for nid in row["nids"]}
    by_sid: dict[str, dict] = {}
    for e in undo:
        row = by_nid.get(e["nid"])
        if row is None or e["before"].strip() != row["sentence"].strip():
            continue
        plans = {fix_row(row, labeller, min_confidence)[0].strip() for labeller in (False, True)}
        if e["after"].strip() in plans:
            continue
        written = e["after"].strip()
        key = row["sid"] + written
        if key not in by_sid:
            by_sid[key] = {"row": row["sid"], "nids": [], "before": written,
                           "after": fix_row(row, True, min_confidence)[0]}  # fmt: skip
        by_sid[key]["nids"].append(e["nid"])
    lines = []
    for r in by_sid.values():
        lines += [f"[{r['row']}] nid:{','.join(map(str, r['nids']))}",
                  f"    written {r['before']}", f"    now     {r['after']}"]  # fmt: skip
    return list(by_sid.values()), lines


def counts(rows: list[dict], labeller: bool, min_confidence: float,
           readings: bool = False) -> Counter:  # fmt: skip
    c: Counter[str] = Counter()
    for row in rows:
        after, made, left = fix_row(row, labeller, min_confidence, readings)
        c["sentences fixed" if after != row["sentence"] else "sentences left as they are"] += 1
        c.update(m.split(":")[0] + " fixes" for m in made)
        c.update("left: " + x.split(":")[0] for x in left)
    return c


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixes", type=Path, default=FIXES)
    parser.add_argument("--labeller", action="store_true", help="the labellers' fixes too")
    parser.add_argument("--readings", action="store_true", help="the reading pass's sentences")
    parser.add_argument("--min-confidence", type=float, default=0.8)
    parser.add_argument("-n", type=int, default=0, help="only the first COUNT rows")
    parser.add_argument("--undo", type=Path, default=UNDO)
    parser.add_argument("--redo", type=Path, metavar="OLD_FIXES",
                        help="rewrite what a run from this older fix list wrote otherwise")  # fmt: skip
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="read the notes and count what --apply would write")  # fmt: skip
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", action="store_true")
    args = parser.parse_args()

    client = anki_connect.AnkiConnect(args.anki_connect)
    config = anki_connect.load_config()
    try:
        if args.revert:
            reverted, refused = kanjify_fix.revert(client, config, args.undo, FIELD_KEY)
            print("\n".join(refused + [f"reverted {reverted} notes"]))
            return 0
        if args.redo:
            rows = []
            fixes, lines = redo(kanjify_fix.read_jsonl(args.redo),
                                kanjify_fix.read_jsonl(args.undo), args.min_confidence)  # fmt: skip
        else:
            rows = kanjify_fix.read_jsonl(args.fixes)[: args.n or None]
            fixes, lines = plan(rows, args.labeller, args.min_confidence, args.readings)
        if args.check:
            nids = sorted({nid for f in fixes for nid in f["nids"]})
            infos = kanjify_fix.notes_info(client, nids)
            writes, refused = kanjify_fix.plan_writes(fixes, infos, config, FIELD_KEY)
            why = Counter(r.split(": ", 1)[1] for r in refused)
            print(f"--apply would write {len(writes)} notes of {len(fixes)} sentences")
            for reason, n in why.most_common():
                print(f"{n:6} refused: {reason}")
            return 0
        if not args.apply:
            OUTPUT.mkdir(exist_ok=True)
            LIST.write_text("\n".join(lines) + "\n", encoding="utf-8")
            found = counts(rows, args.labeller, args.min_confidence, args.readings)
            for kind, n in sorted(found.items()):
                print(f"{n:6} {kind}")
            print(f"{len(fixes)} sentences to write, list in {LIST}")
            print("check it, then rerun with --apply")
            return 0
        written, refused = kanjify_fix.apply(client, fixes, config, args.undo, FIELD_KEY)
        print("\n".join(refused))
        print(f"wrote {written} notes, refused {len(refused)}")
        print(f"old values in {args.undo} (--revert puts them back)")
        return 0
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
