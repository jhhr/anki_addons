"""The words of a word array match_words_to_notes matches, and what it matches them as.

An old word list was grouped by part of speech and a word addressed by its category and index.
A word of an array is addressed by the element itself: gather_targets() walks the array the way
`match_flags.iter_words` does, top level first with sub-words after their parent, and hands back
each word to match together with its element, so a result can be written into that element's
`match_data` however deeply it is nested.

Only words in state 3, `["match"]`, are matched by the main prompt; which words those are is the
word matching judge's call (match_flags.py).
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from . import match_flags
from .match_flags import MatchState

JAPANESE_RE = re.compile(r"[ぁ-んァ-ン一-龯]")

# The array's part of speech labels (generator.POS_MAP and pos_label) as the word notes' part of
# speech field has them, the values the old word list categories gave
NOTE_PART_OF_SPEECH: dict[str, str] = {
    "noun": "Noun",
    "proper noun": "Proper Noun",
    "number": "Number",
    "counter": "Counter",
    "verb": "Verb",
    "adjective": "Adjective",
    "na-adjective": "Adjective",
    "adverb": "Adverb",
    "adjectival": "Adjectival",
    "particle": "Particle",
    "copula": "Particle",
    "auxiliary": "Particle",
    "conjunction": "Conjunction",
    "pronoun": "Pronoun",
    "suffix": "Suffix",
    "prefix": "Prefix",
    "expression": "Expression",
    "interjection": "Expression",
}


# The states a single-word run's mode (match_words_to_notes.WithProcessed) matches: an unprocessed
# word is judged worth a note, a processed one has a note id, with or without match_quality
REMATCH_STATES: dict[str, frozenset[MatchState]] = {
    "only_unprocessed": frozenset({MatchState.MATCH}),
    "only_processed": frozenset({MatchState.LINKED, MatchState.RATED}),
    "both": frozenset({MatchState.MATCH, MatchState.LINKED, MatchState.RATED}),
}


@dataclass
class MatchTarget:
    elem: list
    word: str
    reading: str
    part_of_speech: str


def note_part_of_speech(label: str) -> str:
    return NOTE_PART_OF_SPEECH.get(label, "")


def states_to_match(
    replace_existing: bool = False, reprocess: Optional[str] = None
) -> frozenset[MatchState]:
    """The `match_data` states a run matches with the main prompt. A single-word run takes the
    states of its `reprocess` mode; any other run `["match"]`, and the words already linked to a
    note too when replacing existing matches. Unjudged and dontmatch words are never matched:
    they are the judge's to decide."""
    if reprocess:
        return REMATCH_STATES[reprocess]
    return REMATCH_STATES["both" if replace_existing else "only_unprocessed"]


def gather_targets(
    arr: list,
    states: Iterable[MatchState] = (MatchState.MATCH,),
    limit: Optional[Iterable[tuple[str, str]]] = None,
) -> list[MatchTarget]:
    """The words of `arr` in `states` to match, in array order, each occurrence its own target.
    `limit` keeps only the given (dict_form, reading) pairs. A word without a form or reading,
    or with no Japanese in its form, is not matchable and left out. Raises ValueError on
    match_data in no known state."""
    wanted = set(limit) if limit is not None else None
    targets = []
    for elem in match_flags.elements_in_states(arr, states):
        word, reading = elem[2], elem[3]
        if not word or not reading or not JAPANESE_RE.search(word):
            continue
        if wanted is not None and (word, reading) not in wanted:
            continue
        targets.append(MatchTarget(elem, word, reading, note_part_of_speech(elem[1])))
    return targets


MATCH_QUALITIES = range(1, 6)


def parse_match_quality(value: Any) -> Optional[int]:
    """The match_quality a response gave, 1-5, or None when it gave none that is one. A whole
    number written as a float or a string is taken too."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            return None
        value = int(value)
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int) and value in MATCH_QUALITIES:
        return value
    return None


def save_results(
    targets: list[MatchTarget],
    results: dict[int, Any],
    qualities: Optional[dict[int, int]] = None,
) -> int:
    """Write each matched target's note id into its element's `match_data`, returning how many
    were written. `results` is keyed by target index, each value the word tuple the matching
    left, its note id last, and `qualities` by target index too. The id is a real note's or a
    new note's negative placeholder, which update_fake_note_ids swaps in the field text once the
    note exists. A target without a result stays `["match"]` to be matched on the next run.

    A result with a match_quality is saved `[note_id, match_quality]`, one without `[note_id]`,
    for the secondary prompt to rate."""
    qualities = qualities or {}
    saved = 0
    for index, target in enumerate(targets):
        result = results.get(index)
        if not result:
            continue
        try:
            note_id = int(result[-1])
        except (TypeError, ValueError):
            continue
        quality = qualities.get(index)
        target.elem[4] = [note_id] if quality is None else [note_id, quality]
        saved += 1
    return saved


def highlighted_sentence(arr: list, elem: list) -> Optional[str]:
    """The array's sentence in plain text with this very element in `<b>`, None when the element
    isn't one of the array's words."""
    for _, candidate, sentence in match_flags.iter_highlighted(arr):
        if candidate is elem:
            return sentence
    return None


def example_sentence(
    sentence_field: str, word_list_field: str, word: str, reading: str, note_id: int
) -> str:
    """A word note's example sentence for the matching prompt, its word in `<b>`. When the note's
    word list field holds a word array the sentence is built from it, marking the occurrence
    linked to the note itself, else the first one of the word and reading, else the first of the
    word. Otherwise, or when the word isn't in the array, the sentence field as it is."""
    arr = match_flags.decode_word_array(word_list_field)
    if not arr:
        return sentence_field
    try:
        words = list(match_flags.iter_highlighted(arr))
        fits = (
            lambda e: match_flags.matched_note_id(e) == note_id,
            lambda e: e[2] == word and e[3] == reading,
            lambda e: e[2] == word,
        )
        for fit in fits:
            for _, elem, sentence in words:
                if fit(elem):
                    return sentence
    except (IndexError, TypeError):
        pass
    return sentence_field


def has_placeholder_ids(arr: list) -> bool:
    return any(
        (match_flags.matched_note_id(elem) or 0) < 0 for _, elem in match_flags.iter_words(arr)
    )


def resolve_placeholder_ids(arr: list, find_notes: Callable[[int], list[int]]) -> int:
    """Swap each new note's placeholder id left in `arr` by an earlier run for the id of the note
    `find_notes` says holds it, keeping any match_quality. A placeholder no note holds was never
    added, so its word goes back to `["match"]`; one several notes hold is left as it is. Each
    placeholder is looked up once. Returns how many words changed."""
    found: dict[int, list[int]] = {}
    changed = 0
    for _, elem in match_flags.iter_words(arr):
        fake_id = match_flags.matched_note_id(elem)
        if fake_id is None or fake_id >= 0:
            continue
        if fake_id not in found:
            found[fake_id] = find_notes(fake_id)
        note_ids = found[fake_id]
        if len(note_ids) > 1:
            continue
        elem[4] = [note_ids[0], *elem[4][1:]] if note_ids else [match_flags.MATCH]
        changed += 1
    return changed


def unlink_missing_notes(arr: list, note_exists: Callable[[int], bool]) -> list[int]:
    """Put every word linked to a note that no longer exists back to `["match"]`, to be matched
    again, returning the ids taken away. A negative id is a new note's placeholder, left for
    match_words_to_notes to resolve."""
    unlinked = []
    for _, elem in match_flags.iter_words(arr):
        note_id = match_flags.matched_note_id(elem)
        if note_id is None or note_id < 0 or note_exists(note_id):
            continue
        elem[4] = [match_flags.MATCH]
        unlinked.append(note_id)
    return unlinked
