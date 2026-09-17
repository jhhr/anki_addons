"""The change list an agent pass turns into, and whether the collection still matches it.

A pass over hard cases ends in a plan per case, written in prose for a human to read. Prose is
the right shape for deciding and the wrong shape for applying: before anything is written, each
plan has to become operations with named parameters, and each operation has to be checked
against the collection as it is now. That is what this file is for. It does not write.

The point of the check is that transcription can be wrong. A plan says "repair vocab-kana on nid
N from X to Y", and between the agent reading the note and the edit being made, the note may
have been edited by hand, or the nid may have been mistyped, or X may never have been its
reading at all. An operation whose `from` does not match what the note holds is not applied and
not silently skipped either - it is named, with what was found instead. Every operation carries
the plan it came from, so a rejection points back at the prose that produced it.

    py -3.10 word_array/research/vocab_changes.py skeleton [--plans DIR]
    py -3.10 word_array/research/vocab_changes.py check CHANGES [--fetch]

`skeleton` reads the plans and emits one row per case with the parts that can be taken from them
without interpretation - the note, the verdict, the confidence, the action as written - and an
empty `ops`. Filling those in is the transcription, and is the one step no script can do: the
action is prose, and its parameters are only there in the sense that a reader can see them.

`check` reads a filled-in change list and reports what the collection agrees with. `--fetch`
re-dumps the notes first, since a dump that predates the plans is the one thing that would make
the check agree with a wrong change list.

The output goes to `output/`, which is gitignored: a change list describes edits made once and
is not worth keeping afterwards, while this file is used by every pass.
"""

import argparse
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

from _bootstrap import ADDON_ROOT, load, load_shared

import anki_connect
import vocab_dupes
import vocab_unlink

match_flags = load("match_flags")
to_hiragana = load_shared("jp_text_processing.mecab_controller.kana_conv").to_hiragana

PLANS = ADDON_ROOT / "output" / "vocab_reading_agents" / "plans"
CHANGES = ADDON_ROOT / "output" / "vocab_reading_changes.jsonl"
REPORT = ADDON_ROOT / "output" / "vocab_changes_report.txt"

ARRAY_FIELD = vocab_unlink.ARRAY_FIELD
SENTENCE_FIELD = "sentence-kanjified-furigana"
TAGS = re.compile(r"<[^>]+>")

HEADING = re.compile(r"^#\s*(\d{10,})\s*[—-]\s*(.+)$", re.M)
FIELD = {
    "verdict": re.compile(r"^\*\*Verdict:\*\*\s*(\S+)", re.M),
    "confidence": re.compile(r"^\*\*Confidence:\*\*\s*(\S+)", re.M),
    "action": re.compile(r"^\*\*Proposed action:\*\*\s*(.+?)(?=\n\s*\n|\n##)", re.M | re.S),
}

# Every operation, and the parameters it needs. `none` is not an absence of an operation: it is
# a plan whose answer was "nothing to do here", which is a decision worth recording as one.
OPS = {
    "none": ("why",),
    "set_note_reading": ("nid", "from", "to"),
    "set_note_field": ("nid", "field", "from", "to"),
    "set_element_reading": ("sentence", "dict_form", "from", "to"),
    "set_sentence_furigana": ("sentence", "from", "to"),
    "repoint_element": ("sentence", "dict_form", "from_nid", "to_nid"),
    "unlink_element": ("sentence", "dict_form", "state"),
    "merge_notes": ("keep", "drop"),
    "create_note": ("spelling", "reading", "why"),
}
UNLINK_STATES = ("match", "unjudged")


def clean(text: str) -> str:
    """A field as its text, so a furigana comparison is not defeated by markup."""
    text = TAGS.sub(" ", (text or "").replace("<br>", " ").replace("&nbsp;", " "))
    return " ".join(text.split())


# --- turning the plans into rows to fill in -----------------------------------------------


def links_to(dump: list, note_id: int) -> list:
    """Every array element linking a note, as the parameters an operation would name it by.

    A plan says "re-read the 1 element reading ずるかしこい" without naming its `dict_form`,
    because a reader does not need it named. Transcription does. So the decision is read off the
    prose and the parameters are read off the collection, which also means a `from` cannot be
    mistyped into something the note never held.
    """
    found = []
    for row in dump:
        array = match_flags.decode_word_array(row.get(ARRAY_FIELD) or "")
        if not array:
            continue
        for depth, element in match_flags.iter_words(array):
            if len(element) < 6 or match_flags.matched_note_id(element) != note_id:
                continue
            found.append(
                {
                    "sentence": row["nid"],
                    "dict_form": element[2],
                    "reading": element[3],
                    "raw": element[0],
                    "depth": depth,
                }
            )
    return found


def skeleton(plans: Path, dump: list) -> list:
    """One row per plan: what the plan says, and what the collection currently holds for it."""
    by_nid = {row["nid"]: row for row in dump}
    rows = []
    for path in sorted(plans.glob("nid_*.md")):
        text = path.read_text(encoding="utf-8")
        found = HEADING.search(text)
        if not found:
            raise ValueError("%s has no `# <nid> — <key>` heading" % path.name)
        note_id = int(found.group(1))
        row = {"note_id": note_id, "key": found.group(2).strip(), "ops": []}
        for name, pattern in FIELD.items():
            hit = pattern.search(text)
            row[name] = " ".join(hit.group(1).split()) if hit else ""
        note = by_nid.get(note_id) or {}
        row["note"] = {
            field: clean(note.get(field) or "")
            for field in ("vocab-kanjified", "vocab-kana", "vocab-furigana")
            if clean(note.get(field) or "")
        }
        row["links"] = links_to(dump, note_id)
        rows.append(row)
    return rows


# --- checking a filled-in change list ------------------------------------------------------


def elements_in(row: dict, dict_form: str) -> list:
    """Every word element of a sentence note's array whose `dict_form` is this one."""
    array = match_flags.decode_word_array(row.get(ARRAY_FIELD) or "")
    if not array:
        return []
    return [
        element
        for _, element in match_flags.iter_words(array)
        if len(element) >= 6 and element[2] == dict_form
    ]


def check_op(op: dict, by_nid: dict) -> str:
    """"" when the collection agrees with the operation, else what it holds instead."""
    kind = op.get("op")
    if kind not in OPS:
        return "unknown operation %r" % kind
    missing = [name for name in OPS[kind] if name not in op]
    if missing:
        return "%s is missing %s" % (kind, ", ".join(missing))

    if kind == "none":
        return ""

    if kind == "create_note":
        for row in by_nid.values():
            if op["spelling"] in vocab_unlink.note_spellings(row) and to_hiragana(
                (row.get("vocab-kana") or "").strip()
            ) == to_hiragana(op["reading"]):
                return "a note already holds %s [%s]: nid %d" % (
                    op["spelling"],
                    op["reading"],
                    row["nid"],
                )
        return ""

    if kind == "merge_notes":
        for name in ("keep", "drop"):
            if op[name] not in by_nid:
                return "the note to %s, nid %s, is not in the dump" % (name, op[name])
        return ""

    if kind in ("set_note_reading", "set_note_field"):
        row = by_nid.get(op["nid"])
        if row is None:
            return "nid %s is not in the dump" % op["nid"]
        field = "vocab-kana" if kind == "set_note_reading" else op["field"]
        if field not in vocab_dupes.FIELDS:
            return "%s is not a field the dump holds" % field
        held = clean(row.get(field) or "")
        if held != clean(op["from"]):
            return "%s holds %r, not %r" % (field, held, op["from"])
        return ""

    # The rest all name a sentence note and something its array or its furigana must hold.
    row = by_nid.get(op["sentence"])
    if row is None:
        return "sentence note %s is not in the dump" % op["sentence"]

    if kind == "set_sentence_furigana":
        # Literally, not as `clean()` would see it. The field interleaves `<b>` and `<k>` with
        # the furigana - `<b> 滑[ぬめり]</b>りも` - so a run that reads as contiguous text can
        # still have a tag through the middle of it, and applying the edit is a replacement in
        # the raw field. Checking the cleaned text instead would pass a change the apply refuses.
        if op["from"] not in (row.get(SENTENCE_FIELD) or ""):
            return "the sentence field does not contain %r literally" % op["from"]
        return ""

    elements = elements_in(row, op["dict_form"])
    if not elements:
        return "the array of %s holds no %s element" % (op["sentence"], op["dict_form"])

    if kind == "set_element_reading":
        readings = [element[3] for element in elements]
        if op["from"] not in readings:
            return "the %s elements read %s, not %r" % (
                op["dict_form"],
                ", ".join(repr(r) for r in readings),
                op["from"],
            )
        return ""

    if kind == "unlink_element":
        if op["state"] not in UNLINK_STATES:
            return "state must be one of %s" % ", ".join(UNLINK_STATES)
        return ""

    linked = [match_flags.matched_note_id(element) for element in elements]
    if op["from_nid"] not in linked:
        return "the %s elements link %s, not %s" % (
            op["dict_form"],
            ", ".join(str(n) for n in linked),
            op["from_nid"],
        )
    if op["to_nid"] not in by_nid:
        return "the note to point at, nid %s, is not in the dump" % op["to_nid"]
    return ""


def check(rows: list, by_nid: dict) -> tuple:
    """`(problems, counts, held)`: what the collection disagrees with, the operations by kind,
    and the plans waiting on a decision.

    A held plan is still checked. It is not ready to apply, but its operations are the ones that
    would be applied once the decision is made, and a `from` that has gone stale is worth knowing
    about now rather than after the decision.
    """
    problems, counts, held = [], Counter(), []
    for row in rows:
        if row.get("hold"):
            held.append((row["note_id"], row.get("key", ""), row["hold"]))
        for index, op in enumerate(row.get("ops") or []):
            counts[op.get("op", "?")] += 1
            why = check_op(op, by_nid)
            if why:
                problems.append((row["note_id"], row.get("key", ""), index, op, why))
        if not row.get("ops"):
            counts["(not transcribed)"] += 1
    return problems, counts, held


def report(rows: list, problems: list, counts: Counter, held: list) -> list:
    lines = [
        "%d plans, %d operations." % (len(rows), sum(counts.values())),
        "",
        "operations by kind:",
    ]
    for kind, count in counts.most_common():
        lines.append("  %-22s %d" % (kind, count))
    lines.append("")
    if held:
        lines.append("%d plans are held for a decision before anything is applied:" % len(held))
        for note_id, key, why in held:
            lines.append("  %d %s" % (note_id, key))
            lines.append("    %s" % why)
        lines.append("")
    if not problems:
        lines.append("The collection agrees with every operation.")
    else:
        lines.append("%d the collection does not agree with:" % len(problems))
        lines.append("")
        for note_id, key, index, op, why in problems:
            lines.append("  %d %s  op[%d] %s" % (note_id, key, index, op.get("op")))
            lines.append("    %s" % why)
            lines.append("    %s" % json.dumps(op, ensure_ascii=False))
            lines.append("")
    lines.append("")
    lines.append("--- every operation, by plan ---")
    lines.append("")
    for row in sorted(rows, key=lambda r: (r.get("verdict", ""), r["note_id"])):
        lines.append("%s  %d %s" % (row.get("verdict", "?"), row["note_id"], row.get("key", "")))
        if row.get("hold"):
            lines.append("    HELD: %s" % row["hold"])
        for op in row.get("ops") or []:
            lines.append("    %s" % one_line(op))
        if not row.get("ops"):
            lines.append("    (not transcribed)")
        lines.append("")
    return lines


def one_line(op: dict) -> str:
    """An operation as a line to read, rather than as the JSON that will be applied."""
    kind = op.get("op")
    if kind == "none":
        return "nothing to do: %s" % op.get("why")
    if kind == "set_note_reading":
        return "note %s  vocab-kana  %s -> %s" % (op["nid"], op["from"], op["to"])
    if kind == "set_note_field":
        return "note %s  %s  %s -> %s" % (op["nid"], op["field"], op["from"], op["to"])
    if kind == "set_element_reading":
        return "sentence %s  element %s  reading %s -> %s" % (
            op["sentence"], op["dict_form"], op["from"], op["to"]
        )
    if kind == "set_sentence_furigana":
        return "sentence %s  furigana  %s -> %s" % (op["sentence"], op["from"], op["to"])
    if kind == "repoint_element":
        return "sentence %s  element %s  link %s -> %s" % (
            op["sentence"], op["dict_form"], op["from_nid"], op["to_nid"]
        )
    if kind == "unlink_element":
        return "sentence %s  element %s  unlink (%s)" % (
            op["sentence"], op["dict_form"], op["state"]
        )
    if kind == "merge_notes":
        return "merge: keep %s, drop %s (its links move first)" % (op["keep"], op["drop"])
    if kind == "create_note":
        return "create %s [%s]: %s" % (op["spelling"], op["reading"], op.get("why", ""))
    return json.dumps(op, ensure_ascii=False)


def write(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read(path: Path) -> list:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("skeleton")
    p.add_argument("--plans", type=Path, default=PLANS)
    p.add_argument("--out", type=Path, default=CHANGES)
    p = sub.add_parser("check")
    p.add_argument("changes", type=Path, nargs="?", default=CHANGES)
    p.add_argument("--fetch", action="store_true", help="re-dump the notes first")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "skeleton":
        rows = skeleton(args.plans, vocab_dupes.read_dump())
        write(args.out, rows)
        print("wrote %s (%d plans, ops left empty)" % (args.out, len(rows)))
        return 0

    if args.fetch:
        vocab_dupes.fetch()
    rows = read(args.changes)
    dump = vocab_dupes.read_dump()
    if dump and ARRAY_FIELD not in dump[0]:
        raise anki_connect.AnkiConnectError(
            "The dump has no %s; re-run with --fetch." % ARRAY_FIELD
        )
    problems, counts, held = check(rows, {row["nid"]: row for row in dump})
    lines = report(rows, problems, counts, held)
    io.open(REPORT, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    print("\n".join(lines[: 3 + len(counts)]))
    print(
        "\n%d plans held for a decision, %d operations the collection does not agree with."
        % (len(held), len(problems))
    )
    print("report in %s" % REPORT)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
