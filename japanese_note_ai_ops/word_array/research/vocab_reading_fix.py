"""Repairs the vocab notes whose `vocab-kana` is damaged, where the damage can be shown.

A note and a word array element that spell the same word but read it differently mean one of
the two readings is wrong. Which one is not a thing to guess: readings that differ only by
voicing run both ways - the note holds the rendaku in 狡賢い[ずるがしこい] and 三つ子[みつご],
the array holds it in 砂埃[すなぼこり] - and for a word written in kanji any reading can be
drawn onto the kanji, so the furigana says nothing either.

So nothing here is decided by the family alone. Each case is put to evidence outside the pair,
and only a case the evidence settles is written:

- the spelling is the reading. A katakana or kana word transliterates to exactly one reading,
  so whichever side matches it is right and the other is the typo: ファースト reads ふぁーすと,
  never へぁーすと; カーネーション never かーのーしょん.
- the reading is not Japanese. A `vocab-kana` of "it" for ＩＴ is damage whatever the array
  says, so the array's reading is taken.
- the readings are the same reading. Stray whitespace in one of them is not a disagreement
  about the word, so the spacing is simply removed.

Everything else is held back and listed, because two plausible readings of one spelling is a
judgement about the word rather than about the data.

    py -3.10 word_array/research/vocab_reading_fix.py [--fetch]   # list the repairs
    py -3.10 word_array/research/vocab_reading_fix.py --apply
    py -3.10 word_array/research/vocab_reading_fix.py --revert

`--apply` writes `vocab-kana` and redraws `vocab-furigana` / `vocab-processed-furigana` from
the note's spelling and its new reading, and only where the note still holds the reading the
dump saw. The old field values go to `output/vocab_reading_fix_undo.jsonl` first, since Anki
cannot undo `updateNoteFields`. `--revert` puts them back.

`vocab-kanji-grades` is unaffected: the spelling does not change, only the reading.

Report `output/vocab_reading_fix_report.txt`, repairs `output/vocab_reading_fix_edits.jsonl`.
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from _bootstrap import ADDON_ROOT, load_shared

import anki_connect
import vocab_dupes
import vocab_morphology
import vocab_respell
import vocab_unlink

to_hiragana = load_shared("jp_text_processing.mecab_controller.kana_conv").to_hiragana

REPORT = ADDON_ROOT / "output" / "vocab_reading_fix_report.txt"
EDITS = ADDON_ROOT / "output" / "vocab_reading_fix_edits.jsonl"
UNDO = ADDON_ROOT / "output" / "vocab_reading_fix_undo.jsonl"

READING_FIELD = "vocab-kana"
KANJIFIED_FIELD = "vocab-kanjified"
FURIGANA_FIELD = "vocab-furigana"
PROCESSED_FIELD = "vocab-processed-furigana"

# The families where one reading is wrong without the family saying which.
REPAIRABLE = {
    vocab_morphology.RENDAKU,
    vocab_morphology.TYPO,
    vocab_morphology.WHITESPACE,
    vocab_morphology.WIDTH,
}
KANA_ONLY = re.compile(r"^[ぁ-んァ-ヶーｰ・\s　]+$")
LATIN = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ]")


class Fix(NamedTuple):
    note_id: int
    key: str
    spelling: str
    was: str  # the note's reading now
    now: str  # the reading to write
    why: str
    furigana: str
    processed: str
    links: int


def transliteration(spelling: str) -> str:
    """The one reading a kana spelling can have, or "" when the spelling is not kana."""
    if not KANA_ONLY.match(spelling or ""):
        return ""
    return to_hiragana(unicodedata.normalize("NFKC", spelling)).replace("・", "").strip()


def decide(spelling: str, note_reading: str, link_reading: str) -> tuple:
    """`(reading to write, why)`, or `(None, why not)` when the evidence does not settle it."""
    if not note_reading or not link_reading:
        return None, "one side has no reading"
    said = transliteration(spelling)
    if said:
        # The spelling is the reading, so it decides on its own.
        if said == link_reading and said != note_reading:
            return link_reading, "%s transliterates to %s" % (spelling, said)
        if said == note_reading and said != link_reading:
            return None, "%s transliterates to %s, which the note already has" % (spelling, said)
        return None, "%s transliterates to %s, which is neither reading" % (spelling, said)
    if LATIN.search(note_reading):
        return link_reading, "the note's reading %r is not a Japanese reading" % note_reading
    if note_reading != link_reading and _unspaced(note_reading) == _unspaced(link_reading):
        return _unspaced(note_reading), "the same reading, with the spacing removed"
    return None, "both %s and %s are readings %s could have" % (
        note_reading,
        link_reading,
        spelling,
    )


def _unspaced(reading: str) -> str:
    return vocab_morphology.SPACES.sub("", reading)


def plan(rows: list) -> tuple:
    """The repairs to make, and the notes held back with the reason for each."""
    if rows and vocab_unlink.ARRAY_FIELD not in rows[0]:
        raise anki_connect.AnkiConnectError(
            "The dump has no %s; re-run with --fetch." % vocab_unlink.ARRAY_FIELD
        )
    totals, _ = vocab_unlink.links_in(rows)
    owners = vocab_unlink.owners_by_word(rows)
    by_nid = {row["nid"]: row for row in rows}
    held: dict = defaultdict(list)
    fixes = []

    for note_id, by_link in totals.items():
        row = by_nid.get(note_id)
        if row is None:
            continue
        spelling = (row.get(KANJIFIED_FIELD) or "").strip()
        reading = (row.get(READING_FIELD) or "").strip()
        kana = to_hiragana(reading)
        for link, count in by_link.most_common():
            link_kana = to_hiragana(link.reading)
            family = vocab_morphology.family(spelling, kana, link.form, link_kana)
            if family not in REPAIRABLE:
                continue
            note = "%d %s: has [%s], linked by %s [%s] x%d" % (
                note_id,
                spelling,
                reading,
                link.form,
                link.reading,
                count,
            )
            if owners.get((link.form, link_kana), set()) - {note_id}:
                # Another note owns the word, so the element is that note's and says nothing
                # about this one's reading. `vocab_unlink` unlinks it.
                held["another note owns the linked word"].append(note)
                continue
            new_reading, why = decide(spelling, kana, link_kana)
            if new_reading is None:
                # The undecided reason names both readings, so it is unique per note and
                # would make a section of one; those group under the family instead.
                held[family if why.startswith("both") else why].append(note)
                continue
            drawn, why_not = vocab_respell.redraw_furigana(spelling, new_reading)
            if drawn is None:
                held["the repaired reading cannot be drawn as furigana: %s" % why_not].append(
                    note
                )
                continue
            furigana, processed = drawn
            fixes.append(
                Fix(
                    note_id=note_id,
                    key=(row.get("vocab-key") or "").strip(),
                    spelling=spelling,
                    was=reading,
                    now=new_reading,
                    why=why,
                    furigana=furigana,
                    processed=processed,
                    links=count,
                )
            )
    fixes.sort(key=lambda f: (-f.links, f.note_id))
    return fixes, held


def report(fixes: list, held: dict) -> list:
    lines = [
        "%d notes to repair the reading of." % len(fixes),
        "",
        "vocab-kana is rewritten and the furigana redrawn from it; the spelling is untouched,",
        "so vocab-kanji-grades stays valid.",
        "",
        "%-16s %-16s %-16s %s" % ("note", "was", "now", "why"),
    ]
    for fix in fixes:
        lines.append("%-16s %-16s %-16s %s" % (fix.key, fix.was, fix.now, fix.why))

    lines += ["", "--- held back, nothing written for these ---"]
    for reason in sorted(held, key=lambda r: -len(held[r])):
        notes = held[reason]
        lines += ["", "%d: %s" % (len(notes), reason)]
        lines += ["    %s" % note for note in notes[:60]]
        if len(notes) > 60:
            lines.append("    ... and %d more" % (len(notes) - 60))
    return lines


# --- writing (over AnkiConnect) -----------------------------------------------------------


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def apply(client, fixes: list, undo: Path) -> tuple:
    """Write the readings, keeping what each note held first. Returns `(written, refused)`."""
    written, refused = 0, []
    infos = client.notes_info([fix.note_id for fix in fixes])
    fields_by_nid = {
        info["noteId"]: {name: value["value"] for name, value in info["fields"].items()}
        for info in infos
        if info
    }
    undo.parent.mkdir(parents=True, exist_ok=True)
    with undo.open("a", encoding="utf-8") as log:
        for fix in fixes:
            fields = fields_by_nid.get(fix.note_id)
            if fields is None:
                refused.append("nid %d: no such note" % fix.note_id)
                continue
            if (fields.get(READING_FIELD) or "").strip() != fix.was:
                refused.append(
                    "nid %d: reads %r now, not %r as the dump saw it"
                    % (fix.note_id, (fields.get(READING_FIELD) or "").strip(), fix.was)
                )
                continue
            before = {
                name: fields.get(name, "")
                for name in (READING_FIELD, FURIGANA_FIELD, PROCESSED_FIELD)
            }
            after = {
                READING_FIELD: fix.now,
                FURIGANA_FIELD: fix.furigana,
                PROCESSED_FIELD: fix.processed,
            }
            log.write(
                json.dumps(
                    {"nid": fix.note_id, "before": before, "after": after}, ensure_ascii=False
                )
                + "\n"
            )
            log.flush()
            client.update_note_fields(fix.note_id, after)
            written += 1
    return written, refused


def revert(client, undo: Path) -> tuple:
    """Put the fields back, newest first, where the note still holds what was written.

    A note edited since the repair keeps its entry in the undo log rather than being
    overwritten, so nothing done after the apply is silently undone.
    """
    entries = read_jsonl(undo)
    if not entries:
        return 0, []
    now = {
        info["noteId"]: {name: value["value"] for name, value in info["fields"].items()}
        for info in client.notes_info([entry["nid"] for entry in entries])
        if info
    }
    reverted, refused, kept = 0, [], []
    for entry in reversed(entries):
        fields = now.get(entry["nid"])
        after = entry.get("after")
        if fields is None:
            refused.append("nid %d: no such note, left as is" % entry["nid"])
            kept.append(entry)
            continue
        if after and any(fields.get(name) != value for name, value in after.items()):
            refused.append("nid %d: edited since the repair, left as is" % entry["nid"])
            kept.append(entry)
            continue
        client.update_note_fields(entry["nid"], entry["before"])
        reverted += 1
    undo.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept), "utf-8")
    return reverted, refused


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="dump the notes first")
    parser.add_argument("--apply", action="store_true", help="write the readings")
    parser.add_argument("--revert", action="store_true", help="put the old readings back")
    parser.add_argument("--undo", type=Path, default=UNDO)
    parser.add_argument("--anki-connect", default=None)
    args = parser.parse_args()

    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    try:
        if args.revert:
            reverted, refused = revert(client, args.undo)
            print("\n".join(refused + ["reverted %d notes" % reverted]))
            return 0
        if args.fetch:
            print("%d notes -> %s" % (vocab_dupes.fetch(), vocab_dupes.DUMP))
        fixes, held = plan(vocab_dupes.read_dump())
        lines = report(fixes, held)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with open(EDITS, "w", encoding="utf-8") as out:
            for fix in fixes:
                out.write(json.dumps(fix._asdict(), ensure_ascii=False) + "\n")
        print(lines[0])
        if not args.apply:
            print("report in %s, repairs in %s" % (REPORT, EDITS))
            print("check it, then rerun with --apply")
            return 0
        written, refused = apply(client, fixes, args.undo)
        print("\n".join(refused + ["wrote %d notes, undo in %s" % (written, args.undo)]))
    except anki_connect.AnkiConnectError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
