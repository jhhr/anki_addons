"""The vocab triage's working copy of the collection, and its word list.

    py -3.10 word_array/research/triage_extract.py restore --backup BACKUP.colpkg --to COPY.anki2
    py -3.10 word_array/research/triage_extract.py words --collection COPY.anki2 \\
        --note-type NAME --processing-deck NAME

`restore` unpacks one of Anki's automatic backups into a collection file of its own, outside any
profile folder, so that nothing reads the profile's while Anki holds it open. `words` writes
`words.jsonl`: one row per note tagged `new_matched_jp_word`, judged or not, with what the
features, the Opus prompts and the judging page show of it: the word, its reading and meaning,
its card's state, and its sibling notes (the same word in another sense or reading; see
`triage_data.siblings`). The options are saved in `settings.json` beside it (triage_data), so
the later scripts need none of them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import triage_data as td


def word_rows(col, note_type: str) -> list[dict]:
    notes = td.load_vocab_notes(col, note_type)
    related = td.siblings(notes)
    decks = {d.id: d.name for d in col.decks.all_names_and_ids()}
    rows = []
    for nid in sorted(notes):
        note = notes[nid]
        if not note.has_tag(td.NEW_WORD_TAG):
            continue
        card = note.card
        if card is None:
            continue
        freq = td.plain(note.get("vocab-frequency"))
        rows.append(
            {
                "nid": nid,
                "cid": card.cid,
                "cards": len(note.cards),
                "key": note.key,
                "base": note.base,
                "markers": td.markers(note.get("vocab-key")),
                "word": td.plain(note.get("vocab")),
                "kanjified": note.kanjified,
                "ignore_kanjified": bool(td.plain(note.get("ignore-kanjified-form"))),
                "reading": note.reading,
                "pos": td.plain(note.get("part-of-speech")),
                "meaning": td.plain(note.get("vocab-translation")),
                "meaning_jp": td.plain(note.get("meaning-jp")),
                "sentence": td.plain(note.get("sentence")),
                "sentence_translation": td.plain(note.get("sentence-translation")),
                "sentence_nids": td.linked_nids(
                    "".join("nid:%s " % n for n in note.get("sentence-nids").split(","))
                ),
                "vocab_frequency": int(freq) if freq.isdigit() else None,
                "deck": decks.get(card.odid or card.did, ""),
                "type": card.type,
                "queue": card.queue,
                "ivl": card.ivl,
                "reps": card.reps,
                "lapses": card.lapses,
                "tags": sorted(note.tags),
                "siblings": [
                    {
                        "nid": other,
                        "key": notes[other].key,
                        "reading": notes[other].reading,
                        "relation": relation,
                        "meaning": td.plain(notes[other].get("vocab-translation")),
                        "reviewed": notes[other].reviewed,
                        "tagged": notes[other].has_tag(td.NEW_WORD_TAG),
                    }
                    for other, relation in sorted(related[nid].items())
                ],
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    restore = sub.add_parser("restore", help="unpack a .colpkg backup into a working copy")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--to", type=Path, required=True)
    words = sub.add_parser("words", help="write words.jsonl")
    td.add_settings_args(words)
    args = parser.parse_args()

    if args.command == "restore":
        td.restore_backup(args.backup, args.to)
        col = td.open_collection(str(args.to))
        try:
            check = col.db.scalar("pragma quick_check")
            print(f"{args.to}: {col.note_count()} notes, {col.card_count()} cards, {check}")
        finally:
            col.close()
        td.settings(argparse.Namespace(collection=str(args.to)), required=())
        return 0

    settings = td.settings(args)
    col = td.open_collection(settings["collection"])
    try:
        rows = word_rows(col, settings["note_type"])
    finally:
        col.close()
    path = td.data_file("words.jsonl")
    td.write_jsonl(path, rows)
    print(f"{len(rows)} notes tagged {td.NEW_WORD_TAG} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
