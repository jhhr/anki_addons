"""Fixtures from capture runs, and replaying them with no network (issue #11, stage 3).

A fixture is what a replay of one capture run needs, as three JSON files a person can review:

- corpus.json: the collection as the run found it, cut down to the notes it read, with their
  note types and decks (and a stand-in for a type its config pairs with one of those and the
  run read no note of, `_stand_in_notetypes`); the config it ran with; the generated meanings
  it read; the dictionary lookups it made. Note ids are synthetic, in every field that holds
  one. With CopyAnywhere's config (`copy_anywhere`), also what its add definitions read that
  the run did not: the notes their searches find, from the collection the run ran against, and
  the media files they open (`media`) (`export_with_copy_anywhere`).
- cassette.json: every AI call's answer, by its request key (capture_store.request_key: the
  call's kind and the values its prompt was built from, so a reworded prompt still finds it),
  in the order the run received them.
- expected.json: the notes as the run left them, normalized (`normalized_notes`): the ids a
  replay cannot know, the new notes' and their placeholders' and the placeholders a failed add
  leaves in the word arrays, replaced by symbols. The file
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
never by a guess. The export is strict the same way: a run it cannot reproduce is refused
(`CaptureGap`): one with no note snapshots, one that lost records or whose end is unknown, one
that did not complete, one of another op; and so is a note the run needs that it never recorded.

Names from the user's collection are replaced: note types, but the ones the addon hardcodes,
and decks by generic names, and note ids by synthetic ones. The note text stays the user's own:
whether a fixture may be committed is the user's decision, so `export_fixture` writes where it
is told and commits nothing.

The replay runs in this process, with the stub `mw` of real_anki; `headless` must be imported
first (in a script) or the root conftest must have run (in a test).
"""

from __future__ import annotations

import copy
import gzip
import json
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
from typing import (
    Any,
    Callable,
    ContextManager,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Sequence,
)

# Where the test data checkout is; the other dev scripts and the tests reach it through here
from data_paths import DATA_ROOT_ENV, DEFAULT_ROOT, data_dir, data_root  # noqa: F401

# 2: expected.json holds only the notes the run changed, added or removed; 1 held every note
FORMAT = 2
# The addon's package, as the root conftest and headless register it: its directory's name.
# Worked out once: a Path.resolve() per module of sys.modules cost half a second a scan
PACKAGE = Path(__file__).resolve().parents[1].name
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
# The op a replay runs (match_words_spec), as a run's ops_json names it
MATCH_OP = "bulk_match_words_to_notes"
# Top-level config values a replay never reads that name things of one person's: the claude
# CLI's path (a replay answers every call itself) and the menu exports' Anki searches (`*_query`),
# which name their decks, tags and note types
PRIVATE_CONFIG_KEYS = frozenset({"claude_cli_path"})
PRIVATE_CONFIG_SUFFIX = "_query"
# A note type block's values a replay never reads that name things of one person's: the user's
# own tags the sentence migration carries (sentence_migration's MOVE_TAGS_KEY, COPY_TAGS_KEY)
PRIVATE_BLOCK_KEYS = frozenset({"migration_move_tags", "migration_copy_tags"})
# The keys by which a note type's block names the other type of its two-type layout
# (note_roles): a sentence type's names its vocab type, a vocab type's its sentence type
PARTNER_TYPE_KEYS = ("vocab_note_type", "sentence_note_type")
# What a note type of the user's is called in a fixture, numbered
GENERIC_NOTETYPE = "Note type"
# CopyAnywhere's key for its definitions, in its addon config
COPY_DEFINITIONS_KEY = "copy_definitions"
# Where a replay's logs go: this folder beside each addon's own `logs`, in its user_files. A
# replay's run is not one of the user's, and CopyAnywhere keeps only its newest 200 logs, so
# the hundred adds of one benchmark in the user's folder pruned the logs of their own adds
REPLAY_LOGS = "replay_logs"


class CaptureGap(Exception):
    """The capture lacks something a replay needs. Fixed in the capture, then recorded again:
    an exporter never makes up what a run did not record."""


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
    where = data_root() or f"{DEFAULT_ROOT} (absent; or set {DATA_ROOT_ENV})"
    raise FileNotFoundError(f"no fixture or corpus {name!r}, as a path or in {where}")


@dataclass
class Fixture:
    corpus: dict
    cassette: dict
    expected: dict
    expected_copy_anywhere: Optional[dict] = None
    work: Optional[dict] = None
    # A synthetic note id -> the id of that note in the collection the capture ran on. An
    # export's, for the searches it looks up there (`export_with_copy_anywhere`); never written
    real_ids: Optional[dict[int, int]] = field(default=None, repr=False, compare=False)

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
    store_path: Path,
    run_id: int,
    copy_anywhere: Optional[Mapping[str, Any]] = None,
    source: Optional[SourceNotes] = None,
    media: Optional[Mapping[str, Any]] = None,
) -> Fixture:
    """The fixture of run `run_id` in the capture store at `store_path`. Raises CaptureGap when
    the run cannot be replayed from what it recorded. `copy_anywhere`, CopyAnywhere's addon
    config, goes into the corpus for replays that put its definitions on the add hook.

    `source` is notes of the collection the run ran against that it never read, and `media`
    files by name: what CopyAnywhere's definitions read (`export_with_copy_anywhere`). The
    notes go into the corpus unselected, under ids and names numbered after the run's own, so
    carrying them renames nothing the run recorded; the files go into the replay's media
    folder."""
    with closing(sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise CaptureGap(f"no run {run_id} in {store_path}")
        if not run["notes"]:
            raise CaptureGap(f"run {run_id} recorded no notes (config capture_notes was off)")
        # Set at the run's end only: a run still going, or whose store went off before its end
        # was written, may have lost anything since, and the last it could lose is its finals
        if run["dropped"] is None:
            raise CaptureGap(
                f"run {run_id} has no recorded end, so whether it lost records is unknown"
            )
        if run["dropped"]:
            raise CaptureGap(f"run {run_id} lost {run['dropped']} of its records")
        # A replay runs to the end: a cancelled run's expected state holds only what it finished
        # before the cancel, and a failed one's what it wrote before the raise
        if run["outcome"] != "completed":
            raise CaptureGap(f"run {run_id} ended {run['outcome']!r}; only a completed run replays")
        ops = json.loads(run["ops_json"] or "null") or []
        if ops != [MATCH_OP]:
            raise CaptureGap(f"run {run_id} ran {ops}; a replay runs {MATCH_OP} alone")
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
        failed = _failed_adds(connection, run_id)
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
    run_mids = sorted(notetypes)
    run_dids = sorted(deck_names, key=int)
    carried: dict[int, dict] = {}
    if source is not None:
        carried = {
            nid: record
            for nid, record in source.records.items()
            if nid not in pre and nid not in added
        }
        for payload in source.notetypes:
            notetypes.setdefault(payload["mid"], payload)
        for key, dids in source.decks["notes"].items():
            decks_of.setdefault(key, dids)
        for key, name in source.decks["names"].items():
            deck_names.setdefault(key, name)
    type_names = _names(
        [notetypes[mid]["name"] for mid in run_mids]
        + [notetypes[mid]["name"] for mid in sorted(notetypes) if mid not in run_mids],
        GENERIC_NOTETYPE,
        HARDCODED_NOTETYPES,
    )
    run_types = {notetypes[mid]["name"] for mid in run_mids}
    deck_rename = _names(
        [deck_names[did] for did in run_dids]
        + [deck_names[did] for did in sorted(deck_names, key=int) if did not in run_dids],
        "Deck",
    )
    decks = _DeckNames(deck_rename)

    def notetype_of(record: dict) -> str:
        info = notetypes.get(record["mid"])
        if info is None:
            raise CaptureGap(f"run {run_id} recorded no note type {record['mid']}")
        return type_names[info["name"]]

    def deck_of(nid: int) -> str:
        dids = decks_of.get(str(nid)) or []
        return deck_rename.get(deck_names.get(str(dids[0]), ""), "Default") if dids else "Default"

    corpus_notes = []
    # The carried notes after the run's: their ids are handed out after every one the run's
    # notes name, so carrying them changes no id the run recorded
    for nid in sorted(pre) + sorted(carried):
        record = pre[nid] if nid in pre else carried[nid]
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
    for nid in sorted(set(pre) | set(carried) | set(new_ids)):
        if nid in removed:
            continue
        record = final.get(nid) or pre.get(nid) or carried[nid]
        after.append(
            {
                "id": nid,
                "notetype": notetype_of(record),
                "deck": deck_of(nid),
                "fields": record["fields"],
                "tags": record["tags"],
            }
        )
    expected_notes = normalized_notes(after, new_ids, ids=ids, failed=failed)

    corpus_config = _renamed_config(config, type_names, decks, run_types)
    corpus_notetypes = [
        {
            "name": type_names[info["name"]],
            "fields": info["fields"],
            "sort_field": info["sort_field"],
            "templates": info["templates"],
        }
        for _, info in sorted(notetypes.items())
    ]
    corpus_notetypes += _stand_in_notetypes(
        corpus_config, {notetype["name"] for notetype in corpus_notetypes}
    )
    corpus = {
        "format": FORMAT,
        "op": "match_words",
        "source": {
            "run_id": run_id,
            "label": run["label"],
            "outcome": run["outcome"],
            "versions": json.loads(run["versions_json"] or "null"),
        },
        "config": corpus_config,
        "notetypes": corpus_notetypes,
        "decks": sorted(set(deck_rename.values())),
        "notes": corpus_notes,
        "meanings": {key: value for key, value in sorted(meanings.items()) if value is not None},
        "dictionary": lookups,
    }
    if copy_anywhere is not None:
        corpus["copy_anywhere"] = _renamed_copy_anywhere(
            copy_anywhere, type_names, decks, run_types
        )
    if media:
        corpus["media"] = dict(sorted(media.items()))
    cassette = {"format": FORMAT, "entries": _cassette_entries(calls)}
    expected = {
        "format": FORMAT,
        "notes": expected_notes,
        "meanings": dict(sorted(meanings_final.items())),
        "new_notes": len(new_ids),
    }
    real_ids = {synthetic: nid for nid, synthetic in ids.ids.items()}
    return Fixture(corpus, cassette, expected, real_ids=real_ids)


def _names(originals: Iterable[str], generic: str, keep: frozenset = frozenset()) -> dict:
    renamed: dict[str, str] = {}
    for name in originals:
        if name not in renamed:
            renamed[name] = name if name in keep else f"{generic} {len(renamed) + 1}"
    return renamed


class _DeckNames:
    """A deck name of the user's -> the fixture's: a corpus deck's generic name, and for any
    other deck (one no recorded note is in) a name of its own that no deck of a replay's
    collection has, the same wherever the export meets it. Unrenamed, those were the user's."""

    def __init__(self, corpus: Mapping[str, str]) -> None:
        self.corpus = dict(corpus)
        self._unseen: dict[str, str] = {}

    def __call__(self, name: str) -> str:
        if name in self.corpus:
            return self.corpus[name]
        if name not in self._unseen:
            self._unseen[name] = f"Unseen deck {len(self._unseen) + 1}"
        return self._unseen[name]


def _renamed_config(
    config: dict, type_names: Mapping[str, str], decks: _DeckNames, recorded: Iterable[str]
) -> dict:
    """The run's config with its note type keys and their decks under the fixture's names.
    `type_names` names every note type of the corpus, `recorded` are those the run recorded
    notes of. A note type config of a type the run never saw is dropped: its name is the
    user's. So are the values a replay never reads that name the user's things
    (`PRIVATE_CONFIG_KEYS`, and in a block `PRIVATE_BLOCK_KEYS`).

    But the two types of a two-type layout go together (`_linked_types`): a run over sentence
    notes that read and added no vocab note recorded nothing of the vocab type, and without its
    block the layout check failed every sentence note of the replay. The names a block gives
    its partner (`PARTNER_TYPE_KEYS`) are renamed as the keys are; a partner the corpus has no
    name for gets one of its own, and `_stand_in_notetypes` its note type."""
    linked = _linked_types(config, recorded)
    names = dict(type_names)
    for name in linked:
        if name not in names:
            names[name] = name if name in HARDCODED_NOTETYPES else _unused_name(names.values())
    renamed: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            if key not in linked:
                continue
            value = {name: item for name, item in value.items() if name not in PRIVATE_BLOCK_KEYS}
            if value.get(DECK_CONFIG_KEY):
                value[DECK_CONFIG_KEY] = decks(value[DECK_CONFIG_KEY])
            for partner_key in PARTNER_TYPE_KEYS:
                partner = value.get(partner_key)
                if isinstance(partner, str) and partner:
                    value[partner_key] = names[partner]
            renamed[names[key]] = value
        elif not (
            key.startswith("//") or key in PRIVATE_CONFIG_KEYS or key.endswith(PRIVATE_CONFIG_SUFFIX)
        ):
            renamed[key] = value
    return renamed


def _linked_types(config: Mapping[str, Any], recorded: Iterable[str]) -> list[str]:
    """The note types whose blocks a fixture's config keeps: the `recorded` ones and every type
    linked to one through the two-type layout, the type a kept block names
    (`PARTNER_TYPE_KEYS`) and a block naming a kept type, until none is added, so a layout the
    user got wrong is kept whole and fails the replay's layout check as it failed the run's.
    Then the types only a kept block's value names, which have no block. In the config's order,
    so the names handed out are the same in every export of one run."""
    blocks = {key: value for key, value in config.items() if isinstance(value, dict)}

    def partners(block: Mapping[str, Any]) -> list[str]:
        values = [block.get(key) for key in PARTNER_TYPE_KEYS]
        return [value for value in values if isinstance(value, str) and value]

    linked = set(recorded)
    grown = True
    while grown:
        grown = False
        for key, block in blocks.items():
            named = partners(block)
            if key in linked:
                added = set(named) - linked
            else:
                added = {key} if linked.intersection(named) else set()
            if added:
                linked |= added
                grown = True
    ordered = [key for key in blocks if key in linked]
    ordered += [name for key in ordered for name in partners(blocks[key]) if name not in blocks]
    return list(dict.fromkeys(ordered))


def _unused_name(used: Iterable[str]) -> str:
    """A generic note type name that none of `used` is, numbered past the names `_names`
    hands out, which count from 1."""
    taken = set(used)
    number = len(taken) + 1
    while f"{GENERIC_NOTETYPE} {number}" in taken:
        number += 1
    return f"{GENERIC_NOTETYPE} {number}"


def _stand_in_notetypes(config: Mapping[str, Any], present: Iterable[str]) -> list[dict]:
    """A note type for each block of the fixture's config whose type is not among `present`,
    the corpus's: a two-type partner the run recorded no note of (`_renamed_config`). It has the
    fields its block names, so the run reads and writes the notes it makes in it as in the
    capture run's collection: the match op makes a new vocab note in the vocab type the config
    names, which a run whose only new note failed to add recorded nothing of, and without the
    type the replay could not make the note at all. Its other fields were never read."""
    known = set(present)
    stand_ins = []
    for name, block in config.items():
        if not isinstance(block, dict) or name in known:
            continue
        fields = list(
            dict.fromkeys(
                value
                for key, value in block.items()
                if key.endswith("_field") and isinstance(value, str) and value
            )
        )
        if not fields:
            continue
        sort_field = block.get("word_sort_field")
        stand_ins.append(
            {
                "name": name,
                "fields": fields,
                "sort_field": fields.index(sort_field) if sort_field in fields else 0,
                "templates": [],
            }
        )
    return stand_ins


def _renamed_copy_anywhere(
    config: Mapping[str, Any],
    type_names: Mapping[str, str],
    decks: _DeckNames,
    run_types: Iterable[str],
) -> dict:
    """CopyAnywhere's config as a replay runs it (`copy_anywhere_on_add`): the definitions its
    add hook runs for a note of the run's note types (`run_types`), as CopyAnywhere picks them
    (`note_hooks.get_copy_definitions_for_add_note`), with the note types and decks they name
    under the fixture's names. Every definition, unrenamed, carried the user's note types and
    decks, and a deck whitelist named decks a replay's collection never had. A note carried for
    the definitions' searches is never added, so its type picks none.

    A definition that may run others keeps every definition, since which it runs is in its
    stages; their trigger note types and decks are renamed all the same. What a definition's
    stages say is kept as it is: its code and searches are what it runs."""
    from copy_anywhere.configuration import definition_note_type_names, definition_runs_on_add
    from copy_anywhere.logic.definition_schema import is_format_2, read_effects

    definitions = list(config.get(COPY_DEFINITIONS_KEY) or [])
    added_types = set(run_types)
    runnable = [
        definition
        for definition in definitions
        if definition_runs_on_add(definition)
        and any(name in added_types for name in definition_note_type_names(definition))
    ]
    if any(read_effects(definition)["calls_definitions"] for definition in runnable):
        runnable = definitions
    renamed = []
    for definition in runnable:
        if not is_format_2(definition):
            raise CaptureGap(
                "a CopyAnywhere definition is still in format 1; open CopyAnywhere once, which"
                " migrates it, before exporting its definitions"
            )
        definition = copy.deepcopy(definition)
        triggers = definition.setdefault("triggers", {})
        triggers["note_types"] = [
            type_names[name] for name in triggers.get("note_types") or [] if name in type_names
        ]
        triggers["deck_names"] = [decks(name) for name in triggers.get("deck_names") or []]
        _rename_change_deck(definition.get("stages"), decks)
        renamed.append(definition)
    return {**config, COPY_DEFINITIONS_KEY: renamed}


def _rename_change_deck(value: Any, decks: _DeckNames) -> None:
    """Every card action's `change_deck` in a definition's stages, under the fixture's name."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "change_deck" and isinstance(item, str) and item:
                value[key] = decks(item)
            else:
                _rename_change_deck(item, decks)
    elif isinstance(value, list):
        for item in value:
            _rename_change_deck(item, decks)


# --- what CopyAnywhere's definitions read ------------------------------------------------------
#
# The capture run had no CopyAnywhere on its add hook, so it recorded nothing of what its
# definitions read: the notes their searches find (a kanji note's grade, a word's other notes)
# and the media files their fonts checks open. A replay without them runs every search against
# the few notes the run read and every fonts check against no file, and CopyAnywhere logs the
# failure and carries on, so nothing fails. The export takes the notes from the collection the
# run ran against, as it holds them now, and the files from a media folder that has them.

# How many rounds of looking up an export makes: the searches are built from the notes the run
# adds, so the second round finds no more unless a definition searches with what it found
COPY_ANYWHERE_ROUNDS = 3
# A fonts check's setting naming its dictionary, a media file (copy_anywhere/logic/
# fonts_check_process.py): the one media read of a definition whose name is in its config
FONTS_DICT_KEY = "fonts_dict_file"


@dataclass
class SourceNotes:
    """Notes of the collection a capture ran against that the run never read, for its corpus to
    carry, in the capture's own forms: records as `capture_notes.note_record` makes them, note
    types and decks as `capture_notes.collection_records` does."""

    records: dict[int, dict] = field(default_factory=dict)
    notetypes: list[dict] = field(default_factory=list)
    decks: dict = field(default_factory=lambda: {"notes": {}, "names": {}})
    # How many of them the collection changed after the run started: carried as they are now,
    # as nothing recorded them before
    changed: int = 0


def source_notes(col: Any, nids: Iterable[int], started: float) -> SourceNotes:
    """`nids` of `col`, the collection a run that started at `started` ran against."""
    from anki.utils import ids2str

    from japanese_note_ai_ops.async_api_ops.capture_notes import collection_records, note_record

    wanted = sorted(set(nids))
    records = collection_records(col, wanted)
    if records is None:
        return SourceNotes()
    notetypes, decks = records
    changed = col.db.scalar(
        f"select count() from notes where id in {ids2str(wanted)} and mod > ?", started
    )
    return SourceNotes(
        {nid: note_record(col.get_note(nid)) for nid in wanted}, notetypes, decks, changed
    )


def source_matches(
    col: Any, searches: Iterable[tuple[str, str]], real_ids: Mapping[int, int], started: float
) -> set[int]:
    """The notes `searches`, a replay's `(method, query)` pairs, find in `col`, the collection
    the run ran against, of the ones it held when the run started: a note's id is when it was
    made, so the run's own new notes are left out too. A synthetic id in a query is `col`'s
    again (`real_ids`)."""
    from anki.utils import ids2str

    def real(match: re.Match) -> str:
        return str(real_ids.get(int(match.group(1)), match.group(1)))

    before = int(started * 1000)
    found: set[int] = set()
    for method, query in sorted(set(searches)):
        query = ID_RE.sub(real, query)
        if method == "find_cards":
            cids = col.find_cards(query)
            nids = col.db.list(f"select distinct nid from cards where id in {ids2str(cids)}")
        else:
            nids = col.find_notes(query)
        found.update(int(nid) for nid in nids if int(nid) < before)
    return found


def _run_started(store_path: Path, run_id: int) -> float:
    with closing(sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT started FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None or row[0] is None:
        raise CaptureGap(f"no start recorded for run {run_id} in {store_path}")
    return float(row[0])


def _media_files(value: Any) -> set[str]:
    """The media files the definitions in `value` open by a name their config holds."""
    names: set[str] = set()
    if isinstance(value, dict):
        name = value.get(FONTS_DICT_KEY)
        if isinstance(name, str) and name:
            names.add(name)
        for item in value.values():
            names |= _media_files(item)
    elif isinstance(value, list):
        for item in value:
            names |= _media_files(item)
    return names


def _file_reading_stages(value: Any) -> int:
    """How many read_file stages are in `value`: they name the file in a template only their
    run fills in, so an export cannot know which to carry."""
    if isinstance(value, dict):
        own = 1 if value.get("type") == "read_file" else 0
        return own + sum(_file_reading_stages(item) for item in value.values())
    if isinstance(value, list):
        return sum(_file_reading_stages(item) for item in value)
    return 0


def _cut_media(content: Any, chars: set[str]) -> Any:
    """A fonts dictionary, `{character: [font, ...], "all_fonts": [...]}`, down to the
    characters in `chars`: a fonts check looks up only the characters of the text it checks,
    which a note of the replay holds. A key that is no single character is kept whole. The
    whole dictionary of one collection is 6 MB, most of it characters no corpus has."""
    if not isinstance(content, dict):
        return content
    return {key: value for key, value in content.items() if len(key) != 1 or key in chars}


def _characters(*note_lists: Iterable[Mapping[str, Any]]) -> set[str]:
    return {char for notes in note_lists for note in notes for text in note["fields"].values()
            for char in text}


def export_with_copy_anywhere(
    store_path: Path,
    run_id: int,
    config: Mapping[str, Any],
    source: Any,
    media_dir: Optional[Path],
    lenient: bool = False,
) -> tuple[Fixture, SourceNotes]:
    """`export_fixture` with CopyAnywhere's config, and with what its add definitions read that
    the capture did not record, so a replay with them on does their work as Anki did. Returns
    the fixture and the notes it carries.

    - The notes their searches find in `source`, the open collection the capture ran against,
      of the ones it held when the run started: the run is replayed with the definitions on,
      the searches they make are recorded (`copy_anywhere_on_add`) and made of `source`, and
      what they find goes into the corpus; again, until a round finds no more.
    - The media files their fonts checks open, from `media_dir`, cut to the characters the
      replay's notes hold (`_cut_media`).

    `lenient`, for a corpus a strict replay cannot reproduce, replays as a benchmark does. A
    fixture's replay with the cut files must write what one with the whole files wrote."""
    started = _run_started(store_path, run_id)
    fixture = export_fixture(store_path, run_id, copy_anywhere=config)
    definitions = fixture.corpus["copy_anywhere"][COPY_DEFINITIONS_KEY]
    if _file_reading_stages(definitions):
        raise CaptureGap(
            "a CopyAnywhere add definition reads a file whose name only its run knows (a"
            " read_file stage), which an export cannot carry"
        )
    whole: dict[str, Any] = {}
    for name in sorted(_media_files(definitions)):
        path = media_dir / name if media_dir is not None else None
        if path is None or not path.is_file():
            raise CaptureGap(
                f"CopyAnywhere's add definitions read {name} from the media folder, and"
                f" {media_dir or 'no media folder given'} does not hold it"
            )
        whole[name] = json.loads(path.read_text(encoding="utf-8"))

    carried = SourceNotes()
    for _ in range(COPY_ANYWHERE_ROUNDS):
        fixture = export_fixture(
            store_path, run_id, copy_anywhere=config, source=carried, media=whole
        )
        assert fixture.real_ids is not None
        searches: list[tuple[str, str]] = []
        result = _replay_with_copy_anywhere(fixture, lenient, searches)
        in_corpus = {fixture.real_ids[note["id"]] for note in fixture.corpus["notes"]}
        found = source_matches(source, searches, fixture.real_ids, started) - in_corpus
        if not found:
            break
        carried = source_notes(source, set(carried.records) | found, started)
    else:
        raise CaptureGap(
            f"CopyAnywhere's searches still found notes to carry after {COPY_ANYWHERE_ROUNDS}"
            " rounds: a definition searches with what it found"
        )
    if not whole:
        return fixture, carried

    chars = _characters(fixture.corpus["notes"], fixture.expected["notes"], result.notes)
    cut = {name: _cut_media(content, chars) for name, content in whole.items()}
    final = export_fixture(store_path, run_id, copy_anywhere=config, source=carried, media=cut)
    if not lenient and _replay_with_copy_anywhere(final, lenient, None).notes != result.notes:
        raise CaptureGap(
            f"cut to the replay's characters, {', '.join(sorted(cut))} made CopyAnywhere write"
            " otherwise than the whole files did"
        )
    return final, carried


def _replay_with_copy_anywhere(
    fixture: Fixture, lenient: bool, searches: Optional[list[tuple[str, str]]]
) -> ReplayResult:
    cassette = Cassette(fixture.cassette["entries"], lenient=True) if lenient else None
    return replay(fixture, cassette=cassette, copy_anywhere=True, searches=searches)


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


# A new note's placeholder in a field: make_new_note_id's negative number, standing alone
PLACEHOLDER_RE = re.compile(r"(?<![\d-])-\d{7}(?!\d)")


def _failed_adds(connection: sqlite3.Connection, run_id: Optional[int] = None) -> list[int]:
    """The placeholders of a run's notes whose add failed (`note.add` with an `add_error`), in
    the order of the notes' content as the cleanup proposed them, ids and placeholders masked:
    the same order in every run of the same work, whatever order the adds came in. Every run's
    in the store without `run_id` (a replay's own store holds its one run)."""
    where, params = ("", ()) if run_id is None else (" AND run_id = ?", (run_id,))
    placeholders = []
    for (payload_json,) in connection.execute(
        f"SELECT payload_json FROM events WHERE kind = 'note.add'{where} ORDER BY event_id",
        params,
    ):
        payload = json.loads(payload_json or "null") or {}
        if payload.get("add_error") and payload.get("placeholder") is not None:
            placeholders.append(int(payload["placeholder"]))
    proposed = {
        note_id: json.loads(text)
        for note_id, text in connection.execute(
            "SELECT note_id, text FROM note_snapshots JOIN blobs ON hash = note_hash"
            f" WHERE stage = 'proposed'{where}",
            params,
        )
    }

    def content(placeholder: int) -> str:
        fields = (proposed.get(placeholder) or {}).get("fields", {})
        masked = {
            name: PLACEHOLDER_RE.sub("#", ID_RE.sub("#", value)) for name, value in fields.items()
        }
        return json.dumps(masked, ensure_ascii=False, sort_keys=True)

    return sorted(set(placeholders), key=content)


def normalized_notes(
    notes: Iterable[Mapping[str, Any]],
    new_notes: Mapping[int, Optional[int]],
    ids: Optional[Callable[..., Any]] = None,
    failed: Sequence[int] = (),
) -> list[dict]:
    """Notes as a replay can compare them: each `{"id", "notetype", "deck", "fields", "tags"}`,
    `new_notes` the run's added notes (id -> the placeholder it replaced, or None).

    A new note's id is whatever the collection handed out, and its placeholder was random, so
    both become symbols: `new-N`, numbered in the order of the notes' content (ids masked),
    and `placeholder:new-N`, wherever they appear. `failed` are the placeholders of the notes
    whose add failed, in a fixed order (`_failed_adds`): the word arrays keep them on purpose,
    and they become `placeholder:failed-N`. `ids`, given, maps every other id in a field
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
    placeholders.update(
        {str(placeholder): f"placeholder:failed-{n + 1}" for n, placeholder in enumerate(failed)}
    )

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


def build_collection(
    corpus: Mapping[str, Any], path: Path, background: int = 0
) -> tuple[Any, frozenset[int]]:
    """A fresh collection at `path` holding the corpus (`fill_collection`) and `background`
    notes no request of the run can find (`background_notes`), and the background notes' ids.

    The ids themselves, not their range: a note added in a millisecond whose id a note already
    has gets `max(id) + 1` from Anki, which with background notes in the collection is past
    them, and from 2033 every new note's id is in their range anyway."""
    from anki_shared.testing import real_anki

    notes = background_notes(corpus, background)
    col = real_anki.open_collection(path)
    try:
        fill_collection(col, corpus)
        if notes:
            fill_collection(col, {"notetypes": [], "decks": [], "notes": notes})
    except BaseException:
        # Left open, the file stays locked for as long as the exception is kept
        col.close()
        raise
    return col, frozenset(note["id"] for note in notes)


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
    searches: Optional[list[tuple[str, str]]] = None,
) -> ReplayResult:
    """Build the fixture's corpus in a fresh collection under `workdir` (a temporary directory
    by default), run the op over its selected notes with nothing reaching a network, and return
    the collection's state after it, normalized as the fixture's expected state is.

    `copy_anywhere` puts the corpus's CopyAnywhere definitions on the add hook for the run
    (`copy_anywhere_on_add`); the result is then compared with `expected_copy_anywhere`. The
    searches they make go into `searches`, when given.

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
    col: Any = None
    try:
        # Inside the try: a corpus that fails to build still has its directory removed
        col, background_ids = build_collection(
            fixture.corpus, root / "collection.anki2", background
        )
        stub.col = col
        stub.pm.set_profile_folder(root / "profile")

        def set_config(config: dict) -> None:
            stub.addonManager.configs[PACKAGE] = config

        def run_op(spec: Any, nids: list) -> None:
            run, _ = spec.notes_run(nids)
            with ExitStack() as stack:
                if around_run is not None:
                    stack.enter_context(around_run())
                # Around the run only, after the corpus is built: on the notes the run adds
                if copy_anywhere:
                    stack.enter_context(copy_anywhere_on_add(stub, definitions or {}, searches))
                run(col)

        return _replay(
            fixture, col, stub.pm.media_folder(), root, set_config, run_op, cassette,
            read_store, estimates, background_ids,
        )
    finally:
        stub.col = saved_col
        stub.addonManager.configs = saved_configs
        stub.pm._profile_folder = saved_profile
        if col is not None:
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
def copy_anywhere_on_add(
    mw: Any, config: Mapping[str, Any], searches: Optional[list[tuple[str, str]]] = None
) -> Iterator[None]:
    """CopyAnywhere's add-note definitions run on every note added inside the block, as they do
    in Anki: its handler on `note_will_be_added`, and only that one (its editor and review hooks
    have no use here), with `config` as its addon config. Its logs go to `REPLAY_LOGS`, and the
    searches it makes of the collection into `searches`, when given
    (`export_with_copy_anywhere`).

    For issue #11's stage 5: the match op's cleanup adds its notes with `col.add_note`, which
    fires the hook for each, so its cost lands in the add phase, and what CopyAnywhere reads it
    reads from the whole collection, as in Anki. A script's `headless` registered the
    copy_anywhere package; under pytest the root conftest did."""
    from anki.hooks import note_will_be_added
    from copy_anywhere import logging_setup
    from copy_anywhere.hooks import note_hooks

    def on_add(col: Any, note: Any, deck_id: Any) -> None:
        if searches is None:
            note_hooks.run_copy_fields_on_add(note, deck_id)
            return
        with _searches_recorded(col, searches):
            note_hooks.run_copy_fields_on_add(note, deck_id)

    configs = mw.addonManager.configs
    saved = configs.get(COPY_ANYWHERE)
    configs[COPY_ANYWHERE] = dict(config)
    handler = note_hooks.contained("on add", on_add, note_hooks._nothing)
    note_will_be_added.append(handler)
    try:
        with replay_logs(logging_setup):
            yield
    finally:
        note_will_be_added.remove(handler)
        if saved is None:
            configs.pop(COPY_ANYWHERE, None)
        else:
            configs[COPY_ANYWHERE] = saved


@contextmanager
def _searches_recorded(col: Any, searches: list[tuple[str, str]]) -> Iterator[None]:
    """Each `find_notes` and `find_cards` of `col` on this thread inside the block, as
    `(method, query)` in `searches`: a definition's searches all go through them
    (copy_anywhere/logic/execution/context.py), on the thread that adds the note."""
    thread = threading.get_ident()

    def recording(method: str) -> Callable[..., Any]:
        search = getattr(col, method)

        def recorded(query: Any, *args: Any, **kwargs: Any) -> Any:
            if threading.get_ident() == thread:
                searches.append((method, str(query)))
            return search(query, *args, **kwargs)

        return recorded

    methods = ("find_notes", "find_cards")
    for method in methods:
        setattr(col, method, recording(method))
    try:
        yield
    finally:
        # The instance's own attributes go, and the class's methods are what it has again
        for method in methods:
            delattr(col, method)


@contextmanager
def replay_logs(module: Any) -> Iterator[None]:
    """`module.logs_dir()`, an addon's log folder (call_logging's, CopyAnywhere's
    logging_setup's), as `REPLAY_LOGS` beside it for the block. Both ask it for each file they
    open, so everything the block runs logs there."""
    saved = module.logs_dir
    directory = str(Path(saved()).parent / REPLAY_LOGS)
    module.logs_dir = lambda: directory
    try:
        yield
    finally:
        module.logs_dir = saved


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
    background_ids: frozenset[int] = frozenset(),
) -> ReplayResult:
    from japanese_note_ai_ops import call_logging

    with replay_logs(call_logging):
        return _replay_logged(
            fixture, col, media, root, set_config, run_op, cassette, read_store, estimates,
            background_ids,
        )


def _replay_logged(
    fixture: Fixture,
    col: Any,
    media: Path,
    root: Path,
    set_config: Callable[[dict], None],
    run_op: RunOp,
    cassette: Optional[Cassette],
    read_store: Optional[Callable[[Path], Any]],
    estimates: Optional[dict],
    background_ids: frozenset[int],
) -> ReplayResult:
    from japanese_note_ai_ops import call_logging
    from japanese_note_ai_ops.async_api_ops import base_ops, capture, run_errors
    from japanese_note_ai_ops.async_api_ops.match_words_to_notes import match_words_spec
    from japanese_note_ai_ops.configuration import MEANINGS_DICT_FILE
    from japanese_note_ai_ops.sync_local_ops import mdx_dictionary

    # Installing the replay's own store would close this one, and nothing can open it again as
    # its installer did (its keep_days, versions and profile): refused, not lost for the process
    installed = capture.current_store()
    if installed is not None:
        raise RuntimeError(
            f"a capture store is installed ({installed.path}); a replay records into one of its"
            " own, which would close it: shut it down first"
        )
    corpus = fixture.corpus
    saved_helper = mdx_dictionary.mdx_helper
    # Its own capture, notes and all, is how the replay learns which notes it added and what
    # it decided, the way the export read them from the capture run
    set_config(
        dict(corpus["config"], capture_calls=True, capture_notes=True, log_to_console=False)
    )
    # A file of its own, at the corpus's log level, as the menu opens one for the op it starts.
    # Without it the run's records went to whatever file the process had opened last, outside
    # the replay's folder; closed at the end, so nothing after the replay writes into it
    call_logging.start_call_log(corpus["op"])
    media.mkdir(parents=True, exist_ok=True)
    (media / MEANINGS_DICT_FILE).write_text(
        json.dumps(corpus["meanings"], ensure_ascii=False), encoding="utf-8"
    )
    for name, content in (corpus.get("media") or {}).items():
        (media / name).write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    if cassette is None:
        cassette = Cassette(fixture.cassette["entries"])
    dictionary = Dictionary(corpus["dictionary"])
    errors: list[str] = []
    # What they replace is put back: a script's Headless prints run errors through its own
    previous_deliver = run_errors.deliver_with(lambda title, text: errors.append(f"{title}: {text}"))
    previous_responder = base_ops.set_responder(cassette)
    try:
        # The modules that use it imported it by name, and hold their own reference
        _point_modules_at(dictionary)
        store = root / "capture.sqlite3"
        if not capture.install(str(store), keep_days=None):
            raise RuntimeError(f"the replay's capture store {store} did not open")
        nids = [record["id"] for record in corpus["notes"] if record["selected"]]
        with memory_estimates({} if estimates is None else estimates):
            started = time.monotonic()
            run_op(match_words_spec(), nids)
            seconds = time.monotonic() - started
        capture.shutdown(timeout=30.0)
        events = _run_events(store)
        return ReplayResult(
            notes=normalized_notes(
                _collection_notes(col, background_ids), events.added, failed=events.failed
            ),
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
        base_ops.set_responder(previous_responder)
        run_errors.deliver_with(previous_deliver)
        capture.shutdown(timeout=5.0)
        _point_modules_at(saved_helper)
        call_logging.close_previous_log_handlers(call_logging.addon_logger())


def _point_modules_at(helper: Any) -> None:
    """Make `helper` the dictionary of every module of the addon that holds `mdx_helper`:
    mdx_dictionary's own, and those that imported it by name."""
    import sys

    prefix = PACKAGE + "."
    for name, module in list(sys.modules.items()):
        if name.startswith(prefix) and getattr(module, "mdx_helper", None) is not None:
            setattr(module, "mdx_helper", helper)


@dataclass
class _RunEvents:
    added: dict[int, Optional[int]] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    meanings: dict[str, Any] = field(default_factory=dict)
    failed: list[int] = field(default_factory=list)


def _run_events(store: Path) -> _RunEvents:
    """What the replay's own capture recorded, read as the export reads a capture run's."""
    with closing(sqlite3.connect(str(store))) as connection:
        rows = connection.execute(
            "SELECT kind, note_id, task_id, payload_json FROM events ORDER BY event_id"
        ).fetchall()
        failed = _failed_adds(connection)
    events = _RunEvents(failed=failed)
    for kind, note_id, task_id, payload_json in rows:
        payload = json.loads(payload_json or "null")
        if kind == "note.added":
            events.added[payload["note_id"]] = payload.get("placeholder")
        elif kind == "match.decision":
            events.decisions.append({"note": note_id, "task": task_id, **payload})
        elif kind == "meanings.final":
            events.meanings[payload["key"]] = payload["value"]
    return events


def _collection_notes(col: Any, background_ids: frozenset[int] = frozenset()) -> list[dict]:
    """Every note of the collection but the background notes (`build_collection`)."""
    decks = {int(deck.id): deck.name for deck in col.decks.all_names_and_ids()}
    notes = []
    for nid in col.find_notes(""):
        if nid in background_ids:
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
