"""Reads and writes one note's kanjified sentence field by note id, through AnkiConnect.

Made for agents fixing the kanjify audit's rows one note at a time, who need nothing but the
note id: the field is the note type's `kanjified_sentence_field` from the add-on's config.

    py -3.10 word_array/research/kanjify_note.py get NID
    py -3.10 word_array/research/kanjify_note.py set NID BASE FILE
    py -3.10 word_array/research/kanjify_note.py revert

`get` prints `base <hash>` and then the field's value. `set` writes the UTF-8 text of FILE (so no
Japanese goes through the shell) into the field, refusing when:
  - the field's hash is no longer BASE (someone wrote it since your `get`: get it again),
  - the new value doesn't read as the old one (every tag dropped and every furigana group read as
    its kana, whitespace aside): an edit may only kanjify or un-kanjify, never change the text,
  - its `<k>` and `</k>` tags don't pair up, or nothing changed.
Each write first appends the old value to `output/kanjify_note_undo.jsonl`; `revert` writes the
old values back, newest first, where the field is still what was written (`kanjify_fix.revert`).
"""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from _bootstrap import ADDON_ROOT

import anki_connect
import kanjify_fix

UNDO = ADDON_ROOT / "output" / "kanjify_note_undo.jsonl"
FIELD_KEY = kanjify_fix.FIELD_KEY
TAG_RE = re.compile(r"<[^>]+>")
GROUP_RE = re.compile(r"[\d々ヶヵ〆一-龯㐀-䶿]+\[([^\]]*)\]")
K_TAG_RE = re.compile(r"</?k>")


def field_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def reading(value: str) -> str:
    """What the field reads: tags dropped, each furigana group as its kana, no whitespace."""
    return re.sub(r"\s", "", GROUP_RE.sub(lambda m: m[1], TAG_RE.sub("", value)))


def edit_problem(before: str, after: str) -> Optional[str]:
    if after == before:
        return "nothing changed"
    tags = [m[0] for m in K_TAG_RE.finditer(after)]
    if any(tag != ("<k>", "</k>")[i % 2] for i, tag in enumerate(tags)) or len(tags) % 2:
        return "its <k> and </k> tags don't pair up"
    if reading(after) != reading(before):
        return f"it doesn't read as the old value:\n  old {reading(before)}\n  new {reading(after)}"
    return None


def get(client, config: dict, nid: int) -> tuple[str, str]:
    """The note's kanjified sentence field name and value."""
    infos = client.notes_info([nid])
    if not infos or not infos[0]:
        raise anki_connect.AnkiConnectError(f"No note {nid}.")
    field = anki_connect.sentence_field(config, infos[0].get("modelName", ""), FIELD_KEY)
    return field, anki_connect.note_sentence(config, infos[0], FIELD_KEY)


def set_field(client, config: dict, nid: int, base: str, value: str, undo: Path) -> str:
    field, before = get(client, config, nid)
    if field_hash(before) != base:
        raise anki_connect.AnkiConnectError(
            f"Note {nid} changed since base {base}: get it again and redo the edit on that."
        )
    problem = edit_problem(before, value)
    if problem:
        raise anki_connect.AnkiConnectError(f"Note {nid} not written: {problem}")
    entry = kanjify_fix.Write(nid, field, before, value)
    with open(undo, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry._asdict(), ensure_ascii=False) + "\n")
    client.update_note_fields(nid, {field: value})
    return f"wrote note {nid}, base now {field_hash(value)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    parser.add_argument("--undo", type=Path, default=UNDO)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("get").add_argument("nid", type=int)
    set_parser = sub.add_parser("set")
    set_parser.add_argument("nid", type=int)
    set_parser.add_argument("base", help="the hash `get` printed")
    set_parser.add_argument("file", type=Path, help="UTF-8 file holding the new field value")
    sub.add_parser("revert")
    args = parser.parse_args()

    client = anki_connect.AnkiConnect(args.anki_connect)
    config = anki_connect.load_config()
    try:
        if args.command == "get":
            _, value = get(client, config, args.nid)
            print(f"base {field_hash(value)}\n{value}")
        elif args.command == "set":
            value = args.file.read_text(encoding="utf-8").strip("\r\n")
            print(set_field(client, config, args.nid, args.base, value, args.undo))
        else:
            reverted, refused = kanjify_fix.revert(client, config, args.undo)
            print("\n".join(refused + [f"reverted {reverted} writes"]))
        return 0
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
