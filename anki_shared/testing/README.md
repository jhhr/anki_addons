# `anki_shared/testing` — running addon code without Anki

An addon module cannot even be imported unless `anki` and `aqt` resolve and `aqt.mw`
answers something at import time: `configuration.py` does
`mw.addonManager.addonFromModule(__name__)` at module scope, and a dozen modules do
`from aqt import mw` at line 1. This package exists so a test can get past that, in one of
three modes.

Pick the cheapest mode that can answer the question.

| mode | what is real | what is stubbed | cost per test | use it for |
|---|---|---|---|---|
| `anki_stubs` | nothing | all of `anki` and `aqt` | ~0 | pure functions that only pass notes around |
| `real_anki` | `anki`, a real `Collection` | `aqt.mw` only | ~10ms | anything that stores a value, runs a search, or writes |
| `pytest-anki` | a running `AnkiQt` | nothing | ~1s + teardown | hook registration, `CollectionOp`, the real `addonManager` |

The root `conftest.py` chooses between the first two automatically: it prefers `real_anki`
and falls back to `anki_stubs` when `anki`/`aqt`/PyQt6 are not installed. Tests do not opt
in. The third mode is a separate directory, described at the bottom.

## `anki_stubs` — the stand-in

A fake `anki` and `aqt` installed into `sys.modules` behind a meta-path finder. Needs
nothing installed. Its `Note` is inert: `note["Field"] = x` stores nothing and there is no
database, so any test that reads back what it wrote will pass for the wrong reason or fail
confusingly. That is the whole reason the second mode exists.

## `real_anki` — a real collection behind a stubbed `mw`

The real `anki` package with a real `Collection`, and a `StubMainWindow` on `aqt.mw`
carrying the five attributes addon code reaches for: `.col`, `.progress`, `.taskman`,
`.addonManager`, `.pm`. A collection opens headless in well under a second and needs no Qt
and no GUI, so this is not the expensive option.

```python
from anki_shared.testing import real_anki

stub = real_anki.install()          # idempotent; keeps object identity
col = real_anki.open_collection(tmp_path / "collection.anki2")
stub.col = col
note = real_anki.add_note(col, "Basic", {"Front": "a", "Back": "b"})
```

Helpers worth knowing before writing your own: `add_note`, `add_revlog`, `set_custom_data`
(the `cd` → `fc` sync flag), `make_note_type`, `counting_wrapper` (wrap a method, get a
call counter), `qt_offscreen`, and `rebind_mw`.

### The three things that bite

1. **The circular import.** `import anki.cards` first hits
   `anki.cards → anki.collection → anki.latex → anki.hooks → anki.cards`, which only
   resolves inside a running Anki. `real_anki` imports `anki.collection` before anything
   else can get there. This is why its first import is `import anki.collection  # isort:skip`
   and why that line must stay first.
2. **`from aqt import mw` binds at import time.** Every module holds whichever object was
   on `aqt` when it was imported. Replacing `aqt.mw` afterwards — a new collection, a new
   session — reaches none of them. `rebind_mw(mw, ["copy_anywhere", "anki_shared"])` walks
   `sys.modules` and repoints them all. Anything that swaps the main window must call it,
   and must call it again to put the old one back.
3. **`qt_api = pyqt6` in `pytest.ini` is load-bearing.** `aqt` subclasses real Qt widgets
   and branches on `qtmajor`, so Qt has to genuinely import. Without naming the binding,
   `PyQt6.QtCore` fails to load its DLLs under `pytest-qt` and every test errors at
   collection.

## Running the suites

```bash
python -m pytest -q                                        # the default suite
python -m pytest -q copy_anywhere/test_anki -p no:cacheprovider   # the real-Anki suite
```

The real-Anki suite is deliberately **not** in `testpaths`. Its tests pass, but QtWebEngine
segfaults during interpreter shutdown — after pytest prints its summary — so including it
would make a green run report a crash. It needs `pytest-anki2` installed
(`pip install pytest-anki2 --no-deps`) and skips itself when that is missing.

Its `conftest.py` is where the cross-mode hazards are handled: `mw` is rebound onto every
addon module and back again, and the hook lists are snapshotted and restored, because
`_hooks` is a *class* attribute and a leaked `note_will_be_added` handler would fire inside
every other suite's `col.add_note()`. Read that file's docstring before adding to it.

## Writing a test that uses a collection

`copy_anywhere/test/conftest.py` is the worked example: a fresh collection per test with a
handful of note types and decks, a `RecordingLogger`, and a `media_dir`. Copy its shape
rather than reinventing it.

One rule that trips people up: **nothing below `copy_fields()` writes to the database.**
Those layers mutate the note objects they are handed and expect the caller to run
`update_notes`. Assert on `copied_into_notes` and on the objects the code returns, never on
a re-fetched `col.get_note()` — that will read the unmodified row and the test will fail
for a reason that has nothing to do with the behaviour under test. `copy_fields` itself is
the one layer where re-fetching is the right assertion.
