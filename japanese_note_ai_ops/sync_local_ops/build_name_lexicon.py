"""Build the name lexicon the word array generator uses, from the selected notes' sentences.

Names no dictionary knows (里樹, ひまりん) are found by the honorifics, nickname suffixes and
vocatives around them (`word_array.names`), and one anchored use names every mention, so the
lexicon is only as good as its corpus: select the whole collection. The sentence is taken as the
migration takes it, context stripped. The lexicon replaces the one in user_files, which the
migration op reads.
"""

import logging
from typing import Any, Sequence

from anki.collection import Collection
from anki.notes import NoteId
from aqt import mw
from aqt.operations import QueryOp
from aqt.utils import showInfo, showWarning

from ..html_stripping import strip_context_sentences
from ..utils import get_field_config
from ..word_array import generator, names, resources
from ..generator_resources import with_generator_resources

logger = logging.getLogger(__name__)


def collect_sentences(col: Collection, config: dict, nids: Sequence[NoteId]) -> list[str]:
    """The context-stripped sentence of every note that has one, duplicates included."""
    out = []
    for nid in nids:
        note = col.get_note(nid)
        note_type = note.note_type()
        try:
            field = get_field_config(config, "word_extraction_sentence_field", note_type)
        except Exception:
            continue
        if field in note:
            sentence = strip_context_sentences(note[field])
            if sentence.strip():
                out.append(sentence)
    return out


def build_name_lexicon_from_selected(nids: Sequence[NoteId], parent: Any) -> None:
    config = mw.addonManager.getConfig(__name__)
    if not config:
        showWarning("Missing addon configuration")
        return

    def build(col: Collection) -> str:
        sentences = collect_sentences(col, config, nids)
        lexicon = generator.build_name_lexicon(sentences)
        names.save_lexicon(lexicon, resources.NAME_LEXICON)
        logger.info(f"Name lexicon: {len(lexicon)} names from {len(sentences)} sentences")
        return (
            f"Name lexicon: {len(lexicon)} names from {len(set(sentences))} distinct sentences,"
            f" saved to {resources.NAME_LEXICON}"
        )

    def run() -> None:
        QueryOp(
            parent=parent,
            op=build,
            success=lambda message: showInfo(message, parent=parent, title="Name lexicon"),
        ).with_progress("Building the name lexicon").run_in_background()

    with_generator_resources(parent, run)
