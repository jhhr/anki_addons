"""`match_data` and its five states: whether a word element is to be matched to a note.

The array's structure follows concrete rules and errs towards more words (see README), so the
study-value calls of the old extract_words prompt now live in `match_data`, decided by the word
matching judge: a compound meaning no more than its parts, a word plus the particle it happens
to take, the pieces of a yojijukugo.

1. `[]` - unjudged: new from the generator, or migrated without a note id. The judge's work.
2. `["dontmatch"]` - judged not worth a note. match_words_to_notes skips it.
3. `["match"]` - judged worth a note, not matched yet. match_words_to_notes' main prompt.
4. `[note_id]` - migrated with a note id, so judged already; lacks `match_quality`, which
   match_words_to_notes' secondary prompt fills in.
5. `[note_id, match_quality]` - fully matched.

Numbers other than the base numerals start out judged `dontmatch` (numbers.py). Everything else
is the judge's (`judge_v2`), which asks about the words in the states it is given, each shown in
its sentence by iter_highlighted(). By default the judge sees only unjudged words; the re-judging
modes let it take a link away.
"""

import json
import re
from enum import IntEnum
from typing import Any, Iterable, Iterator, Optional

from . import numbers

DONT_MATCH = "dontmatch"
MATCH = "match"


class MatchState(IntEnum):
    UNJUDGED = 1
    DONT_MATCH = 2
    MATCH = 3
    LINKED = 4
    RATED = 5


JUDGE_NEW = frozenset({MatchState.UNJUDGED})
REJUDGE_MATCHED = frozenset({MatchState.LINKED, MatchState.RATED})
REJUDGE_ALL = frozenset(
    {MatchState.DONT_MATCH, MatchState.MATCH, MatchState.LINKED, MatchState.RATED}
)

TAG_RE = re.compile(r"<[^>]+>")
FURIGANA_RE = re.compile(r" ?([^ >\[\]]+?)\[[^\]]*\]")


def decode_word_array(field_value: str) -> Optional[list]:
    """The word array in a word list field, or None when it holds no array: empty, an old
    extract_words word list, or not JSON."""
    try:
        decoded = json.loads(field_value)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, list) else None


def iter_words(arr: list, depth: int = 0) -> Iterator[tuple[int, list]]:
    """(depth, element) for every word element, sub-words after their parent."""
    for elem in arr:
        if len(elem) > 1:
            yield depth, elem
            yield from iter_words(elem[5], depth + 1)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def match_state(elem: list) -> MatchState:
    """The state of a word element's `match_data`; raises ValueError on anything else."""
    data = elem[4]
    if not data:
        return MatchState.UNJUDGED
    if data == [DONT_MATCH]:
        return MatchState.DONT_MATCH
    if data == [MATCH]:
        return MatchState.MATCH
    if _is_int(data[0]):
        if len(data) == 1:
            return MatchState.LINKED
        if len(data) == 2 and _is_int(data[1]):
            return MatchState.RATED
    raise ValueError(f"Unknown match_data: {data!r}")


def matched_note_id(elem: list) -> Optional[int]:
    first = elem[4][0] if elem[4] else None
    return first if _is_int(first) else None


def is_flagged(elem: list) -> bool:
    """Judged not to be matched."""
    return elem[4] == [DONT_MATCH]


def set_dont_match(elem: list) -> Optional[int]:
    """Judge a word element not worth a note; returns the id of the note it was matched to."""
    previous = matched_note_id(elem)
    elem[4] = [DONT_MATCH]
    return previous


def set_match(elem: list) -> None:
    """Judge a word element worth a note. A link it already has stays."""
    if matched_note_id(elem) is None:
        elem[4] = [MATCH]


def elements_in_states(arr: list, states: Iterable[MatchState]) -> list[list]:
    wanted = set(states)
    return [elem for _, elem in iter_words(arr) if match_state(elem) in wanted]


def elements_to_match(arr: list) -> list[list]:
    """The words for match_words_to_notes' main prompt: judged worth a note, not matched."""
    return elements_in_states(arr, [MatchState.MATCH])


def elements_to_rate(arr: list) -> list[list]:
    """The words for match_words_to_notes' secondary prompt: a note id but no match_quality."""
    return elements_in_states(arr, [MatchState.LINKED])


def default_match_data(part_of_speech: str, dict_form: str, sub_words: list) -> list:
    """match_data for a newly generated word: judged `dontmatch` if it is a number that isn't a
    word of its own (二十八, 千九百三十五), or is built on one (二十八日); otherwise unjudged."""
    if part_of_speech == "number":
        return [] if numbers.is_matched_numeral(dict_form) else [DONT_MATCH]
    if any(s[1] == "number" and is_flagged(s) for s in sub_words if len(s) > 1):
        return [DONT_MATCH]
    return []


# --- words marked in their sentence ---------------------------------------------------------


def plain_text(raw_text: str) -> str:
    return FURIGANA_RE.sub(r"\1", TAG_RE.sub("", raw_text)).replace(" ", "")


def iter_highlighted(
    arr: list, before: str = "", after: str = "", depth: int = 0
) -> Iterator[tuple[int, list, str]]:
    """(depth, element, sentence) for every word element in `iter_words` order, the sentence in
    plain text with that element's own text in `<b>`: the array's raw texts make up the sentence
    and a parent's sub-words make up its raw text, so the mark is on the very occurrence."""
    texts = [plain_text(elem[0]) for elem in arr]
    for index, elem in enumerate(arr):
        if len(elem) > 1:
            left = before + "".join(texts[:index])
            right = "".join(texts[index + 1 :]) + after
            yield depth, elem, f"{left}<b>{texts[index]}</b>{right}"
            yield from iter_highlighted(elem[5], left, right, depth + 1)
