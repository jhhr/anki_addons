"""What the note hooks in `__init__.py` do, decided without Anki.

The hooks (a note being added, a field of the editor losing focus, the Add dialog having added a
note) only read the note and the config, then run an op or not; the decisions are here so that
they can be tested, as `__init__.py` cannot be imported by a test.

The vocab type is hardcoded, as the hooks always had it: "Japanese vocab note" (and "Kanji draw"
in `__init__.py`'s story hook). In the two-type layout its sentence type is the one its config
block names (note_roles): sentences are added as notes of that type and vocab notes are made
only by the match op, so a sentence note added by a person gets its words extracted, and an
added vocab note nothing (D3).

Free of anki and aqt, like note_roles.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from .note_roles import LayoutError, is_sentence_type, sentence_type_of

VOCAB_NOTE_TYPE = "Japanese vocab note"
# The match op tags every note it makes; `__init__.run_op_on_add_note` checks it before anything
NEW_MATCHED_TAG = "new_matched_jp_word"

CLEAN_MEANING = "clean_meaning"
EXTRACT_WORDS = "extract_words"


def two_type_sentence_type(config: Mapping[str, Any]) -> Optional[str]:
    """The sentence type of the hardcoded vocab type in the two-type layout; None in the
    one-type layout, and when that type has no config block (its hooks then fail, logged, as
    they did before roles existed). Raises note_roles.LayoutError for a broken layout."""
    if not isinstance(config.get(VOCAB_NOTE_TYPE), Mapping):
        return None
    sentence_type = sentence_type_of(config, VOCAB_NOTE_TYPE)
    return None if sentence_type == VOCAB_NOTE_TYPE else sentence_type


def ops_on_added_note(
    config: Mapping[str, Any], note_type_name: str, tags: Iterable[str], *, op_adding: bool
) -> tuple[str, ...]:
    """The ops `note_will_be_added` runs on a note, in order (CLEAN_MEANING, EXTRACT_WORDS).

    None for a note the match op made (`new_matched_jp_word`) or any note an op adds in its
    cleanup (`op_adding`, call_logging.in_bulk_op): the migration adds thousands of sentence
    notes, some with an empty array, and a hook's extract would be a request per note in the
    middle of the run's writes. In the one-type layout every note an op adds carries the tag
    anyway. A note added by a person: a vocab note of the one-type layout gets its meaning
    cleaned and its words extracted, as always; in the two-type layout a sentence note gets its
    words extracted and a vocab note nothing.

    Raises note_roles.LayoutError when the note may be of a broken two-type layout, for the
    hook to log.
    """
    if op_adding or any(tag.casefold() == NEW_MATCHED_TAG.casefold() for tag in tags):
        return ()
    # Any other type is none of the layout's: no need to read it
    if note_type_name != VOCAB_NOTE_TYPE and not is_sentence_type(config, note_type_name):
        return ()
    sentence_type = two_type_sentence_type(config)
    if sentence_type is None:
        return (CLEAN_MEANING, EXTRACT_WORDS) if note_type_name == VOCAB_NOTE_TYPE else ()
    return (EXTRACT_WORDS,) if note_type_name == sentence_type else ()


def translates_on_unfocus(config: Mapping[str, Any], note_type_name: str) -> bool:
    """Whether leaving an empty translation field of this type's note translates its sentence:
    the hardcoded vocab type's in the one-type layout, as always; in the two-type layout its
    sentence type's instead. A two-type vocab note's translation is a copy of its example
    sentence's (D1) and its type names no sentence to translate: translating there only logged
    an error. A broken layout keeps the vocab type translating, as before roles existed, and
    its sentence type not, whose error the add hook and the ops name."""
    if note_type_name == VOCAB_NOTE_TYPE:
        try:
            return two_type_sentence_type(config) is None
        except LayoutError:
            return True
    return _is_two_type_sentence_type(config, note_type_name)


def suspends_added_cards(config: Mapping[str, Any], note_type_name: str) -> bool:
    """Whether a note the Add dialog has just added gets its cards suspended: a sentence note
    of the two-type layout, which is never studied (D6). False for a broken layout."""
    return _is_two_type_sentence_type(config, note_type_name)


def _is_two_type_sentence_type(config: Mapping[str, Any], note_type_name: str) -> bool:
    if not is_sentence_type(config, note_type_name):
        return False
    try:
        return two_type_sentence_type(config) == note_type_name
    except LayoutError:
        return False
