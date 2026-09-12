import json
import logging
import os
import random
from typing import Any, Sequence

from anki.notes import NoteId
from aqt import mw
from aqt.utils import tooltip

from ..async_api_ops.base_ops import DEFAULT_SYSTEM_INSTRUCTION
from ..async_api_ops.extract_words import (
    get_extract_words_prompt,
    normalize_word_tuple_for_test_comparison,
)
from ..async_api_ops.kanjify_sentence import (
    KANJIFIED_SENTENCE_RETURN_FIELD,
    get_kanjify_sentence_prompt,
)
from ..utils import get_field_config

logger = logging.getLogger(__name__)

_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
_TRAINING_SPLIT_RATIO = 0.8
_WORD_LIST_KEY_ORDER = [
    "nouns",
    "proper_nouns",
    "numbers",
    "counters",
    "verbs",
    "prefix_verbs",
    "suffix_verbs",
    "compound_verbs",
    "adjectives",
    "adverbs",
    "adjectivals",
    "particles",
    "conjunctions",
    "pronouns",
    "suffixes",
    "prefixes",
    "expressions",
    "yojijukugo",
]


def _clean_word_list_obj(word_list_obj: dict) -> dict:
    """Convert matched word tuples to raw unmatched format and order keys canonically."""
    cleaned: dict = {}
    for key in _WORD_LIST_KEY_ORDER:
        if key not in word_list_obj:
            continue
        raw_tuples = []
        for raw_tuple in word_list_obj[key]:
            normalized = normalize_word_tuple_for_test_comparison(raw_tuple)
            if normalized is not None:
                raw_tuples.append(list(normalized))
        cleaned[key] = raw_tuples
    # Preserve unexpected keys at the end
    for key, value in word_list_obj.items():
        if key not in cleaned:
            cleaned[key] = value
    return cleaned


def _get_validation_output_path(training_output_path: str) -> str:
    base_name, extension = os.path.splitext(training_output_path)
    return f"{base_name}_validation{extension}"


def _split_entries(entries: list[str]) -> tuple[list[str], list[str]]:
    if len(entries) <= 1:
        return entries, []

    shuffled_entries = list(entries)
    random.shuffle(shuffled_entries)

    training_count = max(1, int(len(shuffled_entries) * _TRAINING_SPLIT_RATIO))
    return shuffled_entries[:training_count], shuffled_entries[training_count:]


def _write_jsonl_entries(output_path: str, entries: Sequence[str]) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry + "\n")


def _write_split_fine_tuning_files(output_path: str, entries: list[str]) -> tuple[int, int]:
    training_entries, validation_entries = _split_entries(entries)

    _write_jsonl_entries(output_path, training_entries)
    _write_jsonl_entries(_get_validation_output_path(output_path), validation_entries)

    return len(training_entries), len(validation_entries)


def build_migration_rows(pairs: Sequence[tuple[str, str]]) -> tuple[list[str], int]:
    """One jsonl row per distinct sentence, holding the sentence and its word list exactly as
    the fields have them: unparsed, so that invalid production data reaches the migration test
    as it is. Returns the rows and how many duplicate sentences were left out, the first
    note's word list winning."""
    rows: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    for sentence, word_list in pairs:
        if sentence in seen:
            duplicates += 1
            continue
        seen.add(sentence)
        rows.append(json.dumps({"sentence": sentence, "word_list": word_list}, ensure_ascii=False))
    return rows, duplicates


def make_extract_words_migration_data(
    nids: Sequence[NoteId],
    parent: Any = None,
) -> None:
    """Export the old extract_words word lists for testing the word array migration on the
    whole collection. The sentence is the raw `word_extraction_sentence_field`, `<i>` context
    included - the migration strips that itself. Notes whose list is already an array are
    skipped: there is nothing left to migrate in them."""
    config = mw.addonManager.getConfig(__name__)
    if not config:
        logger.error("Make extract-words migration data: missing addon configuration.")
        return

    pairs: list[tuple[str, str]] = []
    skipped = 0
    already_migrated = 0

    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(_OUTPUT_DIR, "extract_words_migration_data.jsonl")

    for nid in nids:
        note = mw.col.get_note(nid)
        note_type = note.note_type()
        try:
            sentence_field = get_field_config(config, "word_extraction_sentence_field", note_type)
            word_list_field = get_field_config(config, "word_list_field", note_type)
        except Exception:
            skipped += 1
            continue
        if sentence_field not in note or word_list_field not in note:
            skipped += 1
            continue

        sentence = note[sentence_field].strip()
        word_list_raw = note[word_list_field].strip()
        if not sentence or not word_list_raw:
            skipped += 1
            continue
        if word_list_raw.startswith("["):
            already_migrated += 1
            continue
        pairs.append((sentence, word_list_raw))

    rows, duplicates = build_migration_rows(pairs)
    _write_jsonl_entries(output_path, rows)
    tooltip(
        f"Wrote {len(rows)} sentences to {output_path}. Skipped {duplicates} duplicate"
        f" sentences, {already_migrated} already migrated notes and {skipped} notes missing"
        " a sentence or word list.",
        period=10000,
    )


def make_kanjify_sentence_fine_tuning_data(
    nids: Sequence[NoteId],
    parent: Any = None,
) -> None:
    config = mw.addonManager.getConfig(__name__)
    if not config:
        logger.error("Make kanjify fine-tuning data: missing addon configuration.")
        return

    entries: list[str] = []
    skipped = 0

    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(_OUTPUT_DIR, "kanjify_sentence_fine_tuning.jsonl")

    for nid in nids:
        log_prefix = f"Make kanjify fine-tuning data--nid:{nid}--"
        note = mw.col.get_note(nid)
        note_type = note.note_type()
        furigana_field = get_field_config(config, "furigana_sentence_field", note_type)
        kanjified_field = get_field_config(config, "kanjified_sentence_field", note_type)

        sentence = note[furigana_field].strip() if furigana_field else ""
        kanjified = note[kanjified_field].strip() if kanjified_field else ""

        if not sentence or not kanjified:
            logger.debug(f"{log_prefix}Skipping: missing sentence or kanjified field.")
            skipped += 1
            continue

        prompt_text = get_kanjify_sentence_prompt(sentence)
        assistant_json = json.dumps(
            {KANJIFIED_SENTENCE_RETURN_FIELD: kanjified}, ensure_ascii=False
        )
        entries.append(
            json.dumps(
                {
                    "messages": [
                        {"role": "system", "content": DEFAULT_SYSTEM_INSTRUCTION},
                        {"role": "user", "content": prompt_text},
                        {"role": "assistant", "content": assistant_json},
                    ]
                },
                ensure_ascii=False,
            )
        )

    training_written, validation_written = _write_split_fine_tuning_files(output_path, entries)
    validation_output_path = _get_validation_output_path(output_path)
    tooltip(
        "Wrote "
        f"{training_written} kanjify training examples to {output_path} and "
        f"{validation_written} validation examples to {validation_output_path}. "
        f"Skipped {skipped} notes."
    )


def make_extract_words_fine_tuning_data(
    nids: Sequence[NoteId],
    parent: Any = None,
) -> None:
    config = mw.addonManager.getConfig(__name__)
    if not config:
        logger.error("Make extract-words fine-tuning data: missing addon configuration.")
        return

    entries: list[str] = []
    skipped = 0

    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(_OUTPUT_DIR, "extract_words_fine_tuning.jsonl")

    for nid in nids:
        log_prefix = f"Make extract-words fine-tuning data--nid:{nid}--"
        note = mw.col.get_note(nid)
        note_type = note.note_type()
        sentence_field = get_field_config(config, "word_extraction_sentence_field", note_type)
        word_list_field = get_field_config(config, "word_list_field", note_type)

        sentence = note[sentence_field].strip() if sentence_field else ""
        word_list_raw = note[word_list_field].strip() if word_list_field else ""

        if not sentence or not word_list_raw:
            logger.debug(f"{log_prefix}Skipping: missing sentence or word list field.")
            skipped += 1
            continue

        try:
            word_list_obj = json.loads(word_list_raw)
        except json.JSONDecodeError:
            logger.warning(f"{log_prefix}Skipping: word list field is not valid JSON.")
            skipped += 1
            continue

        word_list_obj = _clean_word_list_obj(word_list_obj)
        prompt_text = get_extract_words_prompt(sentence)
        assistant_json = json.dumps(word_list_obj, ensure_ascii=False)
        entries.append(
            json.dumps(
                {
                    "messages": [
                        {"role": "system", "content": DEFAULT_SYSTEM_INSTRUCTION},
                        {"role": "user", "content": prompt_text},
                        {"role": "assistant", "content": assistant_json},
                    ]
                },
                ensure_ascii=False,
            )
        )

    training_written, validation_written = _write_split_fine_tuning_files(output_path, entries)
    validation_output_path = _get_validation_output_path(output_path)
    tooltip(
        "Wrote "
        f"{training_written} extract-words training examples to {output_path} and "
        f"{validation_written} validation examples to {validation_output_path}. "
        f"Skipped {skipped} notes."
    )
