"""Applies a change list, one note at a time, against what the notes hold right now.

`vocab_changes.py` checks a change list against the dump. That is the right check for reviewing
it and the wrong one for writing it: a dump is a photograph, and between taking it and writing
the edit a note can be edited by hand - which is exactly what happened to five of the plans this
was first built for. So every operation is checked again here against the note as AnkiConnect
answers for it, immediately before anything is written.

A note is written whole or not at all. Several operations often land on one note - a reading and
the furigana drawn from it, or two elements of one array - and applying some of them would leave
the note in a state no plan asked for. When any operation on a note disagrees with what the note
holds, the whole note is refused and named, and the rest of the change list still goes in.

    py -3.10 word_array/research/vocab_apply.py [CHANGES]            # list the writes
    py -3.10 word_array/research/vocab_apply.py [CHANGES] --apply
    py -3.10 word_array/research/vocab_apply.py --revert
    py -3.10 word_array/research/vocab_apply.py --only set_element_reading --apply

`--only` takes an operation kind and writes nothing else, so a change list can go in a kind at a
time, the cheapest to undo first. `--apply` appends each note's old field values to
`output/vocab_apply_undo.jsonl` before writing it, since Anki cannot undo `updateNoteFields`, and
`--revert` puts them back newest first, skipping any note edited since.

`merge_notes` and `create_note` are listed and never applied: this script only writes fields,
and deleting a note is not a field.

Report `output/vocab_apply_report.txt`.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from _bootstrap import ADDON_ROOT, load

import anki_connect
import vocab_changes
import vocab_respell

match_flags = load("match_flags")

REPORT = ADDON_ROOT / "output" / "vocab_apply_report.txt"
UNDO = ADDON_ROOT / "output" / "vocab_apply_undo.jsonl"

ARRAY_FIELD = vocab_changes.ARRAY_FIELD
SENTENCE_FIELD = vocab_changes.SENTENCE_FIELD
READING_FIELD = "vocab-kana"
KANJIFIED_FIELD = vocab_respell.KANJIFIED_FIELD
FURIGANA_FIELD = vocab_respell.FURIGANA_FIELD
PROCESSED_FIELD = vocab_respell.PROCESSED_FIELD

# What this script can write. The rest of the schema is listed and left alone.
FIELD_OPS = {
    "set_note_reading",
    "set_note_field",
    "set_element_reading",
    "set_sentence_furigana",
    "repoint_element",
    "unlink_element",
}
UNWRITABLE = {"none": "a decision to change nothing", "merge_notes": "deletes a note",
              "create_note": "makes a note"}


class Write(NamedTuple):
    nid: int
    before: dict  # field -> the text the note holds now
    after: dict  # field -> the text to write
    why: list  # one line per operation, for the report


def target(op: dict) -> int:
    """The note an operation writes to, which is not always the note the plan is about."""
    return op["nid"] if "nid" in op else op["sentence"]


def processed_for(furigana: str) -> tuple:
    """`(processed furigana, "")` for a furigana string, or `(None, why not)`."""
    _, highlight = vocab_respell._kana()
    try:
        return (
            highlight.kana_highlight(
                kanji_to_highlight="",
                text=furigana,
                return_type="furigana",
                with_tags_def=highlight.WithTagsDef(
                    with_tags=True,
                    merge_consecutive=False,
                    onyomi_to_katakana=False,
                    include_suru_okuri=True,
                ),
            ),
            "",
        )
    except Exception as e:  # the kana pipeline raises a variety of its own errors
        return None, "processed furigana: %s: %s" % (type(e).__name__, e)


# --- turning operations into field values --------------------------------------------------


def apply_to_fields(op: dict, fields: dict) -> str:
    """Fold one operation into `fields`, or say why the note does not match it.

    `fields` starts as what the note holds and is edited in place, so operations on one note
    compose: two elements of one array are two edits to the same decoded array, and a furigana
    written explicitly replaces the one a reading change drew.
    """
    kind = op["op"]

    if kind == "set_note_reading":
        held = (fields.get(READING_FIELD) or "").strip()
        if held != op["from"]:
            return "%s holds %r, not %r" % (READING_FIELD, held, op["from"])
        if op.get("furigana"):
            # A spelling with no kanji has nothing to draw a reading over: ｎｍ read as
            # なのめーとる is a convention, not a decomposition, and the plan supplies it.
            processed, why_not = processed_for(op["furigana"])
            if processed is None:
                return why_not
            drawn = (op["furigana"], processed)
        else:
            spelling = (fields.get(KANJIFIED_FIELD) or "").strip()
            drawn, why_not = vocab_respell.redraw_furigana(spelling, op["to"])
            if drawn is None:
                return "the repaired reading cannot be drawn as furigana: %s" % why_not
        fields[READING_FIELD] = op["to"]
        fields[FURIGANA_FIELD], fields[PROCESSED_FIELD] = drawn
        return ""

    if kind == "set_note_field":
        field = op["field"]
        held = vocab_changes.clean(fields.get(field) or "")
        if held != vocab_changes.clean(op["from"]):
            return "%s holds %r, not %r" % (field, held, op["from"])
        fields[field] = op["to"]
        if field == FURIGANA_FIELD:
            # The processed furigana is drawn from the furigana, so it cannot be left behind.
            processed, why_not = processed_for(op["to"])
            if processed is None:
                return why_not
            fields[PROCESSED_FIELD] = processed
        return ""

    if kind == "set_sentence_furigana":
        # `sentence-kanjified-furigana` is generated from `sentence-furigana` by the kanjify op,
        # so a repair made only in the generated field is undone the next time it is generated.
        # Which field an edit belongs in is the plan's to say, and it is usually both.
        field = op.get("field", SENTENCE_FIELD)
        held = fields.get(field) or ""
        if op["from"] not in held:
            return "%s does not contain %r literally" % (field, op["from"])
        if held.count(op["from"]) > 1:
            return "%s holds %r %d times" % (field, op["from"], held.count(op["from"]))
        fields[field] = held.replace(op["from"], op["to"])
        return ""

    array = match_flags.decode_word_array(fields.get(ARRAY_FIELD) or "")
    if array is None:
        return "the word array does not parse"
    hits = [
        element
        for _, element in match_flags.iter_words(array)
        if len(element) >= 6 and element[2] == op["dict_form"]
    ]
    if kind == "set_element_reading":
        hits = [element for element in hits if element[3] == op["from"]]
        if not hits:
            return "no %s element reads %r" % (op["dict_form"], op["from"])
        for element in hits:
            element[3] = op["to"]
    elif kind == "repoint_element":
        hits = [e for e in hits if match_flags.matched_note_id(e) == op["from_nid"]]
        if not hits:
            return "no %s element links %s" % (op["dict_form"], op["from_nid"])
        for element in hits:
            element[4] = [op["to_nid"]]
    elif kind == "unlink_element":
        if not hits:
            return "the array holds no %s element" % op["dict_form"]
        for element in hits:
            element[4] = [match_flags.MATCH] if op["state"] == "match" else []
    else:
        return "%s is not an operation this script writes" % kind
    fields[ARRAY_FIELD] = match_flags.format_word_array(array)
    return ""


def array_moved(before: str, after: str) -> bool:
    """Whether two array field texts say different things, rather than saying it differently."""
    parsed_before = match_flags.decode_word_array(before)
    parsed_after = match_flags.decode_word_array(after)
    if parsed_before is None or parsed_after is None:
        return before != after
    return parsed_before != parsed_after


def plan_writes(rows: list, live: dict, only: str = "") -> tuple:
    """`(writes, refused, skipped)` for a change list against the notes as they are now."""
    by_note: dict = defaultdict(list)
    skipped: dict = defaultdict(list)
    for row in rows:
        for op in row.get("ops") or []:
            kind = op.get("op")
            if kind in UNWRITABLE:
                skipped[kind].append((row["note_id"], row.get("key", ""), op))
                continue
            if kind not in FIELD_OPS:
                skipped["unknown"].append((row["note_id"], row.get("key", ""), op))
                continue
            if only and kind != only:
                continue
            by_note[target(op)].append((row, op))

    writes, refused = [], []
    for nid in sorted(by_note):
        fields = live.get(nid)
        if fields is None:
            refused.append((nid, "no such note, or AnkiConnect did not answer for it"))
            continue
        working = dict(fields)
        why, trouble = [], ""
        for row, op in by_note[nid]:
            problem = apply_to_fields(op, working)
            if problem:
                trouble = "%s (plan %d %s): %s" % (
                    op["op"],
                    row["note_id"],
                    row.get("key", ""),
                    problem,
                )
                break
            why.append(vocab_changes.one_line(op))
        if trouble:
            refused.append((nid, trouble))
            continue
        changed = {name for name, value in working.items() if fields.get(name) != value}
        if ARRAY_FIELD in changed and not array_moved(fields.get(ARRAY_FIELD, ""),
                                                      working[ARRAY_FIELD]):
            # The array is written back from the parsed structure, so the text differs whenever
            # the field was not already in `format_word_array`'s shape - a note edited by hand
            # holds `<br>` and `&nbsp;` instead. Reformatting is not this script's business, and
            # counting it as a change would make a second apply rewrite arrays it did not touch.
            changed.discard(ARRAY_FIELD)
        if not changed:
            continue
        writes.append(
            Write(
                nid=nid,
                before={name: fields.get(name, "") for name in sorted(changed)},
                after={name: working[name] for name in sorted(changed)},
                why=why,
            )
        )
    return writes, refused, skipped


# --- writing -------------------------------------------------------------------------------


def live_fields(client, nids: list) -> dict:
    return {
        info["noteId"]: {name: value["value"] for name, value in info["fields"].items()}
        for info in client.notes_info(sorted(nids))
        if info
    }


def apply(client, writes: list, undo: Path) -> tuple:
    """Write each note, its old field values recorded first. `(written, refused)`."""
    written, refused = 0, []
    undo.parent.mkdir(parents=True, exist_ok=True)
    with undo.open("a", encoding="utf-8") as log:
        for write in writes:
            log.write(
                json.dumps(
                    {"nid": write.nid, "before": write.before, "after": write.after},
                    ensure_ascii=False,
                )
                + "\n"
            )
            log.flush()
            client.update_note_fields(write.nid, write.after)
            written += 1
    return written, refused


def revert(client, undo: Path) -> tuple:
    """Put the fields back, newest first, where the note still holds what was written."""
    entries = vocab_changes.read(undo) if undo.exists() else []
    if not entries:
        return 0, []
    now = live_fields(client, [entry["nid"] for entry in entries])
    reverted, refused, kept = 0, [], []
    for entry in reversed(entries):
        fields = now.get(entry["nid"])
        if fields is None:
            refused.append("nid %d: no such note, left as is" % entry["nid"])
            kept.append(entry)
            continue
        if any(fields.get(name) != value for name, value in entry["after"].items()):
            refused.append("nid %d: edited since the apply, left as is" % entry["nid"])
            kept.append(entry)
            continue
        client.update_note_fields(entry["nid"], entry["before"])
        now[entry["nid"]] = {**fields, **entry["before"]}
        reverted += 1
    undo.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in reversed(kept)),
        encoding="utf-8",
    )
    return reverted, refused


def report(writes: list, refused: list, skipped: dict) -> list:
    lines = ["%d notes to write, %d refused." % (len(writes), len(refused)), ""]
    if refused:
        lines.append("--- refused: the note does not hold what the change list expects ---")
        lines.append("Nothing is written to these, and nothing else on the note either.")
        lines.append("")
        for nid, why in refused:
            lines.append("  nid %d: %s" % (nid, why))
        lines.append("")
    if skipped:
        lines.append("--- listed, never written by this script ---")
        for kind, entries in sorted(skipped.items()):
            lines.append("")
            lines.append("%d %s (%s)" % (len(entries), kind, UNWRITABLE.get(kind, "unknown")))
            for note_id, key, op in entries:
                lines.append("    %d %s: %s" % (note_id, key, vocab_changes.one_line(op)))
        lines.append("")
    lines.append("--- the writes ---")
    for write in writes:
        lines.append("")
        lines.append("nid %d, fields: %s" % (write.nid, ", ".join(sorted(write.after))))
        for line in write.why:
            lines.append("    %s" % line)
    return lines


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("changes", type=Path, nargs="?", default=vocab_changes.CHANGES)
    parser.add_argument("--only", default="", help="write only this kind of operation")
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
            print("\n".join(refused + ["reverted %d notes" % reverted]))
            return 0
        if args.only and args.only not in FIELD_OPS:
            print("--only takes one of: %s" % ", ".join(sorted(FIELD_OPS)))
            return 1
        rows = vocab_changes.read(args.changes)
        wanted = {
            target(op)
            for row in rows
            for op in row.get("ops") or []
            if op.get("op") in FIELD_OPS and (not args.only or op["op"] == args.only)
        }
        writes, refused, skipped = plan_writes(rows, live_fields(client, list(wanted)), args.only)
        lines = report(writes, refused, skipped)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(lines[0])
        if not args.apply:
            print("report in %s" % REPORT)
            print("check it, then rerun with --apply")
            return 0
        written, also_refused = apply(client, writes, args.undo)
        print("\n".join(also_refused + ["wrote %d notes, undo in %s" % (written, args.undo)]))
    except anki_connect.AnkiConnectError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
