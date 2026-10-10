import json
import logging
import os
from typing import Any, Callable, Sequence

from anki.notes import NoteId
from aqt import mw
from aqt.utils import showInfo, tooltip

from ..utils import get_field_config

logger = logging.getLogger(__name__)

_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")


def _write_jsonl_entries(output_path: str, entries: Sequence[str]) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry + "\n")


def _build_sentence_rows(
    notes: Sequence[tuple[NoteId, str, str]], value_key: str
) -> tuple[list[str], int]:
    rows: dict[str, dict] = {}
    duplicates = 0
    for nid, sentence, value in notes:
        row = rows.get(sentence)
        if row is None:
            rows[sentence] = {"sentence": sentence, value_key: value, "nids": [nid]}
        else:
            duplicates += 1
            row["nids"].append(nid)
    return [json.dumps(row, ensure_ascii=False) for row in rows.values()], duplicates


def build_kanjify_rows(notes: Sequence[tuple[NoteId, str, str]]) -> tuple[list[str], int]:
    """One jsonl row per distinct sentence, holding the furigana sentence and its kanjified
    version as the fields have them. `notes` are (note id, furigana, kanjified). A row's
    `nids` are every note with that sentence, so that a tool editing the sentence reaches
    them all. Returns the rows and how many duplicate sentences were folded in, the first
    note's kanjified sentence winning. No prompt is baked in, so a fine-tuning or eval format
    can be built from the rows with whatever prompt is current."""
    return _build_sentence_rows(notes, "kanjified")


def _run_with_config(write: Callable[[dict, Sequence[NoteId]], str], nids: Sequence[NoteId]):
    config = mw.addonManager.getConfig(__name__)
    if not config:
        logger.error("Make test data: missing addon configuration.")
        return
    tooltip(write(config, nids), period=10000)


def make_kanjify_sentence_data(nids: Sequence[NoteId], parent: Any = None) -> None:
    _run_with_config(_write_kanjify_sentence_data, nids)


def _write_kanjify_sentence_data(config: dict, nids: Sequence[NoteId]) -> str:
    """Export furigana sentences with their kanjified versions, one row per sentence, for
    evaluating and fine-tuning `kanjify_sentence`."""
    notes: list[tuple[NoteId, str, str]] = []
    skipped = 0

    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(_OUTPUT_DIR, "kanjify_sentence_data.jsonl")

    for nid in nids:
        note = mw.col.get_note(nid)
        note_type = note.note_type()
        if note_type is None:
            skipped += 1
            continue
        try:
            furigana_field = get_field_config(config, "furigana_sentence_field", note_type)
            kanjified_field = get_field_config(config, "kanjified_sentence_field", note_type)
        except Exception:
            skipped += 1
            continue
        if furigana_field not in note or kanjified_field not in note:
            skipped += 1
            continue

        sentence = note[furigana_field].strip()
        kanjified = note[kanjified_field].strip()
        if not sentence or not kanjified:
            skipped += 1
            continue
        notes.append((nid, sentence, kanjified))

    rows, duplicates = build_kanjify_rows(notes)
    _write_jsonl_entries(output_path, rows)
    return (
        f"Wrote {len(rows)} kanjify sentences to {output_path}. Skipped {duplicates} duplicate"
        f" sentences and {skipped} notes missing a sentence or kanjified sentence."
    )


# Config key holding the search query for each export, in the order they run.
TEST_DATA_EXPORTS: list[tuple[str, Callable[[dict, Sequence[NoteId]], str]]] = [
    ("kanji_sentence_fine_tuning_data_query", _write_kanjify_sentence_data),
]


def run_test_data_exports(
    config: dict,
    find_notes: Callable[[str], Sequence[NoteId]],
    exports: Sequence[tuple[str, Callable[[dict, Sequence[NoteId]], str]]] = TEST_DATA_EXPORTS,
) -> list[str]:
    """Run each export on the notes its config query finds, one message per export. An empty
    query skips that export; a failing one is reported without stopping the rest."""
    messages: list[str] = []
    for query_key, write in exports:
        query = (config.get(query_key) or "").strip()
        if not query:
            messages.append(f"Skipped: `{query_key}` is not set.")
            continue
        try:
            messages.append(write(config, find_notes(query)))
        except Exception as e:
            logger.error(f"Make test data: `{query_key}` failed: {e}", exc_info=True)
            messages.append(f"`{query_key}` failed: {e}")
    return messages


def make_all_test_data(parent: Any = None) -> None:
    """Tools menu action: every export, each on the notes its config query finds."""
    config = mw.addonManager.getConfig(__name__)
    if not config:
        logger.error("Make test data: missing addon configuration.")
        return
    messages = run_test_data_exports(config, mw.col.find_notes)
    showInfo("\n\n".join(messages), parent=parent or mw, title="AI ops: generate test data")
