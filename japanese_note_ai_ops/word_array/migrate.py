"""Phase 2: fitting an old extract_words word list into a generated word array.

The generated array is taken as correct, so the only thing the migration carries over is what
the old data holds and the generator cannot produce: the id of the note a word was already
matched to. `meaning_index` is dropped - a word's position in the array is what tells two
occurrences of it apart now - and so is the note's sort field value, which is derivable from
the note itself. An entry with no note id therefore carries nothing at all and is counted but
not reported.

An entry finds its element by dictionary form and reading, in steps, each of which needs exactly
one element to fit. The step that found an element also says how strong its claim on it is, so
that two entries wanting the same word don't both have to stand down:

1. the form and the reading as they stand;
2. the same reading and the same kanji, okurigana ignored, which carries the note's spelling
   over to JMdict's (向う -> 向こう);
3. the form alone, where the old reading is the colloquial one the generator now normalizes
   away (何[なん] -> 何[なに], 物[もん] -> 物[もの]) or is simply wrong, as a hand-checked
   corpus still shows it to be (ノイローゼ read はいろーぜ);
4. the reading alone, where the element's part of speech fits the list the entry came from.
   The generator kanjifies dictionary forms the way Sudachi normalizes them (する -> 為る,
   これ -> 此れ, くださる -> 下さる), which no comparison of written forms undoes;
5. the element's own raw text, for what the dictionary form hides: the copula written です,
   the noun form of a verb (突き under 突く), an inflection the old list kept (為せる).

What fits nothing, fits several elements, or is wanted by two entries at once is reported
instead of guessed at (the user's call): match_words_to_notes can match such a word again from
the sentence, with `<b>` marking which occurrence it is, and that is more reliable than a coin
toss here. The exception is several occurrences of one word, where the link goes on all of them
(`_spreads`): the old list naming it once gave it one note.
Only a lost note id is worth the caller's attention - see `lost_note_ids`.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..kana_conv import is_kana_str, to_hiragana
from . import match_flags

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

# Why an entry holding a note id did not get carried over.
NO_WORD = "no_word"  # nothing to match with: a bare note id, a word with no reading
NO_ELEMENT = "no_element"  # no word of the array fits
AMBIGUOUS = "ambiguous"  # several words fit, and nothing here says which
CONTESTED = "contested"  # two entries fit the same word, with different note ids
FLAGGED = "flagged"  # the word that fits is flagged "dont_match"


@dataclass(frozen=True)
class OldEntry:
    """One entry of a stored word list, read positionally."""

    category: str
    word: str
    reading: str
    note_id: Optional[int]

    def __str__(self) -> str:
        return f"{self.category} {self.word}[{self.reading}] -> {self.note_id}"


@dataclass(frozen=True)
class Leftover:
    entry: OldEntry
    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.entry}: {self.reason}{f' ({self.detail})' if self.detail else ''}"


@dataclass
class MigrationReport:
    entries: int = 0
    """Entries the old field held, malformed ones included."""
    without_note_id: int = 0
    """Of those, the ones that carried nothing to migrate."""
    unreadable: int = 0
    """Of those, the ones that held no word either (`normalize_word_tuple`'s shapes)."""
    by_step: dict[str, int] = field(default_factory=lambda: {s: 0 for s in STEPS})
    """Note ids carried over, by the step that found the element."""
    spread: int = 0
    """Of those, the ones put on every occurrence of a function word rather than one."""
    linked_by_step: dict[str, list[tuple[OldEntry, list]]] = field(default_factory=dict)
    """The entry and the element it was linked to, by step, for review."""
    leftovers: list[Leftover] = field(default_factory=list)
    """Note ids not carried over, and why."""

    @property
    def linked(self) -> int:
        return sum(self.by_step.values())

    @property
    def lost_note_ids(self) -> list[int]:
        return [lo.entry.note_id for lo in self.leftovers if lo.entry.note_id is not None]

    def summary(self) -> str:
        steps = ", ".join(f"{step} {n}" for step, n in self.by_step.items() if n)
        return (
            f"{self.entries} entries, {self.linked} note ids carried over"
            f"{f' ({steps})' if steps else ''}, {self.spread} of them over several occurrences,"
            f" {len(self.leftovers)} lost, {self.without_note_id} without a note id"
        )


def read_entry(category: str, entry: Any) -> Optional[OldEntry]:
    """One stored entry as word, reading and note id, or None when it holds neither.

    The shapes written by extract_words and match_words_to_notes are [word, reading],
    [word, reading, meaning_index], [word, reading, sort_value, note_id] and
    [word, reading, meaning_index, sort_value, note_id]. Real word lists also hold the
    malformed ones `normalize_word_tuple` documents: a bare string, a one-element list, an
    empty list and a bare note id, the last of which is a link worth keeping if only it said
    which word it belonged to.
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


def _find(entry: OldEntry, elements: list[list]) -> tuple[list[list], str]:
    """The elements an entry fits, and the step that found them. The steps are tried in order
    and the first that fits anything decides, so a form match is never widened into a
    reading-only one. A flagged element is not a place a link can go, so it only counts where
    nothing else fits - otherwise a flagged word elsewhere in the text would make an
    unambiguous link look ambiguous."""
    for step in STEPS:
        hits = [elem for elem in elements if _fits(entry, elem, step)]
        if hits:
            return [e for e in hits if not match_flags.is_flagged(e)] or hits, step
    return [], ""


def _detail(elements: list[list]) -> str:
    return ", ".join(f"{elem[2]}[{elem[3]}]" for elem in elements)


def _spreads(hits: list[list]) -> bool:
    """True when one link can go on every element it fits instead of none of them: when they
    are all the same word.

    The user's call: an old list naming a word once for a sentence with two occurrences of it
    (為る twice, 見て見ましょう) gave it one note and never said which occurrence it meant.
    First made for particles and the copula only, it was widened to content words once the
    whole collection showed hundreds of links lost that way; the rare two occurrences with two
    meanings are the matching step's to tell apart.
    """
    return len({(elem[2], elem[3]) for elem in hits}) == 1


def migrate(word_lists: dict, arr: list) -> MigrationReport:
    """Carry the note ids of an old word list into a generated array, writing `match_data` in
    place. The array is not otherwise touched: the words it holds are the migration's result.
    """
    report = MigrationReport()
    entries, report.entries = read_word_lists(word_lists)

    seen: set[tuple] = set()
    wanted: list[tuple[OldEntry, list[list], str]] = []
    elements = [elem for _, elem in match_flags.iter_words(arr)]
    for entry in entries:
        if entry.note_id is None:
            report.without_note_id += 1
            continue
        if (entry.word, entry.reading, entry.note_id) in seen:
            # The same link written twice: a verb listed under both "verbs" and "prefix_verbs",
            # which the extract_words prompt allows.
            continue
        seen.add((entry.word, entry.reading, entry.note_id))

        if not entry.word or not entry.reading:
            report.unreadable += 1
            report.leftovers.append(Leftover(entry, NO_WORD))
            continue
        hits, step = _find(entry, elements)
        if not hits:
            report.leftovers.append(Leftover(entry, NO_ELEMENT))
        elif len(hits) == 1 or _spreads(hits):
            wanted.append((entry, hits, step))
        else:
            report.leftovers.append(Leftover(entry, AMBIGUOUS, _detail(hits)))

    claims: dict[int, list[tuple[OldEntry, str]]] = {}
    for entry, elems, step in wanted:
        for elem in elems:
            claims.setdefault(id(elem), []).append((entry, step))
    done: set[int] = set()
    for entry, elems, step in wanted:
        # Two entries wanting one element are only in each other's way when they are different
        # links - the same note id under two spellings (きっと and 屹度) is one link written
        # twice - and then the step that found each decides: だ found as written beats です
        # found through the raw text. Only a tie on the step leaves nothing to choose by.
        rival = next(
            (
                e
                for elem in elems
                for e, s in claims[id(elem)]
                if e.note_id != entry.note_id and STEPS.index(s) <= STEPS.index(step)
            ),
            None,
        )
        if rival is not None:
            report.leftovers.append(Leftover(entry, CONTESTED, f"{_detail(elems)} {rival}"))
        elif all(match_flags.is_flagged(elem) for elem in elems):
            report.leftovers.append(Leftover(entry, FLAGGED, _detail(elems)))
        elif any(id(elem) not in done for elem in elems):
            for elem in elems:
                if id(elem) not in done:
                    done.add(id(elem))
                    elem[4] = [entry.note_id]
            report.by_step[step] += 1
            report.spread += len(elems) > 1
            report.linked_by_step.setdefault(step, []).append((entry, elems[0]))
    return report
