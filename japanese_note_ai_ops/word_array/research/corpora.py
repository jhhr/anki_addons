"""The sentence corpora the research scripts read, each sentence with its old extract_words word
list (see `old_word_lists.py`):

- `export`: `output/extract_words_migration_data.jsonl`, every sentence of the collection with
  its raw word list field, as the since-removed `make_extract_words_migration_data` wrote it
  before the migration - `{"sentence", "word_list", "nids"}` per row, `nids` every note with the
  sentence. A field that is not valid JSON is counted and skipped.
- `checked`: `output/extract_words_migration_data_checked.jsonl`, a hand-checked subset of that.

`hand_judge`, `judge_eval`, `proper_nouns`, `proper_noun_eval`, `sub_readings`,
`okurigana_decomp`, `name_lexicon`, `canonical_forms` and `unbalanced_tags` read their sentences
through `CORPORA` and `read_export`, and the ones that generate arrays like the op does build
the name lexicon with `export_name_lexicon`.
"""

import json
import time
from collections import Counter
from pathlib import Path

from _bootstrap import ADDON_ROOT, load, load_root

generator = load("generator")
html_stripping = load_root("html_stripping")

CORPORA = {
    "export": ADDON_ROOT / "output" / "extract_words_migration_data.jsonl",
    "checked": ADDON_ROOT / "output" / "extract_words_migration_data_checked.jsonl",
}


def name_lexicon(corpus: list[tuple[str, dict]]) -> dict:
    """The name lexicon of a corpus' sentences. The op builds its lexicon from the whole
    collection's sentences, so give this the export for arrays like the op's."""
    before = time.perf_counter()
    sentences = [html_stripping.strip_context_sentences(s) for s, _ in corpus]
    lexicon = generator.build_name_lexicon(sentences)
    print(f"Name lexicon: {len(lexicon)} names, {time.perf_counter() - before:.0f} s")
    return lexicon


def export_name_lexicon() -> dict:
    """The name lexicon of the export, the collection's sentences."""
    return name_lexicon(read_export(CORPORA["export"], Counter()))


def read_export(path: Path, invalid: Counter) -> list[tuple[str, dict]]:
    """(sentence, word list dict) for every row of an export, the dict empty where the field
    was empty. What can't be read is counted in `invalid` and left out."""
    out: list[tuple[str, dict]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid["unreadable row"] += 1
            continue
        sentence, text = row.get("sentence") or "", (row.get("word_list") or "").strip()
        if not text:
            invalid["empty word list"] += 1
            out.append((sentence, {}))
            continue
        try:
            word_lists = json.loads(text)
        except json.JSONDecodeError:
            invalid["json unreadable (skipped)"] += 1
            continue
        if not isinstance(word_lists, dict):
            invalid[f"word list a {type(word_lists).__name__}, not a dict"] += 1
            continue
        out.append((sentence, word_lists))
    return out

