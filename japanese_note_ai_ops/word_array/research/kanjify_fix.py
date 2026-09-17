"""Writes the kanjify audit's policy and format fixes into the checked notes, through AnkiConnect.

`kanjify_audit.py` leaves `output/kanjify_audit_fixes.jsonl`, one row per sentence: its note ids,
the kanjified field as exported (`before`) and fixed (`after`). By default this script only lists
the changes (stdout and `output/kanjify_fix_list.txt`) for the user to check.

`--apply` writes `after` into the `kanjified_sentence_field` of every note, but only where the field
is still `before`: a note edited since the export is listed as refused. Each write first appends
the old value to `output/kanjify_fix_undo.jsonl`, since Anki can't undo `updateNoteFields`.
`--revert` writes those old values back, newest first, where the field is still what was written;
reverted entries leave the undo file, refused ones stay.

    py -3.10 word_array/research/kanjify_fix.py [--fixes FILE] [-n COUNT] [--apply | --revert]

Rows without note ids (the old fine-tuning files) are listed but never written.
"""

import argparse
import json
from pathlib import Path
from typing import NamedTuple, Optional

from _bootstrap import ADDON_ROOT

import anki_connect

OUTPUT = ADDON_ROOT / "output"
FIXES = OUTPUT / "kanjify_audit_fixes.jsonl"
UNDO = OUTPUT / "kanjify_fix_undo.jsonl"
LIST = OUTPUT / "kanjify_fix_list.txt"
FIELD_KEY = "kanjified_sentence_field"


class Write(NamedTuple):
    nid: int
    field: str
    before: str
    after: str


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def change_list(rows: list[dict]) -> list[str]:
    lines = []
    for row in rows:
        nids = ",".join(map(str, row.get("nids") or [])) or "- (no nids: not written)"
        fixes = ", ".join(
            f"{f['class']} {f['word']}" + (f" -> {f['to']}" if "to" in f else "")
            for f in row.get("fixes", [])
        )
        lines += [
            f"[{row.get('row')}] nid {nids}",
            f"    fixes  {fixes}",
            f"    before {row['before']}",
            f"    after  {row['after']}",
        ]
    return lines


def _current(infos: list[dict], config: dict) -> dict[int, tuple[Optional[str], str]]:
    """Per note id: (its field name, the field's value), or (None, why it can't be read)."""
    out: dict[int, tuple[Optional[str], str]] = {}
    for info in infos:
        if not info:
            continue
        nid = info["noteId"]
        try:
            field = anki_connect.sentence_field(config, info.get("modelName", ""), FIELD_KEY)
            out[nid] = (field, anki_connect.note_sentence(config, info, FIELD_KEY))
        except anki_connect.AnkiConnectError as e:
            out[nid] = (None, str(e))
    return out


def _padding(value: str) -> tuple[str, str]:
    """The whitespace around the field's text; the export strips it, notes starting with a
    furigana group keep a leading space, which is part of that group's markup."""
    return value[: len(value) - len(value.lstrip())], value[len(value.rstrip()) :]


def plan_writes(rows: list[dict], infos: list[dict], config: dict) -> tuple[list[Write], list[str]]:
    """The writes that apply the rows to notes whose field is still `before`, padding aside; why
    the others are refused. A written field keeps the padding it had."""
    current = _current(infos, config)
    writes, refused = [], []
    for row in rows:
        for nid in row.get("nids") or []:
            field, value = current.get(nid, (None, "no such note"))
            lead, tail = _padding(value or "")
            if field is None:
                refused.append(f"nid {nid}: {value}")
            elif value.strip() == row["after"].strip():
                refused.append(f"nid {nid}: already fixed")
            elif value.strip() != row["before"].strip():
                refused.append(f"nid {nid}: field changed since the export")
            else:
                writes.append(Write(nid, field, value, lead + row["after"].strip() + tail))
    return writes, refused


def apply(client, rows: list[dict], config: dict, undo: Path) -> tuple[int, list[str]]:
    """Writes the fixes, recording each old value in `undo` before its write; how many were
    written and the refusals."""
    nids = sorted({nid for row in rows for nid in row.get("nids") or []})
    writes, refused = plan_writes(rows, client.notes_info(nids) if nids else [], config)
    written = 0
    for w in writes:
        with open(undo, "a", encoding="utf-8") as f:
            f.write(json.dumps(w._asdict(), ensure_ascii=False) + "\n")
        client.update_note_fields(w.nid, {w.field: w.after})
        written += 1
    return written, refused


def revert(client, config: dict, undo: Path) -> tuple[int, list[str]]:
    """Writes back the old values of `undo`, newest first, where the field is still what was
    written; reverted entries leave the file."""
    entries = [Write(**d) for d in read_jsonl(undo)]
    infos = client.notes_info(sorted({e.nid for e in entries})) if entries else []
    current = {nid: value for nid, (field, value) in _current(infos, config).items() if field}
    kept, refused, reverted = [], [], 0
    for e in reversed(entries):
        if current.get(e.nid) != e.after:
            refused.append(f"nid {e.nid}: field changed since the fix, left as is")
            kept.append(e)
            continue
        client.update_note_fields(e.nid, {e.field: e.before})
        current[e.nid] = e.before
        reverted += 1
    text = "".join(json.dumps(e._asdict(), ensure_ascii=False) + "\n" for e in reversed(kept))
    undo.write_text(text, encoding="utf-8")
    return reverted, refused


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixes", type=Path, default=FIXES)
    parser.add_argument("-n", type=int, default=0, help="only the first COUNT rows")
    parser.add_argument("--undo", type=Path, default=UNDO)
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", action="store_true")
    args = parser.parse_args()

    client = anki_connect.AnkiConnect(args.anki_connect)
    config = anki_connect.load_config()
    try:
        if args.revert:
            reverted, refused = revert(client, config, args.undo)
            print("\n".join(refused + [f"reverted {reverted} notes"]))
            return 0
        rows = read_jsonl(args.fixes)[: args.n or None]
        if not args.apply:
            LIST.write_text("\n".join(change_list(rows)) + "\n", encoding="utf-8")
            with_nids = sum(1 for r in rows if r.get("nids"))
            print(f"{len(rows)} fix rows ({with_nids} with note ids), list in {LIST}")
            print("check it, then rerun with --apply")
            return 0
        written, refused = apply(client, rows, config, args.undo)
        skipped = sum(1 for r in rows if not r.get("nids"))
        print("\n".join(refused))
        print(f"wrote {written} notes, refused {len(refused)}, rows without nids {skipped}")
        print(f"old values in {args.undo} (--revert puts them back)")
        return 0
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
