"""The old extract_words word lists in the research corpora, and the words of a generated array
their entries name.

No note holds an old word list any more (the collection was migrated to word arrays on
2026-09-16, and the migration is gone), but the corpora in `output/` are made of them, and a
hand-checked one is the ground truth several evaluations score against: `judge_eval.py` labels
the words of a generated array by which of them an old entry names, and `proper_nouns.py`,
`proper_noun_eval.py` and `name_lexicon.py` compare against the old lists' proper nouns.

An entry finds its element by dictionary form and reading, in steps, the first that fits
anything deciding:

1. the form and the reading as they stand;
2. the same reading and the same kanji, okurigana ignored (向う -> 向こう);
3. the form alone, where the old reading is the colloquial one the generator normalizes away
   (何[なん] -> 何[なに], 物[もん] -> 物[もの]) or is simply wrong, as the hand-checked corpus
   still shows it to be (ノイローゼ read はいろーぜ);
4. the reading alone, where the element's part of speech fits the list the entry came from.
   The generator kanjifies dictionary forms the way Sudachi normalizes them (する -> 為る,
   これ -> 此れ, くださる -> 下さる), which no comparison of written forms undoes;
5. the element's own raw text, for what the dictionary form hides: the copula written です,
   the noun form of a verb (突き under 突く), an inflection the old list kept (為せる).

Loaded as a package module, `_bootstrap.load("research.old_word_lists")`, for its relative
imports; mypy.ini excludes it for the same reason.
"""

import re
from dataclasses import dataclass
from typing import Any, Optional

from ...kana_conv import is_kana_str, to_hiragana
from .. import match_flags

KANA_RE = re.compile(r"[ぁ-んァ-ヶーゝゞヽヾ]")

# The generator's part of speech labels an old word list's category can turn into. Only the
# reading-only step consults them, where the written forms give nothing to compare; "expression"
# fits every category (a multi-word unit lands there whatever its words were), and a category
# not listed here - "expressions", "yojijukugo", anything newer - fits any label.
CATEGORY_POS: dict[str, tuple[str, ...]] = {
    "nouns": ("noun", "proper noun", "na-adjective", "counter", "number"),
    "proper_nouns": ("proper noun", "noun"),
    "numbers": ("number", "noun", "counter"),
    "counters": ("counter", "suffix", "noun"),
    "verbs": ("verb",),
    "prefix_verbs": ("verb",),
    "suffix_verbs": ("verb",),
    "compound_verbs": ("verb",),
    "adjectives": ("adjective", "na-adjective"),
    "adverbs": ("adverb", "noun", "na-adjective"),
    "adjectivals": ("adjectival", "na-adjective", "adjective"),
    "particles": ("particle", "copula", "auxiliary", "suffix"),
    "conjunctions": ("conjunction", "particle", "adverb"),
    # この was listed as a pronoun; the generator has 此の as an adjectival
    "pronouns": ("pronoun", "noun", "adjectival"),
    "suffixes": ("suffix", "counter", "noun", "auxiliary"),
    "prefixes": ("prefix", "noun"),
}

STEPS = ("form", "okurigana", "written", "reading", "raw")


@dataclass(frozen=True)
class OldEntry:
    """One entry of a stored word list, read positionally."""

    category: str
    word: str
    reading: str
    note_id: Optional[int]

    def __str__(self) -> str:
        return f"{self.category} {self.word}[{self.reading}] -> {self.note_id}"


def read_entry(category: str, entry: Any) -> Optional[OldEntry]:
    """One stored entry as word, reading and note id, or None when it holds neither.

    The shapes written by extract_words and match_words_to_notes are [word, reading],
    [word, reading, meaning_index], [word, reading, sort_value, note_id] and
    [word, reading, meaning_index, sort_value, note_id]. Real word lists also hold malformed
    ones: a bare string, a one-element list, an empty list and a bare note id.
    """
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return OldEntry(category, "", "", entry if entry > 0 else None)
    if isinstance(entry, str):
        values: list = [entry]
    elif isinstance(entry, (list, tuple)):
        values = list(entry)
    else:
        return None

    note_id = values[-1] if len(values) >= 4 else None
    if not isinstance(note_id, int) or isinstance(note_id, bool) or note_id <= 0:
        # A negative id is a placeholder for a note that was to be created; a stale one links
        # to nothing that exists.
        note_id = None

    if not values or not isinstance(values[0], str) or not values[0].strip():
        return OldEntry(category, "", "", note_id) if note_id else None
    word = values[0].strip()
    reading = values[1].strip() if len(values) > 1 and isinstance(values[1], str) else ""
    if not reading:
        # Recoverable exactly when the word is all kana, since then it is its own reading.
        reading = word if is_kana_str(word) else ""
    return OldEntry(category, word, to_hiragana(reading), note_id)


def read_word_lists(word_lists: dict) -> tuple[list[OldEntry], int]:
    """Every entry of a stored word list dict, and how many entries there were in all."""
    entries, total = [], 0
    for category, values in word_lists.items():
        if not isinstance(values, list):
            continue
        for value in values:
            total += 1
            entry = read_entry(str(category), value)
            if entry is not None:
                entries.append(entry)
    return entries, total


def _skeleton(word: str) -> str:
    return KANA_RE.sub("", word)


def _fits(entry: OldEntry, elem: list, step: str) -> bool:
    form, reading = elem[2], to_hiragana(elem[3])
    if step == "form":
        return form == entry.word and reading == entry.reading
    if step == "written":
        return form == entry.word
    if step == "raw":
        plain = match_flags.plain_text(elem[0])
        return plain == entry.word or to_hiragana(plain) == entry.reading
    if reading != entry.reading:
        return False
    if step == "okurigana":
        skeleton = _skeleton(entry.word)
        return bool(skeleton) and _skeleton(form) == skeleton
    allowed = CATEGORY_POS.get(entry.category)
    return allowed is None or elem[1] in allowed or elem[1] == "expression"


def find_elements(entry: OldEntry, elements: list[list]) -> tuple[list[list], str]:
    """The elements an entry fits, and the step that found them. The steps are tried in order
    and the first that fits anything decides, so a form match is never widened into a
    reading-only one. A flagged element only counts where nothing else fits, so that a word
    judged `dontmatch` elsewhere in the sentence doesn't take the entry from the one it names."""
    for step in STEPS:
        hits = [elem for elem in elements if _fits(entry, elem, step)]
        if hits:
            return [e for e in hits if not match_flags.is_flagged(e)] or hits, step
    return [], ""
