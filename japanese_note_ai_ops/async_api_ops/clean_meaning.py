import logging
import re
from typing import Any, NamedTuple, Optional, Sequence
from anki.errors import NotFoundError
from anki.notes import Note, NoteId
from anki.collection import Collection
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning
from collections.abc import Sequence

from .collection_access import (
    find_notes as col_find_notes,
    get_notes as col_get_notes,
)
from .note_cache import NoteCache
from .role_gate import notes_of_role
from .sentence_cache import SentenceCache
from .word_index import WordIndex
from .chain_types import ChainStep
from .base_ops import (
    get_response,
    bulk_notes_op,
    selected_notes_op,
    AsyncTaskProgressUpdater,
)
from ..sync_local_ops.mdx_dictionary import MDXLookupError, mdx_helper
from ..configuration import (
    NO_DICTIONARY_ENTRY_TAG,
    MEANING_MAPPED_TAG,
    GeneratedMeaningsDictType,
    GeneratedMeaningType,
    EnAndJPSentence,
    WordAndSentences,
)
from .make_all_meanings import (
    add_meanings_for_usage,
    load_meanings_dict_from_file,
    write_meanings_dict_to_file,
    make_meaning_dict_key,
)

from ..note_roles import VOCAB_ROLE, example_id_field, note_type_search, sentence_type_of
from ..utils import get_field_config
from ..html_stripping import strip_html
from ..word_array import match_targets

logger = logging.getLogger(__name__)


def _field_value(config: dict[str, str], note: Note, field_key: str) -> str:
    """The note's value of an optional configured field, empty when unconfigured or absent."""
    note_type = note.note_type()
    if note_type is None:
        return ""
    try:
        field = get_field_config(config, field_key, note_type)
    except Exception:
        return ""
    return note[field] if field in note else ""


def _example_sentence_id(
    config: dict[str, str], note: Note, vocab_type_name: str
) -> Optional[NoteId]:
    """The id of a two-type vocab note's example sentence note, None when it names none.

    Logged rather than raised: a vocab note without an example still has the sentences that
    link it, and its meaning is cleaned from those.
    """
    id_field = example_id_field(config, vocab_type_name)
    if not id_field or id_field not in note:
        logger.error(
            f'Note type "{vocab_type_name}" has no example sentence id field'
            f" ({id_field or 'example_sentence_id_field is not set'}): the sentences of note"
            f" {note.id} are without its own"
        )
        return None
    value = note[id_field].strip()
    try:
        example_id = int(value)
    except ValueError:
        example_id = 0
    if example_id <= 0:
        logger.warning(
            f"Note {note.id} names no example sentence note ({id_field}: {value!r}): its"
            " sentences are without its own"
        )
        return None
    return NoteId(example_id)


def get_sentences_for_note(
    config: dict[str, str],
    note: Note,
    exclude_self: bool = False,
    sentence_cache: Optional[SentenceCache] = None,
    note_cache: Optional[NoteCache] = None,
    own_sentence: Optional[EnAndJPSentence] = None,
) -> list[EnAndJPSentence]:
    """The sentences this note's word appears in, its own first unless excluded.

    :param sentence_cache: The run's sentence cache, if the caller has one. What is cached is
        the *other* notes' sentences: `exclude_self` decides only whether this note's own goes
        on the front, so caching the return value would let the two call shapes poison each
        other. Ops that run outside a bulk run pass nothing and scan every time.
    :param note_cache: The run's fetched notes, if the caller has one. The sibling notes are
        fetched in one turn either way; through the cache, a note the run already holds costs
        no turn at all.
    :param own_sentence: The note's own sentence, from a caller that holds it, in place of the
        one read off the note or its example sentence note. The match op has it for the note
        it is making (id 0), whose sentence is the one being matched. The others are found as
        without it.

    The own sentence is the note's own fields in the one-type layout. In the two-type layout
    (note_roles) it is its example sentence note's (`example_sentence_id_field`), and there is
    none when the note names no example or that note is gone. The others are the notes whose
    word list field links this note, each once and never the own sentence's note; in the
    two-type layout only notes of its sentence type, read by that type's field names, since
    the vocab notes keep their old sentence fields, under the same names, until the user
    deletes them.

    A sentence from a note whose word list field holds a word array has this note's word in
    `<b>`: the occurrence linked to this note, else the first of its word
    (`match_targets.example_sentence`), so a sentence using the word twice says which one the
    meaning is for. A note whose field holds no word array gives its sentence as it is.
    """
    note_type = note.note_type()
    if not note_type:
        logger.error(f"note_type() call failed for note {note.id}")
        return []
    sentence_type = sentence_type_of(config, note_type["name"])
    two_type = sentence_type != note_type["name"]
    sentence_note_type: dict[str, Any] = {"name": sentence_type} if two_type else note_type
    word_list_field = get_field_config(config, "word_list_field", sentence_note_type)
    sentence_field = get_field_config(config, "sentence_field", sentence_note_type)
    translated_sentence_field = get_field_config(
        config, "translated_sentence_field", sentence_note_type
    )
    word = _field_value(config, note, "word_kanjified_field") or _field_value(
        config, note, "word_field"
    )
    reading = _field_value(config, note, "word_reading_field")

    def make_en_and_jp_sentence(onote: Note) -> EnAndJPSentence:
        return EnAndJPSentence(
            jp_sentence=match_targets.example_sentence(
                strip_html(onote[sentence_field]),
                onote[word_list_field] if word_list_field in onote else "",
                word,
                reading,
                note.id,
            ),
            en_sentence=strip_html(onote[translated_sentence_field]),
        )

    def fetch(note_ids: Sequence[NoteId]) -> dict[NoteId, Note]:
        # One turn with the collection for all of them rather than one turn each: a note's
        # word list names a handful of siblings, and taking and releasing the collection per
        # note let every other waiting caller in between.
        if note_cache is not None:
            return note_cache.get_notes_blocking(note_ids)
        return {onote.id: onote for onote in col_get_notes(note_ids)}

    def read_example_sentence(example_id: NoteId) -> list[EnAndJPSentence]:
        try:
            example = fetch([example_id]).get(example_id)
        except NotFoundError:
            # What Anki's get_note raises for an id with no note, through either fetch
            example = None
        if example is None or sentence_field not in example:
            logger.warning(
                f"Note {note.id}'s example sentence note {example_id} is gone or is not a"
                f' "{sentence_type}" note: its sentences are without its own'
            )
            return []
        return [make_en_and_jp_sentence(example)]

    # The note the own sentence is read off, which the others leave out. A note not added yet
    # has no others, so its example is not looked up when its sentence is not wanted either.
    own_note_id: Optional[NoteId] = note.id
    if two_type:
        wanted = note.id != 0 or (own_sentence is None and not exclude_self)
        own_note_id = _example_sentence_id(config, note, note_type["name"]) if wanted else None
    if own_sentence is not None:
        cur_note_sentences = [own_sentence]
    elif not two_type:
        cur_note_sentences = [make_en_and_jp_sentence(note)]
    elif own_note_id is None or exclude_self:
        cur_note_sentences = []
    else:
        cur_note_sentences = read_example_sentence(own_note_id)
    if note.id == 0:
        # New note, can't search for others using its ID
        if exclude_self:
            return []
        return cur_note_sentences

    if two_type:
        # Only the sentence type's notes: a vocab note keeps its old word list field, under
        # the same name, until the user deletes it, and nothing keeps the array there current
        query = note_type_search(sentence_type) + f' "{word_list_field}:*{note.id}*"'
        if own_note_id is not None:
            query += f" -nid:{own_note_id}"
    else:
        query = f'"{word_list_field}:*{note.id}*" -nid:{note.id}'

    def scan_for_other_sentences() -> list[EnAndJPSentence]:
        logger.debug(f"Getting sentences for note {note.id} with query: {query}")
        other_sentence_note_ids = col_find_notes(query)
        by_id = fetch(other_sentence_note_ids)
        # Back into the order the search gave them: these become prompt lines, and neither the
        # cache's hit-then-miss order nor a fetch's is the order the run used to see. Each note
        # once and never the own sentence's note, told apart by note id; the check this
        # replaces compared a note's sentence text with the dicts listed so far, and so never
        # left anything out.
        listed = {own_note_id}
        sentences: list[EnAndJPSentence] = []
        for onid in other_sentence_note_ids:
            onote = by_id.get(onid)
            if onote is None or onid in listed or sentence_field not in onote:
                continue
            listed.add(onid)
            sentences.append(make_en_and_jp_sentence(onote))
        return sentences

    if sentence_cache is not None:
        other_sentences = sentence_cache.get(note.id, scan_for_other_sentences)
    else:
        other_sentences = scan_for_other_sentences()
    if exclude_self:
        # Copied, because the cached list belongs to the cache and every caller gets the same
        # one; the callers below build on what they are handed.
        return list(other_sentences)
    return cur_note_sentences + list(other_sentences)


def get_other_meaning_notes(
    config: dict[str, str],
    note: Note,
    notes_to_add_dict: Optional[dict[str, list[Note]]] = None,
    notes_to_update_dict: Optional[dict[NoteId, Note]] = None,
    allow_reupdate_existing: bool = False,
    include_pending_notes: bool = True,
    word_note_index: Optional[WordIndex] = None,
) -> list[Note]:
    """Get notes in the same meaning group as ``note``, excluding ``note`` itself.

    Answered from the run's word index when it has one, and by searching the collection when
    it does not. The search is a whole-collection regex over the sort field - the fields it
    looks in are note fields and nothing indexes them - and it ran 898 times in one run to
    retrieve six notes in total. `word_index.meaning_group_note_ids` has the term-by-term
    translation; `word_index.py` has why one pass over the notes table can stand in for it.

    :param word_note_index: The run's index, if the caller has one. Ops that run outside a
        matching run pass nothing and get the search.
    """
    note_type = note.note_type()
    if not note_type:
        logger.error(f"note_type() call failed for note {note.id}")
        return []

    word_sort_field = get_field_config(config, "word_sort_field", note_type)
    word_normal_field = get_field_config(config, "word_normal_field", note_type)
    word_reading_field = get_field_config(config, "word_reading_field", note_type)
    word_field = get_field_config(config, "word_field", note_type)
    if (
        not word_sort_field
        or not word_normal_field
        or not word_reading_field
        or not word_field
        or word_reading_field not in note
        or word_normal_field not in note
        or word_field not in note
    ):
        logger.error(
            f"Could not build meaning-group query for note {note.id}, missing fields in note"
        )
        return []

    meaning_notes_query = (
        f"{word_sort_field}:re:m\\d+ -{word_sort_field}:re:x\\d+"
        f' -nid:{note.id} "{word_reading_field}:{note[word_reading_field]}"'
        f' ("{word_normal_field}:{note[word_normal_field]}" OR'
        f' "{word_field}:{note[word_field]}")'
    )
    other_meaning_note_ids: Optional[Sequence[NoteId]] = None
    answered_by = "index"
    if word_note_index is not None and word_note_index.covers(
        kanjified=word_field,
        normal=word_normal_field,
        reading=word_reading_field,
        sort=word_sort_field,
    ):
        other_meaning_note_ids = word_note_index.meaning_group_note_ids(
            reading=note[word_reading_field],
            normal_value=note[word_normal_field],
            kanjified_value=note[word_field],
            exclude_note_id=note.id if note.id > 0 else None,
        )
    if other_meaning_note_ids is None:
        answered_by = "search"
        other_meaning_note_ids = col_find_notes(meaning_notes_query)

    notes_to_update_dict = notes_to_update_dict or {}
    # Fetched in one turn with the collection rather than one turn each: a group is a handful
    # of notes, and taking and releasing the collection per note let every other waiting caller
    # in between. Only the ones not already in hand are asked for.
    ids_to_fetch = [
        onid
        for onid in other_meaning_note_ids
        if allow_reupdate_existing or onid not in notes_to_update_dict
    ]
    # Deliberately not the run's note cache: `allow_reupdate_existing` means this caller wants
    # the collection's copy rather than whatever the run has edited, and a cache would hand it
    # the edited object instead.
    fetched = {fetched_note.id: fetched_note for fetched_note in col_get_notes(ids_to_fetch)}
    other_meaning_notes = [
        (
            fetched[onid]
            if allow_reupdate_existing or onid not in notes_to_update_dict
            else notes_to_update_dict[onid]
        )
        for onid in other_meaning_note_ids
    ]

    if include_pending_notes and notes_to_add_dict:
        m_pattern = re.compile(r"m\d+")
        x_pattern = re.compile(r"x\d+")
        # A snapshot, both levels of it. This runs in an `asyncio.to_thread` worker while other
        # word tasks are still going, and creating a note is
        # `notes_to_add_dict.setdefault(key, []).append(note)` - which can add a key to the dict
        # and an entry to a list this loop is part-way through. Iterating either while that
        # happens is a `RuntimeError: dictionary changed size during iteration`, and the notes
        # arriving mid-loop are not ones this caller is entitled to see anyway: it is asking
        # what the meaning group held when it asked. The same reason the cleanup phase copies
        # these two dicts before working through them.
        for pending_notes in list(notes_to_add_dict.values()):
            for pending_note in list(pending_notes):
                if id(pending_note) == id(note):
                    continue
                if word_sort_field not in pending_note:
                    continue
                pending_sort = pending_note[word_sort_field]
                if not m_pattern.search(pending_sort) or x_pattern.search(pending_sort):
                    continue
                if word_reading_field not in pending_note:
                    continue
                if pending_note[word_reading_field] != note[word_reading_field]:
                    continue
                if (
                    word_normal_field in pending_note
                    and pending_note[word_normal_field] == note[word_normal_field]
                ) or (word_field in pending_note and pending_note[word_field] == note[word_field]):
                    other_meaning_notes.append(pending_note)

    logger.debug(
        f"Other meaning notes count: {len(other_meaning_notes)}, by {answered_by}, query:"
        f" {meaning_notes_query}"
    )
    return other_meaning_notes


class CleanResult(NamedTuple):
    """What cleaning one note did: whether its meaning changed, and the other note of its word
    whose sense the note's sentences use, when the cleaning found one - a duplicate of that
    note. Found by the prompt, or by the note taking the generated meaning that note holds."""

    changed: bool
    same_sense_as: Optional[Note] = None


NOT_CHANGED = CleanResult(False)

# Tags the note with the id of the note whose sense it repeats, so that the two can be found and
# merged. A note not added yet has no id to name.
SAME_SENSE_TAG = "meaning_same_sense_as"

# How many of each other note's sentences the prompts show, its own first. They are there to tell
# the notes' senses apart, which a few do; all of them made one mapping of あの 30,000 characters,
# a sibling of the note being linked from 135 sentences.
OTHER_NOTE_SENTENCES = 3

# The fields of the rework and map prompts' answers: the prompts name them and the op reads them
REWORK_JP_FIELD = "jp_meaning"
REWORK_EN_FIELD = "en_meaning"
SAME_SENSE_FIELD = "same_sense_as"
MAP_POSSIBLE_INDEX_FIELD = "possible_meaning_index"
MAP_SCORE_FIELD = "mapping_score"


def _sentence_lines(sentences: Sequence[EnAndJPSentence]) -> str:
    return "".join(f"- JP: {sen['jp_sentence']} -- EN: {sen['en_sentence']}\n" for sen in sentences)


def _other_notes_listing(
    others: Sequence[WordAndSentences],
    possible_meanings: Optional[Sequence[GeneratedMeaningType]] = None,
) -> str:
    """The word's other notes, numbered as `same_sense_as` counts them. Given the possible
    meanings, each says which one it uses: the one whose English is its own, as mapping writes
    it."""
    listing = ""
    for i, other in enumerate(others):
        uses = ""
        if possible_meanings is not None:
            used = next(
                (
                    index
                    for index, possible in enumerate(possible_meanings)
                    if possible["en_meaning"] == other["en_meaning"]
                ),
                None,
            )
            uses = f" (uses possible meaning {used + 1})" if used is not None else " (not mapped)"
        listing += f"""---
Other note {i + 1}{uses}:
Japanese meaning: {other['jp_meaning'] or '(empty)'}
English meaning: {other['en_meaning'] or '(empty)'}
Sentences:
{_sentence_lines(other['sentences'])}"""
    return listing or "(none)\n"


def rework_note_prompt(
    word: str,
    reading: str,
    target: WordAndSentences,
    others: Sequence[WordAndSentences],
    dictionary_entry: Optional[str],
) -> str:
    """The prompt that writes one note's meaning beside the word's other notes, `others` in the
    order `same_sense_as` counts them. Without a dictionary entry it has no entry and no rules
    for one.

    The others are shown as fixed and the answer is one object for the target. Asked to rework
    every meaning, the model reworded a sibling in nearly every answer, all of it thrown away,
    and left the note it was asked for out of most of them."""
    entry_rules = (
        """- Use the dictionary entry as reference where it covers the word's use in the target's sentences, extracting and rephrasing the parts that fit. Where it does not - it is for another word with the same reading, or the use is a name - write the meaning from the sentences.
- Omit any example sentences included in the dictionary entry (often within 「」 brackets).
"""
        if dictionary_entry
        else ""
    )
    entry_section = (
        f"""
Dictionary entry:
{dictionary_entry}
---"""
        if dictionary_entry
        else ""
    )
    return f"""Below {'is the dictionary entry for a word or phrase, followed by' if dictionary_entry else 'are'} the meanings the notes of {'that' if dictionary_entry else 'a'} word or phrase use, each with the sentences it is used in. One note, the TARGET, needs its meaning written. The other notes' meanings are fixed: they will not change, and are shown so that the target's meaning is told apart from them.

Write the target's meaning so that it fits the target's sentences. Follow these rules:
{entry_rules}- DO NOT OVERFIT the definition to the sentences, especially when there is only one. Aim for a general definition that broadly fits the theme of the target's sentences.
- If the word has two usage patterns in the target's sentences - for example, one literal and one figurative - describe both shortly in the one meaning.
- Do not repeat or absorb a sense that one of the other notes covers.
- Shorten and simplify the meaning as much as possible, ideally into 1 sentence and at most 2.
- The English meaning should ideally be a short list of equivalent words or phrases, explained in a sentence only when necessary.
- If the target's sentences use the word in the sense one of the other notes' meanings describes, set "{SAME_SENSE_FIELD}" to that note's number. Otherwise set it to 0. Judge by what the other notes' meanings say, since a note's sentences may not fit its meaning.

Return a JSON object with the fields "{REWORK_JP_FIELD}", "{REWORK_EN_FIELD}" and "{SAME_SENSE_FIELD}".

Word or phrase (and its reading):
{word} ({reading})
---{entry_section}
TARGET:
Current Japanese meaning: {target['jp_meaning'] or '(empty)'}
Current English meaning: {target['en_meaning'] or '(empty)'}
Sentences:
{_sentence_lines(target['sentences'])}
OTHER NOTES (fixed):
{_other_notes_listing(others)}"""


def rework_note_meaning(
    config: dict[str, str],
    word: str,
    reading: str,
    target: WordAndSentences,
    others: Sequence[WordAndSentences],
    dictionary_entry: Optional[str],
    context: dict[str, Any],
) -> Optional[tuple[str, str, int]]:
    """The target's new Japanese and English meaning, and the 1-based number of the other note
    whose sense it uses (0 for none); None when the call failed or its answer is unusable."""
    inputs: dict[str, Any] = {
        "word": word,
        "reading": reading,
        "target": target,
        "others": list(others),
        "dictionary_entry": dictionary_entry,
    }
    prompt = rework_note_prompt(**inputs)
    logger.debug(f"Prompt for reworking a note's meaning: {prompt}")
    response_schema = {
        "type": "object",
        "properties": {
            REWORK_JP_FIELD: {"type": "string"},
            REWORK_EN_FIELD: {"type": "string"},
            SAME_SENSE_FIELD: {"type": "integer"},
        },
        "required": [REWORK_JP_FIELD, REWORK_EN_FIELD, SAME_SENSE_FIELD],
        "additionalProperties": False,
    }
    result = get_response(
        config.get("word_meaning_model", ""),
        prompt,
        response_schema=response_schema,
        kind="clean_meaning.rework_note",
        inputs=inputs,
        context=context,
    )
    if not isinstance(result, dict):
        logger.warning(f"Reworked meaning result was not a dictionary: {result}")
        return None
    jp_meaning, en_meaning = result.get(REWORK_JP_FIELD), result.get(REWORK_EN_FIELD)
    if not (
        isinstance(jp_meaning, str)
        and jp_meaning.strip()
        and isinstance(en_meaning, str)
        and en_meaning.strip()
    ):
        logger.warning(f"Reworked meaning result lacks a meaning: {result}")
        return None
    same_sense = result.get(SAME_SENSE_FIELD)
    if not isinstance(same_sense, int) or not 0 <= same_sense <= len(others):
        same_sense = 0
    return jp_meaning, en_meaning, same_sense


def map_note_prompt(
    word: str,
    reading: str,
    possible_meanings: Sequence[GeneratedMeaningType],
    target: WordAndSentences,
    others: Sequence[WordAndSentences],
) -> str:
    """The prompt that maps one note to the word's generated (possible) meanings, both lists in
    the order it numbers them. The other notes are shown with the possible meaning each uses,
    as reference only.

    Mapped together, the notes of a word competed for the possible meanings: each one another
    note used was barred, so a note whose sense a sibling held was pushed onto a wrong meaning,
    and the low score it got revised the list the siblings were mapped to."""
    possible = "".join(
        f"---\nPossible meaning {i + 1}:\nJapanese meaning: {meaning['jp_meaning']}\n"
        f"English meaning: {meaning['en_meaning']}\n"
        for i, meaning in enumerate(possible_meanings)
    )
    return f"""Below is a list of all possible meanings of a word or phrase (made from Japanese dictionary entries), and one of its notes, the TARGET, with its current meaning and the sentences it is used in. Choose the possible meaning that best fits the word's use in the target's sentences.

The word's other notes are shown with the possible meaning each one uses, for reference only: several notes may use the same possible meaning when their sentences use the word in the same sense, and a possible meaning another note uses is not excluded.
- Choose by the target's sentences, not by the wording of its current meaning.
- Give a score of 1 to 5 for how well the chosen possible meaning fits the target's sentences, where 5=perfect fit, 3=acceptable fit, 1=unacceptable fit. A low score says none of the possible meanings covers this use.
- If the target's sentences use the word in the same sense as one of the other notes, set "{SAME_SENSE_FIELD}" to that note's number. Otherwise set it to 0.

Return a JSON object with the fields "{MAP_POSSIBLE_INDEX_FIELD}" (the 1-based index of the chosen possible meaning), "{MAP_SCORE_FIELD}" and "{SAME_SENSE_FIELD}".

WORD OR PHRASE (READING):
{word} ({reading})

ALL POSSIBLE MEANINGS:
{possible}---
TARGET:
Current Japanese meaning: {target['jp_meaning'] or '(empty)'}
Current English meaning: {target['en_meaning'] or '(empty)'}
Sentences:
{_sentence_lines(target['sentences'])}
OTHER NOTES:
{_other_notes_listing(others, possible_meanings)}"""


class NoteMapping(NamedTuple):
    """A map answer: the possible meaning chosen, its score (1 to 5) and the 1-based number of
    the other note whose sense the target uses (0 for none)."""

    meaning: GeneratedMeaningType
    score: int
    same_sense: int


def map_note_to_generated_meaning(
    config: dict[str, str],
    word: str,
    reading: str,
    possible_meanings: Sequence[GeneratedMeaningType],
    target: WordAndSentences,
    others: Sequence[WordAndSentences],
    context: dict[str, Any],
) -> Optional[NoteMapping]:
    """The possible meaning that fits the target, None when the call failed or its answer is
    unusable."""
    inputs: dict[str, Any] = {
        "word": word,
        "reading": reading,
        "possible_meanings": list(possible_meanings),
        "target": target,
        "others": list(others),
    }
    prompt = map_note_prompt(**inputs)
    logger.debug(f"Prompt for mapping a note's meaning: {prompt}")
    response_schema = {
        "type": "object",
        "properties": {
            MAP_POSSIBLE_INDEX_FIELD: {"type": "integer"},
            MAP_SCORE_FIELD: {"type": "integer"},
            SAME_SENSE_FIELD: {"type": "integer"},
        },
        "required": [MAP_POSSIBLE_INDEX_FIELD, MAP_SCORE_FIELD, SAME_SENSE_FIELD],
        "additionalProperties": False,
    }
    result = get_response(
        config.get("word_meaning_model", ""),
        prompt,
        response_schema=response_schema,
        kind="clean_meaning.map_note",
        inputs=inputs,
        context=context,
    )
    if not isinstance(result, dict):
        logger.warning(f"Mapped meaning result was not a dictionary: {result}")
        return None
    index, score = result.get(MAP_POSSIBLE_INDEX_FIELD), result.get(MAP_SCORE_FIELD)
    if not (
        isinstance(index, int)
        and 1 <= index <= len(possible_meanings)
        and isinstance(score, int)
        and 1 <= score <= 5
    ):
        logger.warning(f"Invalid possible meaning index or score in mapping result: {result}")
        return None
    same_sense = result.get(SAME_SENSE_FIELD)
    if not isinstance(same_sense, int) or not 0 <= same_sense <= len(others):
        same_sense = 0
    return NoteMapping(possible_meanings[index - 1], score, same_sense)


# The fields of the extract prompt's answer
EXTRACT_JP_FIELD = "cleaned_meaning"
EXTRACT_EN_FIELD = "english_meaning"


def extract_meaning_prompt(
    word: str,
    reading: str,
    sentences: Sequence[EnAndJPSentence],
    dictionary_entry: str,
    prev_en_meaning: str,
) -> str:
    """The prompt that makes one meaning out of the dictionary entry for the word's use in
    `sentences`. One sentence or several, and an English meaning or none, are its variants."""
    sentences_formatted = ""
    if len(sentences) > 1:
        for sen in sentences:
            sentences_formatted += f"- JP: {sen['jp_sentence']} -- EN: {sen['en_sentence']}\n"
    else:
        sentences_formatted = (
            f"JP: {sentences[0]['jp_sentence']} -- EN: {sentences[0]['en_sentence']}"
        )
    return f"""Below, the dictionary entry for the word or phrase may contain multiple meanings. Your task is to either 1) extract the one meaning 2) or combine and rephrase meanings matching the usage of the word in the sentence{'s' if len(sentences) > 1 else ''}.

Selection criteria:
- DO NOT overfit the definition to the sentence{'s' if len(sentences) > 1 else ''}, but rather pick as many meanings as possible that can broadly fit the theme of the sentence{'s' if len(sentences) > 1 else ''}.
- Figurative uses of literal meanings should be one definition: When there is a pair of meanings for this word or phrase, one literal and one figurative, always pick both and shorten their respective descriptions. Do this even if the sentence{'s' if len(sentences) > 1 else ''} only uses one of the meanings.
- In case there is only a single meaning, return that.
- Exclude any dictionary entries from consideration that are for a different word. These may be included in the list of dictionary entries below due to matching the reading but not the word itself.
- If all dictionary entries appear to be for different words, generate a new meaning following the simplification omission rules below.{' Use the current english meaning as reference for the new meaning.' if prev_en_meaning.strip() != "" else ''}

Omission rules:
- Omit any example sentences the matching meaning(s) included (often included within 「」 brackets).
- Descriptions of animals and plants are often scientific. From these omit descriptions on their ecology and only describe their appearance and type of plant/animal with simple language.

Simplification rules:
- YOU MUST Shorten and simplify the meaning as much as possible, ideally into 1 sentence and at most 2 (if describing both a literal and figurative usage), with more complex meanings being allowed more explanation.

Formatting rules:
- Clean off meaning numberings and other notation leaving only a plain text description.

Additionally, but only if it seems necessary, reword the English dictionary definition to fit the Japanese one. The English definition should ideally simply list equivalent words, if there are some, and only explain in sentences when it's necessary.

Return a JSON object with two fields:
Return the extracted and possibly modified Japanese meaning as the value of the key "{EXTRACT_JP_FIELD}".
Return the possibly modified English meaning as the value of the key "{EXTRACT_EN_FIELD}".

Word or phrase (and its reading):
{word} ({reading})
---
Sentence{'s' if len(sentences) > 1 else ''}:
{sentences_formatted}
{f'''---
Current English meaning:
{prev_en_meaning}''' if prev_en_meaning.strip() != "" else ""}
---
Japanese dictionary entry:
{dictionary_entry}
"""


def get_single_meaning_from_mdx_dict_entry(
    config: dict[str, str],
    word: str,
    reading: str,
    sentences: list[EnAndJPSentence],
    jp_mdx_dict_entry: str,
    prev_en_meaning: str = "",
    note_id: Optional[int] = None,
):
    """`note_id`, the note the meaning is for (its placeholder id until it is added), is only
    recorded, in the call's capture context: calls.note_id is the driver's note, which for a word
    note the match op makes a meaning for is the sentence note."""
    jp_meaning_return_field = EXTRACT_JP_FIELD
    en_meaning_return_field = EXTRACT_EN_FIELD
    logger.debug(f"Getting single meaning with {len(sentences)} sentences")
    inputs: dict[str, Any] = {
        "word": word,
        "reading": reading,
        "sentences": sentences,
        "dictionary_entry": jp_mdx_dict_entry,
        "prev_en_meaning": prev_en_meaning,
    }
    prompt = extract_meaning_prompt(**inputs)
    logger.debug(f"Prompt for cleaning meaning: {prompt}")
    model = config.get("word_meaning_model", "")
    result = get_response(
        model,
        prompt,
        kind="clean_meaning.extract",
        inputs=inputs,
        context={"note_id": note_id},
    )
    # Nothing if the cleaning failed, as get_new_meaning_from_model: this gave back the raw
    # dictionary entry, which the note was then saved with
    if result is None:
        return "", ""
    try:
        return result[jp_meaning_return_field], result[en_meaning_return_field]
    except KeyError:
        return "", ""


# The fields of the generate prompt's answer
GENERATE_JP_FIELD = "new_meaning"
GENERATE_EN_FIELD = "english_meaning"


def generate_meaning_prompt(
    word: str, reading: str, sentences: Sequence[str], prev_en_meaning: str
) -> str:
    """The prompt that makes a meaning from the word's use in `sentences` alone, which are
    the Japanese sentences only. One sentence or several, and an English meaning or none,
    are its variants."""
    sentences_formatted = ""
    if len(sentences) > 1:
        for sen in sentences:
            sentences_formatted += f"- {sen}\n"
    else:
        sentences_formatted = sentences[0]
    return f"""Below {'is a sentence' if len(sentences) == 1 else 'are sentences each'} containing a certain word or phrase. Your task is to generate a short monolingual dictionary style definition of the general meaning used in the sentence by the word or phrase.

- Generally aim to for the definition to be a single sentence. If it is necessary to explain more, the maximum length should be 3 sentences.
- Do not overfit the definition to the sentence{'s' if len(sentences) > 1 else ''}, but rather aim for a general definition that fits the usage in {'each sentence' if len(sentences) > 1 else 'the sentence'}.
- If there are two usage patterns for this word or phrase - for example, one literal and one figurative - describe both shortly.
- If there are more than two usage patterns for this word or phrase, describe the one used in the sentence.
- The word itself should not be used in the definition.

The definition should be in the same language as the sentence. {'Use the current english meaning as reference for the new japanese meaning. Improve the english meaning  as well, if needed.' if prev_en_meaning.strip() != "" else 'Also, generate a very short English translation of the meaning, ideally a list of equivalent words or phrases but explaining further, if necessary.'}


Return a JSON object with two fields:
Return the meaning as the value of the key "{GENERATE_JP_FIELD}".
Return the English translation as the value of the key "{GENERATE_EN_FIELD}".

Word or phrase (and its reading):
{word} ({reading})
{f'''---
Current English meaning:
{prev_en_meaning}''' if prev_en_meaning.strip() != "" else ""}
---
Sentence{'s' if len(sentences) > 1 else ''}:
{sentences_formatted}
"""


def get_new_meaning_from_model(
    config: dict[str, str],
    word: str,
    reading: str,
    sentences: list[EnAndJPSentence],
    prev_en_meaning: str = "",
    note_id: Optional[int] = None,
) -> tuple[str, str]:
    """`note_id` as get_single_meaning_from_mdx_dict_entry's: only recorded."""
    logger.debug(f"Getting new meaning with {len(sentences)} sentences")
    jp_meaning_return_field = GENERATE_JP_FIELD
    en_meaning_return_field = GENERATE_EN_FIELD
    inputs: dict[str, Any] = {
        "word": word,
        "reading": reading,
        # The prompt shows no translation
        "sentences": [sentence["jp_sentence"] for sentence in sentences],
        "prev_en_meaning": prev_en_meaning,
    }
    prompt = generate_meaning_prompt(**inputs)
    logger.debug(f"Prompt for new meaning: {prompt}")
    model = config.get("word_meaning_model", "")
    result = get_response(
        model,
        prompt,
        kind="clean_meaning.generate",
        inputs=inputs,
        context={"note_id": note_id},
    )
    if result is None:
        # Return nothing if the generating failed
        return "", ""

    new_meaning = ""
    en_meaning = ""
    try:
        new_meaning = result[jp_meaning_return_field]
    except KeyError:
        print(f"Error: '{jp_meaning_return_field}' not found in the result")

    try:
        en_meaning = result[en_meaning_return_field]
    except KeyError:
        print(f"Error: '{en_meaning_return_field}' not found in the result")
    return new_meaning, en_meaning


def clean_meaning_in_note(
    config: dict[str, str],
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    all_generated_meanings_dict: GeneratedMeaningsDictType,
    allow_reupdate_existing: bool = False,
    other_meaning_notes: Optional[Sequence[Note]] = None,
    map_only: bool = False,
    word_note_index: Optional[WordIndex] = None,
    sentence_cache: Optional[SentenceCache] = None,
    note_cache: Optional[NoteCache] = None,
    own_sentence: Optional[EnAndJPSentence] = None,
) -> CleanResult:
    """Clean the meaning of `note`, and of no other note.

    With generated meanings for its word, the note is mapped to the one that fits its sentences;
    when none fits well, a meaning for its use is added to the list and it is mapped again.
    Without, it is reworked beside the word's other notes or, alone, made from the dictionary
    entry or its sentences.

    The word's other notes are context: shown to the prompts, never written. This used to write
    the whole meaning group it fetched, so every caller that gave no notes - the match op's
    loops, the add-note hook, the clean op - reworded studied notes of a word whenever one note
    of it was cleaned.

    :param config: Addon configuration dictionary.
    :param note: The note to clean.
    :param notes_to_add_dict: The run's notes to add, whose pending notes of the word are among
        the others fetched.
    :param notes_to_update_dict: The run's edited notes; the note goes in when it is edited.
    :param all_generated_meanings_dict: Dict of all generated meanings for reuse across multiple
        calls, which a mapping may add a meaning to. Provided to avoid doing file operations
        during async operations and to avoid doing file reading in every op.
    :param allow_reupdate_existing: Clean a note this run has edited already; without it such a
        note is skipped.
    :param other_meaning_notes: The word's other notes, in place of the meaning group the
        cleaning would fetch. The match op's CREATE NEW gives every meaning the word has, pending
        notes and the first one without an (mN) marker included.
    :param map_only: Only map the note, and leave it as it is when its word has no generated
        meanings: the match op's loops, which clean every unmapped note of a word before
        matching it. A rework there left nothing to say it was done, so the note stayed unmapped
        and its word's notes were reworked again on every target of the word, in every run.
    :param word_note_index: The run's word index, if the caller has one. Passed straight to
        get_other_meaning_notes, which uses it instead of a whole-collection search.
    :param sentence_cache: The run's sentence cache, if the caller has one. Passed straight to
        get_sentences_for_note, which scans the collection once per note id instead of once
        per ask.
    :param note_cache: The run's fetched notes, if the caller has one. Passed the same way.
    :param own_sentence: The note's own sentence, when the caller has it: passed to
        get_sentences_for_note for this note only, never for the others.
    :return: Whether the note's meaning changed, and the other note of its word whose sense it
        repeats, if the cleaning found one.
    """
    note_type = note.note_type()
    if not note_type:
        logger.error(f"note_type() call failed for note {note.id}")
        return NOT_CHANGED

    # A missing key or a broken layout goes past the KeyError below to the caller, as
    # get_field_config's Exception or note_roles' LayoutError: each names the type and key
    try:
        meaning_field = get_field_config(config, "meaning_field", note_type)
        english_meaning_field = get_field_config(config, "english_meaning_field", note_type)
        word_field = get_field_config(config, "word_field", note_type)
        word_reading_field = get_field_config(config, "word_reading_field", note_type)
        # A vocab note of the two-type layout holds no sentence: its sentences are its sentence
        # type's notes' (get_sentences_for_note)
        sentence_fields = (
            [get_field_config(config, "sentence_field", note_type)]
            if sentence_type_of(config, note_type["name"]) == note_type["name"]
            else []
        )
        new_note_id_field = get_field_config(config, "new_note_id_field", note_type)
    except KeyError as e:
        logger.error(str(e))
        return NOT_CHANGED
    if not all(
        field in note
        for field in (
            meaning_field,
            english_meaning_field,
            word_field,
            word_reading_field,
            *sentence_fields,
            new_note_id_field,
        )
    ):
        logger.error(f"note {note.id} is missing fields")
        return NOT_CHANGED

    logger.debug(
        f"cleaning meaning in note {note.id}, allow_reupdate_existing: {allow_reupdate_existing},"
        f" map_only: {map_only}, provided other_meaning_notes: {other_meaning_notes is not None}"
    )
    # Read before anything below registers the note: its own no-entry tagging did, and the clean
    # op then skipped every note of a word with no dictionary entry as already updated
    if note.id > 0 and note.id in notes_to_update_dict:
        if not allow_reupdate_existing:
            logger.debug(f"Skipping note {note.id} as it's already marked as updated by a previous op")
            return NOT_CHANGED
        note = notes_to_update_dict[note.id]

    word = note[word_field]
    reading = note[word_reading_field]
    mdx_helper.load_mdx_dictionaries_if_needed(config, show_progress=True, finish_progress=False)
    try:
        jp_mdx_dict_entry = mdx_helper.get_definition_text(
            word=word,
            reading=reading,
            pick_dictionary=config.get("mdx_pick_dictionary", "all"),
        )
    except MDXLookupError as e:
        # A dictionary that could not answer, which is not the same as a word that is in none of
        # them. Nothing is tagged and nothing is written: NO_DICTIONARY_ENTRY_TAG is terminal -
        # see MDXLookupError - so tagging here would take the note out of every later run over a
        # transient failure. The note is simply left for one.
        logger.error(
            f"Dictionary lookup failed for note {note.id}, word '{word}' ({reading}): {e};"
            " leaving the note for a later run"
        )
        return NOT_CHANGED

    def register() -> None:
        # A note not added yet (id 0) is saved by being added, not through this dict
        if note.id > 0 and note.id not in notes_to_update_dict:
            notes_to_update_dict[note.id] = note

    def tag(tags: Sequence[str]) -> bool:
        """Adds the tags the note lacks; whether it lacked any."""
        missing = [t for t in tags if not note.has_tag(t)]
        for t in missing:
            note.add_tag(t)
        return bool(missing)

    if not jp_mdx_dict_entry and tag([NO_DICTIONARY_ENTRY_TAG]):
        # Set tag to find notes without dictionary entry later
        register()

    word_key = make_meaning_dict_key(word, reading)
    generated_meanings = all_generated_meanings_dict.get(word_key)
    if map_only and not generated_meanings:
        logger.debug(f"No generated meanings for word {word_key} to map note {note.id} to")
        return NOT_CHANGED

    def meaning_note_key(n: Note) -> NoteId:
        """The note's id, or the placeholder id it carries until it has been added.

        A note not added that carries no placeholder is keyed by its id, 0: a vocab note
        added by hand, which the add-note hook cleans with its field empty, raised here and
        lost its word extraction with it. Only the match op's pending notes carry a
        placeholder, so there is at most one such note, and 0 is neither a placeholder
        (negative) nor an added note's id.
        """
        if n.id != 0:
            return n.id
        try:
            return NoteId(int(n[new_note_id_field]))
        except (KeyError, ValueError, TypeError):
            return n.id

    if other_meaning_notes is None:
        other_meaning_notes = get_other_meaning_notes(
            config=config,
            note=note,
            notes_to_add_dict=notes_to_add_dict,
            notes_to_update_dict=notes_to_update_dict,
            allow_reupdate_existing=allow_reupdate_existing,
            include_pending_notes=True,
            word_note_index=word_note_index,
        )
    # Added notes first, oldest first, then the run's new ones: the order the prompts number
    # them in, and the one a duplicate's sibling is picked by
    others = sorted(
        (
            n
            for n in other_meaning_notes
            if n is not note and not (note.id > 0 and n.id == note.id)
        ),
        key=lambda n: (n.id <= 0, meaning_note_key(n)),
    )

    def usage(
        n: Note, max_sentences: Optional[int] = None, own: Optional[EnAndJPSentence] = None
    ) -> WordAndSentences:
        sentences = get_sentences_for_note(
            config, n, sentence_cache=sentence_cache, note_cache=note_cache, own_sentence=own
        )
        return WordAndSentences(
            jp_meaning=n[meaning_field],
            en_meaning=n[english_meaning_field],
            sentences=sentences[:max_sentences],
        )

    def without_sentences(sentences: Sequence[EnAndJPSentence]) -> bool:
        """Whether the note has no sentence to clean its meaning from, which only a vocab note
        of the two-type layout can have: no example sentence note, and none linking it. The
        extract and generate prompts are built on the first sentence, and the map and rework
        prompts would ask about a use they cannot show."""
        if sentences:
            return False
        logger.warning(
            f"Note {meaning_note_key(note)} of word {word_key} has no sentence to clean its"
            " meaning from: left as it was"
        )
        return True

    def holder_of(en_meaning: str) -> Optional[Note]:
        """The other note that holds this generated meaning already: the note would be its
        duplicate, which is a fact of the strings and needs no prompt."""
        return next((o for o in others if o[english_meaning_field] == en_meaning), None)

    def numbered(number: int) -> Optional[Note]:
        return others[number - 1] if 1 <= number <= len(others) else None

    def same_sense_tags(same_sense_as: Optional[Note]) -> list[str]:
        if same_sense_as is None or same_sense_as.id <= 0:
            return []
        return [f"{SAME_SENSE_TAG}::{same_sense_as.id}"]

    def write(
        jp_meaning: str, en_meaning: str, tags: Sequence[str], same_sense_as: Optional[Note]
    ) -> CleanResult:
        changed = (note[meaning_field], note[english_meaning_field]) != (jp_meaning, en_meaning)
        note[meaning_field] = jp_meaning
        note[english_meaning_field] = en_meaning
        if tag([*tags, *same_sense_tags(same_sense_as)]) or changed:
            register()
        if same_sense_as is not None:
            logger.debug(
                f"Note {meaning_note_key(note)} repeats the sense of note"
                f" {meaning_note_key(same_sense_as)} of word {word_key}"
            )
        return CleanResult(changed, same_sense_as)

    if generated_meanings:
        generated_en_meanings = {meaning["en_meaning"] for meaning in generated_meanings}
        if note[english_meaning_field] in generated_en_meanings:
            # Mapped already, as a note copied from a generated meaning is when made
            holder = holder_of(note[english_meaning_field])
            if tag([MEANING_MAPPED_TAG, *same_sense_tags(holder)]):
                register()
            return CleanResult(False, holder)

        target = usage(note, own=own_sentence)
        if without_sentences(target["sentences"]):
            return NOT_CHANGED
        other_usages = [usage(o, OTHER_NOTE_SENTENCES) for o in others]

        def mapped(
            possible_meanings: Sequence[GeneratedMeaningType], depth: int
        ) -> Optional[NoteMapping]:
            return map_note_to_generated_meaning(
                config,
                word,
                reading,
                possible_meanings,
                target,
                other_usages,
                # `same_sense_as` counts in other_note_ids, and the possible meaning index in
                # inputs.possible_meanings. The depth tells the second try, after a meaning was
                # added, from the first
                context={
                    "target_note_id": meaning_note_key(note),
                    "other_note_ids": [meaning_note_key(o) for o in others],
                    "depth": depth,
                },
            )

        mapping = mapped(generated_meanings, 0)
        if mapping is None:
            return NOT_CHANGED
        # A threshold of 3 sometimes gives a score of 3 again after the list has grown, so there
        # is only the one try: a meaning is added and the note mapped once more
        if mapping.score <= 3:
            logger.debug(
                f"Note {meaning_note_key(note)} of word {word_key} fits no generated meaning"
                f" well (score {mapping.score}), adding one for its use"
            )
            if add_meanings_for_usage(config, word, reading, target, all_generated_meanings_dict):
                remapped = mapped(all_generated_meanings_dict[word_key], 1)
                if remapped is not None:
                    mapping = remapped
        # 3 is still an acceptable mapping: the most common reason a second try ends where it is
        # is that the score could not be raised above it
        if mapping.score <= 2:
            logger.debug(
                f"Note {meaning_note_key(note)} of word {word_key} left unmapped, score"
                f" {mapping.score}"
            )
            return NOT_CHANGED
        holder = holder_of(mapping.meaning["en_meaning"])
        return write(
            mapping.meaning["jp_meaning"],
            mapping.meaning["en_meaning"],
            ["updated_jp_meaning", f"meaning_mapping_score::{mapping.score}", MEANING_MAPPED_TAG],
            holder if holder is not None else numbered(mapping.same_sense),
        )

    if others:
        target = usage(note, own=own_sentence)
        if without_sentences(target["sentences"]):
            return NOT_CHANGED
        reworked = rework_note_meaning(
            config,
            word,
            reading,
            target,
            [usage(o, OTHER_NOTE_SENTENCES) for o in others],
            jp_mdx_dict_entry,
            # `same_sense_as` counts in other_note_ids
            context={
                "target_note_id": meaning_note_key(note),
                "other_note_ids": [meaning_note_key(o) for o in others],
            },
        )
        if reworked is None:
            return NOT_CHANGED
        jp_meaning, en_meaning, same_sense = reworked
        return write(jp_meaning, en_meaning, ["updated_jp_meaning"], numbered(same_sense))

    sentences = get_sentences_for_note(
        config,
        note,
        sentence_cache=sentence_cache,
        note_cache=note_cache,
        own_sentence=own_sentence,
    )
    if without_sentences(sentences):
        return NOT_CHANGED
    if jp_mdx_dict_entry:
        # Call API to get single meaning from the raw dictionary entry
        new_jp_meaning, new_en_meaning = get_single_meaning_from_mdx_dict_entry(
            config,
            word,
            reading,
            sentences,
            jp_mdx_dict_entry,
            note[english_meaning_field],
            note_id=meaning_note_key(note),
        )
    else:
        # If there's no dict_entry, we'll let a model generate one from scratch
        new_jp_meaning, new_en_meaning = get_new_meaning_from_model(
            config,
            word,
            reading,
            sentences,
            note[english_meaning_field],
            note_id=meaning_note_key(note),
        )
    # A failed call leaves the note as it was: it used to write the raw dictionary entry, or
    # empty the fields, and a note added by hand was saved that way
    if not (new_jp_meaning and new_en_meaning):
        return NOT_CHANGED
    return write(new_jp_meaning, new_en_meaning, [], None)


def bulk_clean_notes_op(
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list,
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
):
    config = mw.addonManager.getConfig(__name__)
    if not config:
        showWarning("Missing addon configuration")
        return
    message = "Cleaning meaning"

    all_generated_meanings_dict = load_meanings_dict_from_file()
    # This op scans for the same note's sentences once per note it cleans, exactly as the
    # matching op does, so it gets the same run-scoped caches. Both die with the closure.
    sentence_cache = SentenceCache()
    note_cache = NoteCache()

    def op(
        config: dict[str, str],
        note: Note,
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
    ) -> bool:
        nonlocal all_generated_meanings_dict
        return clean_meaning_in_note(
            config,
            note,
            notes_to_add_dict,
            notes_to_update_dict,
            all_generated_meanings_dict,
            sentence_cache=sentence_cache,
            note_cache=note_cache,
        ).changed

    def on_end():
        nonlocal all_generated_meanings_dict
        # Write updated meanings dictionary to file after successful operation
        write_meanings_dict_to_file(all_generated_meanings_dict)

    return bulk_notes_op(
        message,
        config,
        op,
        col,
        notes_of_role(config, notes, VOCAB_ROLE),
        edited_nids,
        progress_updater,
        notes_to_add_dict=notes_to_add_dict,
        notes_to_update_dict=notes_to_update_dict,
        on_end=on_end,
    )


def clean_selected_notes(
    nids: Sequence[NoteId], parent: Browser, chain: Optional[ChainStep] = None
):
    progress_updater = AsyncTaskProgressUpdater(title="Async AI op: Cleaning meanings")
    done_text = "Updated meaning"
    bulk_op = bulk_clean_notes_op
    return selected_notes_op(done_text, bulk_op, nids, parent, progress_updater, chain=chain)
