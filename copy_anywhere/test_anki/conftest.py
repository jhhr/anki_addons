"""Fixtures for the one CopyAnywhere suite that drives a real, running Anki.

Everything else in the suite runs against a real `Collection` behind a stubbed `mw`, which
is roughly eight times cheaper per test. What that mode cannot reach is exactly what lives
here: `init_note_hooks()` actually attaching to Anki's hook objects, `CollectionOp` really
going through `mw.taskman.with_progress`, `mw.addonManager` really reading config.json and
meta.json off disk, and the sync hooks firing from `gui_hooks`.

Four pieces of infrastructure make that safe to run in the same process as the other 786
tests:

1. **Rebinding `mw`.** Addon modules do `from aqt import mw` at import time, so each one
   holds whichever object was on `aqt` when the root conftest imported it -- the stub. A
   running `AnkiQt` on `aqt.mw` is invisible to them. `real_anki.rebind_mw` walks
   `sys.modules` and repoints every module of the named packages, and it takes any main
   window, not only a stub, so the real one goes in the same way. It has to be undone
   afterwards: `pytest_anki` tears the main window down but leaves the dead `AnkiQt` on
   `aqt.mw`, so without the restore every later test talks to a destroyed window.

2. **Restoring the hook lists.** `_hooks` is a *class* attribute on each hook object, and
   `init_note_hooks()` appends to it permanently. `col.add_note()` fires
   `note_will_be_added` in every suite, so a leaked handler would silently run inside the
   stub-mode tests. `pytest_anki`'s teardown only clears the legacy `anki.hooks._hooks`
   dict, not these, so this file snapshots and restores them itself.

3. **Silencing the deck browser.** Every op that reports `OpChanges` makes Anki redraw the
   deck browser through a webview, which in a headless run is both slow and pointless;
   `DeckBrowser.refresh` and `_DeckBrowser__renderPage` are stubbed out for the duration.

4. **Letting the main window go quiet before it is destroyed.** A `CollectionOp` a test
   started is still on a background thread when the test body ends, and Anki keeps repeating
   timers on `mw`; both outlive the test and land on a half-torn-down or already-restored
   `mw`, where they surface as errors in an unrelated test. The `real_mw` fixture waits for
   the background ops and stops the timers.

`aqt.sound`, the third thing the issue's spike warned about, needed no stubbing here: no
test in this file answers a card through the reviewer UI, which is the only path that
reaches an audio device.

**Why this directory is not in `testpaths`.** Every test here passes, but the process
segfaults during interpreter shutdown, *after* pytest has printed its summary, so the run
exits 139 with nothing failing. That is not this addon's doing: a file whose only test opens
an `anki_session` and asserts the collection exists crashes the same way on this machine,
and one full-tree run also died mid-collection in QtWebEngine's GPU code. Adding these tests
to the default run would make a green suite report a crash, so they are run on demand:

    python -m pytest -q copy_anywhere/test_anki/ -p no:cacheprovider
"""

import sys
from typing import Any, Iterator

import pytest

pytest.importorskip("pytest_anki", reason="the real-Anki suite needs pytest-anki installed")

import aqt  # noqa: E402
from anki.hooks import note_will_be_added  # noqa: E402
from aqt.qt import QTimer  # noqa: E402
from aqt.gui_hooks import (  # noqa: E402
    editor_did_load_note,
    editor_did_unfocus_field,
    reviewer_did_answer_card,
    sync_did_finish,
    sync_will_start,
)

from anki_shared.testing import real_anki  # noqa: E402

ADDON_PACKAGE = "copy_anywhere"

# The packages whose modules hold a `from aqt import mw` binding. `anki_shared` is in the
# list because the addon's `shared/` subpackage is a copy of it and several helpers there
# reach through `mw` too.
REBOUND_PACKAGES = [ADDON_PACKAGE, "anki_shared"]

# The hook objects `init_note_hooks()` and `init_sync_hook()` append to. Restoring these is
# not optional: they are process-global and the other 786 tests add notes.
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

# How long to let a background op finish before tearing the profile down.
OP_DRAIN_TIMEOUT = 15000

BASE_CONFIG: dict[str, Any] = {
    "log_level": "error",
    "copy_fields_shortcut": "Ctrl+Shift+C",
    "copy_definitions": [],
}


@pytest.fixture(autouse=True)
def restore_stub_mw() -> Iterator[Any]:
    """Put `aqt.mw`, the addon modules' `mw` and the hook lists back when the test ends.

    This is autouse and takes no other fixture on purpose: it must be set up *before*
    `anki_session` so that it is torn down *after* it. `pytest_anki`'s own teardown reads
    `aqt.mw` -- and would find a stub with no `.app` if this restored it too early -- and
    then leaves the destroyed `AnkiQt` in place, which is what the restore below repairs.

    Note that `real_anki.install()` is *not* the way to get the stub back: it only reuses
    an existing stub when one is already on `aqt`, so calling it while a real `mw` is
    installed builds a second stub and evicts the running Anki. The stub is captured here
    instead, before Anki starts.
    """
    stub = aqt.mw
    assert isinstance(stub, real_anki.StubMainWindow), (
        "the real-Anki suite has to start from the stub the rest of the suite was imported"
        f" against; aqt.mw is {type(stub).__name__}"
    )
    saved_hooks = [(hook, list(hook._hooks)) for hook in HOOKS]

    yield stub

    # `_hooks` is a class attribute, so an `init_note_hooks()` in one test would otherwise
    # stay attached for the rest of the process -- and `col.add_note()` fires
    # `note_will_be_added` in every other suite.
    for hook, callbacks in saved_hooks:
        hook._hooks[:] = callbacks
    aqt.mw = stub
    real_anki.rebind_mw(stub, REBOUND_PACKAGES)


@pytest.fixture
def real_mw(anki_session, restore_stub_mw) -> Iterator[Any]:
    """A running Anki with a loaded profile, with every addon module pointed at its `mw`."""
    mw = anki_session.mw

    # Redrawing the deck browser means a webview render per `OpChanges`, and one during
    # profile load, so this goes on before the profile opens.
    deck_browser_class = type(mw.deckBrowser)
    original_refresh = deck_browser_class.refresh
    original_render = deck_browser_class._DeckBrowser__renderPage
    deck_browser_class.refresh = lambda self: None
    deck_browser_class._DeckBrowser__renderPage = lambda self, *args, **kwargs: None

    real_anki.rebind_mw(mw, REBOUND_PACKAGES)
    try:
        with anki_session.profile_loaded():
            real_anki.make_note_type(mw.col, VOCAB, VOCAB_FIELDS)
            yield mw
            # A `CollectionOp` a test started may still be on its background thread. Closing
            # the profile out from under one makes it fail with "target undo op not found"
            # from the collection it was midway through writing to, which is reported as an
            # error in *this* fixture's teardown rather than in the test that started it.
            anki_session.qtbot.waitUntil(
                lambda: mw._background_op_count == 0, timeout=OP_DRAIN_TIMEOUT
            )
    finally:
        deck_browser_class.refresh = original_refresh
        deck_browser_class._DeckBrowser__renderPage = original_render
        # Anki keeps a repeating two-second timer on `mw` that re-applies the theme by
        # reading `aqt.mw.pm.theme()`. It can fire once more after this test's session is
        # gone, by which point `aqt.mw` is the stub again -- and `StubProfileManager` has no
        # `theme()`, so the next test inherits an AttributeError out of the Qt event loop.
        for timer in mw.findChildren(QTimer):
            timer.stop()


@pytest.fixture
def addon_config(anki_session):
    """Write a real config.json/meta.json pair for the addon and keep them for the test.

    `AddonManager.getConfig` reads both files off disk on every call and merges them, with
    no caching, so a definition written here is picked up by the next `Config().load()`.
    """
    contexts = []

    def write(**overrides) -> None:
        config = dict(BASE_CONFIG)
        config.update(overrides)
        context = anki_session.addon_config_created(
            package_name=ADDON_PACKAGE,
            default_config=dict(BASE_CONFIG),
            user_config=config,
        )
        context.__enter__()
        contexts.append(context)

    yield write

    for context in reversed(contexts):
        context.__exit__(None, None, None)


def pytest_report_header(config):
    return f"copy_anywhere real-Anki suite: a running AnkiQt (python {sys.version.split()[0]})"
