"""Dumps every note's kanjify input and kanjified sentence through AnkiConnect, for the kanjify
golden set (`kanjify_golden.py`).

For every note type the addon's config gives both a `furigana_sentence_field` (the op's input)
and a `kanjified_sentence_field`, each note's two fields go to `evals/kanjify_golden/dump.jsonl`:
`{"nid", "mod", "model", "sentence", "kanjified", "translation"}`, sorted by note id,
whitespace around the fields stripped. `translation` is the note's `translated_sentence_field`
as plain text, "" when its type has none: the prompts show it, since the sentence alone
sometimes doesn't say which word it means. The golden set's workers read this file, never
Anki, so that they can run on a machine without the collection; rerun it when the collection
has changed and the inventory should follow. Read only: nothing is written to the collection.

    py -3.10 word_array/research/kanjify_golden_dump.py [--out FILE] [--translations]

`--translations` fills in only the translations of the dump there is, from the notes as they
are now: the sentences are what the inventory, the queues and every result are keyed by, so a
new dump in the middle of a run would orphan them, while a translation is only read by the
prompts.

Also writes `dump_meta.json` beside it: when, how many notes per note type, and how many notes
have an empty input field (left out).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from collections import Counter
from pathlib import Path

import anki_connect
import kanjify_golden as golden

INPUT_KEY = "furigana_sentence_field"
OUTPUT_KEY = "kanjified_sentence_field"
TRANSLATION_KEY = "translated_sentence_field"
CHUNK = 500
BREAK_RE = re.compile(r"<(?:br|/?div|/?p|/?li)\b[^>]*>", re.I)


def note_types(config: dict) -> dict[str, tuple[str, str, str]]:
    """Note type name -> (input field, kanjified field, translation field or ""), for each type
    the config gives the first two."""
    out = {}
    for name, value in config.items():
        if isinstance(value, dict) and value.get(INPUT_KEY) and value.get(OUTPUT_KEY):
            out[name] = (value[INPUT_KEY], value[OUTPUT_KEY], value.get(TRANSLATION_KEY) or "")
    return out


def plain(value: str) -> str:
    """A translation field as one line of plain text, the way a prompt quotes it: a line or
    block break becomes a space, any other tag nothing (`<b>` inside a word)."""
    text = html.unescape(golden.TAG_RE.sub("", BREAK_RE.sub(" ", value)))
    return " ".join(text.split())


def field_text(info: dict, field: str) -> str:
    return info.get("fields", {}).get(field, {}).get("value", "") if field else ""


def add_translations(client, config: dict, rows: list[dict]) -> int:
    """Sets each dump row's `translation` from its note as it is now and changes nothing else;
    the number of rows that have one."""
    fields = {model: f[2] for model, f in note_types(config).items()}
    model_of = {r["nid"]: r["model"] for r in rows}
    found: dict[int, str] = {}
    nids = list(model_of)
    for start in range(0, len(nids), CHUNK):
        for info in client.notes_info(nids[start : start + CHUNK]):
            if info:
                nid = info["noteId"]
                found[nid] = plain(field_text(info, fields.get(model_of[nid], "")))
        print(f"translations: {min(start + CHUNK, len(nids))}/{len(nids)}")
    for r in rows:
        r["translation"] = found.get(r["nid"], "")
    return sum(1 for r in rows if r["translation"])


def dump(client, config: dict) -> tuple[list[dict], Counter]:
    rows: list[dict] = []
    counts: Counter[str] = Counter()
    for model, (input_field, output_field, translation_field) in note_types(config).items():
        nids = client.invoke("findNotes", query='note:"%s"' % model)
        for start in range(0, len(nids), CHUNK):
            for info in client.notes_info(nids[start : start + CHUNK]):
                if not info:
                    continue
                fields = info.get("fields", {})
                sentence = fields.get(input_field, {}).get("value", "").strip()
                if not sentence:
                    counts["empty input"] += 1
                    continue
                rows.append(
                    {
                        "nid": info["noteId"],
                        "mod": info.get("mod"),
                        "model": model,
                        "sentence": sentence,
                        "kanjified": fields.get(output_field, {}).get("value", "").strip(),
                        "translation": plain(field_text(info, translation_field)),
                    }
                )
                counts[model] += 1
            print(f"{model}: {min(start + CHUNK, len(nids))}/{len(nids)}")
    rows.sort(key=lambda r: r["nid"])
    return rows, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=golden.DUMP)
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    parser.add_argument("--translations", action="store_true",
                        help="only fill in the translations of the existing dump")  # fmt: skip
    args = parser.parse_args()
    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    meta_path = args.out.parent / "dump_meta.json"
    try:
        if args.translations:
            rows = golden.read_jsonl(args.out)
            n = add_translations(client, anki_connect.load_config(), rows)
        else:
            rows, counts = dump(client, anki_connect.load_config())
            n = sum(1 for r in rows if r["translation"])
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1
    golden.write_jsonl(args.out, rows)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if args.translations:
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        meta.update({"translations": n, "translations_added": now})
    else:
        meta = {
            "dumped": now,
            "notes": len(rows),
            "sentences": len({r["sentence"] for r in rows}),
            "counts": dict(counts),
            "translations": n,
        }
    meta_path.write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
