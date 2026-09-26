"""The note types and config the CopyAnywhere backend suite is written against.

A module of its own rather than part of `conftest.py`, so that tests can import these names
and mypy can see them. mypy.ini excludes this directory's `conftest.py`, which would
otherwise collide with the repo root's as the top-level module `conftest`. So every
`from conftest import VOCAB` resolved to the root one, which has no such name, and counted
as an error in each test that did it.
"""

from typing import Any

# A plain two-card note type: two templates, so "a card of this note is missing" and
# "two cards of one note come back from one query" both have somewhere to happen.
VOCAB = "CA Vocab"
VOCAB_FIELDS = ["Word", "Reading", "Meaning", "Freq", "Note"]
VOCAB_TEMPLATES = [
    ("Recognition", "{{Word}}", "{{FrontSide}}<hr id=answer>{{Meaning}}"),
    ("Recall", "{{Meaning}}", "{{FrontSide}}<hr id=answer>{{Word}}"),
]

# A single-card note type, for the multi-note-type card value path, which raises as soon as
# a note has more than one card type.
SENTENCE = "CA Sentence"
SENTENCE_FIELDS = ["Sentence", "Vocab", "Audio"]
SENTENCE_TEMPLATES = [("Card 1", "{{Sentence}}", "{{FrontSide}}<hr id=answer>{{Vocab}}")]

# A second single-card type, so a multi-note-type definition has two to span.
KANJI = "CA Kanji"
KANJI_FIELDS = ["Kanji", "Keyword"]
KANJI_TEMPLATES = [("Card 1", "{{Kanji}}", "{{FrontSide}}<hr id=answer>{{Keyword}}")]

# Cloze, whose card values key by ordinal rather than by template name.
CLOZE = "CA Cloze"
CLOZE_FIELDS = ["Text", "Extra"]

# A note type whose template name itself contains a double underscore, which is the one
# thing CARD_VALUE_RE's greedy first group has to get right.
ODD_TEMPLATE = "CA Odd"
ODD_TEMPLATE_FIELDS = ["Front", "Back"]
ODD_TEMPLATE_TEMPLATES = [("Card__Front", "{{Front}}", "{{FrontSide}}<hr id=answer>{{Back}}")]

DEFAULT_CONFIG: dict[str, Any] = {
    "log_level": "error",
    "copy_fields_shortcut": "Ctrl+Shift+C",
    "copy_definitions": [],
}
