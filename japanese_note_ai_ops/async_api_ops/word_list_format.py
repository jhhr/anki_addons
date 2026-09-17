"""Formatting the old extract_words word list, for the ops that still read that field shape.

"Extract words" writes word arrays now (`word_array`), but a field can still hold the old dict
of per-part-of-speech lists: notes the migration never reached, and the readers that fall back
to it (`match_words_to_notes`, `find_missing_matched_note_ids`, `migrate_compound_verbs`). They
write the dict back one element per row, which is how it has always been stored, so a note
edited by them does not come out of Anki's diff as one enormous changed line.
"""

import json
from typing import Union


def format_word_list_dict(word_lists: dict) -> str:
    """Format a word list dict with one list element per row, per-category line. Returns '{}' for empty."""
    if not word_lists:
        return "{}"
    return (
        "{\n"
        + ",\n".join(
            f'  "{k}": ['
            + (f"\n    " if v else "")
            + ",\n    ".join(json.dumps(item, ensure_ascii=False) for item in v)
            + (f"\n  " if v else "")
            + "]"
            for k, v in word_lists.items()
        )
        + "\n}"
    )


def word_lists_str_format(
    word_lists: dict,
) -> Union[str, None]:
    """
    Convert the word list dict into the same format used in the prompt
    """
    if not word_lists:
        return None
    return format_word_list_dict(word_lists)
