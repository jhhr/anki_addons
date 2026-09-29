"""Fixtures from capture runs, and replaying them with no network (issue #11, stage 3).

A fixture is what a replay of one capture run needs, as three JSON files a person can review:

- corpus.json: the collection as the run found it, cut down to the notes it read, with their
  note types and decks; the config it ran with; the generated meanings it read; the dictionary
  lookups it made. Note ids are synthetic, in every field that holds one.
- cassette.json: every AI call's answer, by its request key (capture_store.request_key: the
  call's kind and the values its prompt was built from, so a reworded prompt still finds it),
  in the order the run received them.
- expected.json: the notes as the run left them, normalized (`normalized_notes`): the ids a
  replay cannot know, the new notes' and their placeholders', replaced by symbols. The file
  holds only the notes the run added or changed, and the keys of those it removed; a note it
  holds no entry for is as the corpus has it (`Fixture.read` puts the whole list together).

Optional, next to them:

- expected_copy_anywhere.json: the notes as a replay with the corpus's CopyAnywhere definitions
  (`corpus["copy_anywhere"]`, its addon config) on the add hook left them. The capture run did
  not have them on; this is a replay's result kept to compare later replays with, not a capture.
- work.json: what a benchmark run of a corpus did, counted (benchmark.py's `work`), to check a
  run on another machine against.

A big file may be gzipped (`corpus.json.gz`), written with a fixed header so the same content
is the same bytes. Fixtures of a real collection hold its note text and excerpts of the
dictionaries it looked words up in, so they are kept out of this public repo, in a checkout of
the private test data repo (`data_root`): `fixtures/` for the ones test_replay replays strictly,
`corpora/` for benchmark.py's.

`export_fixture` makes one from a capture store; `replay` builds the corpus in a fresh
collection, runs the op over its selected notes with the cassette answering every
`get_response` (`base_ops.set_responder`) and the corpus answering every dictionary lookup, and
reports what differed. Strict: a request the cassette has no answer left for, an answer nothing
asked for, and a lookup the corpus lacks are each reported, and answered as a failure would be,
never by a guess. The export is strict the same way: a run that dropped records or has no note
snapshots is refused (`CaptureGap`), and so is a note the run needs that it never recorded.

Names from the user's collection are replaced: note types, but the ones the addon hardcodes,
and decks by generic names, and note ids by synthetic ones. The note text stays the user's own:
whether a fixture may be committed is the user's decision, so `export_fixture` writes where it
is told and commits nothing.

The replay runs in this process, with the stub `mw` of real_anki; `headless` must be imported
first (in a script) or the root conftest must have run (in a test).
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Iterator, Mapping, Optional

# 2: expected.json holds only the notes the run changed, added or removed; 1 held every note
FORMAT = 2
ADDON_DIR = Path(__file__).resolve().parents[1]
# The test data checkout: <repo>/test_data, gitignored, unless this names another directory
DATA_ROOT_ENV = "ANKI_ADDONS_TEST_DATA"
# This addon's directory in it: the repo's name for the addon, whatever Anki's folder is called
DATA_ADDON = "japanese_note_ai_ops"
COPY_ANYWHERE = "copy_anywhere"
# Synthetic note ids: 13 digits, as Anki's millisecond ids are, so a field's layout is kept
SYNTHETIC_BASE = 1_000_000_000_000
# Note ids found in fields without a note of the corpus behind them (a word linked to a note the
# run never read): still ids, still replaced, from a range of their own
SYNTHETIC_UNKNOWN_BASE = 1_500_000_000_000
# A note id in a field: 13 digits standing alone. Anki's ids are milliseconds since 1970, 13
# digits from 2001 to 2286
ID_RE = re.compile(r"(?<![\d.])(\d{13})(?![\d.])")
# Note types the addon's code names, which a fixture keeps: renaming one would change what runs
HARDCODED_NOTETYPES = frozenset({"Japanese vocab note", "Kanji draw"})
# The config key naming the deck new notes go into, in a note type's config
DECK_CONFIG_KEY = "insert_deck"


class CaptureGap(Exception):
    """The capture lacks something a replay needs. Fixed in the capture, then recorded again:
    an exporter never makes up what a run did not record."""


def data_root() -> Optional[Path]:
    """The test data checkout, or None on a machine without one."""
    configured = os.environ.get(DATA_ROOT_ENV)
    root = Path(configured) if configured else ADDON_DIR.parent / "test_data"
    return root if root.is_dir() else None


def data_dir(kind: str) -> Optional[Path]:
    """`fixtures` or `corpora` of this addon in the test data checkout, if there is one."""
    root = data_root()
    return root / DATA_ADDON / kind if root is not None else None


def find_fixture(name: str) -> Path:
    """A fixture's or corpus's directory: `name` itself when it is one, else the one of that
    name in the test data checkout."""
    path = Path(name)
    if Fixture.exists(path):
        return path
    for kind in ("fixtures", "corpora"):
        directory = data_dir(kind)
        if directory is not None and Fixture.exists(directory / name):
            return directory / name
    where = data_root() or f"{ADDON_DIR.parent / 'test_data'} (absent; or set {DATA_ROOT_ENV})"
    raise FileNotFoundError(f"no fixture or corpus {name!r}, as a path or in {where}")


@dataclass
class Fixture:
    corpus: dict
    cassette: dict
    expected: dict
    expected_copy_anywhere: Optional[dict] = None
    work: Optional[dict] = None

    FILES = ("corpus", "cassette", "expected")
    OPTIONAL_FILES = ("expected_copy_anywhere", "work")

    @staticmethod
    def exists(directory: Path) -> bool:
        return any((directory / f"corpus{suffix}").is_file() for suffix in (".json", ".json.gz"))

    def write(self, directory: Path, compress: bool = False) -> None:
        """The files, the expected ones as the notes that differ from the corpus; `compress`
        gzips the big ones. A file of the other form, or an optional one this fixture lacks,
        left by an earlier write is removed: a reader must not take it for this one's."""
        directory.mkdir(parents=True, exist_ok=True)
        for name in self.FILES + self.OPTIONAL_FILES:
            value = getattr(self, name)
            if value is not None and name.startswith("expected"):
                value = _changes(self.corpus, value)
            _write_json(directory / f"{name}.json", value, compress and name != "work")

    @classmethod
    def read(cls, directory: Path) -> "Fixture":
        parts = {name: _read_json(directory / f"{name}.json") for name in cls.FILES}
        for name, value in parts.items():
            if value is None:
                raise FileNotFoundError(f"{directory}: no {name}.json or {name}.json.gz")
        parts.update({name: _read_json(directory / f"{name}.json") for name in cls.OPTIONAL_FILES})
        for name in ("expected", "expected_copy_anywhere"):
            if parts[name] is not None:
                parts[name] = _expanded(parts["corpus"], parts[name])
        return cls(**parts)


def _write_json(path: Path, value: Any, compress: bool) -> None:
    gzipped = path.with_name(path.name + ".gz")
    target, other = (gzipped, path) if compress else (path, gzipped)
    other.unlink(missing_ok=True)
    if value is None:
        target.unlink(missing_ok=True)
        return
    data = (json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()
    # mtime=0: the header holds no time, so rewriting the same content changes nothing in git
    target.write_bytes(gzip.compress(data, 9, mtime=0) if compress else data)


def _read_json(path: Path) -> Any:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    gzipped = path.with_name(path.name + ".gz")
    if gzipped.is_file():
        return json.loads(gzip.decompress(gzipped.read_bytes()).decode("utf-8"))
    return None


def _corpus_as_expected(corpus: Mapping[str, Any]) -> dict[str, dict]:
    """The corpus's notes as expected.json's, by key: what a note the run left alone is."""
    return {
        str(note["id"]): {
            "note": str(note["id"]),
            "notetype": note["notetype"],
            "deck": note["deck"],
            "fields": note["fields"],
            "tags": sorted(note["tags"]),
        }
        for note in corpus["notes"]
    }


def _changes(corpus: Mapping[str, Any], expected: Mapping[str, Any]) -> dict:
    """`expected` as its file holds it: the notes that differ from the corpus, and the keys of
    the corpus notes the run removed. Most notes a run reads it leaves as they were, so the
    whole list is mostly the corpus again (12 of a 500-note corpus's 29 MB)."""
    before = _corpus_as_expected(corpus)
    after = {note["note"]: note for note in expected["notes"]}
    return {
        **{key: value for key, value in expected.items() if key != "notes"},
        "format": FORMAT,
        "notes": [after[key] for key in sorted(after) if before.get(key) != after[key]],
        "removed": sorted(set(before) - set(after)),
    }


def _expanded(corpus: Mapping[str, Any], stored: dict) -> dict:
    """`stored`, an expected file, with every note in it again: what `_changes` left out."""
    if stored.get("format", 1) < 2:
        return stored
    notes = _corpus_as_expected(corpus)
    for key in stored.get("removed", []):
        notes.pop(key, None)
    notes.update({note["note"]: note for note in stored["notes"]})
    return {**stored, "notes": [notes[key] for key in sorted(notes)]}


# --- export ------------------------------------------------------------------------------


class _IdMap:
    """Real note id -> synthetic, handed out in the order asked, so an export is the same
    every time: the corpus notes first, in id order, then any other id met in a field."""

    def __init__(self, corpus_ids: Iterable[int]) -> None:
        self.ids: dict[int, int] = {
            nid: SYNTHETIC_BASE + 10 * index for index, nid in enumerate(sorted(corpus_ids))
        }
        self._unknown = 0

    def __call__(self, nid: int) -> int:
        synthetic = self.ids.get(nid)
        if synthetic is None:
            synthetic = SYNTHETIC_UNKNOWN_BASE + 10 * self._unknown
            self._unknown += 1
            self.ids[nid] = synthetic
        return synthetic

    def text(self, value: str, keep: Optional[Mapping[int, str]] = None) -> str:
        """`value` with each note id replaced: by `keep`'s symbol for the ids in it, else by its
        synthetic id."""
        symbols = keep or {}

        def swap(match: re.Match) -> str:
            nid = int(match.group(1))
            return symbols[nid] if nid in symbols else str(self(nid))

        return ID_RE.sub(swap, value)


def export_fixture(
    store_path: Path, run_id: int, copy_anywhere: Optional[Mapping[str, Any]] = None
) -> Fixture:
    """The fixture of run `run_id` in the capture store at `store_path`. Raises CaptureGap when
    the run cannot be replayed from what it recorded. `copy_anywhere`, CopyAnywhere's addon
    config, goes into the corpus for replays that put its definitions on the add hook."""
    with closing(sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise CaptureGap(f"no run {run_id} in {store_path}")
        if not run["notes"]:
            raise CaptureGap(f"run {run_id} recorded no notes (config capture_notes was off)")
        if run["dropped"]:
            raise CaptureGap(f"run {run_id} dropped {run['dropped']} records at a full queue")
        snapshots = connection.execute(
            "SELECT note_id, stage, text FROM note_snapshots JOIN blobs ON hash = note_hash"
            " WHERE run_id = ? ORDER BY snapshot_id",
            (run_id,),
        ).fetchall()
        events = [
            (row["kind"], row["note_id"], json.loads(row["payload_json"] or "null"))
            for row in connection.execute(
                "SELECT kind, note_id, payload_json FROM events WHERE run_id = ?"
                " ORDER BY event_id",
                (run_id,),
            )
        ]
        calls = connection.execute(
            "SELECT kind, request_key, prompt_key, inputs_json, response_json, outcome,"
            " latency_ms FROM calls WHERE run_id = ? ORDER BY started, call_id",
            (run_id,),
        ).fetchall()
    config = json.loads(run["config_json"] or "{}")

    pre: dict[int, dict] = {}
    selected: list[int] = []
    final: dict[int, dict] = {}
    for row in snapshots:
        record = json.loads(row["text"])
        nid, stage = row["note_id"], row["stage"]
        if stage in ("selected", "read") and nid not in pre and not record.get("missing"):
            pre[nid] = record
            if stage == "selected":
                selected.append(nid)
        elif stage == "final":
            final[nid] = record
    added: dict[int, Optional[int]] = {}
    removed: set[int] = set()
    notetypes: dict[int, dict] = {}
    decks_of: dict[str, list[int]] = {}
    deck_names: dict[str, str] = {}
    meanings: dict[str, Any] = {}
    meanings_final: dict[str, Any] = {}
    lookups: list[dict] = []
    records: list[str] = []
    for kind, nid, payload in events:
        if kind == "environment":
            records = payload.get("records") or []
        elif kind == "note.added":
            added[payload["note_id"]] = payload.get("placeholder")
        elif kind == "note.removed":
            removed.update(payload["note_ids"])
        elif kind == "notetype":
            notetypes[payload["mid"]] = payload
        elif kind == "decks":
            decks_of = payload["notes"]
            deck_names = payload["names"]
        elif kind == "meanings.read":
            meanings.setdefault(payload["key"], payload["value"])
        elif kind == "meanings.final":
            meanings_final[payload["key"]] = payload["value"]
        elif kind == "dictionary.lookup" and payload not in lookups:
            lookups.append(payload)
    if config.get("mdx_filenames") and "dictionary.lookup" not in records:
        raise CaptureGap(
            f"run {run_id} read dictionaries but predates the recording of their lookups"
        )
    for nid in added:
        pre.pop(nid, None)
    unrecorded = sorted(nid for nid in final if nid not in pre and nid not in added)
    if unrecorded:
        raise CaptureGap(f"run {run_id} wrote notes it has no state before for: {unrecorded}")
    if not selected:
        raise CaptureGap(f"run {run_id} recorded no selected notes")

    ids = _IdMap(pre)
    type_names = _names(
        (notetypes[mid]["name"] for mid in sorted(notetypes)), "Note type", HARDCODED_NOTETYPES
    )
    deck_rename = _names((deck_names[did] for did in sorted(deck_names, key=int)), "Deck")

    def notetype_of(record: dict) -> str:
        info = notetypes.get(record["mid"])
        if info is None:
            raise CaptureGap(f"run {run_id} recorded no note type {record['mid']}")
        return type_names[info["name"]]

    def deck_of(nid: int) -> str:
        dids = decks_of.get(str(nid)) or []
        return deck_rename.get(deck_names.get(str(dids[0]), ""), "Default") if dids else "Default"

    corpus_notes = []
    for nid in sorted(pre):
        record = pre[nid]
        corpus_notes.append(
            {
                "id": ids(nid),
                "notetype": notetype_of(record),
                "deck": deck_of(nid),
                "guid": record["guid"],
                "fields": {name: ids.text(value) for name, value in record["fields"].items()},
                "tags": sorted(record["tags"]),
                "selected": nid in selected,
            }
        )

    # The run's final state, in the same names; new notes by symbol (normalized_notes)
    new_ids = {nid: placeholder for nid, placeholder in added.items() if nid in final}
    after: list[dict] = []
    for nid in sorted(set(pre) | set(new_ids)):
        if nid in removed:
            continue
        record = final.get(nid) or pre[nid]
        after.append(
            {
                "id": nid,
                "notetype": notetype_of(record),
                "deck": deck_of(nid),
                "fields": record["fields"],
                "tags": record["tags"],
            }
        )
    expected_notes = normalized_notes(after, new_ids, ids=ids)

    corpus = {
        "format": FORMAT,
        "op": "match_words",
        "source": {
            "run_id": run_id,
            "label": run["label"],
            "outcome": run["outcome"],
            "versions": json.loads(run["versions_json"] or "null"),
        },
        "config": _renamed_config(config, type_names, deck_rename),
        "notetypes": [
            {
                "name": type_names[info["name"]],
                "fields": info["fields"],
                "sort_field": info["sort_field"],
                "templates": info["templates"],
            }
            for _, info in sorted(notetypes.items())
        ],
        "decks": sorted(set(deck_rename.values())),
        "notes": corpus_notes,
        "meanings": {key: value for key, value in sorted(meanings.items()) if value is not None},
        "dictionary": lookups,
    }
    if copy_anywhere is not None:
        corpus["copy_anywhere"] = dict(copy_anywhere)
    cassette = {"format": FORMAT, "entries": _cassette_entries(calls)}
    expected = {
        "format": FORMAT,
        "notes": expected_notes,
        "meanings": dict(sorted(meanings_final.items())),
        "new_notes": len(new_ids),
    }
    return Fixture(corpus, cassette, expected)


def _names(originals: Iterable[str], generic: str, keep: frozenset = frozenset()) -> dict:
    renamed: dict[str, str] = {}
    for name in originals:
        if name not in renamed:
            renamed[name] = name if name in keep else f"{generic} {len(renamed) + 1}"
    return renamed


def _renamed_config(config: dict, type_names: dict, deck_names: dict) -> dict:
    """The run's config with its note type keys and their decks under the fixture's names. A
    note type config of a type the run never saw is dropped: its name is the user's."""
    renamed: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            if key not in type_names:
                continue
            value = dict(value)
            if value.get(DECK_CONFIG_KEY) in deck_names:
                value[DECK_CONFIG_KEY] = deck_names[value[DECK_CONFIG_KEY]]
            renamed[type_names[key]] = value
        elif not key.startswith("//"):
            renamed[key] = value
    return renamed


def _cassette_entries(calls: Iterable[sqlite3.Row]) -> list[dict]:
    """One entry per request key, its answers in the order the run received them."""
    by_key: dict[str, dict] = {}
    for call in calls:
        entry = by_key.setdefault(
            call["request_key"],
            {
                "kind": call["kind"],
                "request_key": call["request_key"],
                "inputs": json.loads(call["inputs_json"] or "null"),
                "answers": [],
            },
        )
        entry["answers"].append(
            {
                "response": json.loads(call["response_json"] or "null"),
                "outcome": call["outcome"],
                "prompt_key": call["prompt_key"],
                # How long the answer took to come, for a timed replay (benchmark.py)
                "latency_ms": round(call["latency_ms"] or 0.0),
            }
        )
    return sorted(by_key.values(), key=lambda entry: (entry["kind"], entry["request_key"]))


# --- normalizing ---------------------------------------------------------------------------


def normalized_notes(
    notes: Iterable[Mapping[str, Any]],
    new_notes: Mapping[int, Optional[int]],
    ids: Optional[Callable[..., Any]] = None,
) -> list[dict]:
    """Notes as a replay can compare them: each `{"id", "notetype", "deck", "fields", "tags"}`,
    `new_notes` the run's added notes (id -> the placeholder it replaced, or None).

    A new note's id is whatever the collection handed out, and its placeholder was random, so
    both become symbols: `new-N`, numbered in the order of the notes' content (ids masked),
    and `placeholder:new-N`, wherever they appear. `ids`, given, maps every other id in a field
    (`_IdMap.text`); a replay's are synthetic already. Tags sorted; the list in note order.
    """
    notes = list(notes)

    def content(note: Mapping[str, Any]) -> str:
        masked = {name: ID_RE.sub("#", value) for name, value in note["fields"].items()}
        return json.dumps([note["notetype"], masked], ensure_ascii=False, sort_keys=True)

    new = sorted((note for note in notes if note["id"] in new_notes), key=content)
    symbols = {int(note["id"]): f"new-{index + 1}" for index, note in enumerate(new)}
    placeholders = {
        str(placeholder): f"placeholder:{symbols[nid]}"
        for nid, placeholder in new_notes.items()
        if placeholder is not None and nid in symbols
    }

    def text(value: str) -> str:
        if ids is not None:
            value = ids.text(value, keep=symbols)  # type: ignore[attr-defined]
        else:
            value = ID_RE.sub(lambda m: symbols.get(int(m.group(1)), m.group(1)), value)
        for placeholder, symbol in placeholders.items():
            value = re.sub(rf"(?<![\d-]){re.escape(placeholder)}(?!\d)", symbol, value)
        return value

    normalized = []
    for note in notes:
        nid = int(note["id"])
        if nid in symbols:
            key = symbols[nid]
        elif ids is not None:
            key = str(ids(nid))
        else:
            key = str(nid)
        normalized.append(
            {
                "note": key,
                "notetype": note["notetype"],
                "deck": note["deck"],
                "fields": {name: text(value) for name, value in note["fields"].items()},
                "tags": sorted(note["tags"]),
            }
        )
    return sorted(normalized, key=lambda note: note["note"])


def fields_differing(replayed: list[dict], expected: list[dict]) -> dict[str, list[str]]:
    """Note key -> the fields it holds otherwise than the capture run left it; a note one side
    lacks is listed with ["<missing>"]."""
    want = {note["note"]: note for note in expected}
    have = {note["note"]: note for note in replayed}
    differing: dict[str, list[str]] = {}
    for key in sorted(set(want) | set(have)):
        if key not in want or key not in have:
            differing[key] = ["<missing>"]
            continue
        fields = sorted(
            name
            for name in set(want[key]["fields"]) | set(have[key]["fields"])
            if want[key]["fields"].get(name) != have[key]["fields"].get(name)
        )
        if want[key]["tags"] != have[key]["tags"]:
            fields.append("<tags>")
        if fields:
            differing[key] = fields
    return differing


def new_note_fields(notes: list[dict]) -> dict[str, dict[str, str]]:
    """The fields of the notes a run added, by symbol: what every addon's add hook wrote into
    them, to compare between two replays of one fixture (headless and in a running Anki)."""
    return {note["note"]: note["fields"] for note in notes if note["note"].startswith("new-")}


# --- replay --------------------------------------------------------------------------------


# The inputs that say what a request is about when its exact inputs were never seen: which word,
# in which sentence. A replay of a run whose notes contended for a word (two sentences, one new
# note) can ask about it with another list of meanings than the capture did, lock order being
# timing; a benchmark still wants an answer of the kind for it (Cassette's `lenient`)
LOOSE_INPUTS = ("word", "reading", "sentence")


def _loose_key(kind: str, inputs: Any) -> tuple:
    if not isinstance(inputs, dict):
        return (kind,)
    return (kind,) + tuple(json.dumps(inputs.get(name), ensure_ascii=False) for name in LOOSE_INPUTS)


@dataclass
class Cassette:
    """The answers of a fixture, handed out by request key, each as many times as the run
    received it, in the same order. What it cannot answer it records and answers with None,
    as a failed call is answered.

    `lenient`, for benchmarks: a request with no exact answer left gets one of the same kind
    about the same word and sentence (`LOOSE_INPUTS`), else any answer of its kind, in turn, so
    the run does the work the capture run did; each is counted (`counts`), not failed.
    `latency(answer)` is how long to wait before answering, in seconds: on the calling thread,
    which is the pool worker a provider's request blocks too."""

    entries: list[dict]
    lenient: bool = False
    latency: Optional[Callable[[dict], float]] = None
    misses: list[dict] = field(default_factory=list)
    used: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._by_key = {entry["request_key"]: entry for entry in self.entries}
        self._by_loose: dict[tuple, list[dict]] = defaultdict(list)
        self._by_kind: dict[str, list[dict]] = defaultdict(list)
        for entry in self.entries:
            for answer in entry["answers"]:
                self._by_loose[_loose_key(entry["kind"], entry["inputs"])].append(answer)
                self._by_kind[entry["kind"]].append(answer)
        self._turns: dict[Any, int] = defaultdict(int)

    def __call__(self, request: Any) -> Any:
        answer = self._answer(request)
        if answer is None:
            return None
        if self.latency is not None:
            seconds = self.latency(answer)
            if seconds > 0:
                time.sleep(seconds)
        return answer["response"]

    def _answer(self, request: Any) -> Optional[dict]:
        from japanese_note_ai_ops.async_api_ops.capture_store import request_key

        key = request_key(request.kind, request.inputs)
        with self._lock:
            entry = self._by_key.get(key)
            index = self.used[key]
            if entry is not None and index < len(entry["answers"]):
                self.used[key] = index + 1
                self.counts["exact"] += 1
                return entry["answers"][index]
            if self.lenient:
                loose = _loose_key(request.kind, request.inputs)
                pools: tuple[tuple[Any, Optional[list[dict]], str], ...] = (
                    (loose, self._by_loose.get(loose), "loose"),
                    (request.kind, self._by_kind.get(request.kind), "by kind"),
                )
                for pool_key, answers, tally in pools:
                    if answers:
                        turn = self._turns[pool_key]
                        self._turns[pool_key] = turn + 1
                        self.counts[tally] += 1
                        return answers[turn % len(answers)]
            self.counts["missed"] += 1
            self.misses.append(
                {"kind": request.kind, "request_key": key, "inputs": request.inputs,
                 "known": entry is not None}
            )
            return None

    def unused(self) -> list[dict]:
        return [
            {"kind": entry["kind"], "request_key": entry["request_key"],
             "left": len(entry["answers"]) - self.used[entry["request_key"]]}
            for entry in self.entries
            if self.used[entry["request_key"]] < len(entry["answers"])
        ]


class Dictionary:
    """The dictionary lookups of a fixture, answered as the run was answered, in place of the
    MDX files; a lookup the fixture lacks is recorded and answered as not found."""

    def __init__(self, lookups: Iterable[Mapping[str, Any]]) -> None:
        self._answers = {
            (lookup["word"], lookup["reading"], lookup["pick"], lookup["max_length"]): lookup
            for lookup in lookups
        }
        self.misses: list[dict] = []
        self._lock = threading.Lock()

    def load_mdx_dictionaries_if_needed(self, *args: Any, **kwargs: Any) -> "Dictionary":
        return self

    def get_definition_text(
        self,
        word: str,
        reading: Optional[str] = None,
        pick_dictionary: str = "all",
        max_length: Optional[int] = None,
    ) -> Optional[str]:
        from japanese_note_ai_ops.sync_local_ops.mdx_dictionary import MDXLookupError

        lookup = self._answers.get((word, reading, pick_dictionary, max_length))
        if lookup is None:
            with self._lock:
                self.misses.append(
                    {"word": word, "reading": reading, "pick": pick_dictionary,
                     "max_length": max_length}
                )
            return None
        if "error" in lookup:
            raise MDXLookupError(lookup["error"])
        return lookup.get("text")


@dataclass
class ReplayResult:
    notes: list[dict]
    meanings: dict[str, Any]
    misses: list[dict]
    unused: list[dict]
    dictionary_misses: list[dict]
    new_notes: int
    decisions: list[dict]
    # What the run reported without failing (run_errors): a replay reproducing the run's own
    # errors is still a replay, so these are for reading, not compared
    errors: list[str]
    # How long the op's run took, and what the cassette answered how (Cassette.counts)
    seconds: float = 0.0
    answered: dict[str, int] = field(default_factory=dict)
    # What `replay`'s `read_store` made of the replay's own capture store
    store_data: Any = None

    def differences(self, expected: Mapping[str, Any]) -> list[str]:
        """What differs from the fixture's expected state, as lines a test can print; empty
        when the replay reproduced the run."""
        problems = [f"cassette had no answer: {miss}" for miss in self.misses]
        problems += [f"cassette answer never asked for: {entry}" for entry in self.unused]
        problems += [f"dictionary had no lookup: {miss}" for miss in self.dictionary_misses]
        if self.new_notes != expected["new_notes"]:
            problems.append(f"new notes: {self.new_notes}, expected {expected['new_notes']}")
        want = {note["note"]: note for note in expected["notes"]}
        have = {note["note"]: note for note in self.notes}
        for key in sorted(set(want) | set(have)):
            if want.get(key) != have.get(key):
                problems.append(
                    f"note {key}:\n  expected {json.dumps(want.get(key), ensure_ascii=False)}"
                    f"\n  replayed {json.dumps(have.get(key), ensure_ascii=False)}"
                )
        for key in sorted(set(expected["meanings"]) | set(self.meanings)):
            if expected["meanings"].get(key) != self.meanings.get(key):
                problems.append(f"meanings {key}: expected {expected['meanings'].get(key)!r},"
                                f" replayed {self.meanings.get(key)!r}")
        return problems


def build_collection(corpus: Mapping[str, Any], path: Path, background: int = 0):
    """A fresh collection at `path` holding the corpus (`fill_collection`) and `background`
    notes no request of the run can find (`background_notes`)."""
    from anki_shared.testing import real_anki

    col = real_anki.open_collection(path)
    fill_collection(col, corpus)
    if background:
        fill_collection(col, {"notetypes": [], "decks": [], "notes": background_notes(corpus, background)})
    return col


# Background notes' ids, clear of the corpus's and of the ids its fields name
BACKGROUND_BASE = 2_000_000_000_000
BACKGROUND_LINK_BASE = 3_000_000_000_000
# Where a background note's Japanese goes: the Hangul syllables, a block no word, reading or
# sentence a run asks about has a character in
_HANGUL_FIRST, _HANGUL_COUNT = 0xAC00, 11172


def _is_japanese(char: str) -> bool:
    code = ord(char)
    return 0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x9FFF or 0xFF66 <= code <= 0xFF9F


def _background_text(text: str, links: Iterator[int]) -> str:
    """`text` with every Japanese character moved into Hangul, one to one, and every note id
    replaced by one no note has: the same length and shape (the same HTML, the same JSON of a
    word array), so a scan pays for it as for a real note, and nothing the run looks for in it."""
    text = ID_RE.sub(lambda _: str(next(links)), text)
    return "".join(
        chr(_HANGUL_FIRST + ord(char) % _HANGUL_COUNT) if _is_japanese(char) else char
        for char in text
    )


def background_notes(corpus: Mapping[str, Any], count: int) -> list[dict]:
    """`count` notes shaped like the corpus's, to bring a small corpus's collection up to a real
    one's size: the corpus notes in turn, each with its text moved out of Japanese
    (`_background_text`), under ids of their own. The index build, a whole-collection search and
    a table scan pay for them as for real notes; no lookup, search or index answer the run gets
    can hold one, so the run's work is the same with them and without (test_pipeline)."""
    templates = corpus["notes"]
    if not templates or count <= 0:
        return []
    links = iter(range(BACKGROUND_LINK_BASE, BACKGROUND_LINK_BASE + 10**12, 10))
    notes = []
    for index in range(count):
        template = templates[index % len(templates)]
        notes.append(
            {
                "id": BACKGROUND_BASE + 10 * index,
                "notetype": template["notetype"],
                "deck": template["deck"],
                "guid": f"bg{index:09d}",
                "fields": {
                    name: _background_text(value, links)
                    for name, value in template["fields"].items()
                },
                "tags": list(template["tags"]),
                "selected": False,
            }
        )
    return notes


def fill_collection(col: Any, corpus: Mapping[str, Any]) -> None:
    """Put the corpus into an empty collection: its note types (one card template each), decks
    and notes, each note under its synthetic id."""
    from anki_shared.testing import real_anki

    assert col.db is not None
    for notetype in corpus["notetypes"]:
        real_anki.make_note_type(
            col,
            notetype["name"],
            notetype["fields"],
            templates=[(notetype["templates"][0] if notetype["templates"] else "Card 1",
                        "{{%s}}" % notetype["fields"][0], "{{FrontSide}}")],
        )
        model = col.models.by_name(notetype["name"])
        assert model is not None
        model["sortf"] = notetype["sort_field"]
        col.models.update_dict(model)
    for deck in corpus["decks"]:
        col.decks.id(deck)
    for record in corpus["notes"]:
        model = col.models.by_name(record["notetype"])
        assert model is not None
        note = col.new_note(model)
        note.guid = record["guid"]
        for name, value in record["fields"].items():
            note[name] = value
        note.tags = list(record["tags"])
        deck_id = col.decks.id(record["deck"])
        assert deck_id is not None
        col.add_note(note, deck_id)
        # Anki hands out the ids; the fields name the corpus's
        col.db.execute("update cards set nid = ? where nid = ?", record["id"], note.id)
        col.db.execute("update notes set id = ? where id = ?", record["id"], note.id)


# How a replay starts the op over its selected notes: headless, the run on this thread; in a
# running Anki, selected_notes_op and a wait for its CollectionOp
RunOp = Callable[[Any, list], None]


def replay(
    fixture: Fixture,
    workdir: Optional[Path] = None,
    cassette: Optional[Cassette] = None,
    around_run: Optional[Callable[[], ContextManager[Any]]] = None,
    read_store: Optional[Callable[[Path], Any]] = None,
    background: int = 0,
    estimates: Optional[dict] = None,
    copy_anywhere: bool = False,
) -> ReplayResult:
    """Build the fixture's corpus in a fresh collection under `workdir` (a temporary directory
    by default), run the op over its selected notes with nothing reaching a network, and return
    the collection's state after it, normalized as the fixture's expected state is.

    `copy_anywhere` puts the corpus's CopyAnywhere definitions on the add hook for the run
    (`copy_anywhere_on_add`); the result is then compared with `expected_copy_anywhere`.

    For a benchmark: `cassette` answers in place of a strict one of the fixture's own (a timed
    or lenient one), the run happens inside `around_run()` (a memory profile), `background`
    notes bring the collection to a real one's size (`background_notes`), and
    `read_store(path)` reads the replay's own capture store before it is deleted. The
    background notes are left out of the notes returned: nothing may have changed them."""
    definitions = fixture.corpus.get("copy_anywhere")
    if copy_anywhere and definitions is None:
        raise ValueError("the fixture holds no CopyAnywhere definitions (export --copy-anywhere)")
    from anki_shared.testing import real_anki

    owned = workdir is None
    root = Path(tempfile.mkdtemp(prefix="jnaio_replay_")) if workdir is None else workdir
    stub = real_anki.install()
    saved_col, saved_configs = stub.col, dict(stub.addonManager.configs)
    saved_profile = stub.pm._profile_folder
    col = build_collection(fixture.corpus, root / "collection.anki2", background)
    try:
        stub.col = col
        stub.pm.set_profile_folder(root / "profile")

        def set_config(config: dict) -> None:
            stub.addonManager.configs[_package()] = config

        def run_op(spec: Any, nids: list) -> None:
            run, _ = spec.notes_run(nids)
            with ExitStack() as stack:
                if around_run is not None:
                    stack.enter_context(around_run())
                # Around the run only, after the corpus is built: on the notes the run adds
                if copy_anywhere:
                    stack.enter_context(copy_anywhere_on_add(stub, definitions or {}))
                run(col)

        return _replay(
            fixture, col, stub.pm.media_folder(), root, set_config, run_op, cassette,
            read_store, estimates,
        )
    finally:
        stub.col = saved_col
        stub.addonManager.configs = saved_configs
        stub.pm._profile_folder = saved_profile
        col.close()
        if owned:
            shutil.rmtree(root, ignore_errors=True)


def replay_in_anki(
    mw: Any,
    fixture: Fixture,
    workdir: Path,
    set_config: Callable[[dict], None],
    run_op: RunOp,
    cassette: Optional[Cassette] = None,
    read_store: Optional[Callable[[Path], Any]] = None,
    before_run: Optional[Callable[[], None]] = None,
    estimates: Optional[dict] = None,
) -> ReplayResult:
    """`replay` in a running Anki: the corpus goes into `mw.col`, which must be a fresh
    profile's, the config is written by `set_config` (the real AddonManager reads it off disk),
    and `run_op` starts the op the way the menu does, a real CollectionOp, and waits for it.

    `before_run()` attaches the other addons' hooks, once the corpus is in: attached before,
    an add hook would run on every corpus note as it was built, which is not the collection
    the capture run found. From then on they run in the run's add phase, as in Anki."""
    fill_collection(mw.col, fixture.corpus)
    if before_run is not None:
        before_run()
    media = Path(mw.pm.profileFolder(), "collection.media")
    return _replay(
        fixture, mw.col, media, workdir, set_config, run_op, cassette, read_store, estimates
    )


@contextmanager
def copy_anywhere_on_add(mw: Any, config: Mapping[str, Any]) -> Iterator[None]:
    """CopyAnywhere's add-note definitions run on every note added inside the block, as they do
    in Anki: its handler on `note_will_be_added`, and only that one (its editor and review hooks
    have no use here), with `config` as its addon config.

    For issue #11's stage 5: the match op's cleanup adds its notes with `col.add_note`, which
    fires the hook for each, so its cost lands in the add phase, and what CopyAnywhere reads it
    reads from the whole collection, as in Anki. A script's `headless` registered the
    copy_anywhere package; under pytest the root conftest did."""
    from anki.hooks import note_will_be_added
    from copy_anywhere.hooks import note_hooks

    configs = mw.addonManager.configs
    saved = configs.get(COPY_ANYWHERE)
    configs[COPY_ANYWHERE] = dict(config)
    handler = note_hooks.contained(
        "on add",
        lambda _col, note, deck_id: note_hooks.run_copy_fields_on_add(note, deck_id),
        note_hooks._nothing,
    )
    note_will_be_added.append(handler)
    try:
        yield
    finally:
        note_will_be_added.remove(handler)
        if saved is None:
            configs.pop(COPY_ANYWHERE, None)
        else:
            configs[COPY_ANYWHERE] = saved


@contextmanager
def memory_estimates(estimates: dict) -> Iterator[None]:
    """The gate's learned per-task costs read from and written to `estimates` instead of the
    user's user_files/memory_estimates.json: a replay's run is not one the real runs should
    learn from. An empty dict is a cold start; one kept across replays, a warm one."""
    from japanese_note_ai_ops.async_api_ops import concurrency

    saved = (concurrency.load_per_task_estimates, concurrency.save_per_task_estimate)

    def save(op_key: str, value: float, baseline: Optional[float] = None) -> None:
        estimates[op_key] = value

    concurrency.load_per_task_estimates = lambda: dict(estimates)  # type: ignore[assignment]
    concurrency.save_per_task_estimate = save  # type: ignore[assignment]
    try:
        yield
    finally:
        concurrency.load_per_task_estimates, concurrency.save_per_task_estimate = saved


def _replay(
    fixture: Fixture,
    col: Any,
    media: Path,
    root: Path,
    set_config: Callable[[dict], None],
    run_op: RunOp,
    cassette: Optional[Cassette],
    read_store: Optional[Callable[[Path], Any]],
    estimates: Optional[dict] = None,
) -> ReplayResult:
    from japanese_note_ai_ops.async_api_ops import base_ops, capture, run_errors
    from japanese_note_ai_ops.async_api_ops.match_words_to_notes import match_words_spec
    from japanese_note_ai_ops.configuration import MEANINGS_DICT_FILE
    from japanese_note_ai_ops.sync_local_ops import mdx_dictionary

    corpus = fixture.corpus
    saved_helper = mdx_dictionary.mdx_helper
    # Its own capture, notes and all, is how the replay learns which notes it added and what
    # it decided, the way the export read them from the capture run
    set_config(
        dict(corpus["config"], capture_calls=True, capture_notes=True, log_to_console=False)
    )
    media.mkdir(parents=True, exist_ok=True)
    (media / MEANINGS_DICT_FILE).write_text(
        json.dumps(corpus["meanings"], ensure_ascii=False), encoding="utf-8"
    )
    if cassette is None:
        cassette = Cassette(fixture.cassette["entries"])
    dictionary = Dictionary(corpus["dictionary"])
    errors: list[str] = []
    try:
        # The modules that use it imported it by name, and hold their own reference
        _point_modules_at(dictionary)
        store = root / "capture.sqlite3"
        if not capture.install(str(store), keep_days=None):
            raise RuntimeError(f"the replay's capture store {store} did not open")
        run_errors.deliver_with(lambda title, text: errors.append(f"{title}: {text}"))
        base_ops.set_responder(cassette)
        nids = [record["id"] for record in corpus["notes"] if record["selected"]]
        with memory_estimates({} if estimates is None else estimates):
            started = time.monotonic()
            run_op(match_words_spec(), nids)
            seconds = time.monotonic() - started
        capture.shutdown(timeout=30.0)
        events = _run_events(store)
        return ReplayResult(
            notes=normalized_notes(_collection_notes(col), events.added),
            meanings=dict(sorted(events.meanings.items())),
            misses=cassette.misses,
            unused=cassette.unused(),
            dictionary_misses=dictionary.misses,
            new_notes=len(events.added),
            decisions=events.decisions,
            errors=errors,
            seconds=seconds,
            answered=dict(cassette.counts),
            store_data=read_store(store) if read_store is not None else None,
        )
    finally:
        base_ops.set_responder(None)
        run_errors.deliver_with(None)
        capture.shutdown(timeout=5.0)
        _point_modules_at(saved_helper)


def _package() -> str:
    return Path(__file__).resolve().parents[1].name


def _point_modules_at(helper: Any) -> None:
    """Make `helper` the dictionary of every module of the addon that holds `mdx_helper`:
    mdx_dictionary's own, and those that imported it by name."""
    import sys

    for name, module in list(sys.modules.items()):
        if name.startswith(_package() + ".") and getattr(module, "mdx_helper", None) is not None:
            setattr(module, "mdx_helper", helper)


@dataclass
class _RunEvents:
    added: dict[int, Optional[int]] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    meanings: dict[str, Any] = field(default_factory=dict)


def _run_events(store: Path) -> _RunEvents:
    """What the replay's own capture recorded, read as the export reads a capture run's."""
    with closing(sqlite3.connect(str(store))) as connection:
        rows = connection.execute(
            "SELECT kind, note_id, task_id, payload_json FROM events ORDER BY event_id"
        ).fetchall()
    events = _RunEvents()
    for kind, note_id, task_id, payload_json in rows:
        payload = json.loads(payload_json or "null")
        if kind == "note.added":
            events.added[payload["note_id"]] = payload.get("placeholder")
        elif kind == "match.decision":
            events.decisions.append({"note": note_id, "task": task_id, **payload})
        elif kind == "meanings.final":
            events.meanings[payload["key"]] = payload["value"]
    return events


def _collection_notes(col: Any) -> list[dict]:
    decks = {int(deck.id): deck.name for deck in col.decks.all_names_and_ids()}
    notes = []
    for nid in col.find_notes(""):
        if BACKGROUND_BASE <= nid < BACKGROUND_LINK_BASE:
            continue
        note = col.get_note(nid)
        did = col.db.scalar("select did from cards where nid = ? order by ord limit 1", nid)
        notes.append(
            {
                "id": int(nid),
                "notetype": note.note_type()["name"],
                "deck": decks.get(int(did), "Default") if did is not None else "Default",
                "fields": dict(note.items()),
                "tags": list(note.tags),
            }
        )
    return notes
