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


def save_results(targets: list[MatchTarget], results: dict[int, Any]) -> int:
    """Write each matched target's note id into its element's `match_data`, returning how many
    were written. `results` is keyed by target index, each value the word tuple the matching
    left, its note id last. The id is a real note's or a new note's negative placeholder, which
    update_fake_note_ids swaps in the field text once the note exists. A target without a
    result stays `["match"]` to be matched on the next run.

    The saved `[note_id]` has no match_quality until the main prompt gives one."""
    saved = 0
    for index, target in enumerate(targets):
        result = results.get(index)
        if not result:
            continue
        try:
            note_id = int(result[-1])
        except (TypeError, ValueError):
            continue
        target.elem[4] = [note_id]
        saved += 1
    return saved


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
