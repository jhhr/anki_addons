"""Dumps every note's kanjify input and kanjified sentence through AnkiConnect, for the kanjify
golden set (`kanjify_golden.py`).

For every note type the addon's config gives both a `furigana_sentence_field` (the op's input)
and a `kanjified_sentence_field`, each note's two fields go to `evals/kanjify_golden/dump.jsonl`:
`{"nid", "mod", "model", "sentence", "kanjified"}`, sorted by note id, whitespace around the
fields stripped. The golden set's workers read this file, never Anki, so that they can run on a
machine without the collection; rerun it when the collection has changed and the inventory
should follow. Read only: nothing is written to the collection.

    py -3.10 word_array/research/kanjify_golden_dump.py [--out FILE]

Also writes `dump_meta.json` beside it: when, how many notes per note type, and how many notes
have an empty input field (left out).
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import anki_connect
import kanjify_golden as golden

INPUT_KEY = "furigana_sentence_field"
OUTPUT_KEY = "kanjified_sentence_field"
CHUNK = 500


def note_types(config: dict) -> dict[str, tuple[str, str]]:
    """Note type name -> (input field, kanjified field), for each type the config gives both."""
    out = {}
    for name, value in config.items():
        if isinstance(value, dict) and value.get(INPUT_KEY) and value.get(OUTPUT_KEY):
            out[name] = (value[INPUT_KEY], value[OUTPUT_KEY])
    return out


def dump(client, config: dict) -> tuple[list[dict], Counter]:
    rows: list[dict] = []
    counts: Counter[str] = Counter()
    for model, (input_field, output_field) in note_types(config).items():
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
    args = parser.parse_args()
    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    try:
        rows, counts = dump(client, anki_connect.load_config())
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1
    golden.write_jsonl(args.out, rows)
    meta = {
        "dumped": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "notes": len(rows),
        "sentences": len({r["sentence"] for r in rows}),
        "counts": dict(counts),
    }
    (args.out.parent / "dump_meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
