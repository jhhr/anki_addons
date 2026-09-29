"""Run the addon's ops over a collection file, without Anki's main window.

For the capture runs of issue #11 and, from them, replays: a real `anki` Collection opened from
a file, the stub `mw` of anki_shared/testing/real_anki.py where Anki's would be, and the op's run
started as its menu entry starts it (the op's `NotesRunSpec`), through `base_ops.notes_run` on a
thread of this process. Nothing in dev/ is shipped: build.json excludes it.

What Anki would have provided, and what stands in for it here:

- The addon imported as a package without its __init__.py, which builds menus and registers
  hooks, the way the root conftest.py does, with its vendored lib/ on sys.path.
- `mw.addonManager.getConfig`: config.json's defaults under the user's meta.json config, as
  aqt merges them, with every key that names a secret removed (`capture.scrub_config`), then
  the caller's overrides. Nothing of the config is printed.
- `mw.pm.profileFolder()`: a folder of the caller's, whose collection.media holds the generated
  meanings file the ops read and rewrite; never the real profile's.
- `mw.progress`: prints the progress now and then instead of drawing a dialog, and is how Ctrl+C
  cancels the run the way the dialog's Cancel does, so the run still saves what it did.
- A capture store of the caller's: a running Anki writes user_files/capture.sqlite3, and two
  processes on one store hand out the same ids.

Other addons' hooks only when asked for: `col.add_note` fires note_will_be_added with nothing
registered, so copy_anywhere's on-add definitions do not fill a new note's fields unless a run
puts them on (`replay.copy_anywhere_on_add`, with the user's `copy_anywhere_config()` or a
fixture's). The copy_anywhere package is registered here for that.

Import this module before anything of the addon or of anki: `prepare_process` runs at import.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import types
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, TextIO

ADDON_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ADDON_DIR.parent
PACKAGE = ADDON_DIR.name

# What a Claude Code session puts in the environment of what it starts, naming that session: its
# id, the socket and token of its messages, that it is a session. Every `claude -p` a terminal-
# model starts inherits this process's environment, and those children must not take themselves
# for part of the session that started the script. Only these: the rest (a login token in the
# environment, a Bedrock switch) is how the children reach the model at all.
SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_PID",
)

# Models go by the name of their config key; this one has no default in config.json
EXTRA_MODEL_KEYS = ("make_meanings_model",)
TERMINAL_PREFIX = "terminal-"
# Registered as a package too, for the runs that put its add definitions on
COPY_ANYWHERE = "copy_anywhere"


def _register(name: str, path: Path) -> types.ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    module.__package__ = name
    sys.modules[name] = module
    return module


def prepare_process() -> None:
    """The environment and the imports, once, before anything of the addon or anki is imported.
    Leaves what a root conftest already registered as it is."""
    for name in SESSION_ENV:
        os.environ.pop(name, None)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _register("anki_shared", REPO_ROOT / "anki_shared")
    for name in (PACKAGE, COPY_ANYWHERE):
        directory = REPO_ROOT / name if name != PACKAGE else ADDON_DIR
        addon = _register(name, directory)
        if not (directory / "shared").is_dir() and not hasattr(addon, "shared"):
            setattr(addon, "shared", _register(f"{name}.shared", REPO_ROOT / "anki_shared"))
    from anki_shared.testing import real_anki
    from anki_shared.utils.vendor_path import add_vendor_paths

    import aqt

    # A running Anki's own mw is left alone: install() would put a stub over it
    if getattr(aqt, "mw", None) is None or isinstance(aqt.mw, real_anki.StubMainWindow):
        real_anki.qt_offscreen()
        real_anki.install()
    add_vendor_paths(str(ADDON_DIR))


prepare_process()

# After prepare_process, which makes these importable. isort: off
from anki.collection import Collection  # noqa: E402
from anki.notes import NoteId  # noqa: E402

from anki_shared.testing import real_anki  # noqa: E402
from anki_shared.word_array.field_text import read_word_array  # noqa: E402
from japanese_note_ai_ops import call_logging, configuration  # noqa: E402
from japanese_note_ai_ops.async_api_ops import capture, run_errors  # noqa: E402
from japanese_note_ai_ops.async_api_ops.base_ops import NotesRunSpec, RunResult  # noqa: E402
from japanese_note_ai_ops.word_array import match_targets  # noqa: E402

# isort: on


def user_config(overrides: Optional[Mapping[str, Any]] = None, model: Optional[str] = None) -> dict:
    """The config a run uses: config.json under meta.json's config, secrets removed, then
    `overrides`; with `model`, every model key set to it. Raises ValueError when a model key
    would reach an API provider rather than the claude CLI: a headless run is meant to cost
    nothing but the subscription."""
    config: dict = json.loads((ADDON_DIR / "config.json").read_text(encoding="utf-8"))
    meta_path = ADDON_DIR / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        config.update(meta.get("config") or {})
    config = capture.scrub_config(config)
    config.update(overrides or {})
    keys = model_keys(config)
    if model is not None:
        for key in keys:
            config[key] = model
    not_terminal = [key for key in keys if not str(config.get(key, "")).startswith(TERMINAL_PREFIX)]
    if not_terminal:
        raise ValueError(
            f"not a {TERMINAL_PREFIX} model, so an API call: {', '.join(not_terminal)}; give"
            " --model or set them"
        )
    return config


# CopyAnywhere's copy of every definition as it was before its staged migration
# (copy_anywhere/configuration.py PRE_STAGE_MIGRATION_KEY): a backup it never reads
COPY_ANYWHERE_BACKUP_KEY = "pre_stage_migration_copy_definitions"


def copy_anywhere_config() -> dict:
    """CopyAnywhere's config as the user's Anki has it: its config.json under its meta.json's,
    secrets removed, without the migration's backup, which a fixture need not carry."""
    directory = REPO_ROOT / COPY_ANYWHERE
    config: dict = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    meta_path = directory / "meta.json"
    if meta_path.exists():
        config.update(json.loads(meta_path.read_text(encoding="utf-8")).get("config") or {})
    config.pop(COPY_ANYWHERE_BACKUP_KEY, None)
    return capture.scrub_config(config)


def model_keys(config: Mapping[str, Any]) -> list[str]:
    """The config's model keys. A key starting `//` is one the user commented out, which
    nothing reads."""
    keys = {key for key in config if key.endswith("_model") and not key.startswith("//")}
    return sorted(keys | set(EXTRA_MODEL_KEYS))


class ConsoleProgress(real_anki.StubProgress):
    """`mw.progress` for a script: prints the op's label every `interval` seconds, keeps none of
    the updates (a run of a thousand notes makes hundreds of thousands), and cancels on
    `set_cancel(True)`, which Ctrl+C in `Headless.run` does."""

    def __init__(self, out: TextIO = sys.stderr, interval: float = 20.0) -> None:
        super().__init__()
        self.out = out
        self.interval = interval
        self._next = 0.0
        self.last: dict[str, Any] = {}

    def update(self, **kwargs: Any) -> None:
        self.last = kwargs
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + self.interval
        self.print_status()

    def print_status(self) -> None:
        label = re.sub(r"<[^>]+>", " ", str(self.last.get("label") or ""))
        label = re.sub(r"\s+", " ", label).strip()
        print(f"[{time.strftime('%H:%M:%S')}] {label}", file=self.out, flush=True)

    def set_title(self, title: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] == {title}", file=self.out, flush=True)


class Headless:
    """A collection file opened for ops, with the stand-ins above in place, and a capture store.

    `profile_dir` stands in for the profile folder; `meanings_from`, the generated meanings file
    it starts from when its collection.media has none yet (the meanings file the collection's
    own profile holds, copied once, so that the collection and its meanings agree). Use as a
    context manager, which closes the store and the collection.
    """

    def __init__(
        self,
        collection_path: Path,
        profile_dir: Path,
        config: dict,
        capture_path: Optional[Path] = None,
        meanings_from: Optional[Path] = None,
        capture_max_queue: int = 500_000,
        out: TextIO = sys.stderr,
    ) -> None:
        self.out = out
        self.mw = real_anki.install()
        self.mw.addonManager.configs[PACKAGE] = config
        self.progress = ConsoleProgress(out)
        self.mw.progress = self.progress
        self.mw.pm.set_profile_folder(profile_dir)
        meanings = self.mw.pm.media_folder() / configuration.MEANINGS_DICT_FILE
        if meanings_from is not None and not meanings.exists():
            shutil.copy2(meanings_from, meanings)
        self.meanings_path = meanings
        self.col = real_anki.open_collection(collection_path)
        self.mw.col = self.col
        self.capture_path = capture_path
        if capture_path is not None:
            installed = capture.install(
                str(capture_path),
                # Capture runs are kept until deleted by hand: they are what the fixtures
                # and evals are made from
                keep_days=None,
                versions=configuration.capture_versions(),
                log_path=call_logging.current_log_path,
                profile=Path(collection_path).stem,
                max_queue=capture_max_queue,
            )
            if not installed:
                raise RuntimeError(f"the capture store {capture_path} did not open")
        # What the dialog's error pane would have shown; run_errors keeps only what a pane did
        self._errors_lock = threading.Lock()
        self.errors = 0
        run_errors.deliver_with(self._print_error)

    def _print_error(self, title: str, text: str) -> None:
        with self._errors_lock:
            self.errors += 1
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        print(f"! {title}: {first_line}", file=self.out, flush=True)

    def __enter__(self) -> "Headless":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        capture.shutdown(timeout=30.0)
        self.col.close()

    def run(self, spec: NotesRunSpec, nids: list[NoteId], log_name: str) -> "RunReport":
        """Run `spec` over `nids`, as the menu would, on a thread of this process, and wait for
        it. Ctrl+C cancels the run the way the dialog's Cancel does: it still saves what it did.
        """
        call_logging.start_call_log(log_name)
        self.progress.set_cancel(False)
        errors_before = self.errors
        run, result = spec.notes_run(nids)
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                run(self.col)
            except BaseException as e:  # noqa: BLE001 - handed to the waiting thread
                outcome["error"] = e

        started = time.monotonic()
        thread = threading.Thread(target=target, name="headless-op")
        thread.start()
        while thread.is_alive():
            try:
                thread.join(0.5)
            except KeyboardInterrupt:
                print("Cancelling: the run saves what it has done", file=self.out, flush=True)
                self.progress.set_cancel(True)
        return RunReport(
            result=result,
            seconds=time.monotonic() - started,
            error=outcome.get("error"),
            error_count=self.errors - errors_before,
            run_id=last_run_id(self.capture_path) if self.capture_path else None,
        )


class RunReport:
    def __init__(
        self,
        result: RunResult,
        seconds: float,
        error: Optional[BaseException],
        error_count: int,
        run_id: Optional[int],
    ) -> None:
        self.result = result
        self.seconds = seconds
        self.error = error
        self.error_count = error_count
        self.run_id = run_id


def last_run_id(capture_path: Path) -> Optional[int]:
    """The newest non-implicit run in the store: the one just run. Flushes first."""
    store = capture.current_store()
    if store is not None:
        store.flush(30.0)
    with closing(sqlite3.connect(f"file:{capture_path}?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT MAX(run_id) FROM runs WHERE implicit = 0").fetchone()
    return row[0] if row else None


def notes_to_match(
    col: Collection,
    config: Mapping[str, Any],
    note_type_names: Optional[Iterable[str]] = None,
) -> list[NoteId]:
    """The notes a match run would work on: of every note type the config gives a word list
    field (or only `note_type_names`), those whose word array has a word to match in the states
    a run matches (`match_targets.states_to_match`), in id order. One pass over each type's
    notes: 40k notes decode in seconds."""
    states = match_targets.states_to_match(bool(config.get("replace_existing_matched_words")))
    wanted = set(note_type_names) if note_type_names is not None else None
    db = col.db
    assert db is not None
    found: list[NoteId] = []
    for notetype in col.models.all():
        name = notetype["name"]
        model_config = config.get(name)
        if not isinstance(model_config, dict) or (wanted is not None and name not in wanted):
            continue
        field = model_config.get("word_list_field")
        ords = {f["name"]: f["ord"] for f in notetype["flds"]}
        if field not in ords:
            continue
        ord_ = ords[field]
        for nid, flds in db.all("select id, flds from notes where mid = ?", notetype["id"]):
            values = flds.split("\x1f")
            if ord_ >= len(values):
                continue
            arr, _ = read_word_array(values[ord_])
            if not arr:
                continue
            try:
                if match_targets.gather_targets(arr, states):
                    found.append(NoteId(nid))
            except ValueError:
                # match_data in no known state: the run tags such a note, it matches nothing
                continue
    return sorted(found)


def capture_summary(capture_path: Path, run_id: int) -> dict[str, Any]:
    """What the store holds of a run: its row's outcome, completeness and size, its calls by
    kind and outcome, its snapshots by stage, its events by kind."""
    with closing(sqlite3.connect(f"file:{capture_path}?mode=ro", uri=True)) as connection:
        run = connection.execute(
            "SELECT label, outcome, notes, dropped, note_count, started, ended FROM runs"
            " WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        calls = connection.execute(
            "SELECT kind, outcome, COUNT(*), ROUND(SUM(latency_ms) / 1000.0, 1) FROM calls"
            " WHERE run_id = ? GROUP BY kind, outcome ORDER BY kind, outcome",
            (run_id,),
        ).fetchall()
        stages = connection.execute(
            "SELECT stage, COUNT(*) FROM note_snapshots WHERE run_id = ? GROUP BY stage",
            (run_id,),
        ).fetchall()
        events = connection.execute(
            "SELECT kind, COUNT(*) FROM events WHERE run_id = ? GROUP BY kind ORDER BY kind",
            (run_id,),
        ).fetchall()
    label, outcome, notes, dropped, note_count, started, ended = run
    return {
        "run_id": run_id,
        "label": label,
        "outcome": outcome,
        "notes": notes,
        "dropped": dropped,
        "note_count": note_count,
        "seconds": None if ended is None else round(ended - started, 1),
        "calls": [
            {"kind": kind, "outcome": out, "count": count, "seconds": seconds}
            for kind, out, count, seconds in calls
        ],
        "snapshots": dict(stages),
        "events": dict(events),
    }


def op_specs() -> dict[str, Callable[[], NotesRunSpec]]:
    """The ops a script can run, by their op_registry key: those with a NotesRunSpec."""
    from japanese_note_ai_ops.async_api_ops.match_words_to_notes import match_words_spec

    return {"match_words": match_words_spec}
