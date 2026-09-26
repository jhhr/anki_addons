"""Reading and writing a word array field.

The array itself is japanese_note_ai_ops' (word_array/README.md): a partition of a furigana
sentence into `[raw_text, part_of_speech, dict_form, reading, match_data, sub_words]`. The
field text is here because two add-ons need it -- the one that generates arrays and the one
that highlights a sentence from them -- and one decoder that both use is the only way the two
stay able to read each other's fields.

`match_flags` re-exports these, so nothing on the generating side had to change.
"""

import json
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

WORD_VALUES = ("raw_text", "part_of_speech", "dict_form", "reading", "match_data", "sub_words")


def decode_word_array(field_value: str) -> Optional[list]:
    """The word array in a word list field, or None when it holds none: empty, or broken.
    A broken one is logged with where it breaks; see read_word_array."""
    arr, problem = read_word_array(field_value)
    if problem is not None:
        logger.warning(f"Not a valid word array, {problem}: {field_value}")
    return arr


def read_word_array(field_value: str) -> Tuple[Optional[list], Optional[str]]:
    """The word array in a word list field and None, or None and why the field holds none:
    None again for an empty field, else what is wrong and where.

    Broken covers text that is no JSON and JSON that is not an array's shape. The second is
    what a bracket lost in a hand edit leaves as often as the first - a word closed early, its
    sub-words spilled into its parent - and every reader indexes elements by position, so an
    array of the wrong shape has to be refused here rather than raise halfway through an op."""
    if isinstance(field_value, str) and not field_value.strip():
        return None, None
    decoded, problem = _parse_word_array(field_value)
    if decoded is None and isinstance(field_value, str):
        # format_word_array writes the field over several lines, and Anki's editor turns
        # those newlines into `<br>` and the indentation into `&nbsp;` as soon as anyone
        # edits the note by hand. Only tried once the field has failed to parse as it
        # stands, so a raw text holding an entity of its own is never rewritten; an op that
        # goes on to write the array back leaves the field clean again.
        repaired = field_value.replace("<br>", "\n").replace("&nbsp;", " ")
        if repaired != field_value:
            # Reported on the repaired text, whose line and column are the ones a person
            # fixing the field in the editor sees rows of
            decoded, problem = _parse_word_array(repaired)
    return decoded, problem


def _parse_word_array(field_value: str) -> Tuple[Optional[list], Optional[str]]:
    try:
        decoded = json.loads(field_value)
    except json.JSONDecodeError as e:
        return None, f"not JSON ({e})"
    except TypeError:
        return None, f"not text but {type(field_value).__name__}"
    if not isinstance(decoded, list):
        return None, f"JSON {type(decoded).__name__}, not a list"
    problem = word_array_problem(decoded)
    return (None, problem) if problem else (decoded, None)


def word_array_problem(arr: list, path: str = "") -> Optional[str]:
    """Where `arr` is not the shape of a word array, or None. Every element is a tag or piece
    of punctuation, `[text]`, or a word, `[raw_text, part_of_speech, dict_form, reading,
    match_data, sub_words]` with sub_words an array of its own. What match_data holds is not
    checked: an unknown state is match_flags.match_state's to refuse, one word at a time."""
    for index, elem in enumerate(arr):
        where = f"element [{path}{index}]"
        if not isinstance(elem, list):
            return f"{where} is {_excerpt(elem)}, not a list"
        if len(elem) == 1:
            if not isinstance(elem[0], str):
                return f"{where} is {_excerpt(elem)}: a tag or punctuation holds one text"
            continue
        if len(elem) != len(WORD_VALUES):
            return (
                f"{where} has {len(elem)} values, where a tag or punctuation has 1 and a word"
                f" {len(WORD_VALUES)} ({', '.join(WORD_VALUES)}): {_excerpt(elem)}"
            )
        for name, value in zip(WORD_VALUES[:4], elem):
            if not isinstance(value, str):
                return f"{where} {name} is {_excerpt(value)}, not text: {_excerpt(elem)}"
        for name, value in zip(WORD_VALUES[4:], elem[4:]):
            if not isinstance(value, list):
                return f"{where} {name} is {_excerpt(value)}, not a list: {_excerpt(elem)}"
        problem = word_array_problem(elem[5], f"{path}{index}][5][")
        if problem:
            return problem
    return None


def _excerpt(value: object, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else f"{text[:limit]}..."


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
