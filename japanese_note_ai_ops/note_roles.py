"""Which configured note type plays which part when sentences and vocab are two note types.

Config has one block of field names per note type, `config[<type name>][<key>]`, and both
types of the two-type layout are configured with the same keys: a sentence type ("sentence
note", holding the sentence fields and the word array) and a vocab type ("Japanese vocab note",
holding a word and copies of a few fields of its example sentence). A key alone cannot say
which type to read it from, so these helpers say it: the role a type's block gives it, which
type a sentence type's words become notes of, which type's arrays link a vocab type's notes,
and what a vocab note copies from its example sentence note.

The one-type layout, one block with both roles naming no other type, is what every config held
before the split and stays valid; in it each helper answers as the code did before: the type
is its own vocab type and its own sentence type, the example copy maps every sentence field onto
itself, and there is no example id field unless the block names one.

Free of anki and aqt, unlike utils.py (which imports anki for `Note`): blocks are read straight
from the config dict and a note is anything with item access and `in`, so all of it is tested on
plain dicts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Optional, Protocol

# The fields of a sentence, as config keys. On a sentence block they name its fields; on a
# vocab block, the fields its notes keep a copy of from their example sentence (in the two-type
# layout usually only translation and audio). `sentence_seen_count_field` is a sentence-only
# key, filled by the migration and never copied, so it is deliberately not one of them.
SENTENCE_KEYS: tuple[str, ...] = (
    "sentence_field",
    "furigana_sentence_field",
    "kanjified_sentence_field",
    "word_extraction_sentence_field",
    "translated_sentence_field",
    "sentence_audio_field",
)


class LayoutError(Exception):
    """The config gives a note type no usable layout: it is not configured, it and the type
    it names do not name each other, one of them lacks its role, or a field it names is not on
    the note. A class of its own so that a pre-flight or a hook can tell a config problem, whose
    message is for the user, from a bug: `utils.get_field_config` raises a plain Exception."""


class NoteFields(Protocol):
    """What these helpers need of a note: `anki.notes.Note` has it, and so does a dict."""

    def __getitem__(self, key: str, /) -> str: ...

    def __setitem__(self, key: str, value: str, /) -> None: ...

    def __contains__(self, key: str, /) -> bool: ...


def _block(config: Mapping[str, Any], note_type_name: str) -> Optional[Mapping[str, Any]]:
    # The config's top level mixes the blocks with plain settings ("log_level", ...)
    block = config.get(note_type_name)
    return block if isinstance(block, Mapping) else None


def _name(block: Mapping[str, Any], key: str) -> str:
    """What a block gives a key, "" when it gives nothing usable: an empty value is how a
    block leaves a key unconfigured (`get_match_fields` treats it as missing too)."""
    value = block.get(key)
    return value if isinstance(value, str) else ""


def _other(block: Mapping[str, Any], key: str, note_type_name: str) -> str:
    # A block naming itself describes the one-type layout, the same as naming nothing
    named = _name(block, key)
    return "" if named == note_type_name else named


def is_sentence_type(config: Mapping[str, Any], note_type_name: str) -> bool:
    """Its notes hold sentences and their word arrays: its block names `word_list_field`."""
    block = _block(config, note_type_name)
    return block is not None and bool(_name(block, "word_list_field"))


def is_vocab_type(config: Mapping[str, Any], note_type_name: str) -> bool:
    """Its notes are the ones words are matched to: its block names `word_sort_field`."""
    block = _block(config, note_type_name)
    return block is not None and bool(_name(block, "word_sort_field"))


def _has(note_type_name: str, block: Mapping[str, Any], key: str) -> str:
    value = _name(block, key)
    if value:
        return f'"{note_type_name}" has {key} "{value}"'
    return f'"{note_type_name}" has no {key}'


def _names_both_error(note_type_name: str, block: Mapping[str, Any]) -> Optional[str]:
    names_vocab = _other(block, "vocab_note_type", note_type_name)
    names_sentence = _other(block, "sentence_note_type", note_type_name)
    if names_vocab and names_sentence:
        return (
            f'Note type "{note_type_name}" names both a vocab_note_type ("{names_vocab}") and a'
            f' sentence_note_type ("{names_sentence}"): a type is either the sentence type or'
            " the vocab type of a two-type layout."
        )
    return None


def _pair_error(config: Mapping[str, Any], sentence_type: str, vocab_type: str) -> Optional[str]:
    """Why these two cannot be the sentence and vocab type of one two-type layout, if so."""
    sentence_block = _block(config, sentence_type)
    vocab_block = _block(config, vocab_type)
    if sentence_block is None or vocab_block is None:
        unconfigured = sentence_type if sentence_block is None else vocab_type
        return (
            f'The note types "{sentence_type}" (sentences) and "{vocab_type}" (vocab) must name'
            f' each other in the settings, but "{unconfigured}" has not been configured.'
        )
    error = _names_both_error(sentence_type, sentence_block) or _names_both_error(
        vocab_type, vocab_block
    )
    if error:
        return error
    if (
        _name(sentence_block, "vocab_note_type") != vocab_type
        or _name(vocab_block, "sentence_note_type") != sentence_type
    ):
        return (
            f'The note types "{sentence_type}" (sentences) and "{vocab_type}" (vocab) must name'
            f" each other in the settings: {_has(sentence_type, sentence_block, 'vocab_note_type')}"
            f" and {_has(vocab_type, vocab_block, 'sentence_note_type')}."
        )
    # Each type keeps to its own role, since the roles decide what is done to a type's notes: a
    # sentence type with word_sort_field would be a vocab type too (meanings written into
    # sentence notes), a vocab type with word_list_field a sentence type too (words extracted
    # from the old sentence field the vocab notes keep until the user deletes it)
    if not _name(sentence_block, "word_list_field"):
        return (
            f'"{sentence_type}" is the sentence note type of "{vocab_type}" but has no'
            " word_list_field in the settings."
        )
    if _name(sentence_block, "word_sort_field"):
        return (
            f'"{sentence_type}" is the sentence note type of "{vocab_type}" but has'
            f' word_sort_field in the settings: only the vocab note type "{vocab_type}" may.'
        )
    if not _name(vocab_block, "word_sort_field"):
        return (
            f'"{vocab_type}" is the vocab note type of "{sentence_type}" but has no'
            " word_sort_field in the settings."
        )
    if _name(vocab_block, "word_list_field"):
        return (
            f'"{vocab_type}" is the vocab note type of "{sentence_type}" but has'
            f' word_list_field in the settings: only the sentence note type "{sentence_type}"'
            " may."
        )
    return None


def layout_error(config: Mapping[str, Any], note_type_name: str) -> Optional[str]:
    """Why the config gives this note type no usable layout, naming the types; None if it does.

    The pre-flight: an op or hook checks this before it starts and fails with the message,
    rather than meeting the inconsistency halfway through a run. `vocab_type_of` and the rest
    raise `LayoutError` with the same message, so a caller that skips the pre-flight still
    never works from half a layout.

    A pair is checked whichever of its types is asked about: the type's own `vocab_note_type`
    or `sentence_note_type`, and any other block naming this type under either key, must all be
    answered by the other block naming it back. A type nothing names and naming nothing is the
    one-type layout and always usable.
    """
    block = _block(config, note_type_name)
    if block is None:
        return f'Note type "{note_type_name}" has not been configured in the settings.'
    error = _names_both_error(note_type_name, block)
    if error:
        return error
    names_vocab = _other(block, "vocab_note_type", note_type_name)
    names_sentence = _other(block, "sentence_note_type", note_type_name)
    # (sentence type, vocab type), its own naming first so its message is the one shown
    pairs: list[tuple[str, str]] = []
    if names_vocab:
        pairs.append((note_type_name, names_vocab))
    if names_sentence:
        pairs.append((names_sentence, note_type_name))
    for other_name, other_block in config.items():
        if other_name == note_type_name or not isinstance(other_block, Mapping):
            continue
        if _name(other_block, "vocab_note_type") == note_type_name:
            pairs.append((other_name, note_type_name))
        if _name(other_block, "sentence_note_type") == note_type_name:
            pairs.append((note_type_name, other_name))
    for sentence_type, vocab_type in dict.fromkeys(pairs):
        error = _pair_error(config, sentence_type, vocab_type)
        if error:
            return error
    return None


def _checked_block(config: Mapping[str, Any], note_type_name: str) -> Mapping[str, Any]:
    error = layout_error(config, note_type_name)
    block = _block(config, note_type_name)
    if error or block is None:
        raise LayoutError(error)
    return block


def vocab_type_of(config: Mapping[str, Any], sentence_type_name: str) -> str:
    """The note type the words of this type's arrays are notes of: its `vocab_note_type`, else
    the type itself (the one-type layout). Raises LayoutError as `layout_error` describes."""
    block = _checked_block(config, sentence_type_name)
    return _other(block, "vocab_note_type", sentence_type_name) or sentence_type_name


def sentence_type_of(config: Mapping[str, Any], vocab_type_name: str) -> str:
    """The note type whose arrays link this type's notes: its `sentence_note_type`, else the
    type itself (the one-type layout). Raises LayoutError as `layout_error` describes."""
    block = _checked_block(config, vocab_type_name)
    return _other(block, "sentence_note_type", vocab_type_name) or vocab_type_name


def _example_blocks(
    config: Mapping[str, Any], vocab_type_name: str
) -> tuple[str, Mapping[str, Any], Mapping[str, Any]]:
    """(sentence type, its block, vocab block) of a vocab type. A type without the vocab role
    is refused: in the two-type layout the sentence type is its own sentence type, and copying
    its fields onto themselves would quietly do nothing where a caller meant to fill a vocab
    note."""
    vocab_block = _checked_block(config, vocab_type_name)
    if not _name(vocab_block, "word_sort_field"):
        raise LayoutError(
            f'Note type "{vocab_type_name}" is not a vocab note type: it has no word_sort_field'
            " in the settings."
        )
    sentence_type = _other(vocab_block, "sentence_note_type", vocab_type_name) or vocab_type_name
    return sentence_type, _checked_block(config, sentence_type), vocab_block


def _field_pairs(
    sentence_block: Mapping[str, Any], vocab_block: Mapping[str, Any]
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key in SENTENCE_KEYS:
        sentence_field = _name(sentence_block, key)
        vocab_field = _name(vocab_block, key)
        if sentence_field and vocab_field:
            pairs.append((sentence_field, vocab_field))
    return pairs


def example_field_pairs(config: Mapping[str, Any], vocab_type_name: str) -> list[tuple[str, str]]:
    """(sentence note field, vocab note field) for each of SENTENCE_KEYS that both the vocab
    type's block and its sentence type's block name, in SENTENCE_KEYS' order.

    In the two-type layout, the few fields a vocab note keeps a copy of (translation, audio);
    in the one-type layout, every configured sentence field onto itself, which is what the match
    op copies into a new note from the note its word was found in.
    """
    _, sentence_block, vocab_block = _example_blocks(config, vocab_type_name)
    return _field_pairs(sentence_block, vocab_block)


def example_id_field(config: Mapping[str, Any], vocab_type_name: str) -> Optional[str]:
    """The vocab note's field holding its example sentence note's id
    (`example_sentence_id_field`), None when the block names none: a one-type note is its own
    example and needs no link to itself."""
    _, _, vocab_block = _example_blocks(config, vocab_type_name)
    return _name(vocab_block, "example_sentence_id_field") or None


def copy_example(
    config: Mapping[str, Any],
    vocab_type_name: str,
    *,
    sentence_note: NoteFields,
    sentence_id: int,
    vocab_note: NoteFields,
) -> None:
    """Make `sentence_note` the example of `vocab_note`: copy every `example_field_pairs`
    field, and write `sentence_id` into the `example_id_field` when there is one.

    The id comes as an argument rather than off the note so that a test's note can be a dict;
    it must be a real note id, since a link to a sentence note not added yet (id 0) leads
    nowhere. The notes are keyword-only because both are the same shape: swapped, the copy
    would overwrite the sentence note without a word.

    Every field is checked before any is written, so a field the config names but the note
    lacks raises LayoutError and leaves the vocab note as it was. One note as both, in the
    one-type layout, copies each field onto itself.
    """
    # Read once: the migration copies into every vocab note of the collection
    sentence_type, sentence_block, vocab_block = _example_blocks(config, vocab_type_name)
    pairs = _field_pairs(sentence_block, vocab_block)
    id_field = _name(vocab_block, "example_sentence_id_field")
    missing_sentence = [field for field, _ in pairs if field not in sentence_note]
    if missing_sentence:
        raise LayoutError(
            f'The settings of note type "{sentence_type}" name fields its sentence note does'
            f" not have: {', '.join(missing_sentence)}"
        )
    vocab_fields = [field for _, field in pairs] + ([id_field] if id_field else [])
    missing_vocab = [field for field in vocab_fields if field not in vocab_note]
    if missing_vocab:
        raise LayoutError(
            f'The settings of note type "{vocab_type_name}" name fields its vocab note does'
            f" not have: {', '.join(missing_vocab)}"
        )
    if id_field and sentence_id <= 0:
        raise ValueError(
            f"Example sentence id {sentence_id} is not a note id: the sentence note has to be"
            " added before a vocab note can link it."
        )
    for sentence_field, vocab_field in pairs:
        vocab_note[vocab_field] = sentence_note[sentence_field]
    if id_field:
        vocab_note[id_field] = str(sentence_id)


# The characters Anki's search syntax gives a meaning inside a quoted `note:` search
_NOTE_SEARCH_SPECIAL = re.compile(r'[\\"*_]')


def note_type_search(note_type_name: str) -> str:
    """`"note:<name>"`: a search term for exactly this note type's notes, to join with others.

    Quoted because names have spaces; for a name without `\\`, `"`, `*` or `_` (such as
    "Japanese vocab note") it is the `f'"note:{name}"'` the ops have always built. Anki reads
    `_` and `*` in a note: search as wildcards (`note:a_b` also finds "axb"), and a lone
    backslash makes the search invalid, so each of those is escaped with a backslash, as Anki's
    own `build_search_string(SearchNode(note=...))` does (checked against Anki 26.09). So is
    `"`, though Anki removes double quotes from a note type's name when it saves one.
    """
    return '"note:' + _NOTE_SEARCH_SPECIAL.sub(r"\\\g<0>", note_type_name) + '"'
