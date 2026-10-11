"""Set furigana from audio: a sentence note's furigana, written as its clip says it.

A sentence note made from a show's captions (subs2srs) holds the caption in `sentence_field`
and, once "Transcribe sentence audio" has run, what the models heard in
`sentence_transcription_field`. This op writes the sentence with its furigana into
`furigana_sentence_field` (`word_array/audio_furigana.py`): each kanji word read as the
captions read it, else as a transcript hears it where that is Sudachi's draft, a reading
JMdict has for the spelling or the show's for a name. A word the transcripts do not settle
goes to the config's `furigana_models`, when it names any: the arbiter
(`word_array/furigana_arbiter.py`). Each model, asked once a note given the line, its
translation, the transcripts and what each heard at the word, answers a reading for it, and the
reading is written where every model is sure of it and a transcript heard it there. A word
still unsettled is written with the readings to choose between,
方[ほう？かた] (`audio_furigana.doubt_furigana`), and the note is tagged `furigana-review`:
kanjify and word extraction leave it alone until a person has fixed each such word and taken
the tag off. Speaker labels and sound descriptions are not said, so the furigana leaves them
out.

Only a note whose furigana field is empty is written, so furigana a person typed or fixed is
never replaced: empty the field to have it set again. A note needs a transcript from each of
the config's `transcribe_models`, and one of them hearing a reading is enough to write it: the
furigana audio eval measured that rule on the default pair, which sent half as many words to
review as asking both, for no more wrong readings.

The show: subs2srs tags a card as its export says, and tagged `<show>::<episode>`, the first
level of a note's first hierarchical tag names its show. The captions read a name in katakana
at its first mention in an episode (猫猫(マオマオ)) and seldom after, so a show's names are
every surface its captions read in katakana somewhere, each with the reading they give it
most; one they read in hiragana somewhere is a word, not a name. Names the captions never read
go in `user_files/show_names.json`, by show (`{"<show>": {"響迂": "きょうう"}}`), and win over
the captions. A note without such a tag is read with its own caption alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Callable, Iterable, Mapping, Optional

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.browser import Browser

from ..configuration import ADDON_USER_FILES_DIR, FURIGANA_REVIEW_TAG
from ..generator_resources import with_generator_resources
from ..kana_conv import to_hiragana
from ..note_roles import SENTENCE_ROLE, note_type_search
from ..utils import get_field_config
from ..word_array import furigana_arbiter, jmdict_index
from ..word_array.audio_furigana import Furigana, Reading, audio_furigana
from ..word_array.captions import BaseFinder, ReadingBase, show_readings
from . import collection_access
from .base_ops import (
    AsyncTaskProgressUpdater,
    NotesRunSpec,
    bulk_notes_op,
    get_response,
    selected_notes_op,
)
from .chain_types import ChainStep
from .role_gate import notes_of_role
from .run_errors import report_error
from .transcribe_audio import label, parse_transcripts, transcribe_models

logger = logging.getLogger(__name__)

REVIEW_TAG = FURIGANA_REVIEW_TAG
NAMES_FILE = os.path.join(ADDON_USER_FILES_DIR, "show_names.json")
# Of the transcripts, how many must hear a reading for it to be written
QUORUM = 1
ARBITER_KIND = "furigana_arbiter.note"
# The characters Anki's search syntax gives a meaning inside a quoted `tag:` search
_TAG_SEARCH_SPECIAL = re.compile(r'[\\"*_]')

# A spelling's readings: JMdict's in the op, a table in the tests
Dictionary = Callable[[str], set[str]]


def sudachi_morphemes(text: str) -> list:
    # Imported here, so that this module loads without SudachiPy, as the tests load it
    from ..word_array.generator import sudachi_morphemes as morphemes

    return morphemes(text)


def show_of(note: Note) -> Optional[str]:
    """The show a note is of: the first level of its first hierarchical tag, `<show>::01`."""
    for tag in note.tags:
        if "::" in tag:
            return tag.split("::", 1)[0]
    return None


def show_search(show: str) -> str:
    """`"tag:<show>::*"`: the notes tagged with an episode of the show, the characters Anki's
    search reads as wildcards escaped as `note_roles.note_type_search` escapes them."""
    return '"tag:' + _TAG_SEARCH_SPECIAL.sub(r"\\\g<0>", show) + '::*"'


def read_names_file(path: str) -> dict[str, dict[str, str]]:
    """The names file's names by show, each surface's reading in hiragana: {} when there is no
    file. One that is not such JSON is reported and read as none."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        report_error(f"{path} could not be read ({e}), so the names in it are not used")
        return {}
    if not isinstance(data, dict):
        report_error(f'{path} is not {{"<show>": {{"<name>": "<reading>"}}}}: not used')
        return {}
    out: dict[str, dict[str, str]] = {}
    for show, names in data.items():
        if isinstance(names, dict):
            out[show] = {s: to_hiragana(r) for s, r in names.items() if isinstance(r, str) and r}
    return out


def show_captions(show: str, note_type_name: str, field: str) -> list[str]:
    """The captions of the show's notes of one note type, in the order they were added."""
    query = f"{note_type_search(note_type_name)} {show_search(show)}"
    ids = sorted(collection_access.find_notes(query))
    return [note[field] for note in collection_access.get_notes(ids) if field in note]


@dataclass
class Show:
    names: dict[str, str]  # surface -> reading
    words: set[str]  # surfaces the captions read in hiragana
    base: BaseFinder  # which kanji an inline reading is for


class Shows:
    """The shows of a run's notes, each read from its captions when a note of it first comes:
    its names, and which kanji of a run an inline reading of its captions is for. Notes run on
    several threads, and a show is read once: the others wait for it."""

    def __init__(
        self,
        captions_of: Callable[[str, str, str], list[str]],
        dictionary: Dictionary,
        user_names: Mapping[str, Mapping[str, str]],
    ) -> None:
        self.captions_of = captions_of
        self.dictionary = dictionary
        self.user_names = user_names
        self._read: dict[tuple[str, str, str], Show] = {}
        self._lock = threading.Lock()

    def get(self, show: Optional[str], note_type_name: str, field: str, caption: str) -> Show:
        if show is None:
            return self._make([caption], {})
        key = (show, note_type_name, field)
        with self._lock:
            if key not in self._read:
                texts = self.captions_of(show, note_type_name, field) or [caption]
                self._read[key] = self._make(texts, self.user_names.get(show, {}))
            return self._read[key]

    def _make(self, texts: Iterable[str], user_names: Mapping[str, str]) -> Show:
        texts = list(texts)
        base = ReadingBase(texts, self.dictionary)
        names, words = show_readings(texts, base)
        names.update(user_names)
        return Show(names, words - set(user_names), base)


def arbiter_models(config: dict) -> list[str]:
    """The models that settle the words the audio did not, all of them agreeing; none for a
    config without `furigana_models`. One model may be named without a list."""
    models = config.get("furigana_models")
    if isinstance(models, str):
        models = [models]
    if not isinstance(models, list):
        return []
    return [m.strip() for m in models if isinstance(m, str) and m.strip()]


def settle_by_arbiter(
    models: Sequence[str],
    result: Furigana,
    caption: str,
    translation: str,
    transcripts: Sequence[tuple[str, str]],
) -> list[Reading]:
    """Asks each of `models` about the words `result` leaves in review and writes those they
    all settle alike (`furigana_arbiter.settle`); the words it wrote. A failed call settles
    none, since every model has to agree, and the provider has reported it."""
    inputs = furigana_arbiter.review_inputs(caption, translation, transcripts, result)
    prompt = furigana_arbiter.review_prompt(**inputs)
    answers = []
    for model in models:
        response = get_response(
            model,
            prompt,
            response_schema=furigana_arbiter.RESPONSE_SCHEMA,
            kind=ARBITER_KIND,
            inputs=inputs,
        )
        if response is None:
            return []
        answers.append(furigana_arbiter.answers_from_response(response, len(result.review)))
    return furigana_arbiter.settle(result, answers)


def set_furigana_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    *,
    shows: Shows,
    tokenize: Callable[[str], Sequence],
    dictionary: Dictionary,
    arbiters: Sequence[str] = (),
) -> bool:
    """Writes the note's furigana from its transcripts, if its furigana field is empty, with
    the words they leave unsettled put to the `arbiters`' models, when there are any, and tags
    it for review when a word is still unsettled."""
    note_type = note.note_type()
    if not note_type:
        logger.error(f"note_type() call failed for note {note.id}")
        return False
    try:
        sentence_field = get_field_config(config, "sentence_field", note_type)
        furigana_field = get_field_config(config, "furigana_sentence_field", note_type)
        transcript_field = get_field_config(config, "sentence_transcription_field", note_type)
    except Exception as e:
        report_error(str(e))
        return False
    fields = (sentence_field, furigana_field, transcript_field)
    missing = [f for f in fields if f not in note]
    if missing:
        report_error(f'"{note_type["name"]}" has no field "{missing[0]}"')
        return False
    if furigana_field == sentence_field:
        report_error(
            f'"{note_type["name"]}": furigana_sentence_field is the sentence_field, which holds'
            " the caption the furigana is made from: name another field"
        )
        return False
    caption = note[sentence_field]
    if note[furigana_field].strip() or not caption.strip():
        return False
    models = transcribe_models(config)
    heard = parse_transcripts(note[transcript_field])
    lacking = [label(m) for m in models if label(m) not in heard]
    if lacking:
        report_error(
            f'No transcript from {", ".join(lacking)} yet: run "Transcribe sentence audio" first'
        )
        return False
    show = shows.get(show_of(note), note_type["name"], sentence_field, caption)
    transcripts = [(label(m), heard[label(m)]) for m in models]
    result = audio_furigana(
        caption,
        [text for _, text in transcripts],
        tokenize,
        show.names,
        dictionary,
        base=show.base,
        show_words=show.words,
        quorum=QUORUM,
    )
    if result.review and arbiters:
        # The translation is context only: a note type without one is asked all the same
        translation_field = config.get(note_type["name"], {}).get("translated_sentence_field")
        translation = note[translation_field] if translation_field in note else ""
        settled = settle_by_arbiter(
            arbiters,
            result,
            caption,
            furigana_arbiter.translation_text(translation),
            transcripts,
        )
        if settled:
            logger.info(
                "Note %s: settled by the arbiter: %s",
                note.id,
                ", ".join(f"{r.surface} {r.written}" for r in settled),
            )
    note[furigana_field] = result.text
    if result.review:
        note.add_tag(REVIEW_TAG)
        logger.info(
            "Note %s: furigana to review: %s",
            note.id,
            ", ".join(f"{r.surface} ({r.why})" for r in result.review),
        )
    else:
        note.remove_tag(REVIEW_TAG)
    notes_to_update_dict[note.id] = note
    return True


async def bulk_set_furigana_op(
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]] = {},
    notes_to_update_dict: dict[NoteId, Note] = {},
):
    config = mw.addonManager.getConfig(__name__)
    if not config:
        report_error("Missing addon configuration")
        return None
    dictionary = jmdict_index.spelling_readings
    shows = Shows(show_captions, dictionary, read_names_file(NAMES_FILE))
    op = partial(
        set_furigana_in_note,
        shows=shows,
        tokenize=sudachi_morphemes,
        dictionary=dictionary,
        arbiters=arbiter_models(config),
    )
    # Not a sync op: the arbiter's calls wait on a provider, and Sudachi, which the notes
    # share, takes them one at a time (generator.TOKENIZER_LOCK)
    return await bulk_notes_op(
        "Setting furigana from audio",
        config,
        op,
        col,
        notes_of_role(config, notes, SENTENCE_ROLE),
        edited_nids,
        progress_updater,
        notes_to_add_dict,
        notes_to_update_dict,
    )


def set_furigana_spec() -> NotesRunSpec:
    """The op's run, as the menu starts it and as a script does (`NotesRunSpec`)."""
    return NotesRunSpec(
        done_text="Set furigana from audio",
        title="Setting furigana from audio",
        bulk_op=bulk_set_furigana_op,
    )


def set_furigana_selected_notes(
    nids: Sequence[NoteId], parent: Browser, chain: Optional[ChainStep] = None
):
    """Needs the generator's resources (Sudachi and JMdict), asked about before any note is
    touched."""

    def run():
        spec = set_furigana_spec()
        selected_notes_op(
            spec.done_text,
            spec.bulk_op,
            nids,
            parent,
            AsyncTaskProgressUpdater(title=spec.title),
            chain=chain,
        )

    with_generator_resources(parent, run, chain=chain)
