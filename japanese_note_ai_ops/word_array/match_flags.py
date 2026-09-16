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
is the judge's (`judge`), which asks about the words in the states it is given, each shown in
its sentence by iter_highlighted(). By default the judge sees only unjudged words; the re-judging
modes let it take a link away.

The field text is this module's too, because the states are what a search of it looks for:
`decode_word_array` reads it, `format_word_array` writes it one word per row, and
`word_array_query_regex` finds a word in a given state without parsing it.
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


# --- the field text -------------------------------------------------------------------------


def decode_word_array(field_value: str) -> Optional[list]:
    """The word array in a word list field, or None when it holds no array: empty, an old
    extract_words word list, or not JSON."""
    decoded = _parse_word_array(field_value)
    if decoded is None and isinstance(field_value, str):
        # format_word_array writes the field over several lines, and Anki's editor turns
        # those newlines into `<br>` and the indentation into `&nbsp;` as soon as anyone
        # edits the note by hand. Only tried once the field has failed to parse as it
        # stands, so a raw text holding an entity of its own is never rewritten; an op that
        # goes on to write the array back leaves the field clean again.
        repaired = field_value.replace("<br>", "\n").replace("&nbsp;", " ")
        if repaired != field_value:
            decoded = _parse_word_array(repaired)
    return decoded


def _parse_word_array(field_value: str) -> Optional[list]:
    try:
        decoded = json.loads(field_value)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, list) else None


def format_word_array(arr: list) -> str:
    """The field text for a word array: one top-level word per line, sub-words indented under
    their parent. `json.loads` of it is the array that went in, and every op that writes the
    field writes it through here.

    One word per row is how the word list field has always been stored, and what makes the
    field readable in the note editor and a change to it legible in a diff: the single line
    `json.dumps` produces is 513 characters for the median sentence in the collection and
    4160 for the longest. The separators within a word stay `", "`, which is what
    `word_array_query_regex` and the collection searches built on it expect."""
    if not arr:
        return "[]"
    rows = ",\n".join(_format_word(word, 2) for word in arr)
    return f"[\n{rows}\n]"


def _format_word(word: list, indent: int) -> str:
    pad = " " * indent
    if len(word) < 6:
        # A tag or a piece of punctuation: ["<k>"], ["。"]
        return pad + json.dumps(word, ensure_ascii=False)
    # Everything up to and including match_data, minus the closing bracket.
    head = json.dumps(word[:5], ensure_ascii=False)[:-1]
    sub_words = word[5]
    if not sub_words:
        return f"{pad}{head}, []]"
    rows = ",\n".join(_format_word(sub_word, indent + 2) for sub_word in sub_words)
    return f"{pad}{head}, [\n{rows}\n{pad}]]"


# What follows a word's reading in the field text, by state. A word's own values are always
# separated by ", " (format_word_array only breaks the line between words).
_STATE_REGEX = {
    MatchState.UNJUDGED: r"\]",
    MatchState.DONT_MATCH: rf'"{DONT_MATCH}"\]',
    MatchState.MATCH: rf'"{MATCH}"\]',
    MatchState.LINKED: r"-?\d+\]",
    MatchState.RATED: r"-?\d+,\s*\d+\]",
}


def word_array_query_regex(word: str, reading: str, states: Iterable[MatchState]) -> str:
    """A regex finding, in a word array field's text, a word element of this dict_form and
    reading whose `match_data` is in one of `states`, sub-words included. For a collection
    search, as get_word_list_query_regex_for_word_and_reading is for the old word lists.

    Only the dict_form and reading strings of an element are followed by `match_data`, so the
    raw text and part of speech never pass for them."""
    ends = "|".join(_STATE_REGEX[state] for state in sorted(set(states)))
    return rf',\s*"{re.escape(word)}",\s*"{re.escape(reading)}",\s*\[\s*({ends})'


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
