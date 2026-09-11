"""Running-Anki mode: a real `AnkiQt` from pytest-anki, in the same process as the stub suites.

`real_anki` keeps the real `anki` package and stubs only `mw`, which reaches almost all of an
addon. What it cannot reach is whatever only exists while Anki runs: Anki's own hook objects
firing (`col.add_note()` running `note_will_be_added`, `gui_hooks` at all), `CollectionOp`
really going through `mw.taskman.with_progress`, and `mw.addonManager` really reading
config.json and meta.json off disk. For those, pytest-anki (the `pytest-anki2` distribution)
starts a real `AnkiQt` per test through its `anki_session` fixture.

pytest-anki is written for a process that runs nothing else, and this repo runs these tests
alongside several hundred stub-mode ones. The helpers here are what makes that safe; an
addon's `test_anki/conftest.py` turns them into fixtures (`copy_anywhere/test_anki/conftest.py`
is the worked example, and README.md has the template). Five things need handling:

1. **Rebinding `mw`.** Addon modules do `from aqt import mw` at import time, so each one holds
   whichever object was on `aqt` when the root conftest imported it -- the stub. A running
   `AnkiQt` on `aqt.mw` is invisible to them, so `main_window()` repoints every module of the
   named packages at it with `real_anki.rebind_mw`. It has to be undone afterwards:
   pytest-anki tears the main window down but leaves the dead `AnkiQt` on `aqt.mw`, so
   without `stub_mw_restored()` every later test talks to a destroyed window.

2. **Restoring the hook lists.** `_hooks` is a *class* attribute on each hook object, and an
   addon's init function appends to it permanently. `col.add_note()` fires
   `note_will_be_added` in every suite, so a leaked handler would silently run inside the
   stub-mode tests. pytest-anki's teardown only clears the legacy `anki.hooks._hooks` dict,
   so `stub_mw_restored()` snapshots and restores the hook objects it is given.

3. **Silencing the deck browser.** Every op that reports `OpChanges` makes Anki redraw the
   deck browser through a webview, which in a headless run is both slow and pointless;
   `main_window()` stubs `DeckBrowser.refresh` and `_DeckBrowser__renderPage` out for the
   duration.

4. **Keeping audio out of it.** Every profile load starts an mpv process whether or not
   anything plays, and its occasional start-up timeout turned into a teardown error in
   whichever test was running. `main_window()` stubs `AnkiQt.setup_sound` and
   `cleanup_sound`, so no test gets a player.

5. **Letting the main window go quiet before it is destroyed.** A `CollectionOp` a test
   started is still on a background thread when the test body ends, and Anki keeps repeating
   timers on `mw`; both outlive the test and land on a half-torn-down or already-restored
   `mw`, where they surface as errors in an unrelated test. `main_window()` waits for the
   background ops and stops the timers.

Two more pieces live elsewhere because they are process-wide rather than per-test:
`real_anki.qt_offscreen()` keeps QtWebEngine off the GPU, and `pytest_plugin`'s shutdown
guard stops the process crashing on the way out once a real Anki has run in it.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator, Sequence

import aqt
from aqt.qt import QTimer

from . import real_anki

if TYPE_CHECKING:
    from aqt.main import AnkiQt
    from pytest_anki import AnkiSession

# How long to let a background op finish before tearing the profile down.
OP_DRAIN_TIMEOUT = 15000


@contextmanager
def stub_mw_restored(
    packages: Sequence[str], hooks: Sequence[Any]
) -> Iterator[real_anki.StubMainWindow]:
    """Put `aqt.mw`, the `mw` in every module of `packages`, and `hooks` back on exit.

    Enter it from an **autouse** fixture that takes no other fixture. It must be set up
    *before* `anki_session` so that it is torn down *after* it: pytest-anki's own teardown
    reads `aqt.mw` -- and would find a stub with no `.app` if this restored it too early --
    and then leaves the destroyed `AnkiQt` in place, which is what the restore repairs. A
    plain fixture would not do, because a test that lists `anki_session` before the fixture
    that depends on this one gets `anki_session` set up first.

    `hooks` are the hook objects the addon's init functions append to. A hook that gets a
    handler at *import* time of some module -- `aqt.editor` does this to
    `editor_did_load_note` -- is only safe to list if that module is imported before the
    snapshot, which a top-level import in the test module guarantees.

    The stub is captured here rather than rebuilt afterwards because `real_anki.install()`
    only reuses a stub that is already on `aqt`; called while a real `mw` is installed, it
    builds a second stub and evicts the running Anki.
    """
    stub = aqt.mw
    assert isinstance(stub, real_anki.StubMainWindow), (
        "a real-Anki test has to start from the stub the rest of the suite was imported"
        f" against; aqt.mw is {type(stub).__name__}"
    )
    saved_hooks = [(hook, list(hook._hooks)) for hook in hooks]
    try:
        yield stub
    finally:
        for hook, callbacks in saved_hooks:
            hook._hooks[:] = callbacks
        aqt.mw = stub
        real_anki.rebind_mw(stub, list(packages))


@contextmanager
def main_window(
    anki_session: "AnkiSession",
    packages: Sequence[str],
    drain_timeout: int = OP_DRAIN_TIMEOUT,
) -> Iterator["AnkiQt"]:
    """The session's running Anki with its profile loaded, and `packages` pointed at it.

    The body of the `with` block -- a fixture's note-type setup and its own `yield` -- runs
    inside the loaded profile. Leaving the block waits for any `CollectionOp` still running
    before the profile closes.
    """
    mw = anki_session.mw

    # Redrawing the deck browser means a webview render per `OpChanges`, and one during
    # profile load, so this goes on before the profile opens.
    deck_browser_class = type(mw.deckBrowser)
    original_refresh = deck_browser_class.refresh
    original_render = deck_browser_class._DeckBrowser__renderPage
    deck_browser_class.refresh = lambda self: None
    deck_browser_class._DeckBrowser__renderPage = lambda self, *args, **kwargs: None

    # Loading a profile starts an mpv process for audio and unloading it stops that again,
    # whether or not anything ever plays. Under test the start sometimes times out ("mpv
    # timed out, restarting"), and closing the half-started player's socket then fails the
    # teardown of whichever test was running with "The handle is invalid". No test here
    # plays sound, so the profile never gets a player.
    main_window_class = type(mw)
    original_setup_sound = main_window_class.setup_sound
    original_cleanup_sound = main_window_class.cleanup_sound
    main_window_class.setup_sound = lambda self: None
    main_window_class.cleanup_sound = lambda self: None

    real_anki.rebind_mw(mw, list(packages))
    try:
        with anki_session.profile_loaded():
            yield mw
            # A `CollectionOp` a test started may still be on its background thread. Closing
            # the profile out from under one makes it fail with "target undo op not found"
            # from the collection it was midway through writing to, which is reported as an
            # error in the fixture's teardown rather than in the test that started it.
            anki_session.qtbot.waitUntil(
                lambda: mw._background_op_count == 0, timeout=drain_timeout
            )
    finally:
        deck_browser_class.refresh = original_refresh
        deck_browser_class._DeckBrowser__renderPage = original_render
        main_window_class.setup_sound = original_setup_sound
        main_window_class.cleanup_sound = original_cleanup_sound
        # Anki keeps a repeating two-second timer on `mw` that re-applies the theme by
        # reading `aqt.mw.pm.theme()`. It can fire once more after this session is gone, by
        # which point `aqt.mw` is the stub again -- and `StubProfileManager` has no
        # `theme()`, so the next test inherits an AttributeError out of the Qt event loop.
        for timer in mw.findChildren(QTimer):
            timer.stop()


@contextmanager
def addon_config(
    anki_session: "AnkiSession", package: str, base_config: dict[str, Any]
) -> Iterator[Callable[..., None]]:
    """A `write(**overrides)` that puts a real config.json/meta.json pair on disk for `package`.

    `base_config` becomes the shipped config.json and `base_config` updated with the
    overrides becomes the user's meta.json. `AddonManager.getConfig` reads both files on
    every call and merges them, with no caching, so what `write` puts there is what the
    addon's next config load sees. Every pair written is removed on exit.
    """
    contexts = []

    def write(**overrides: Any) -> None:
        config = dict(base_config)
        config.update(overrides)
        context = anki_session.addon_config_created(
            package_name=package,
            default_config=dict(base_config),
            user_config=config,
        )
        context.__enter__()
        contexts.append(context)

    try:
        yield write
    finally:
        for context in reversed(contexts):
            context.__exit__(None, None, None)
