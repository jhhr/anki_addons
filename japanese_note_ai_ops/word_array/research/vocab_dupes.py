"""How many vocab notes are duplicates of one another under the canonical key (task 25).

The old `match_words_to_notes` created a note per spelling it met, so one learnable word can own
several notes (近付く / 近づく, 氣 / 気, という / と言う). The canonical key of task 18 —
`generator.canonical_form` over the note's sort field minus its markers, keyed with the reading —
says which notes are the same word. This script only measures; the op is task 26.

    py -3.10 word_array/research/vocab_dupes.py --fetch   # dump the notes over AnkiConnect first
    py -3.10 word_array/research/vocab_dupes.py           # survey the dump

Dump `output/vocab_notes.jsonl`, report `output/vocab_dupes_report.txt`.
"""

import argparse
import json
import re
from collections import Counter, defaultdict

from _bootstrap import ADDON_ROOT, load
from anki_connect import AnkiConnect, load_config

generator = None

DUMP = ADDON_ROOT / "output" / "vocab_notes.jsonl"
REPORT = ADDON_ROOT / "output" / "vocab_dupes_report.txt"
MODEL = "Japanese vocab note"
FIELDS = [
    "vocab-key",
    "vocab",
    "vocab-kanjified",
    "vocab-kana",
    "vocab-furigana",
    "part-of-speech",
    "vocab-id",
    "meaning-jp",
    "vocab-translation",
    "sentence-kanjified-furigana",
    "sentence-vocab-list",
]
MARKERS_RE = re.compile(r"(\s*\([^)]*\))+$")
MARKER_RE = re.compile(r"\(([^)]*)\)")
NOTE_ID_RE = re.compile(r"\d{10,}")

# Task 20's kanjify policy in the shape a stored word takes: a helper verb after て/で and the
# copula are kana, the same verb alone is kanji. A key spelling one of these in kanji is the same
# word as the key spelling it in kana.
HELPERS = [
    ("て見る", "てみる"),
    ("て居る", "ている"),
    ("て来る", "てくる"),
    ("て行く", "ていく"),
    ("て仕舞う", "てしまう"),
    ("て呉れる", "てくれる"),
    ("て下さる", "てくださる"),
    ("て下さい", "てください"),
    ("て遣る", "てやる"),
    ("て貰う", "てもらう"),
    ("て置く", "ておく"),
    ("て有る", "てある"),
    ("で有る", "である"),
    ("で無い", "でない"),
    ("じゃ無い", "じゃない"),
    ("て無い", "てない"),
    ("で御座います", "でございます"),
]


def fetch(out=DUMP) -> int:
    """Dump every vocab note's key fields, with `studied` (the note has a card past its first)."""
    client = AnkiConnect(timeout=300)
    nids = client.invoke("findNotes", query='note:"%s"' % MODEL)
    studied = set(client.invoke("findNotes", query='note:"%s" -is:new' % MODEL))
    written = 0
    with out.open("w", encoding="utf-8") as fh:
        for start in range(0, len(nids), 500):
            for info in client.notes_info(nids[start : start + 500]):
                if not info:
                    continue
                values = info.get("fields", {})
                row = {
                    "nid": info["noteId"],
                    "tags": info.get("tags", []),
                    "studied": info["noteId"] in studied,
                }
                for name in FIELDS:
                    row[name] = values.get(name, {}).get("value", "")
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
    return written


def read_dump(path=DUMP) -> list:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def base_form(key: str) -> str:
    """The sort field without its trailing `(m2)`, `(kun)`, `(r1)`, `(x2)` markers."""
    return MARKERS_RE.sub("", key).strip()


def markers(key: str) -> list:
    return MARKER_RE.findall(key[len(base_form(key)) :])


def unkanjified(form: str) -> str:
    """The form with task 20's grammar helpers in kana (為て見る -> 為てみる)."""
    for kanji, kana in HELPERS:
        form = form.replace(kanji, kana)
    return form


def _generator():
    """Loaded on demand: the key helpers above work without Sudachi and JMdict."""
    global generator
    if generator is None:
        generator = load("generator")
    return generator


def canonical_key(key: str, reading: str) -> str:
    return unkanjified(_generator().canonical_form(base_form(key), reading))


def survey(rows: list) -> dict:
    groups: dict = defaultdict(list)
    for row in rows:
        reading = row["vocab-kana"].strip()
        row["base"] = base_form(row["vocab-key"])
        row["canon"] = canonical_key(row["vocab-key"], reading)
        groups[(row["canon"], reading)].append(row)

    # Links: every reference to a vocab note id in a note's word list / word array.
    ids = set(str(row["nid"]) for row in rows)
    links: Counter = Counter()
    for row in rows:
        for found in set(NOTE_ID_RE.findall(row["sentence-vocab-list"])):
            if found in ids:
                links[found] += 1
    for row in rows:
        row["links"] = links[str(row["nid"])]

    spelling = dict((k, v) for k, v in groups.items() if len(set(r["base"] for r in v)) > 1)
    same_key = dict((k, v) for k, v in groups.items() if len(v) > 1 and k not in spelling)
    return {"groups": groups, "spelling": spelling, "same_key": same_key}


def describe(rows: list) -> str:
    parts = []
    for row in sorted(rows, key=lambda r: r["nid"]):
        mark = "*" if row["studied"] else ""
        link = ("<-%d" % row["links"]) if row["links"] else ""
        parts.append("%s#%s%s%s" % (row["vocab-key"], row["nid"], mark, link))
    return " | ".join(parts)


def write_report(rows: list, data: dict, path: str) -> None:
    spelling, same_key = data["spelling"], data["same_key"]
    dupes = sorted(spelling.values(), key=len, reverse=True)
    losers = [r for group in dupes for r in group if r["base"] != r["canon"]]
    studied_losers = [r for r in losers if r["studied"]]
    linked_losers = [r for r in losers if r["links"]]
    exact = Counter(r["vocab-key"].strip() for r in rows)
    exact_dupes = dict((k, c) for k, c in exact.items() if c > 1)
    with open(path, "w", encoding="utf-8") as out:
        out.write(
            f"{len(rows)} vocab notes, {len(data['groups'])} canonical keys"
            " (key = canonical_form(sort field without markers), read with the note's reading)\n"
            f"{len(spelling)} keys spelled several ways:"
            f" {sum(len(v) for v in spelling.values())} notes,"
            f" {len(losers)} not spelled canonically"
            f" ({len(studied_losers)} studied, {len(linked_losers)} linked from a word list)\n"
            f"{len(same_key)} keys with several notes of one spelling:"
            f" {sum(len(v) for v in same_key.values())} notes (meaning and reading splits)\n"
            f"{len(exact_dupes)} sort field values sit on more than one note"
            f" ({sum(exact_dupes.values())} notes)\n"
            f"{sum(1 for r in rows if r['studied'])} notes studied,"
            f" {sum(1 for r in rows if r['links'])} linked from a word list\n\n"
            "A note is written key#id, * = studied, <-n = n word lists link to it.\n\n"
        )
        out.write("== keys spelled several ways ==\n")
        for group in dupes:
            out.write(
                "%s [%s] x%d: %s\n"
                % (group[0]["canon"], group[0]["vocab-kana"], len(group), describe(group))
            )
        out.write("\n== spellings that canonicalise to another key ==\n")
        respelled = Counter((r["base"], r["canon"]) for r in rows if r["base"] != r["canon"])
        for (before, after), count in respelled.most_common():
            out.write("%4d %s -> %s\n" % (count, before, after))
        out.write("\n== one spelling, several notes: marker shapes ==\n")
        shapes: Counter = Counter()
        for group in same_key.values():
            shape = tuple(sorted(set(m for r in group for m in markers(r["vocab-key"]))))
            shapes[shape] += 1
        for shape, count in shapes.most_common(30):
            out.write("%5d %s\n" % (count, "".join("(%s)" % m for m in shape) or "(no markers)"))
        out.write("\n== sort field values on more than one note ==\n")
        for key, count in sorted(exact_dupes.items(), key=lambda kv: -kv[1]):
            out.write("%4d %s\n" % (count, key))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="re-dump the notes over AnkiConnect")
    ap.add_argument("--out", default=str(REPORT))
    args = ap.parse_args()
    if args.fetch:
        load_config()  # fails early when the add-on config can't be read
        print("%d notes -> %s" % (fetch(), DUMP))
    rows = read_dump()
    write_report(rows, survey(rows), args.out)
    print(args.out)


if __name__ == "__main__":
    main()
