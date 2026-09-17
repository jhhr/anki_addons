import logging

from anki.notes import Note

from ..shared.jp_text_processing.word.word_highlight import (
    word_highlight,
)

logger = logging.getLogger(__name__)


def word_highlight_process(
    text: str,
    word_field: str,
    note: Note,
) -> str:
    """
    Wraps the word_highlight function to be used as an extra processing step in the copy fields
    chain.
    """

    # Get the word to highlight
    word_to_highlight = None
    if word_field:
        for name, field_text in note.items():
            if name == word_field:
                word_to_highlight = field_text
                if word_to_highlight:
                    break
        if not word_to_highlight:
            logger.error("Error in word_highlight: word_field '%s' not found in note.", word_field)
    logger.debug("word_to_highlight: %s, text: %s", word_to_highlight, text)
    result = word_highlight(text, word_to_highlight)
    logger.debug("word_highlight result: %s", result)
    return result
