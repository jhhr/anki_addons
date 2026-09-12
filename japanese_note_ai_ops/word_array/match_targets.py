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
from typing import Iterable, Optional

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


@dataclass
class MatchTarget:
    elem: list
    word: str
    reading: str
    part_of_speech: str


def note_part_of_speech(label: str) -> str:
    return NOTE_PART_OF_SPEECH.get(label, "")


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
