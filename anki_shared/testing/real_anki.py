"""Real-collection mode: the real `anki` package, a real collection, a stubbed `mw`.

The other mode (`anki_stubs.py`) puts a fake Anki in sys.modules. That is enough for code
that only carries notes around, but not for anything that stores a field value or runs a
search: its `Note.__setitem__` does nothing and there is no database behind it. Copy
definitions need both, so this mode keeps the real `anki` package and stubs only the one
thing that genuinely cannot exist outside a running Anki -- `aqt.mw`.

A real `Collection` opens headless in well under a second and needs no Qt and no GUI, so
this is not the expensive option; the expensive option is driving a real `AnkiQt`, which is
what the hook tests need and these tests deliberately avoid.

Three things bite, in this order, and `install()` handles all three:

1. `import anki.cards` first hits a circular import (`anki.cards` -> `anki.collection` ->
   `anki.latex` -> `anki.hooks` -> `anki.cards`) that only resolves inside a running Anki.
   Importing `anki.collection` first breaks the cycle, so that is done here before anything
   an addon imports can get there.
2. Addon modules read `mw` at *import* time -- `configuration.py` does
   `tag = mw.addonManager.addonFromModule(__name__)` at module scope -- so the stub `mw`
   must be on `aqt` before any addon module is imported, and `getConfig` must return a real
   dict. A `MagicMock` default there leaves `Config.data` unusable.
3. `mw.progress.want_cancel()` must return a real `False`. A bare `MagicMock` is truthy, so
   a bulk loop reads it as "the user cancelled".

Qt is imported for real, because addon UI modules subclass real Qt widget classes and
branch on `qtmajor > 5`, which a mock cannot answer. Point `qt_api = pyqt6` at the right
binding in the pytest ini or `PyQt6.QtCore` fails to load its DLLs.
"""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Union
from unittest.mock import MagicMock

# Resolve the circular import before anything else can trip over it. Every other `anki`
# import in the process is fine once this one has completed.
import anki.collection  # noqa: F401  isort:skip

from anki.collection import Collection
from anki.notes import Note

if TYPE_CHECKING:
    from aqt.main import AnkiQt


def is_available() -> bool:
    """Whether the real `anki` package -- rather than a stand-in -- is importable."""
    return not getattr(sys.modules.get("anki"), "_is_addon_test_stub", False)


class StubProgress:
    """`mw.progress`, reduced to what a background op asks of it.

    `want_cancel` is a real bool rather than a mock because the bulk loop treats any truthy
    answer as "cancelled"; `set_cancel(True)` is how a test drives that branch.
    """

    def __init__(self) -> None:
        self.cancel = False
        self.updates: list[dict] = []
        self.titles: list[str] = []
        self.finish_count = 0

    def want_cancel(self) -> bool:
        return self.cancel

    def set_cancel(self, value: bool) -> None:
        self.cancel = value

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    def set_title(self, title: str) -> None:
        self.titles.append(title)

    def start(self, *args, **kwargs) -> None:
        pass

    def finish(self) -> None:
        self.finish_count += 1


class StubTaskman:
    """`mw.taskman`, running everything inline: the tests are single-threaded."""

    def run_on_main(self, callback: Callable[[], Any]) -> None:
        callback()

    def run_in_background(self, task, on_done=None, **kwargs):
        result = task()
        if on_done is not None:
            on_done(result)
        return result


class StubAddonManager:
    """`mw.addonManager`, backed by a plain dict per addon.

    Addon modules resolve their config key with `addonFromModule(__name__)`, so the key is
    the first dotted segment of the module name -- the addon's directory name.
    """

    def __init__(self, configs: Optional[dict[str, dict]] = None) -> None:
        self.configs: dict[str, dict] = configs or {}
        self.written: list[tuple[str, dict]] = []

    def addonFromModule(self, module: str) -> str:
        return module.split(".")[0]

    def getConfig(self, tag: str) -> dict:
        return self.configs.setdefault(tag, {})

    def writeConfig(self, tag: str, data: dict) -> None:
        self.configs[tag] = data
        self.written.append((tag, data))

    def setConfigUpdatedAction(self, module: str, action) -> None:
        pass


class StubProfileManager:
    """`mw.pm`, which addons reach through for the profile folder.

    The media helpers and the fonts-check process build their paths as
    `Path(mw.pm.profileFolder(), "collection.media")` rather than going through
    `col.media`, so a test that writes a file needs this to point somewhere real.
    """

    def __init__(self, profile_folder: Optional[Union[str, Path]] = None) -> None:
        self._profile_folder = Path(profile_folder) if profile_folder else None

    def set_profile_folder(self, path: Union[str, Path]) -> Path:
        """Point at `path`, creating it and its collection.media alongside."""
        self._profile_folder = Path(path)
        (self._profile_folder / "collection.media").mkdir(parents=True, exist_ok=True)
        return self._profile_folder

    def profileFolder(self) -> str:
        assert self._profile_folder is not None, "no profile folder set on the stub mw.pm"
        return str(self._profile_folder)

    def media_folder(self) -> Path:
        return Path(self.profileFolder()) / "collection.media"


class StubMainWindow:
    """The `mw` an addon reaches for, with a real collection behind `col`.

    `col` starts as a permissive mock rather than `None` so that a test which never opens a
    collection can still import and exercise code that merely reaches through `mw.col` for
    a name. Assign a real `Collection` to it -- what the `anki_collection` fixture does --
    for anything that stores a field value or runs a search.
    """

    def __init__(
        self,
        col: Optional[Collection] = None,
        configs: Optional[dict[str, dict]] = None,
    ) -> None:
        self.col: Any = col if col is not None else MagicMock()
        self.progress = StubProgress()
        self.taskman = StubTaskman()
        self.addonManager = StubAddonManager(configs)
        self.pm = StubProfileManager()

    def reset(self) -> None:
        pass


def install(configs: Optional[dict[str, dict]] = None) -> StubMainWindow:
    """Put a stub `mw` on `aqt` and return it. Safe to call more than once.

    Call this before importing any addon module, or `configuration.py` reads `mw` at import
    time and finds `None`. Calling it again keeps the same object so that the `from aqt
    import mw` already bound inside addon modules stays valid; only its contents are reset.
    """
    import aqt

    existing = getattr(aqt, "mw", None)
    if isinstance(existing, StubMainWindow):
        if configs is not None:
            existing.addonManager.configs = configs
        return existing

    stub = StubMainWindow(configs=configs)
    # `aqt.mw` is declared as the real `AnkiQt`; installing a stand-in is the whole point.
    setattr(aqt, "mw", stub)
    return stub


def rebind_mw(stub: Union["StubMainWindow", "AnkiQt"], package_names: list[str]) -> None:
    """Point every already-imported module of these packages at `stub`.

    `stub` is the stub main window outside a real-Anki test and the running `AnkiQt` inside
    one -- `running_anki.main_window` rebinds the real thing through here.

    Addon modules do `from aqt import mw`, which binds the object that was on `aqt` at
    import time. Anything that replaces `mw` afterwards -- a new collection, a new Anki
    session -- has to reach into each module that holds the old one, `configuration.py`
    included, or half the addon keeps talking to a torn-down main window.
    """
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] not in package_names:
            continue
        if getattr(module, "mw", None) is not None:
            setattr(module, "mw", stub)


def open_collection(path: Union[str, Path]) -> Collection:
    """Open a real collection at `path`, headless."""
    return Collection(str(path))


def add_note(
    col: Collection,
    note_type_name: str,
    fields: dict[str, str],
    deck_name: str = "Default",
    tags: Optional[list[str]] = None,
) -> Note:
    """Add a note of `note_type_name` into `deck_name` and return it, freshly loaded.

    The note is re-fetched after adding so that its id and cards are the ones the database
    holds, which is what the code under test will see.
    """
    model = col.models.by_name(note_type_name)
    assert model is not None, f"No note type named {note_type_name!r}"
    note = col.new_note(model)
    for field_name, value in fields.items():
        note[field_name] = value
    if tags:
        note.tags = list(tags)
    deck_id = col.decks.id(deck_name)
    assert deck_id is not None
    col.add_note(note, deck_id)
    return col.get_note(note.id)


def add_revlog(
    col: Collection,
    card_id: int,
    count: int = 1,
    ease: int = 3,
    ivl: int = 10,
    factor: int = 2500,
    review_type: int = 1,
    taken_ms: int = 1000,
    first_id: Optional[int] = None,
) -> list[int]:
    """Write `count` review rows for a card and return their ids.

    Review history is a plain table, and several card values (`__Card_Last_Reps`,
    `Least_reps` selection, the review-time formatting) read it directly, so tests that care
    about those need real rows rather than a mocked `db`. The row id is the review's
    epoch-millisecond timestamp and is what `ORDER BY id` sorts on, so ids are handed out in
    ascending order from `first_id`.
    """
    assert col.db is not None
    base = first_id if first_id is not None else card_id
    ids = []
    for i in range(count):
        rid = base + i
        col.db.execute(
            "INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rid,
            card_id,
            -1,
            ease,
            ivl,
            ivl,
            factor,
            taken_ms,
            review_type,
        )
        ids.append(rid)
    return ids


def set_custom_data(col: Collection, card_id: int, custom_data: str) -> None:
    """Set a card's `custom_data` JSON string and save it."""
    card = col.get_card(card_id)  # type: ignore[arg-type]
    card.custom_data = custom_data
    col.update_card(card)


def counting_wrapper(obj: Any, method_name: str) -> Callable[[], int]:
    """Replace `obj.method_name` with a counting passthrough; return a call counter.

    Cache behaviour is only observable as "how many times did the expensive thing run", so
    the assertion has to be on a counter rather than on the answer -- a cache that never
    hits still returns the right result.
    """
    original = getattr(obj, method_name)
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    setattr(obj, method_name, wrapper)
    return lambda: calls["n"]


def make_note_type(
    col: Collection,
    name: str,
    field_names: list[str],
    templates: Optional[list[tuple[str, str, str]]] = None,
    is_cloze: bool = False,
) -> dict:
    """Create and save a note type. `templates` are (name, question, answer) triples.

    A cloze type is a standard one with `type` set to MODEL_CLOZE and exactly one template
    -- there is no `new_cloze` on ModelManager any more -- and its cards are keyed by
    ordinal rather than by template name, which is the reason to have one in a suite at all.
    """
    from anki.consts import MODEL_CLOZE

    models = col.models
    model = models.new(name)
    if is_cloze:
        model["type"] = MODEL_CLOZE
    for field_name in field_names:
        models.add_field(model, models.new_field(field_name))

    if is_cloze:
        templates = templates or [
            ("Cloze", "{{cloze:%s}}" % field_names[0], "{{cloze:%s}}" % field_names[0])
        ]
    elif not templates:
        templates = [
            (
                "Card 1",
                "{{%s}}" % field_names[0],
                "{{FrontSide}}<hr id=answer>{{%s}}" % field_names[-1],
            )
        ]
    for template_name, question, answer in templates:
        template = models.new_template(template_name)
        template["qfmt"] = question
        template["afmt"] = answer
        models.add_template(model, template)

    models.add(model)
    saved = models.by_name(name)
    assert saved is not None
    return saved


def qt_offscreen() -> None:
    """Make Qt headless: no window from importing aqt, and no GPU for QtWebEngine.

    Only the running-Anki tests ever start QtWebEngine, but Chromium reads its flags once per
    process, when QtWebEngine first starts, so they belong with the rest of the process-wide
    Qt setup rather than with the tests that need them. Offscreen, its GPU process keeps
    losing its context ("RasterDecoderImpl: Context lost during MakeCurrent"), and about one
    run in five died mid-test on a breakpoint exception in that code. Software rendering is
    all a test needs.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--disable-gpu" not in flags.split():
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{flags} --disable-gpu".strip()
