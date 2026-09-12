"""The "dont_match" flag: word elements that are not to be matched to notes.

`match_data[0]` holds the matched note's id once match_words_to_notes has run; the flag is the
string "dont_match" in the same slot, so one check skips both. The array's structure follows
concrete rules and errs towards more words (see README), so the flag is where the study-value
calls of the old extract_words prompt now live: a compound meaning no more than its parts, a
word plus the particle it happens to take, the pieces of a yojijukugo.

Numbers other than the base numerals start out flagged (numbers.py). Everything else is the
flagging job's: flag_prompt() numbers the word elements and states the rules for a model, and
apply_flag_response() applies its answer. The job runs on old, migrated arrays as well as new
ones, so it may flag a word that was matched, which drops the link to that note.
"""

import re
from typing import Any, Iterator, Optional

from . import numbers

DONT_MATCH = "dont_match"

TAG_RE = re.compile(r"<[^>]+>")
FURIGANA_RE = re.compile(r" ?([^ >\[\]]+?)\[[^\]]*\]")


def iter_words(arr: list, depth: int = 0) -> Iterator[tuple[int, list]]:
    """(depth, element) for every word element, sub-words after their parent."""
    for elem in arr:
        if len(elem) > 1:
            yield depth, elem
            yield from iter_words(elem[5], depth + 1)


def matched_note_id(elem: list) -> Optional[int]:
    first = elem[4][0] if elem[4] else None
    return first if isinstance(first, int) and not isinstance(first, bool) else None


def is_flagged(elem: list) -> bool:
    return bool(elem[4]) and elem[4][0] == DONT_MATCH


def set_dont_match(elem: list) -> Optional[int]:
    """Flag a word element; returns the id of the note it was matched to, if it was."""
    previous = matched_note_id(elem)
    elem[4] = [DONT_MATCH]
    return previous


def elements_to_match(arr: list) -> list[list]:
    """The word elements match_words_to_notes still has to match: neither matched nor flagged."""
    return [elem for _, elem in iter_words(arr) if not elem[4]]


def default_match_data(part_of_speech: str, dict_form: str, sub_words: list) -> list:
    """match_data for a newly generated word: flagged if it is a number that isn't a word of
    its own (二十八, 千九百三十五), or is built on one (二十八日); otherwise empty."""
    if part_of_speech == "number":
        return [] if numbers.is_matched_numeral(dict_form) else [DONT_MATCH]
    if any(s[1] == "number" and is_flagged(s) for s in sub_words if len(s) > 1):
        return [DONT_MATCH]
    return []


# --- the flagging job ----------------------------------------------------------------------

FLAG_RETURN_FIELD = "dont_match"


def _plain(raw_text: str) -> str:
    return FURIGANA_RE.sub(r"\1", TAG_RE.sub("", raw_text)).replace(" ", "")


def flag_prompt(sentence: str, arr: list) -> tuple[str, list[list]]:
    """The prompt for the flagging job, and the word elements in the order it numbers them."""
    elements = []
    lines = []
    for depth, elem in iter_words(arr):
        note = " (already not matched)" if is_flagged(elem) else ""
        lines.append(
            f"{'    ' * depth}{len(elements)}. {_plain(elem[0])}: {elem[2]} [{elem[3]}],"
            f" {elem[1]}{note}"
        )
        elements.append(elem)
    entries = "\n".join(lines)
    prompt = f"""Below is a Japanese sentence and the words it is made of, as a numbered list. Each entry shows the text, its dictionary form, [reading] and part of speech. An indented entry is a part of the entry above it: a component of a compound word, or a word of a multi-word expression.

The list is made by fixed rules, so it contains every unit that could be a word, including many that are not worth studying. Your task is to pick the entries that should NOT get a vocabulary note, a flashcard for learning what the word means in this sentence.

Pick an entry when:
- It is a compound or expression that means no more than its parts put together: 遂行能力 is simply 遂行 + 能力, 連れて行く simply 連れる + 行く. Its parts keep their notes. Don't pick one whose meaning is more than its parts, like 登り切る, 見た目, 鳥肌が立つ or 間も無く.
- It is a word plus the particle or copula it happens to take here: 此れは, 上の, 無しに. Don't pick a fixed expression, or a form far more common than the bare word: 正に, 共に, 先ずは, 同時に, ように.
- It is a component of a four-kanji idiom (yojijukugo) or of a proper noun. The idiom or the name itself keeps its note.
- It is a component that is not a word in this sentence, like 合 in 場合.

Don't pick particles, the copula, auxiliary words, prefixes or suffixes (の, だ, 御, さん, 達): they get notes too. Don't pick ordinary single words for being common or easy. Entries marked "already not matched" need not be picked again.

Sentence: {_plain(sentence)}

Entries:
{entries}

Return a JSON object with the key "{FLAG_RETURN_FIELD}": an array of the numbers of the entries you pick, [] if none."""
    return prompt, elements


def apply_flag_response(elements: list[list], response: Any) -> list[int]:
    """Flag the elements the model picked; returns the note ids the flag unlinked. Numbers that
    don't name an element are ignored rather than trusted."""
    picked = response.get(FLAG_RETURN_FIELD) if isinstance(response, dict) else None
    if not isinstance(picked, list):
        raise ValueError(f"Expected a {FLAG_RETURN_FIELD!r} array in the response: {response!r}")
    unlinked = []
    for index in picked:
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(elements):
            note_id = set_dont_match(elements[index])
            if note_id is not None:
                unlinked.append(note_id)
    return unlinked
