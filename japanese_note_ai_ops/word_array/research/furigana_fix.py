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
  differently. These change readings and okurigana, so the list is for checking them first.
A sentence with nothing either can fix (a kanji with no reading, several readings in one group)
is listed as left for the user, never written.

By default the script only lists: `output/furigana_fix_list.txt` and a count per kind; `--check`
also reads the notes and counts what `--apply` would write and refuse. `--apply`
writes each note whose field is still the sentence as dumped (`kanjify_fix.plan_writes`),
appending the old value to `output/furigana_fix_undo.jsonl` before each write, since Anki can't
undo `updateNoteFields`; `--revert` puts them back.

    py -3.10 word_array/research/furigana_fix.py [--labeller [--min-confidence 0.8]]
        [-n COUNT] [--check | --apply | --revert]
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
# The kinds of kanjify_golden.furigana_suspects that fix_groups repairs
REPAIRED = ("no space before the group", "kana inside the group")


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


def fix_row(row: dict, labeller: bool, min_confidence: float) -> tuple[str, list[str], list[str]]:
    """The sentence with the fixes applied, the fixes made and what was left unfixed."""
    text = row["sentence"]
    made: list[str] = []
    left: list[str] = []
    if labeller:
        use, left = labeller_fixes(row, min_confidence)
        for group, fix in use:
            if text.count(group) != 1:
                left.append(f"not in the sentence once: {group}")
                continue
            text = text.replace(group, fix)
            made.append(f"labeller: {group} -> {fix}")
    fixed = golden.fix_groups(text)
    found = [p for p in row.get("program", []) if p.startswith(REPAIRED)]
    if fixed != text:
        # a labeller's fix can join kana into a group (まだ五 分[ふん] -> まだ五分[ごぶ]) and so
        # leave it with no space before it, which no check of the dumped sentence saw
        made.append("program: " + ("; ".join(found) or "a space before a group a fix made"))
    left += [p for p in row.get("program", []) if not p.startswith(REPAIRED)]
    return fixed, made, left


def plan(rows: list[dict], labeller: bool, min_confidence: float) -> tuple[list[dict], list[str]]:
    """`kanjify_fix` rows for every sentence something fixes, and the listing of every row."""
    out, lines = [], []
    for row in rows:
        after, made, left = fix_row(row, labeller, min_confidence)
        nids = ",".join(map(str, row["nids"]))
        lines.append(f"[{row['sid']}] nid:{nids}")
        lines += [f"    fix    {m}" for m in made] + [f"    left   {x}" for x in left]
        if after != row["sentence"]:
            out.append({"row": row["sid"], "nids": row["nids"], "before": row["sentence"],
                        "after": after})  # fmt: skip
            lines += [f"    before {row['sentence']}", f"    after  {after}"]
    return out, lines


def counts(rows: list[dict], labeller: bool, min_confidence: float) -> Counter:
    c: Counter[str] = Counter()
    for row in rows:
        after, made, left = fix_row(row, labeller, min_confidence)
        c["sentences fixed" if after != row["sentence"] else "sentences left as they are"] += 1
        c.update(m.split(":")[0] + " fixes" for m in made)
        c.update("left: " + x.split(":")[0] for x in left)
    return c


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixes", type=Path, default=FIXES)
    parser.add_argument("--labeller", action="store_true", help="the labellers' fixes too")
    parser.add_argument("--min-confidence", type=float, default=0.8)
    parser.add_argument("-n", type=int, default=0, help="only the first COUNT rows")
    parser.add_argument("--undo", type=Path, default=UNDO)
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
        rows = kanjify_fix.read_jsonl(args.fixes)[: args.n or None]
        fixes, lines = plan(rows, args.labeller, args.min_confidence)
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
            for kind, n in sorted(counts(rows, args.labeller, args.min_confidence).items()):
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
