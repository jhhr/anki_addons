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
is the judge's: judge_prompt() numbers the elements in the states it is given and states the
rules for a model, and apply_judge_response() applies its answer. By default the judge sees only
unjudged words; the re-judging modes let it take a link away, which apply_judge_response reports.
"""

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


# --- the word matching judge ---------------------------------------------------------------

JUDGE_RETURN_FIELD = DONT_MATCH

CONTEXT_NOTES = {
    MatchState.UNJUDGED: "",
    MatchState.DONT_MATCH: " (no note)",
    MatchState.MATCH: " (gets a note)",
    MatchState.LINKED: " (has a note)",
    MatchState.RATED: " (has a note)",
}


def plain_text(raw_text: str) -> str:
    return FURIGANA_RE.sub(r"\1", TAG_RE.sub("", raw_text)).replace(" ", "")


def judge_prompt(
    sentence: str, arr: list, states: Iterable[MatchState] = JUDGE_NEW
) -> tuple[str, list[list]]:
    """The judge's prompt, and the word elements it numbers: those in `states`. The other words
    are listed unnumbered for context. With no element to judge the prompt is still built; the
    caller should skip the request when the element list is empty."""
    wanted = set(states)
    elements = []
    lines = []
    for depth, elem in iter_words(arr):
        state = match_state(elem)
        if state in wanted:
            label, note = f"{len(elements)}.", ""
            elements.append(elem)
        else:
            label, note = "-", CONTEXT_NOTES[state]
        lines.append(
            f"{'    ' * depth}{label} {plain_text(elem[0])}: {elem[2]} [{elem[3]}],"
            f" {elem[1]}{note}"
        )
    entries = "\n".join(lines)
    prompt = f"""Below is a Japanese sentence and the words it is made of. Each entry shows the text, its dictionary form, [reading] and part of speech. An indented entry is a part of the entry above it: a component of a compound word, or a word of a multi-word expression.

The list is made by fixed rules, so it contains every unit that could be a word, including many that are not worth studying. Your task is to decide which of the numbered entries should NOT get a vocabulary note, a flashcard for learning what the word means in this sentence. Entries marked with "-" are already decided and are shown only for context.

Pick an entry when:
- It is a compound or expression that means no more than its parts put together: 遂行能力 is simply 遂行 + 能力, 連れて行く simply 連れる + 行く. Its parts keep their notes. Don't pick one whose meaning is more than its parts, like 登り切る, 見た目, 鳥肌が立つ or 間も無く.
- It is a word plus the particle or copula it happens to take here: 此れは, 上の, 無しに. Don't pick a fixed expression, or a form far more common than the bare word: 正に, 共に, 先ずは, 同時に, ように.
- It is a component of a four-kanji idiom (yojijukugo) or of a proper noun. The idiom or the name itself keeps its note.
- It is a component that is not a word in this sentence, like 合 in 場合.

Don't pick particles, the copula, auxiliary words, prefixes or suffixes (の, だ, 御, さん, 達): they get notes too. Don't pick ordinary single words for being common or easy. Every numbered entry you don't pick gets a note.

Sentence: {plain_text(sentence)}

Entries:
{entries}

Return a JSON object with the key "{JUDGE_RETURN_FIELD}": an array of the numbers of the entries you pick, [] if none."""
    return prompt, elements


def apply_judge_response(elements: list[list], response: Any) -> list[int]:
    """Judge every element: the picked ones `dontmatch`, the rest `match` (keeping a link they
    have). Returns the note ids the picks unlinked. Numbers that don't name an element are
    ignored rather than trusted; a response without the array changes nothing."""
    picked = response.get(JUDGE_RETURN_FIELD) if isinstance(response, dict) else None
    if not isinstance(picked, list):
        raise ValueError(f"Expected a {JUDGE_RETURN_FIELD!r} array in the response: {response!r}")
    chosen = {i for i in picked if _is_int(i) and 0 <= i < len(elements)}
    unlinked = []
    for index, elem in enumerate(elements):
        if index in chosen:
            note_id = set_dont_match(elem)
            if note_id is not None:
                unlinked.append(note_id)
        else:
            set_match(elem)
    return unlinked
