# `anki_shared/testing` — running addon code without Anki

An addon module cannot even be imported unless `anki` and `aqt` resolve and `aqt.mw`
answers something at import time: `configuration.py` does
`mw.addonManager.addonFromModule(__name__)` at module scope, and a dozen modules do
`from aqt import mw` at line 1. This package exists so a test can get past that, in one of
three modes.

Pick the cheapest mode that can answer the question.

| mode | module | what is real | what is stubbed | cost per test | use it for |
|---|---|---|---|---|---|
| stand-in | `anki_stubs` | nothing | all of `anki` and `aqt` | ~0 | pure functions that only pass notes around |
| real collection | `real_anki` | `anki`, a real `Collection` | `aqt.mw` only | ~10ms | anything that stores a value, runs a search, or writes |
| running Anki | `running_anki` + pytest-anki | a running `AnkiQt` | audio, deck-browser redraws | ~0.5s | hook registration, `CollectionOp`, the real `addonManager` |

The root `conftest.py` chooses between the first two automatically: it prefers `real_anki`
and falls back to `anki_stubs` when `anki`/`aqt`/PyQt6 are not installed. Tests do not opt
in. The third mode lives in its own directory per addon (`<addon>/test_anki/`), described
further down. All three run in one process, in one command.

## Installing what the tests need

Into the interpreter the tests run on, which is also the one to run `python -m mypy` with
and to point your editor at, since all of them read `anki` and `aqt` out of it:

```bash
python -m pip install -r requirements-dev.txt
python -m pip install --no-deps -r requirements-dev-nodeps.txt
```

The second step is `pytest-anki2` on its own. Its metadata pins `pytest-qt~=4.4.0`, and
its `anki-XXXX` extras pin anki/aqt to a line's first release, so installing it normally
would downgrade what the first step put in. `pip check` goes on reporting the pytest-qt pin
afterwards; that is expected. Without `pytest-anki2` every test that needs `anki_session`
is reported as skipped, with the install command as its reason, and everything else runs.

`pytest-xdist` is optional and works: `python -m pytest -n 4`.

## Running the suites

```bash
python -m pytest -q                              # everything in testpaths
python -m pytest -q copy_anywhere/test           # one suite
python -m pytest -q copy_anywhere/test_anki      # the running-Anki suite alone
python -m pytest -q path/to/test_file.py::TestClass::test_name
```

A fresh clone does not need `python build.py link` first. Addon code imports shared code
through `<addon>/shared/`, which only exists once `build.py` has materialised it, so where
it is missing the root conftest registers `<addon>.shared` over `anki_shared/` itself.
That view lets every shared package resolve, not just the declared ones;
`python build.py check` is what catches an undeclared import.

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

### Writing a test that uses a collection

`copy_anywhere/test/conftest.py` is the worked example: a fresh collection per test with a
handful of note types and decks, a `RecordingLogger`, and a `media_dir`. Copy its shape
rather than reinventing it.

One rule that trips people up: **nothing below `copy_fields()` writes to the database.**
Those layers mutate the note objects they are handed and expect the caller to run
`update_notes`. Assert on `copied_into_notes` and on the objects the code returns, never on
a re-fetched `col.get_note()` — that will read the unmodified row and the test will fail
for a reason that has nothing to do with the behaviour under test. `copy_fields` itself is
the one layer where re-fetching is the right assertion.

## `running_anki` — a real `AnkiQt`, in the same process

pytest-anki's `anki_session` fixture starts a real Anki per test. It is written for a
process that runs nothing else, and here it shares one with the stub-`mw` suites, so
`running_anki` supplies what makes that safe. Its module docstring explains each piece;
in short:

- `stub_mw_restored(packages, hooks)` — puts `aqt.mw`, every addon module's `mw`, and the
  given hook lists back afterwards. `_hooks` is a *class* attribute, so a handler an init
  function attached would otherwise fire inside every other suite's `col.add_note()`.
- `main_window(anki_session, packages)` — loads the profile, points the addon's modules at
  the real `mw`, silences the deck browser's webview redraws, keeps the profile from
  starting an mpv audio player, and on exit waits for background ops and stops `mw`'s
  repeating timers.
- `addon_config(anki_session, package, base_config)` — a `write(**overrides)` that puts a
  real config.json/meta.json pair where the real `AddonManager` reads them.
- `media_servers_waited_for()` — makes each main window's `mediaServer.getPort()` wait for
  its own server, with a 15s bound. aqt keeps the readiness `Event` on the `MediaServer`
  class, so once one test's server had come up, a later test could ask for its port before
  its own server existed ("'MediaServer' object has no attribute 'server'"). You do not
  call this one: the repo plugin wraps every test that uses `anki_session` in it, because
  it has to be in place before `anki_session` builds the main window.

`anki_shared/test_anki/` tests the harness itself, forcing each race it guards against to
lose every time so that a fix that stops working fails there rather than as a flake.

### Adding running-Anki tests to another addon

1. Create `<addon>/test_anki/` with an empty `__init__.py`. The name matters: `build.py`
   excludes `test_anki` from the released package.
2. Add the path to `testpaths` in `pytest.ini`.
3. Write its `conftest.py` over the shared helpers. `copy_anywhere/test_anki/conftest.py`
   is the worked example; the shape is:

```python
import pytest
from aqt.gui_hooks import reviewer_did_answer_card  # the hooks your init functions touch

from anki_shared.testing import running_anki

ADDON_PACKAGE = "my_addon"
REBOUND_PACKAGES = [ADDON_PACKAGE, "anki_shared"]
HOOKS = [reviewer_did_answer_card]
BASE_CONFIG = {...}  # what the addon's config.json ships


@pytest.fixture(autouse=True)  # autouse and no arguments: it must wrap anki_session
def restore_stub_mw():
    with running_anki.stub_mw_restored(REBOUND_PACKAGES, HOOKS) as stub:
        yield stub


@pytest.fixture
def real_mw(anki_session, restore_stub_mw):
    with running_anki.main_window(anki_session, REBOUND_PACKAGES) as mw:
        # per-test setup inside the loaded profile goes here
        yield mw


@pytest.fixture
def addon_config(anki_session):
    with running_anki.addon_config(anki_session, ADDON_PACKAGE, BASE_CONFIG) as write:
        yield write
```

`restore_stub_mw` has to be autouse. It must be set up before `anki_session` so that it is
torn down after it, and a test that happens to list `anki_session` before `real_mw` would
otherwise get the opposite order.

Do not guard the conftest with `pytest.importorskip("pytest_anki")`. A conftest under
`testpaths` is loaded while pytest is still parsing its arguments, where a skip aborts the
whole run instead of skipping anything. The repo plugin already marks every test that
needs `anki_session` as skipped when pytest-anki2 is missing.

## The shutdown guard

Once a real Anki has run in a process, that process crashes on the way out: PyQt's own
`atexit` handler destroys the `QApplication`, which has had QtWebEngine inside it, and dies
with an access violation. Every test has passed and the summary is already printed, but
the exit status is 139. It is not any addon's doing: it is pytest-anki's application being
torn down by PyQt, which a test process has no need for.

`pytest_plugin.py`, registered from the root conftest, unregisters that one handler at the
end of any run in which a `QApplication` exists. Nothing else about exiting changes: the
exit status is still pytest's own (1 for a failure, 2 for an interrupt or collection error,
5 for nothing collected), pytest's temporary directories are still cleaned up, and
`pytest.main()` callers and xdist workers are unaffected. A stub-only run creates no
`QApplication` and is left alone entirely.

It does not use `os._exit`, the obvious alternative, because that would skip every other
`atexit` handler too — including pytest's own, which remove this run's lock on its
`pytest-of-<user>/pytest-N` directory and prune old ones, so every run would leave ~90 MB
behind for three days. `os._exit` remains only as the fallback for a future PyQt whose
handler it cannot find.

`ANKI_TEST_SHUTDOWN_GUARD=0` switches the guard off, to see the crash or debug Qt's own
shutdown; `ANKI_TEST_SHUTDOWN_GUARD=exit` forces the `os._exit` fallback.

On Linux the default does not hold: with PyQt's handler unregistered the process still
exits 139, in four runs out of four, and with the guard off it did in two of four. `ANKI_TEST_SHUTDOWN_GUARD=exit` is the mode that exits cleanly
there, at the cost of the leftover temporary directories described above.

Separately, `real_anki.qt_offscreen()` adds `--disable-gpu` to
`QTWEBENGINE_CHROMIUM_FLAGS`. Offscreen, QtWebEngine's GPU process kept losing its context,
and about one run in five died mid-test on it.

## Cloud sessions

A Claude Code cloud session starts from a fresh clone on a stock Ubuntu image, as root.
Left alone, `python -m pytest` there finds no `anki` and runs every suite against the
stand-in, so the real-collection and running-Anki tests say nothing. Two things close that.

`.claude/hooks/session-start.sh` runs when a cloud session starts or resumes, and exits at
once anywhere else. It creates a venv at `/opt/anki-venv` (hardcoded, so a setup script can
build the same one ahead of time) and installs `requirements-dev*.txt` and
`japanese_note_ai_ops/requirements.txt` into it. It also installs `libegl1` if the image
lacks it, checks out the submodule if it never was, runs `build.py link`, and puts the venv
first on the session's `PATH`.

The cloud environment's settings supply the rest. Three environment variables:

| variable | why |
| --- | --- |
| `QTWEBENGINE_DISABLE_SANDBOX=1` | QtWebEngine will not start its sandbox as root: the first running-Anki test ends the process with "Running as root without --no-sandbox is not supported" |
| `ANKI_TEST_SHUTDOWN_GUARD=exit` | on Linux the default guard mode exits 139; see the shutdown guard above |
| `PYTHONIOENCODING=utf-8` | the jp_text_processing suite prints Japanese |

A setup script is optional: the hook installs everything itself. But a setup script's result
is cached and the hook's is not guaranteed to be, so building the venv there spares later
sessions the install. It must use the hook's venv path, and should only read the clone, not
check out the submodule or link: a cached checkout could reach a later session on another
branch with the submodule at the wrong commit.

```bash
#!/bin/bash
set -euo pipefail
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends libegl1
"$(command -v python3.10 || command -v python3)" -m venv /opt/anki-venv
REPO=...  # where the environment clones this repository
if [ -f "$REPO/requirements-dev.txt" ]; then  # the clone may not exist yet
  cd "$REPO"
  /opt/anki-venv/bin/python -m pip install -q \
    -r requirements-dev.txt -r japanese_note_ai_ops/requirements.txt
  /opt/anki-venv/bin/python -m pip install -q --no-deps -r requirements-dev-nodeps.txt
fi
```

The word-array tests in `japanese_note_ai_ops/test` still skip: they need the Sudachi
dictionary and JMdict in that addon's `user_files/`, and JMdict comes from
`www.edrdg.org`, which the default network access level does not allow.
