"""Fixtures for the one CopyAnywhere suite that drives a real, running Anki.

Everything else in the suite runs against a real `Collection` behind a stubbed `mw`, which
is roughly eight times cheaper per test. What that mode cannot reach is exactly what lives
here: `init_note_hooks()` actually attaching to Anki's hook objects, `CollectionOp` really
going through `mw.taskman.with_progress`, `mw.addonManager` really reading config.json and
meta.json off disk, and the sync hooks firing from `gui_hooks`.

These tests run in the same process as the stub-mode suites, and the machinery that makes
that safe -- rebinding `mw` and back, restoring the hook lists, silencing the deck browser
and the audio player, letting background ops drain -- is shared with any other addon that
wants the same, in `anki_shared/testing/running_anki.py`; its docstring explains each
piece. What is left here is what is CopyAnywhere's own: which packages hold a `mw` binding,
which hooks its init functions append to, and the note type and config its tests are
written against.

`aqt.sound`, the third thing the issue's spike warned about, turned out to matter even
though no test here answers a card through the reviewer UI: every profile load starts an
mpv process regardless, which is why `running_anki.main_window` keeps one from starting.

This directory is in `testpaths`. The process used to segfault on the way out after any
real-Anki test, with every test passed and the summary already printed; the shutdown guard
in `anki_shared/testing/pytest_plugin.py` is what stopped that, and its docstring says how.
The same plugin skips these tests when pytest-anki2 is not installed, which this file cannot
do itself with `importorskip`: as a `testpaths` conftest it is loaded before collection
starts, where a skip aborts the whole run.
"""

import sys
from typing import Any, Iterator

import pytest
from anki.hooks import note_will_be_added
from aqt.gui_hooks import (
    editor_did_load_note,
    editor_did_unfocus_field,
    reviewer_did_answer_card,
    sync_did_finish,
    sync_will_start,
)

from anki_shared.testing import real_anki, running_anki

ADDON_PACKAGE = "copy_anywhere"

# The packages whose modules hold a `from aqt import mw` binding. `anki_shared` is in the
# list because the addon's `shared/` subpackage is a copy of it and several helpers there
# reach through `mw` too.
REBOUND_PACKAGES = [ADDON_PACKAGE, "anki_shared"]

# The hook objects `init_note_hooks()` and `init_sync_hook()` append to. Restoring these is
# not optional: they are process-global and the stub-mode suites add notes.
HOOKS = [
    note_will_be_added,
    editor_did_load_note,
    editor_did_unfocus_field,
    reviewer_did_answer_card,
    sync_will_start,
    sync_did_finish,
]

# A note type simple enough that a copy definition over it is one line of setup.
VOCAB = "CA Vocab"
VOCAB_FIELDS = ["Word", "Reading", "Meaning"]

BASE_CONFIG: dict[str, Any] = {
    "log_level": "error",
    "copy_fields_shortcut": "Ctrl+Shift+C",
    "copy_definitions": [],
}


@pytest.fixture(autouse=True)
def restore_stub_mw() -> Iterator[Any]:
    """Put `aqt.mw`, the addon modules' `mw` and the hook lists back when the test ends.

    Autouse, and taking no other fixture, so that it is set up before `anki_session` and
    torn down after it -- `running_anki.stub_mw_restored` explains why that order matters.
    """
    with running_anki.stub_mw_restored(REBOUND_PACKAGES, HOOKS) as stub:
        yield stub


@pytest.fixture
def real_mw(anki_session, restore_stub_mw) -> Iterator[Any]:
    """A running Anki with a loaded profile, with every addon module pointed at its `mw`."""
    with running_anki.main_window(anki_session, REBOUND_PACKAGES) as mw:
        real_anki.make_note_type(mw.col, VOCAB, VOCAB_FIELDS)
        yield mw


@pytest.fixture
def addon_config(anki_session):
    """`addon_config(**overrides)` writes the addon's config.json/meta.json for the test."""
    with running_anki.addon_config(anki_session, ADDON_PACKAGE, BASE_CONFIG) as write:
        yield write


def pytest_report_header(config):
    return f"copy_anywhere real-Anki suite: a running AnkiQt (python {sys.version.split()[0]})"
