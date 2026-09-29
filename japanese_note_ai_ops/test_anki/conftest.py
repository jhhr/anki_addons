"""Fixtures for the run's write path in a real, running Anki (issue #11, stage 5).

What the stub-`mw` suites cannot reach: a `CollectionOp` really going through
`mw.taskman.with_progress`, the add hook CopyAnywhere's `init_note_hooks()` really attaches to
`note_will_be_added`, both addons' configs read by the real `AddonManager` off disk, and the
undo queue of a running Anki. The machinery that makes a real Anki safe to run in the same
process as the stub suites is `anki_shared/testing/running_anki.py`'s; what is here is which
packages hold a `mw`, which hooks CopyAnywhere appends to, and the note type the tests use.
"""

import sys
from typing import Any, Iterator

import pytest
from anki.hooks import note_will_be_added
from aqt.gui_hooks import editor_did_load_note, editor_did_unfocus_field, reviewer_did_answer_card

from anki_shared.testing import real_anki, running_anki
from copy_anywhere import logging_setup
from japanese_note_ai_ops.async_api_ops.match_words_to_notes import MATCH_FIELD_KEYS

PACKAGE = "japanese_note_ai_ops"
COPY_ANYWHERE = "copy_anywhere"
REBOUND_PACKAGES = [PACKAGE, COPY_ANYWHERE, "anki_shared"]
# What CopyAnywhere's init_note_hooks() appends to; process-global, so put back after
HOOKS = [note_will_be_added, editor_did_load_note, editor_did_unfocus_field, reviewer_did_answer_card]

# The note type the addon hardcodes, its match fields named after their config keys, and a
# field for CopyAnywhere to fill
NOTETYPE = "Japanese vocab note"
FIELDS = {key: key.removesuffix("_field") for key in MATCH_FIELD_KEYS}
COPIED = "copied"

BASE_CONFIG: dict[str, Any] = {
    NOTETYPE: dict(FIELDS, insert_deck="Default"),
    "capture_calls": True,
    "capture_notes": True,
    "log_to_console": False,
    "mdx_filenames": [],
}
COPY_ANYWHERE_BASE: dict[str, Any] = {
    "log_level": "error",
    "copy_fields_shortcut": "",
    "copy_definitions": [],
}


@pytest.fixture(autouse=True)
def operation_logs(tmp_path, monkeypatch):
    """CopyAnywhere's operation logs, out of the addon's own user_files."""
    directory = tmp_path / "copy_anywhere_logs"
    monkeypatch.setattr(logging_setup, "logs_dir", lambda: str(directory))
    yield directory
    logging_setup.reset_operation_log()


@pytest.fixture(autouse=True)
def restore_stub_mw() -> Iterator[Any]:
    """Autouse and taking no other fixture, so it is torn down after `anki_session`."""
    with running_anki.stub_mw_restored(REBOUND_PACKAGES, HOOKS) as stub:
        yield stub


@pytest.fixture
def anki_mw(anki_session, restore_stub_mw) -> Iterator[Any]:
    """A running Anki with a fresh profile, every addon module pointed at its `mw`."""
    with running_anki.main_window(anki_session, REBOUND_PACKAGES) as mw:
        yield mw


@pytest.fixture
def real_mw(anki_mw) -> Iterator[Any]:
    """`anki_mw` with the tests' note type in it."""
    real_anki.make_note_type(anki_mw.col, NOTETYPE, [*FIELDS.values(), COPIED])
    yield anki_mw


@pytest.fixture
def config(anki_session):
    """`config(**overrides)` writes japanese_note_ai_ops' config.json and meta.json."""
    with running_anki.addon_config(anki_session, PACKAGE, BASE_CONFIG) as write:
        yield write


@pytest.fixture
def copy_anywhere_config(anki_session):
    """`copy_anywhere_config(copy_definitions=[...])` writes CopyAnywhere's."""
    with running_anki.addon_config(anki_session, COPY_ANYWHERE, COPY_ANYWHERE_BASE) as write:
        yield write


def pytest_report_header(config):
    return (
        f"japanese_note_ai_ops real-Anki suite: a running AnkiQt"
        f" (python {sys.version.split()[0]})"
    )
