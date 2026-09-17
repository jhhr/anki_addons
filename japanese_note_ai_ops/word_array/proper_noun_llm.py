"""Proper nouns a language model finds in a sentence, written into its word array.

The generator's rules and the name lexicon (`names.py`) miss the names Sudachi doesn't know and the
corpus never anchors, while a cheap model reading the sentence knows most of them. The model is only
asked which proper nouns the sentence has; code does the rest. Each name is looked for in the
array's plain text, and where an occurrence starts and ends on top-level word boundaries its words
become one `proper noun` with no sub-words (ひま + りん -> ひまりん, 山田 relabelled from a noun). A
name starting or ending inside a word (日本 of 日本語) changes nothing and is reported, unless the
word is one the generator cut across the name and what is left of it is a word of its own (凛と ->
凛 + と, 少年京太郎 -> 少年 + 京太郎), which `generator.name_rest_word` decides.
"""

import re
from typing import Any, Callable, NamedTuple, Optional

from .match_flags import TAG_RE, iter_words, matched_note_id, plain_text

# (the word a name is cut out of, the raw text left of it) -> that rest's word element, or None
RestWord = Callable[[list, str], Optional[list]]
# A raw text's pieces no cut may go inside: a tag, a furigana group, a character
_RAW_TOKEN_RE = re.compile(r"<[^>]+>| ?[^ >\[\]]+?\[[^\]]*\]|.", re.S)

PROPER_NOUN = "proper noun"
NAMES_FIELD = "proper_nouns"
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {NAMES_FIELD: {"type": "array", "items": {"type": "string"}}},
    "required": [NAMES_FIELD],
    "additionalProperties": False,
}
# Models asked for a list write these for none, schema or not
NO_NAMES = {"", "n/a", "na", "none"}


class NameFix(NamedTuple):
    changed: list[str]  # names now one proper noun word that weren't before
    unaligned: list[str]  # names not found starting and ending on word boundaries
    unlinked: list[int]  # note ids of the words and sub-words a merge dropped


def sentence_of(arr: list) -> str:
    """The furigana sentence of an array, html tags dropped."""
    return TAG_RE.sub("", "".join(elem[0] for elem in arr))


def prompt(arr: list) -> str:
    return (
        "Does the Japanese sentence below contain any proper nouns? Respond with a JSON object"
        f' whose "{NAMES_FIELD}" is the list of the proper nouns, each written as it is in the'
        " sentence without its furigana, or an empty list if there are none.\n\n" + sentence_of(arr)
    )


def names_from_response(response: Any) -> list[str]:
    """The distinct names of a response, furigana dropped. Raises ValueError without a list."""
    values = response.get(NAMES_FIELD) if isinstance(response, dict) else None
    if not isinstance(values, list):
        raise ValueError(f"Expected a {NAMES_FIELD!r} list: {response!r}")
    names: list[str] = []
    for value in values:
        name = plain_text(value).strip() if isinstance(value, str) else ""
        if name.lower() not in NO_NAMES and name not in names:
            names.append(name)
    return names


def _spans(arr: list) -> list[tuple[int, int]]:
    out, pos = [], 0
    for elem in arr:
        end = pos + len(plain_text(elem[0]))
        out.append((pos, end))
        pos = end
    return out


def _merge(pieces: list[list], name: str) -> tuple[list, list[int]]:
    """One proper noun of the elements of a name, and the note ids it drops. A single word keeps
    its match_data; several keep the first note id among them."""
    words = [e for e in pieces if len(e) > 1]
    ids = [matched_note_id(e) for e in words]
    sub_ids = [matched_note_id(s) for e in words for _, s in iter_words(e[5])]
    if len(words) == 1:
        match_data, kept = words[0][4], matched_note_id(words[0])
    else:
        kept = next((i for i in ids if i is not None), None)
        match_data = [kept] if kept is not None else []
    dropped = [i for i in ids + sub_ids if i is not None]
    if kept is not None:
        dropped.remove(kept)
    reading = "".join(e[3] if len(e) > 1 else plain_text(e[0]) for e in pieces)
    raw = "".join(e[0] for e in pieces)
    return [raw, PROPER_NOUN, name, reading, match_data, []], dropped


def _split_raw(raw: str, n: int) -> Optional[tuple[str, str]]:
    """`raw` cut where its plain text has `n` characters, or None when that is inside a furigana
    group (少年京太郎[しょうねんきょうたろう] can't be cut after 少年)."""
    pos = count = 0
    while count < n:
        m = _RAW_TOKEN_RE.match(raw, pos)
        if not m:
            return None
        count += len(plain_text(m.group()))
        pos = m.end()
    return (raw[:pos], raw[pos:]) if count == n else None


def _split_word(word: list, n: int) -> Optional[tuple[str, str]]:
    """The word's raw text cut after `n` plain characters, at a sub-word boundary if one is there:
    the sub-words' own furigana then stands for the word's (少年京太郎[しょうねんきょうたろう] ->
    少年[しょうねん] + 京太郎[きょうたろう]), same plain text."""
    subs = [s for s in word[5] if len(s) > 1]
    if subs and plain_text("".join(s[0] for s in word[5])) == plain_text(word[0]):
        pos = 0
        for k, sub in enumerate(word[5]):
            if pos == n:
                return "".join(s[0] for s in word[5][:k]), "".join(s[0] for s in word[5][k:])
            pos += len(plain_text(sub[0]))
    return _split_raw(word[0], n)


def _split_cut(arr: list, at: int, end: int, rest_word: RestWord, unlinked: list[int]) -> bool:
    """Split the one top-level word a name at plain[at:end] starts or ends inside, when the name's
    other end is on a word boundary and `rest_word` gives the word left beside it; in place."""
    spans = _spans(arr)
    words = [k for k, elem in enumerate(arr) if len(elem) > 1]
    starts = any(spans[k][0] == at for k in words)
    ends = any(spans[k][1] == end for k in words)
    if starts == ends:
        return False
    k = next(
        (
            (k for k in words if spans[k][0] < end < spans[k][1] and spans[k][0] >= at)
            if starts
            else (k for k in words if spans[k][0] < at < spans[k][1] and spans[k][1] <= end)
        ),
        None,
    )
    if k is None:
        return False
    word, (s, _) = arr[k], spans[k]
    cut = _split_word(word, end - s if starts else at - s)
    if cut is None:
        return False
    piece_raw, rest_raw = cut if starts else cut[::-1]
    rest = rest_word(word, rest_raw)
    if rest is None or rest[0] != rest_raw:
        return False
    reading = word[3]
    if starts and reading.endswith(rest[3]):
        piece_reading = reading[: len(reading) - len(rest[3])]
    elif not starts and reading.startswith(rest[3]):
        piece_reading = reading[len(rest[3]) :]
    else:
        return False
    piece_plain = plain_text(piece_raw)
    piece = [piece_raw, PROPER_NOUN, piece_plain, piece_reading, [], []]
    kept = []
    for new in (piece, rest):  # a sub-word of the same text and form keeps its match_data
        same = next((x for x in word[5] if len(x) > 1 and x[0] == new[0] and x[2] == new[2]), None)
        if same is not None:
            new[4] = same[4]
            kept.append(id(same))
    unlinked += [
        note_id
        for x in [word] + [x for _, x in iter_words(word[5])]
        if id(x) not in kept and (note_id := matched_note_id(x)) is not None
    ]
    arr[k : k + 1] = [piece, rest] if starts else [rest, piece]
    return True


def fix_array(arr: list, names: list[str], rest_word: Optional[RestWord] = None) -> NameFix:
    """Make every occurrence of each name that starts and ends on top-level word boundaries one
    proper noun word, in place; longer names first, so ナツキ・スバル goes before スバル.

    `rest_word(word, rest_raw)` (`generator.name_rest_word`) lets a name one end of which is inside
    a word split that word first: it gives the word element of the rest (と of 凛と), or None to
    leave the word whole."""
    changed: list[str] = []
    unaligned: list[str] = []
    unlinked: list[int] = []
    for name in sorted(names, key=len, reverse=True):
        aligned, start = False, 0
        plain = "".join(plain_text(elem[0]) for elem in arr)
        while (at := plain.find(name, start)) >= 0:
            end = at + len(name)
            spans = _spans(arr)
            words = [k for k, elem in enumerate(arr) if len(elem) > 1]
            first = next((k for k in words if spans[k][0] == at and spans[k][1] > at), None)
            last = next((k for k in reversed(words) if spans[k][1] == end), None)
            if first is None or last is None or first > last:
                if rest_word is not None and _split_cut(arr, at, end, rest_word, unlinked):
                    if name not in changed:
                        changed.append(name)
                    continue  # the same occurrence again, now on word boundaries
                start = end
                continue
            start = end
            aligned = True
            if first == last and arr[first][1] == PROPER_NOUN and not arr[first][5]:
                continue
            merged, dropped = _merge(arr[first : last + 1], name)
            arr[first : last + 1] = [merged]
            unlinked += dropped
            if name not in changed:
                changed.append(name)
        if not aligned:
            unaligned.append(name)
    return NameFix(changed, unaligned, unlinked)
