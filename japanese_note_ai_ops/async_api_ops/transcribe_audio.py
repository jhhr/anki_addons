"""Transcribe sentence audio: each sentence note's clip, heard by the transcribers alone, their
kana kept in the note's `sentence_transcription_field` for "Set furigana from audio".

The transcribers are the config's `transcribe_models`, each named for where it runs:
`local-<model>` is a model of the local transcription server (dev/transcribe_server.py, a
program of its own, since Anki's Python never loads PyTorch) at `transcribe_url`. The default
pair, kana-anime-whisper and Ruby-ASR's mora mode, is the one the furigana audio eval picked.
The check needs two: one model's mishearing is caught only by another that heard otherwise.

A transcriber hears the clip and nothing else, no caption, draft or names: an audio model
given text follows the text, and a transcript is worth something only as a hearing of its own.

The field holds a line per transcriber, `kana-anime-whisper: そのときは`, joined by `<br>`, so
the note shows what was heard. A note is asked only for the lines its field lacks: one done
is skipped, so a run goes on where a cancelled one stopped, and a transcriber added to the
config later is asked for its own line alone. Notes run one after another: the server hears
one clip at a time anyway, and parallel requests would only queue in front of it and time out.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Optional

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.browser import Browser

from ..note_roles import SENTENCE_ROLE, configured_field
from ..utils import get_field_config
from .api_client import get_session
from .base_ops import (
    LOCAL_TRANSCRIBER_PREFIX,
    AsyncTaskProgressUpdater,
    DialogCancelState,
    bulk_notes_op,
    get_transcript,
    responder_active,
    selected_notes_op,
    transcribe_url,
)
from .chain_types import ChainStep
from .role_gate import notes_of_role, notes_passing
from .run_errors import report_error

logger = logging.getLogger(__name__)

TRANSCRIBE_KIND = "transcribe.audio"
# Where the server is: the released add-on leaves dev/ out (build.json)
SERVER_SOURCE = (
    "japanese_note_ai_ops/dev/transcribe_server.py of https://github.com/jhhr/anki_addons"
)
DEFAULT_MODELS = ("local-kana-anime-whisper", "local-ruby-mora")
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
LINE_RE = re.compile(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$")


def transcribe_models(config: dict) -> list[str]:
    models = config.get("transcribe_models")
    if not isinstance(models, list) or not models:
        return list(DEFAULT_MODELS)
    return [str(m) for m in models]


def label(model: str) -> str:
    """A transcriber's name on its line of the field: a local one's model, kana-anime-whisper."""
    if model.startswith(LOCAL_TRANSCRIBER_PREFIX):
        return model[len(LOCAL_TRANSCRIBER_PREFIX) :]
    return model


def audio_file(field: str) -> Optional[str]:
    """The clip a field names, [sound:clip.opus] -> clip.opus; the first, if it names more."""
    m = SOUND_RE.search(field)
    return m.group(1) if m else None


def parse_transcripts(field: str) -> dict[str, str]:
    """The field's lines by transcriber: `name: kana`, one a line, lines split by <br> or a
    newline. A line without a name is not a transcript and is left out."""
    out: dict[str, str] = {}
    for line in re.split(r"<br\s*/?>|\n", field):
        m = LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def lacking(models: Sequence[str], heard: dict[str, str]) -> list[str]:
    """The transcribers of `models` whose line the field's lines `heard` (`parse_transcripts`)
    do not hold: those a run asks, and whose lines "Set furigana from audio" waits for."""
    return [model for model in models if label(model) not in heard]


def format_transcripts(transcripts: dict[str, str], models: Sequence[str]) -> str:
    """The field's text: the configured transcribers' lines in the config's order, then any
    other the field held."""
    order = [label(m) for m in models]
    names = [n for n in order if n in transcripts] + [n for n in transcripts if n not in order]
    return "<br>".join(f"{name}: {transcripts[name]}" for name in names)


def transcribe_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    *,
    media: Path,
) -> bool:
    """Asks each transcriber the note's field has no line of for one, and writes what came."""
    note_type = note.note_type()
    if not note_type:
        logger.error(f"note_type() call failed for note {note.id}")
        return False
    try:
        audio_field = get_field_config(config, "sentence_audio_field", note_type)
        transcript_field = get_field_config(config, "sentence_transcription_field", note_type)
    except Exception as e:
        report_error(str(e))
        return False
    if audio_field not in note or transcript_field not in note:
        report_error(f'"{note_type["name"]}" has no field "{audio_field}" or "{transcript_field}"')
        return False
    models = transcribe_models(config)
    heard = parse_transcripts(note[transcript_field])
    todo = lacking(models, heard)
    if not todo:
        return False
    name = audio_file(note[audio_field])
    if name is None:
        logger.info("Note %s has no clip in %s", note.id, audio_field)
        return False
    try:
        audio = (media / name).read_bytes()
    except OSError as e:
        report_error(f"{name}: could not read it from the media folder ({e.strerror or e})")
        return False
    # The clip is known by its name and its bytes' hash, which is what a replay's cassette
    # answers by; the model too, since one clip is asked of several
    sha1 = hashlib.sha1(audio).hexdigest()
    cancel_state = DialogCancelState()
    got = dict(heard)
    for model in todo:
        inputs = {"audio": name, "sha1": sha1, "model": model}
        result = get_transcript(
            model, audio, kind=TRANSCRIBE_KIND, inputs=inputs, cancel_state=cancel_state
        )
        if result is None:
            # What came is kept: a later run asks only for the lines still missing
            break
        got[label(model)] = result["text"]
    if got == heard:
        return False
    note[transcript_field] = format_transcripts(got, models)
    notes_to_update_dict[note.id] = note
    return True


def lacks_transcript(config: dict, note: Note) -> bool:
    """Whether a run asks a transcriber about the note, as `transcribe_note` decides it: its
    transcript field lacks the line of one of the config's transcribers, and its audio field
    names a clip. A note added by hand names none, so it never gets a line and never lacks one
    a run could get: counted, it made a run over a selection holding one need the server,
    though nothing was sent. A note whose fields cannot be read counts: `transcribe_note`
    reports why."""
    note_type = note.note_type()
    name = note_type["name"] if note_type else ""
    field = configured_field(config, name, "sentence_transcription_field")
    if not field or field not in note:
        return True
    if not lacking(transcribe_models(config), parse_transcripts(note[field])):
        return False
    audio = configured_field(config, name, "sentence_audio_field")
    return not audio or audio not in note or audio_file(note[audio]) is not None


def same_field_error(config: dict, note_type_name: str) -> Optional[str]:
    """Why a note type's notes cannot be transcribed, if its transcript field is its audio field:
    the transcripts would be written over the clip's [sound:] tag, and the clip would no longer
    play. As `set_furigana` refuses a furigana field that is the sentence's."""
    transcript = configured_field(config, note_type_name, "sentence_transcription_field")
    audio = configured_field(config, note_type_name, "sentence_audio_field")
    if transcript and transcript == audio:
        return (
            f'"{note_type_name}": sentence_transcription_field is the sentence_audio_field,'
            " whose [sound:] tag the transcripts would be written over: name another field."
        )
    return None


def server_problem(config: dict, models: Sequence[str]) -> Optional[str]:
    """Why the local transcription server cannot hear the clips with `models`, or None when it
    can or none of them is local: asked once before a run, so that a server not started fails
    the run at once and not every note after its retries."""
    local = [label(m) for m in models if m.startswith(LOCAL_TRANSCRIBER_PREFIX)]
    if not local:
        return None
    url = transcribe_url(config)
    try:
        response = get_session("local").get(f"{url}/health", timeout=10)
        loaded = json.loads(response.text)["models"]
    except Exception as e:
        return (
            f"The transcription server at {url} does not answer ({type(e).__name__}): start"
            f" it as {SERVER_SOURCE} says (the released add-on leaves it out), or set"
            " transcribe_url to where it runs."
        )
    missing = [name for name in local if name not in loaded]
    if missing:
        return (
            f"The transcription server at {url} has not loaded {', '.join(missing)}: start it"
            f" with --models {' '.join(local)}."
        )
    return None


async def bulk_transcribe_notes_op(
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]] = {},
    notes_to_update_dict: dict[NoteId, Note] = {},
    *,
    undo_label: Optional[str] = None,
):
    config = mw.addonManager.getConfig(__name__)
    if not config:
        report_error("Missing addon configuration")
        return None
    notes = notes_of_role(config, notes, SENTENCE_ROLE)
    # Said once a note type, before the server is asked about or any note is touched
    notes = notes_passing(notes, lambda name: same_field_error(config, name))
    # Only a run that has a clip to send needs the server: one over notes transcribed already
    # (a rerun, or "Run all ops for new notes" again) must not fail for want of it, nor a
    # replay's or a test's, whose responder hears the clips in its place (get_transcript):
    # the check goes to the server itself, past the responder
    if not responder_active() and any(lacks_transcript(config, note) for note in notes):
        problem = server_problem(config, transcribe_models(config))
        if problem:
            report_error(problem)
            return None
    op = partial(transcribe_note, media=Path(col.media.dir()))
    return await bulk_notes_op(
        "Transcribing sentence audio",
        config,
        op,
        col,
        notes,
        edited_nids,
        progress_updater,
        notes_to_add_dict,
        notes_to_update_dict,
        is_sync_op=True,
        undo_label=undo_label,
    )


def transcribe_selected_notes(
    nids: Sequence[NoteId], parent: Browser, chain: Optional[ChainStep] = None
):
    progress_updater = AsyncTaskProgressUpdater(title="Transcribing sentence audio")
    done_text = "Transcribed sentence audio"
    return selected_notes_op(
        done_text, bulk_transcribe_notes_op, nids, parent, progress_updater, chain=chain
    )
