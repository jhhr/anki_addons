"""Reading and writing a word array field.

The array itself is japanese_note_ai_ops' (word_array/README.md): a partition of a furigana
sentence into `[raw_text, part_of_speech, dict_form, reading, match_data, sub_words]`. The
field text is here because two add-ons need it -- the one that generates arrays and the one
that highlights a sentence from them -- and one decoder that both use is the only way the two
stay able to read each other's fields.

`match_flags` re-exports these, so nothing on the generating side had to change.
"""

import json
from typing import Optional


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
