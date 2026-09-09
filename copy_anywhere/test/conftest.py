"""Fixtures for the CopyAnywhere backend suite: a real collection behind a stubbed `mw`.

These tests drive everything below `copy_fields()` directly. They need real field storage
and real search -- a copy definition's whole job is to read fields, run a query and write
fields -- so they run against a real `anki.collection.Collection` opened headless, with only
`aqt.mw` stubbed. The root conftest arranges that; this file opens the collection, builds
the note types the cases are written against, and points `mw` at each fresh collection.

The hook-triggered half of the suite (`note_will_be_added`, `reviewer_did_answer_card`,
`editor_did_unfocus_field`, `editor_did_load_note`) cannot run here: those hooks only exist
inside a running Anki. They belong in a `pytest-anki` suite, at roughly eight times the cost
per test, which is why the backend cases are deliberately not written against one.
"""

import sys
from typing import Any, Optional

import pytest

from anki_shared.testing import real_anki

pytest.importorskip("anki.collection", reason="the CopyAnywhere backend suite needs real anki")


# Note types the cases are written against ------------------------------------------------

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


@pytest.fixture(scope="session")
def stub_mw():
    """The single stubbed `mw` every addon module bound at import time."""
    return real_anki.install({"copy_anywhere": dict(DEFAULT_CONFIG)})


@pytest.fixture
def col(tmp_path, stub_mw):
    """A fresh real collection, with this suite's note types and decks already in it.

    One collection per test: copy definitions write into notes, and a suite that shared one
    would pin the order its tests happen to run in rather than the addon's behaviour.
    """
    collection = real_anki.open_collection(tmp_path / "collection.anki2")

    real_anki.make_note_type(collection, VOCAB, VOCAB_FIELDS, VOCAB_TEMPLATES)
    real_anki.make_note_type(collection, SENTENCE, SENTENCE_FIELDS, SENTENCE_TEMPLATES)
    real_anki.make_note_type(collection, KANJI, KANJI_FIELDS, KANJI_TEMPLATES)
    real_anki.make_note_type(collection, CLOZE, CLOZE_FIELDS, is_cloze=True)
    real_anki.make_note_type(
        collection, ODD_TEMPLATE, ODD_TEMPLATE_FIELDS, ODD_TEMPLATE_TEMPLATES
    )

    # A three-level tree, because decks.children() is recursive and include_subdecks has to
    # be shown pulling in a grandchild, not just a child.
    for deck_name in ["JP vocab", "JP vocab::10-80", "JP vocab::10-80::x", "Other"]:
        collection.decks.id(deck_name)

    previous_col = stub_mw.col
    stub_mw.col = collection
    stub_mw.progress.cancel = False
    stub_mw.addonManager.configs["copy_anywhere"] = dict(DEFAULT_CONFIG)
    try:
        yield collection
    finally:
        stub_mw.col = previous_col
        collection.close()


@pytest.fixture
def media_dir(col, monkeypatch, tmp_path):
    """Point the media helpers at a directory of this test's own.

    `write_to_media_folder` and `file_exists_in_media_folder` both go through `mw.col.media`,
    which a real collection already backs with a real directory next to the collection file,
    so this only asserts where that is rather than redirecting it.
    """
    return col.media.dir()


class RecordingLogger:
    """A `Logger` stand-in that keeps what was logged, per level.

    Several behaviours are only visible as a message -- "did not find any cards", the
    `select_card_count` complaint, an invalid field -- and asserting on the text is the only
    way to tell "skipped, benignly" from "failed".
    """

    def __init__(self, level: str = "debug") -> None:
        self.level = level
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.debugs: list[str] = []
        self.copy_definition_name: Optional[str] = None
        self.nid: Optional[int] = None

    def reset_prefix(self) -> None:
        self.copy_definition_name = None
        self.nid = None

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def has_error(self, fragment: str) -> bool:
        return any(fragment in message for message in self.errors)

    def has_debug(self, fragment: str) -> bool:
        return any(fragment in message for message in self.debugs)


@pytest.fixture
def logger():
    return RecordingLogger()


@pytest.fixture(autouse=True)
def _no_stale_modules():
    """Guard against a test importing an addon module before `mw` exists.

    The root conftest installs the stub `mw` at collection time, so this only fails if that
    ordering is ever broken -- a failure that is otherwise reported as an unrelated
    `AttributeError: 'NoneType' object has no attribute 'addonManager'`.
    """
    import aqt

    assert isinstance(aqt.mw, real_anki.StubMainWindow), (
        "the stub mw must be installed before any addon module is imported;"
        f" aqt.mw is {type(aqt.mw).__name__}"
    )
    yield


def pytest_report_header(config):
    return f"copy_anywhere backend suite: real anki collection, stubbed mw (python {sys.version.split()[0]})"
