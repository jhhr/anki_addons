"""Puts back the match data that one match run wrote as nothing, which left 1,203 word arrays
unreadable.

On 2026-09-22 at 22:00:25 a match_words_to_notes run, whose 2,018 claude calls had all failed
on an expired login (the reason for commit bb64495), saved 1,203 vocab notes in one write with
the match data of every word it was matching written as nothing:

    [" 五[ご]人[にん]", "noun", "五人", "ごにん", , [

That is not JSON, so every reader refuses the field (`read_word_array`), and the next match
run tagged the notes `invalid_word_list_json` and left them alone, as it should. All 2,334 such
words were `["match"]` in the backup of the day before and were the run's targets, so they were
still `["match"]` when it started, and that is what goes back: the state the run found, which
the next match run matches as it would have. What wrote the empty slots is not in any
committed code, then or now (every writer of the field goes through `format_word_array` or
swaps one note id for another); the code Anki ran that evening was an uncommitted working tree.

A slot is only filled where the structure proves it was match data: the field must fail to
parse as it is, and parse once every empty slot holds a mark, with that mark as the match data
of exactly as many words as there were slots, so text inside a word's raw text is never
touched. The marks then become `["match"]`. A field
broken any other way is listed, not written. The repaired note loses the
`invalid_word_list_json` tag the failed parse put on it.

    py -3.10 word_array/research/array_slot_repair.py            # list, write nothing
    py -3.10 word_array/research/array_slot_repair.py --apply
    py -3.10 word_array/research/array_slot_repair.py --revert

Talks to a running Anki through AnkiConnect, which answers for the notes as they are right
before each write. The note types and their word list fields come from the addon's config.
`--apply` appends each note's old field text to `output/array_slot_repair_undo.jsonl` before
writing it, since Anki cannot undo `updateNoteFields`; `--revert` puts it back, newest first,
where the note still holds what was written. Report `output/array_slot_repair_report.txt`.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import NamedTuple, Optional

import anki_connect
from _bootstrap import ADDON_ROOT, load

match_flags = load("match_flags")

REPORT = ADDON_ROOT / "output" / "array_slot_repair_report.txt"
UNDO = ADDON_ROOT / "output" / "array_slot_repair_undo.jsonl"
# match_words_to_notes.INVALID_WORD_ARRAY_TAG; that module needs Anki to import
INVALID_TAG = "invalid_word_list_json"
WORD_LIST_FIELD = "word_list_field"

# A string's closing quote, a comma, and a second comma with nothing between: the lost slot. Not
# after a backslash, which would be a quote inside a word's raw text
EMPTY_SLOT_RE = re.compile(r'(?<!\\)(",\s*),(\s*\[)')
MATCH_SLOT = '["match"]'
SENTINEL = "array_slot_repair: a lost slot"


class Repair(NamedTuple):
    text: Optional[str]  # the field to write, None when refused or nothing to do
    slots: int
    why: str  # why not, for a refused one


def unescaped(field: str) -> str:
    """The field as read_word_array reads a hand-edited one: the editor turns format_word_array's
    newlines into `<br>` and its indentation into `&nbsp;`."""
    return field.replace("<br>", "\n").replace("&nbsp;", " ")


def repair(field: str) -> Repair:
    """What to write in place of `field`, if it is one this script repairs."""
    if not field.strip():
        return Repair(None, 0, "")
    arr, problem = match_flags.read_word_array(field)
    if arr is not None:
        return Repair(None, 0, "")
    # Filled with a mark no field holds first, so the parsed structure can say where each landed.
    # In the text as stored: read_word_array reads a hand-edited field's `<br>` and `&nbsp;`
    # itself, and only when the text does not parse as it stands, since a sentence's own line
    # break is a `["<br>"]` element that turned into a newline would break the JSON
    filled, slots = EMPTY_SLOT_RE.subn(r"\1" + json.dumps([SENTINEL]) + r",\2", field)
    if not slots:
        return Repair(None, 0, f"broken, no empty slot: {problem}")
    fixed, still = match_flags.read_word_array(filled)
    if fixed is None:
        return Repair(None, slots, f"broken otherwise too, with {slots} empty slots: {still}")
    marked = [e for _, e in match_flags.iter_words(fixed) if e[4] == [SENTINEL]]
    if len(marked) != slots:
        return Repair(None, slots, f"{slots - len(marked)} empty slot(s) not a word's match data")
    for element in marked:
        element[4] = [match_flags.MATCH]
    return Repair(match_flags.format_word_array(fixed), slots, "")


def excerpt(field: str, why: str, width: int = 90) -> str:
    """The text around the position a JSON error names, `(char N)`."""
    found = re.search(r"\(char (\d+)\)", why)
    text = unescaped(field)
    at = int(found.group(1)) if found else 0
    return text[max(0, at - width // 2) : at + width // 2].replace("\n", " ")


def word_list_fields(config: dict) -> dict[str, str]:
    """note type name -> its word list field, for every note type the config gives one."""
    return {
        name: value[WORD_LIST_FIELD]
        for name, value in config.items()
        if isinstance(value, dict) and value.get(WORD_LIST_FIELD)
    }


def candidates(client, fields: dict[str, str]) -> list[int]:
    """Notes whose word list field looks broken: an empty slot, or the tag a failed parse adds."""
    nids: set[int] = set()
    for note_type, field in fields.items():
        for query in (f'"note:{note_type}" "{field}:*, , [*"', f'"note:{note_type}" tag:{INVALID_TAG}'):
            nids.update(client.invoke("findNotes", query=query))
    return sorted(nids)


class Planned(NamedTuple):
    nid: int
    field: str
    before: str
    after: Optional[str]
    slots: int
    why: str
    tagged: bool


def plan(client, fields: dict[str, str], nids: list[int]) -> list[Planned]:
    out = []
    for start in range(0, len(nids), 200):
        for info in client.notes_info(nids[start : start + 200]):
            if not info:
                continue
            field = fields.get(info.get("modelName", ""))
            if field is None or field not in info["fields"]:
                continue
            value = info["fields"][field]["value"]
            fix = repair(value)
            if fix.text is None and not fix.why:
                continue
            out.append(Planned(info["noteId"], field, value, fix.text, fix.slots, fix.why,
                               INVALID_TAG in info.get("tags", [])))
    return out


def apply(client, planned: list[Planned], undo: Path) -> tuple[int, list[str]]:
    """Write each repair against the note as it is now; `(written, refused)`."""
    writes = [p for p in planned if p.after is not None]
    now = {info["noteId"]: info for info in client.notes_info([p.nid for p in writes]) if info}
    written, refused = 0, []
    undo.parent.mkdir(parents=True, exist_ok=True)
    with undo.open("a", encoding="utf-8") as log:
        for p in writes:
            current = now.get(p.nid, {}).get("fields", {}).get(p.field, {}).get("value")
            if current != p.before:
                refused.append(f"nid {p.nid}: edited since it was listed, left as is")
                continue
            log.write(json.dumps({"nid": p.nid, "field": p.field, "before": p.before,
                                  "after": p.after, "tag_removed": p.tagged},
                                 ensure_ascii=False) + "\n")
            log.flush()
            client.update_note_fields(p.nid, {p.field: p.after})
            if p.tagged:
                client.remove_tags([p.nid], INVALID_TAG)
            written += 1
    return written, refused


def revert(client, undo: Path) -> tuple[int, list[str]]:
    entries = [json.loads(line) for line in undo.read_text(encoding="utf-8").splitlines()
               if line.strip()] if undo.exists() else []
    if not entries:
        return 0, []
    now = {info["noteId"]: info for info in client.notes_info([e["nid"] for e in entries]) if info}
    reverted, refused, kept = 0, [], []
    for entry in reversed(entries):
        value = now.get(entry["nid"], {}).get("fields", {}).get(entry["field"], {}).get("value")
        if value != entry["after"]:
            refused.append(f"nid {entry['nid']}: edited since the apply, left as is")
            kept.append(entry)
            continue
        client.update_note_fields(entry["nid"], {entry["field"]: entry["before"]})
        if entry.get("tag_removed"):
            client.add_tags([entry["nid"]], INVALID_TAG)
        reverted += 1
    undo.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in reversed(kept)),
                    encoding="utf-8")
    return reverted, refused


def report(planned: list[Planned]) -> list[str]:
    writes = [p for p in planned if p.after is not None]
    other = [p for p in planned if p.after is None]
    lines = [f"{len(writes)} notes to repair, {sum(p.slots for p in writes)} empty slots set to"
             f" {MATCH_SLOT}; {len(other)} broken otherwise, listed and not written.", ""]
    if other:
        lines.append("--- broken otherwise: fix these by hand in the editor ---")
        for p in other:
            lines.append(f"  nid {p.nid}: {p.why}")
            lines.append(f"      ...{excerpt(p.before, p.why)}...")
        lines.append("")
    lines.append("--- the repairs ---")
    for p in writes:
        tag = f"; loses the {INVALID_TAG} tag" if p.tagged else ""
        lines.append(f"  nid {p.nid}: {p.slots} slot(s){tag}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--undo", type=Path, default=UNDO)
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", action="store_true")
    args = parser.parse_args()

    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    try:
        if args.revert:
            reverted, refused = revert(client, args.undo)
            print("\n".join(refused + [f"reverted {reverted} notes"]))
            return 0
        fields = word_list_fields(anki_connect.load_config())
        planned = plan(client, fields, candidates(client, fields))
        lines = report(planned)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(lines[0])
        if not args.apply:
            print(f"report in {REPORT}; check it, then rerun with --apply")
            return 0
        written, refused = apply(client, planned, args.undo)
        print("\n".join(refused + [f"wrote {written} notes, undo in {args.undo}"]))
    except anki_connect.AnkiConnectError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
