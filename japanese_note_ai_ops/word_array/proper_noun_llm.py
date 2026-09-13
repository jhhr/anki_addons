"""Proper nouns a language model finds in a sentence, written into its word array.

The generator's rules and the name lexicon (`names.py`) miss the names Sudachi doesn't know and the
corpus never anchors, while a cheap model reading the sentence knows most of them. The model is only
asked which proper nouns the sentence has; code does the rest. Each name is looked for in the
array's plain text, and where an occurrence starts and ends on top-level word boundaries its words
become one `proper noun` with no sub-words (ひま + りん -> ひまりん, 山田 relabelled from a noun). A
name starting or ending inside a word (日本 of 日本語) changes nothing and is reported.
"""

from typing import Any, NamedTuple

from .match_flags import TAG_RE, iter_words, matched_note_id, plain_text

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


def fix_array(arr: list, names: list[str]) -> NameFix:
    """Make every occurrence of each name that starts and ends on top-level word boundaries one
    proper noun word, in place; longer names first, so ナツキ・スバル goes before スバル."""
    changed: list[str] = []
    unaligned: list[str] = []
    unlinked: list[int] = []
    for name in sorted(names, key=len, reverse=True):
        aligned, start = False, 0
        plain = "".join(plain_text(elem[0]) for elem in arr)
        while (at := plain.find(name, start)) >= 0:
            start = end = at + len(name)
            spans = _spans(arr)
            words = [k for k, elem in enumerate(arr) if len(elem) > 1]
            first = next((k for k in words if spans[k][0] == at and spans[k][1] > at), None)
            last = next((k for k in reversed(words) if spans[k][1] == end), None)
            if first is None or last is None or first > last:
                continue
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
