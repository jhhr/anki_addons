"""Re-keys the vocab notes that are the same word under the canonical key, and tags them (task 26).

`vocab_dupes.py` says which notes are one word: the canonical key (task 18's one spelling per
JMdict entry, task 20's kanjify policy over it) read with the note's reading. This script gives
every note of such a group one sort field, so that "Clean dictionary meaning" and "Deduplicate
existing meaning notes" can collapse them afterwards. It **re-keys and tags; it never deletes.**

Per group (a key spelled several ways, or one sort field value sitting on several notes):

- the group's prefix is the canonical key plus the non-`(mN)` markers its canonically spelled
  notes carry, so every note of the group ends up with the same prefix — `deduplicate_notes_list`
  groups by exactly the text before a trailing `(mN)`;
- every note gets an `(mN)`, numbered in the order the notes should survive in (studied first,
  oldest first within that, canonical spellings before variants). A note keeps its own number when
  that number is free and does not break the order; numbers held by notes of the same prefix
  outside the group are never reused;
- reading markers (`(on)`, `(kun)`, `(rN)`) of a respelled note are dropped: they disambiguated
  the old spelling. The holes that leaves in a kanji's `(rN)` series are listed in the report for
  `match_words_to_notes.update_note_reading_markers` (task 28), not fixed here;
- every touched note is tagged `word-array-duplicate`, and each variant also
  `word-array-keeper::<nid>` — the id of the note meant to survive its group. (The spec's
  `vocab-id` would have broken the dedupe's link repointing: it reads that field as the note's
  *own* reference.)

Spellings the canonical key gets wrong go in `generator.CANONICAL_EXCEPTIONS`; check the re-key
list and `output/vocab_dupes_report.txt`'s "spellings that canonicalise to another key" first.

    py -3.10 word_array/research/vocab_rekey.py [--fetch]   # list the changes, write nothing
    py -3.10 word_array/research/vocab_rekey.py --apply
    py -3.10 word_array/research/vocab_rekey.py --revert

`--apply` writes only where the sort field is still what the dump says; each write appends the old
value to `output/vocab_dedupe_undo.jsonl` first, since Anki cannot undo `updateNoteFields`.
`--revert` puts those values back and removes the tags the run added.
"""

import argparse
import itertools
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

from _bootstrap import ADDON_ROOT

import anki_connect
import vocab_dupes

OUTPUT = ADDON_ROOT / "output"
UNDO = OUTPUT / "vocab_dedupe_undo.jsonl"
CHANGES = OUTPUT / "vocab_rekey_changes.jsonl"
LIST = OUTPUT / "vocab_rekey_list.txt"
SORT_KEY = "word_sort_field"
DUPLICATE_TAG = "word-array-duplicate"
KEEPER_TAG = "word-array-keeper::%d"

MEANING_RE = re.compile(r"^m(\d+)$")
READING_MARKER_RE = re.compile(r"^(on|kun|r\d+)$")


class Change(NamedTuple):
    nid: int
    before: str
    after: str
    keeper: int
    group: str
    dropped: list  # reading markers the re-key drops, for the (rN) holes report


class Write(NamedTuple):
    nid: int
    field: str
    before: str
    after: str
    tags: str


# --- the plan (pure) ---------------------------------------------------------------------------


def split_key(key: str) -> tuple:
    """A sort field as (base, its markers but a trailing `(mN)`, that meaning number or None)."""
    base = vocab_dupes.base_form(key)
    marks = vocab_dupes.markers(key)
    number = None
    if marks:
        found = MEANING_RE.match(marks[-1])
        if found:
            number = int(found.group(1))
            marks = marks[:-1]
    return base, marks, number


def build_key(base: str, marks: Sequence[str], number: int) -> str:
    return "%s %s(m%d)" % (base, "".join("(%s)" % m for m in marks), number)


def rank(row: dict) -> tuple:
    """The order the notes of a group should survive in: studied first, oldest first within that,
    a canonically spelled note before a variant."""
    return (0 if row["studied"] else 1, row["nid"], 0 if row["base"] == row["canon"] else 1)


def assign_numbers(ranked: list, reserved: set) -> list:
    """One meaning number per note, rising with the rank, never one of `reserved`. A note keeps its
    own number where that number is free and above the number before it."""
    used = set(reserved)
    numbers, floor = [], 0
    for row in ranked:
        number = split_key(row["vocab-key"])[2]
        if number is None or number <= floor or number in used:
            number = next(n for n in itertools.count(floor + 1) if n not in used)
        numbers.append(number)
        used.add(number)
        floor = number
    return numbers


def group_prefix(group: list) -> tuple:
    """The base and markers every note of a respelling group takes: the canonical key with the
    markers its canonically spelled notes carry (the commonest set, best-ranked note wins a tie)."""
    canon = [r for r in sorted(group, key=rank) if r["base"] == r["canon"]]
    counts = Counter(tuple(split_key(r["vocab-key"])[1]) for r in canon)
    marks = list(counts.most_common(1)[0][0]) if counts else []
    return group[0]["canon"], marks


def find_groups(rows: list, data: dict) -> list:
    """`(label, base, marks, notes)` per group to re-key: first the keys spelled several ways,
    then the sort field values that sit on several notes outside those."""
    groups: list = []
    picked: set[int] = set()
    for (canon, reading), group in data["spelling"].items():
        base, marks = group_prefix(group)
        groups.append(("%s [%s]" % (canon, reading), base, marks, group))
        picked.update(r["nid"] for r in group)
    same: dict = defaultdict(list)
    for row in rows:
        if row["nid"] not in picked:
            same[row["vocab-key"].strip()].append(row)
    for key, group in same.items():
        if len(group) < 2:
            continue
        base, marks, _ = split_key(key)
        groups.append(("%s x%d" % (key, len(group)), base, marks, group))
    return groups


def _numbers_by_prefix(rows: list) -> dict:
    """Per (base, markers): the meaning numbers in use, by note id."""
    taken: dict = defaultdict(dict)
    for row in rows:
        base, marks, number = split_key(row["vocab-key"])
        if number is not None:
            taken[(base, tuple(marks))][row["nid"]] = number
    return taken


def plan(rows: list) -> list:
    """Every note of every duplicate group as a `Change`, unchanged sort fields included (they are
    tagged too, so the whole group can be selected in Anki)."""
    data = vocab_dupes.survey(rows)
    taken = _numbers_by_prefix(rows)
    changes = []
    for label, base, marks, group in find_groups(rows, data):
        ranked = sorted(group, key=rank)
        own = set(r["nid"] for r in group)
        reserved = set(
            number for nid, number in taken[(base, tuple(marks))].items() if nid not in own
        )
        keeper = ranked[0]["nid"]
        for row, number in zip(ranked, assign_numbers(ranked, reserved)):
            before = row["vocab-key"].strip()
            dropped = [m for m in split_key(before)[1] if READING_MARKER_RE.match(m)]
            changes.append(
                Change(
                    nid=row["nid"],
                    before=before,
                    after=build_key(base, marks, number),
                    keeper=keeper,
                    group=label,
                    dropped=[m for m in dropped if m not in marks],
                )
            )
    return changes


def tags_for(change: Change) -> str:
    """The tags the op adds to the note: the duplicate tag, and on a variant its keeper's id."""
    if change.nid == change.keeper:
        return DUPLICATE_TAG
    return "%s %s" % (DUPLICATE_TAG, KEEPER_TAG % change.keeper)


def change_list(changes: list) -> list:
    """The report the user checks before `--apply`."""
    by_group: dict = defaultdict(list)
    for change in changes:
        by_group[change.group].append(change)
    rekeyed = [c for c in changes if c.before != c.after]
    respelled = [c for c in changes if split_key(c.before)[0] != split_key(c.after)[0]]
    holes = [c for c in changes if c.dropped]
    lines = [
        "%d notes in %d duplicate groups: %d sort fields rewritten, %d of them respelled"
        % (len(changes), len(by_group), len(rekeyed), len(respelled)),
        "every note is tagged %s, every variant also %s"
        % (DUPLICATE_TAG, KEEPER_TAG.replace("%d", "<keeper nid>")),
        "",
        "== groups ==",
    ]
    for label, group in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
        lines.append("%s  keeper #%d" % (label, group[0].keeper))
        for change in group:
            mark = "keeper" if change.nid == change.keeper else "      "
            if change.before == change.after:
                lines.append("  %s #%d  %s  (tag only)" % (mark, change.nid, change.before))
            else:
                lines.append(
                    "  %s #%d  %s  ->  %s" % (mark, change.nid, change.before, change.after)
                )
    lines += [
        "",
        "== respellings ==",
        "the key rewrites a spelling; exceptions go in" " generator.CANONICAL_EXCEPTIONS",
    ]
    counts = Counter((split_key(c.before)[0], split_key(c.after)[0]) for c in respelled)
    for (before, after), count in counts.most_common():
        lines.append("%4d %s -> %s" % (count, before, after))
    lines += [
        "",
        "== reading-marker holes (task 28, not fixed here) ==",
        "a respelled note drops its reading markers, leaving a gap in that spelling's series",
    ]
    for change in holes:
        lines.append(
            "  #%d  %s  ->  %s  (dropped %s)"
            % (change.nid, change.before, change.after, "".join("(%s)" % m for m in change.dropped))
        )
    return lines


# --- writing (over AnkiConnect) ----------------------------------------------------------------


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _current(infos: list, config: dict) -> dict:
    """Per note id: (its sort field's name, the field's value), or (None, why it can't be read)."""
    out: dict[int, tuple[Optional[str], str]] = {}
    for info in infos:
        if not info:
            continue
        nid = info["noteId"]
        try:
            field = anki_connect.sentence_field(config, info.get("modelName", ""), SORT_KEY)
            out[nid] = (field, info["fields"][field]["value"])
        except (anki_connect.AnkiConnectError, KeyError) as e:
            out[nid] = (None, str(e) or "no sort field")
    return out


def plan_writes(changes: list, infos: list, config: dict) -> tuple:
    """The writes for the notes whose sort field is still what the dump held; why the others are
    refused. A note already holding its new key is written no more, only tagged."""
    current = _current(infos, config)
    writes, refused = [], []
    for change in changes:
        field, value = current.get(change.nid, (None, "no such note"))
        if field is None:
            refused.append("nid %d: %s" % (change.nid, value))
        elif value.strip() not in (change.before, change.after):
            refused.append("nid %d: sort field changed since the dump" % change.nid)
        else:
            writes.append(Write(change.nid, field, value.strip(), change.after, tags_for(change)))
    return writes, refused


def _tag_all(client, writes: list) -> None:
    by_tags: dict = defaultdict(list)
    for write in writes:
        by_tags[write.tags].append(write.nid)
    for tags, nids in by_tags.items():
        client.add_tags(nids, tags)


def apply(client, changes: list, config: dict, undo: Path) -> tuple:
    """Writes the new sort fields and adds the tags, recording both in `undo` first; how many sort
    fields were written, how many notes were tagged, and the refusals."""
    infos = client.notes_info([c.nid for c in changes]) if changes else []
    writes, refused = plan_writes(changes, infos, config)
    written = 0
    for write in writes:
        with open(undo, "a", encoding="utf-8") as out:
            out.write(json.dumps(write._asdict(), ensure_ascii=False) + "\n")
        if write.before != write.after:
            client.update_note_fields(write.nid, {write.field: write.after})
            written += 1
    _tag_all(client, writes)
    return written, len(writes), refused


def revert(client, config: dict, undo: Path) -> tuple:
    """Puts back the sort fields of `undo`, newest first, where the field is still what was
    written, and removes the tags the run added; reverted entries leave the file."""
    entries = [Write(**row) for row in read_jsonl(undo)]
    infos = client.notes_info(sorted(set(e.nid for e in entries))) if entries else []
    current = dict((nid, value) for nid, (field, value) in _current(infos, config).items() if field)
    kept, refused, reverted = [], [], 0
    for entry in reversed(entries):
        if entry.before != entry.after and current.get(entry.nid, "").strip() != entry.after:
            refused.append("nid %d: sort field changed since the re-key, left as is" % entry.nid)
            kept.append(entry)
            continue
        if entry.before != entry.after:
            client.update_note_fields(entry.nid, {entry.field: entry.before})
            current[entry.nid] = entry.before
            reverted += 1
        client.remove_tags([entry.nid], entry.tags)
    text = "".join(json.dumps(e._asdict(), ensure_ascii=False) + "\n" for e in reversed(kept))
    undo.write_text(text, encoding="utf-8")
    return reverted, refused


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="re-dump the notes over AnkiConnect")
    parser.add_argument("--undo", type=Path, default=UNDO)
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", action="store_true")
    args = parser.parse_args()

    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    try:
        config = anki_connect.load_config()
        if args.revert:
            reverted, refused = revert(client, config, args.undo)
            print("\n".join(refused + ["reverted %d sort fields, removed the tags" % reverted]))
            return 0
        if args.fetch:
            print("%d notes -> %s" % (vocab_dupes.fetch(), vocab_dupes.DUMP))
        changes = plan(vocab_dupes.read_dump())
        lines = change_list(changes)
        with open(CHANGES, "w", encoding="utf-8") as out:
            for change in changes:
                out.write(json.dumps(change._asdict(), ensure_ascii=False) + "\n")
        LIST.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(lines[0])
        if not args.apply:
            print("changes in %s, list in %s" % (CHANGES, LIST))
            print("check it, then rerun with --apply")
            return 0
        written, tagged, refused = apply(client, changes, config, args.undo)
        print("\n".join(refused))
        print("wrote %d sort fields, tagged %d notes, refused %d" % (written, tagged, len(refused)))
        print("old values in %s (--revert puts them back)" % args.undo)
        return 0
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
