"""Read-only views of the vocabulary notes, for an agent investigating one word.

Nothing here writes: an agent researching a reading needs to see the note, the other notes that
answer to the same spelling or reading, and the sentences whose word array links to it, and it
needs all of that without any risk of editing a collection by accident.

`same` is what a "split" verdict turns on - one spelling, two readings, two words - since it
shows whether a separate note already holds the other reading. `links` is the evidence the
reading question is really decided by: the sentences the element appears in, each with the
element as the array stores it, so a reading that the furigana gives outright can be seen.

Japanese on a Windows command line is mangled, so a search query is read from a UTF-8 file
rather than taken as an argument, and every command takes `--out` to write its answer to a file
that can be read back as UTF-8.

    py -3.10 word_array/research/vocab_lookup.py note NID [NID...]
    py -3.10 word_array/research/vocab_lookup.py same NID [--out FILE]
    py -3.10 word_array/research/vocab_lookup.py links NID [--max N] [--out FILE]
    py -3.10 word_array/research/vocab_lookup.py find QUERY_FILE [--out FILE]

`note`, `same` and `links` read `output/vocab_notes.jsonl`, so they show the collection as
`vocab_dupes --fetch` last dumped it; `find` asks Anki itself and needs Anki running.
"""

import argparse
import io
import json
import re
import sys
from pathlib import Path

from _bootstrap import ADDON_ROOT, load, load_shared

import anki_connect
import vocab_dupes
import vocab_unlink

match_flags = load("match_flags")
to_hiragana = load_shared("jp_text_processing.mecab_controller.kana_conv").to_hiragana

TAGS = re.compile(r"<[^>]+>")

SHOWN = [
    "vocab-key",
    "vocab",
    "vocab-kanjified",
    "vocab-kana",
    "vocab-furigana",
    "part-of-speech",
    "vocab-translation",
    "meaning-jp",
]


def clean(text: str) -> str:
    text = TAGS.sub(" ", (text or "").replace("<br>", " ").replace("&nbsp;", " "))
    return " ".join(text.split())


def note_lines(row: dict) -> list:
    """One note as a block of `field: value` lines, the long fields trimmed."""
    lines = ["nid %d%s" % (row["nid"], "  (studied)" if row.get("studied") else "")]
    for name in SHOWN:
        value = clean(row.get(name) or "")
        if value:
            lines.append("  %-18s %s" % (name, value[:300]))
    if row.get("tags"):
        lines.append("  %-18s %s" % ("tags", " ".join(row["tags"])))
    return lines


def same_word(rows: list, nid: int) -> list:
    """Every note that answers to a spelling of this note, or that reads the same way."""
    by_nid = {row["nid"]: row for row in rows}
    row = by_nid.get(nid)
    if row is None:
        return ["nid %d is not in the dump" % nid]
    spellings = vocab_unlink.note_spellings(row)
    reading = to_hiragana((row.get("vocab-kana") or "").strip())
    lines = ["The note itself:"] + note_lines(row)
    lines.append("")
    lines.append("It answers to the spellings: %s" % ", ".join(sorted(spellings)))
    lines.append("")

    by_spelling, by_reading = [], []
    for other in rows:
        if other["nid"] == nid:
            continue
        if vocab_unlink.note_spellings(other) & spellings:
            by_spelling.append(other)
        elif reading and to_hiragana((other.get("vocab-kana") or "").strip()) == reading:
            by_reading.append(other)

    lines.append("Other notes spelled the same (%d):" % len(by_spelling))
    for other in by_spelling:
        lines += note_lines(other)
    if not by_spelling:
        lines.append("  none: no other note answers to any of those spellings")
    lines.append("")
    lines.append("Other notes read the same but spelled differently (%d):" % len(by_reading))
    for other in by_reading:
        lines += note_lines(other)
    if not by_reading:
        lines.append("  none")
    return lines


def linking_sentences(rows: list, nid: int, limit: int) -> list:
    """The sentences whose array links this note, each with the element as the array stores it."""
    by_nid = {row["nid"]: row for row in rows}
    row = by_nid.get(nid)
    if row is None:
        return ["nid %d is not in the dump" % nid]
    lines = ["Sentences whose word array links nid %d:" % nid, ""]
    shown = 0
    for other in rows:
        array = match_flags.decode_word_array(other.get(vocab_unlink.ARRAY_FIELD) or "")
        if not array:
            continue
        hits = [
            element
            for _, element in match_flags.iter_words(array)
            if len(element) >= 6 and match_flags.matched_note_id(element) == nid
        ]
        if not hits:
            continue
        shown += 1
        if shown > limit:
            continue
        lines.append("--- sentence note %d" % other["nid"])
        sentence = clean(other.get("sentence-kanjified-furigana") or "")
        if sentence:
            lines.append("  %s" % sentence)
        for element in hits:
            lines.append(
                "  element: raw=%s  pos=%s  dict_form=%s  reading=%s  match=%s"
                % (element[0], element[1], element[2], element[3], json.dumps(element[4]))
            )
        lines.append("")
    lines.insert(1, "%d sentence(s) link it; showing up to %d." % (shown, limit))
    return lines


def define(word: str, strip: bool = True) -> list:
    """What the MDX dictionaries under `user_files` say about a word.

    Whether a reading is a headword, a listed variant inside another entry, or not attested at
    all is the evidence that settles most of these questions, and it is the one thing
    `sync_local_ops/mdx_dictionary.py` cannot be reused for: it imports `aqt` at module level and
    so only runs inside Anki.
    """
    sys.path.insert(0, str(ADDON_ROOT / "lib"))
    from mdict_query import IndexBuilder  # type: ignore

    lines = ["%s in the %d dictionaries under user_files:" % (word, len(MDX_FILES())), ""]
    for path in MDX_FILES():
        try:
            builder = IndexBuilder(str(path), sql_index=True, check=False)
            found = builder.mdx_lookup(word)
            # An entry may be a redirect: `@@@LINK=<the real headword>`.
            seen = set()
            while found and found[0].strip().startswith("@@@LINK="):
                target = found[0].strip()[len("@@@LINK="):].strip().rstrip("\x00")
                if target in seen:
                    break
                seen.add(target)
                lines.append("  (%s redirects to %s)" % (path.stem, target))
                found = builder.mdx_lookup(target)
        except Exception as e:  # a missing or unreadable .mdx must not stop the others
            lines.append("--- %s: unreadable (%s)" % (path.stem, e))
            continue
        if not found:
            lines.append("--- %s: no entry" % path.stem)
            continue
        text = "\n".join(found)
        if strip:
            text = " ".join(TAGS.sub(" ", text).split())
        lines.append("--- %s" % path.stem)
        lines.append("  %s" % text[:1500])
    return lines


def MDX_FILES() -> list:
    return sorted((ADDON_ROOT / "user_files").glob("*.mdx"))


def find(query: str) -> list:
    """A live Anki search, as `nid` plus the fields that identify a vocabulary note."""
    client = anki_connect.AnkiConnect(timeout=60)
    nids = client.invoke("findNotes", query=query)
    lines = ["%d note(s) match %s" % (len(nids), query), ""]
    for start in range(0, len(nids[:200]), 100):
        for info in client.notes_info(nids[start : start + 100]):
            if not info:
                continue
            values = info.get("fields", {})
            row = {"nid": info["noteId"], "tags": info.get("tags", [])}
            for name in vocab_dupes.FIELDS:
                row[name] = values.get(name, {}).get("value", "")
            lines += note_lines(row)
    if len(nids) > 200:
        lines.append("... %d more not shown" % (len(nids) - 200))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("note", "same", "links"):
        p = sub.add_parser(name)
        p.add_argument("nids", type=int, nargs="+" if name == "note" else 1)
        p.add_argument("--out", type=Path)
        if name == "links":
            p.add_argument("--max", type=int, default=12, help="how many sentences to show")
    p = sub.add_parser("find")
    p.add_argument("query_file", type=Path, help="a UTF-8 file holding the Anki search query")
    p.add_argument("--out", type=Path)
    p = sub.add_parser("define")
    p.add_argument("word_file", type=Path, help="a UTF-8 file holding the word to look up")
    p.add_argument("--html", action="store_true", help="keep the entries' markup")
    p.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.command == "find":
        lines = find(args.query_file.read_text(encoding="utf-8").strip())
    elif args.command == "define":
        lines = define(args.word_file.read_text(encoding="utf-8").strip(), strip=not args.html)
    else:
        rows = vocab_dupes.read_dump()
        if args.command == "note":
            by_nid = {row["nid"]: row for row in rows}
            lines = []
            for nid in args.nids:
                row = by_nid.get(nid)
                lines += note_lines(row) if row else ["nid %d is not in the dump" % nid]
                lines.append("")
        elif args.command == "same":
            lines = same_word(rows, args.nids[0])
        else:
            lines = linking_sentences(rows, args.nids[0], args.max)

    text = "\n".join(lines) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
        print("wrote %s (%d lines)" % (args.out, len(lines)))
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
