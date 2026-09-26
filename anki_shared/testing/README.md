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

`pytest-xdist` is optional and works: `python -m pytest -n 4`. The plugin sends a parallel
run to `--dist loadfile` when no `--dist` was given, so each file's tests stay on one
worker. xdist's own default, `load`, splits the running-Anki file across workers and
interleaves each worker's share with tests from every other file; a worker running a real
Anki that way segfaults partway through and xdist reports `node down: Not properly
terminated` for a suite that is green run serially. Passing `--dist` explicitly is left
alone, `--dist load` included, which is how to see that crash.

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

Once a real Anki has run in a process, that process crashes on the way out: the
`QApplication` has had QtWebEngine inside it, and tearing it down at interpreter exit dies
with an access violation. Every test has passed and the summary is already printed, but the
exit status is 139 — and the exit status is the only thing CI reads, so an unguarded run
reports every green result as a failure.

`pytest_plugin.py`, registered from the root conftest, ends any run in which a
`QApplication` exists by running the `atexit` handlers itself and then leaving through
`os._exit` with the status pytest returned, so the interpreter never reaches the teardown
that crashes. The exit status is still pytest's own (1 for a failure, 2 for an interrupt or
collection error, 5 for nothing collected), pytest's temporary directories are still cleaned
up, and `pytest.main()` callers are unaffected. A stub-only run creates no `QApplication`
and is left alone entirely; so is an xdist worker, which still has results queued for the
controller at that point. What parallel runs do need is a distribution that keeps a file on
one worker, which is not the guard's doing and is described under installing `pytest-xdist`
above.

The handlers are run rather than skipped because a bare `os._exit` would drop pytest's own,
which remove this run's lock on its `pytest-of-<user>/pytest-N` directory and prune old
ones — every run would otherwise leave ~90 MB behind for three days.

They are run only once PyQt's own handler is known to be out of the registry. PyQt keeps no
reference to `_qtcore_cleanup`, so the only way to reach it is to scan live objects for a
builtin function of that name, and a rename or a PyQt that registers a bound method would
defeat that scan. Running the registry with the handler still in it would invoke the
application teardown the guard exists to avoid, at the point nothing can recover from it:
`os._exit` is never reached and a green suite exits 139. Forcing the scan to fail against
`anki_shared/test_anki/` reproduced that in three runs out of eight, against none with the
scan working and four out of eight with the guard switched off — so a failed scan put the
guard back to roughly no guard at all, intermittently, which reads in CI as a flaky test
rather than a shutdown problem. When the scan comes back empty the hand-run is skipped and
the process leaves immediately: that run leaves its temporary directory behind, and keeps
the exit status the guard is for.

This used to be smaller: it unregistered PyQt's `_qtcore_cleanup` handler and let the
interpreter shut down normally. That stopped being enough at PyQt6 6.11 / QtWebEngine 6.11,
where the process segfaults on the way through QtWebEngine's own teardown even with that
handler gone ("Release of profile requested but WebEnginePage still not deleted"). The
handler is still unregistered — running it by hand would destroy the very application being
protected — but it is now one step inside the guard rather than the whole of it.

`ANKI_TEST_SHUTDOWN_GUARD=0` switches the guard off, to see the crash or debug Qt's own
shutdown. There is no `=exit` mode any more: leaving through `os._exit` is what the guard
does in every case, because unregistering PyQt's `atexit` handler stopped being enough at
PyQt6 6.11 -- the crash moved into QtWebEngine's own teardown.

Separately, `real_anki.qt_offscreen()` sets `QTWEBENGINE_CHROMIUM_FLAGS`. It adds
`--disable-gpu` always: offscreen, QtWebEngine's GPU process kept losing its context, and
about one run in five died mid-test on it. It adds `--no-sandbox` when the tests run as
root, which a CI container usually does — Chromium will not start its zygote as root
without it, and it refuses by dying with no message, no traceback and no pytest summary.
That reads as "this environment cannot run these tests" rather than "one flag is missing",
which is exactly the wrong conclusion to hand someone. A run as an ordinary user keeps the
sandbox. Both flags are appended to whatever is already in the variable, so setting it
yourself does not lose them.

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

The cloud environment's settings supply the rest, one environment variable:

| variable | why |
| --- | --- |
| `PYTHONIOENCODING=utf-8` | the jp_text_processing suite prints Japanese |

Two more used to be needed and no longer are, though setting them does no harm.
`QTWEBENGINE_DISABLE_SANDBOX=1`: `qt_offscreen()` adds `--no-sandbox` itself when the tests
run as root (above). `ANKI_TEST_SHUTDOWN_GUARD=exit`: the guard leaves through `os._exit` in
every case now, and reads any value but an "off" one as on.

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
