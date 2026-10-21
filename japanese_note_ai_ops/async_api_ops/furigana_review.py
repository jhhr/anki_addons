"""Whether a sentence note's furigana has been checked: the one test the ops that read the
furigana make before they do.

"Set furigana from audio" writes a word its audio did not settle with the readings to choose
between, 方[ほう？かた] (`word_array/audio_furigana.py`, `has_doubt`), and tags the note
`furigana-review` (`configuration.FURIGANA_REVIEW_TAG`). A person listens, leaves each such
word with the reading said and takes the tag off. Until then a reading in doubt read as said
would be kanjified, extracted into the word array, or copied on, so kanjify and word extraction
leave such a note alone, and "Run all ops for new notes" gives it neither.

A module of its own, light, so that the ops asking need nothing of set_furigana's: it imports
only the tag and its messages, the doubt test and the config reader.
"""

from __future__ import annotations

from typing import Optional

from anki.notes import Note

from ..configuration import FURIGANA_DOUBT_LEFT, FURIGANA_REVIEW_LEFT, FURIGANA_REVIEW_TAG
from ..note_roles import configured_value
from ..word_array.audio_furigana import has_doubt


def unchecked_furigana(config: dict, note: Note, *also: str) -> Optional[str]:
    """Why the note's furigana is not to be read yet, as an op says it when it leaves the note
    alone; None for checked furigana. Its review tag, or a word still marked "？" in its
    `furigana_sentence_field` or in a field of `also`, a field the caller reads instead (word
    extraction's sentence, often kanjified from the furigana): a tag taken off before every
    such word was fixed would let one through."""
    if note.has_tag(FURIGANA_REVIEW_TAG):
        return FURIGANA_REVIEW_LEFT
    note_type = note.note_type()
    name = note_type["name"] if note_type else ""
    texts = [configured_value(config, name, note, "furigana_sentence_field")]
    texts += [note[field] for field in also if field and field in note]
    if any(has_doubt(text) for text in texts):
        return FURIGANA_DOUBT_LEFT
    return None
