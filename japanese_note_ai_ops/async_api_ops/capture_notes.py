"""Anki notes and collection state as a run that records its notes stores them.

`capture` is kept free of anki, so the records it stores are plain JSON made here: a note as
`note_record`, the note types and decks of the notes a run saw as events. Every function here
does nothing in a run that does not record its notes (`capture.notes_on()`), and checks that
before building anything, since the read points it serves run for every note of every run.

What a replay needs from a run is every note as the run first read it, and every note it wrote
as the collection held it after. The first fetch of a note is its state before the run: a run
writes nothing before its cleanup. A note the run only learned the id of (a search, the word
index) is referenced rather than fetched, and fetched once by `fetch_unread`, before the
cleanup writes, or right after a cleanup lookup (the marker tidying's), which only finds notes
the run did not write or read already, still as they were.

The meanings file is state too, outside the collection: `MeaningsRecorder` records the first
value the run read of each word's generated meanings, and the value it wrote at the end.
"""

from __future__ import annotations

import logging
import os
import threading
from functools import partial
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence

from . import capture

if TYPE_CHECKING:
    from anki.collection import Collection
    from anki.notes import Note

logger = logging.getLogger(__name__)

# The placeholder a new note is named by in the word arrays until it is added: a negative int
# in this field of its note type's config (match_words_to_notes' new_note_id_field)
PLACEHOLDER_FIELD_KEY = "new_note_id_field"

# What a notes run records beyond its notes, named in its `environment` event, so an exporter
# can tell a run that looked nothing up from one captured before lookups were recorded. Add a
# name here when a new kind of record is added
RECORDS = ["dictionary.lookup"]


def note_record(note: "Note") -> dict[str, Any]:
    """A note as the capture stores it: what building it again in another collection needs.

    `mod` and `usn` are left out: they are the collection's bookkeeping, and with them a note
    read unchanged by two runs would be stored twice.

    The fields are the ones the note holds, by name: a note saved before fields were added to
    its note type holds fewer values than the type has fields, and `items()`, which reads one
    per field, raised for it and cost the run the note's record.
    """
    return {
        "id": int(note.id),
        "guid": note.guid,
        "mid": int(note.mid),
        "fields": dict(zip(note.keys(), note.fields)),
        "tags": list(note.tags),
    }


def snapshot_notes(stage: str, notes: Iterable["Note"]) -> None:
    """Record each note at `stage` (`capture.STAGES`) under its own id, once per run and
    stage."""
    if not capture.notes_on():
        return
    for note in notes:
        capture.snapshot_note(stage, int(note.id), partial(note_record, note))


def placeholder_id(note: "Note", config: Mapping[str, Any]) -> Optional[int]:
    """The placeholder id a new note carries in its note type's `new_note_id_field`, or None
    when its type configures none or the field holds no number."""
    try:
        note_type = note.note_type()
        model_config = config.get(note_type["name"]) if note_type else None
        field = model_config.get(PLACEHOLDER_FIELD_KEY) if isinstance(model_config, dict) else None
        if not field or field not in note:
            return None
        return int(note[field])
    except (KeyError, TypeError, ValueError):
        return None


def snapshot_new_notes(
    stage: str, notes: Sequence["Note"], config: Mapping[str, Any]
) -> None:
    """Record notes not added yet, each under its placeholder id; one without a placeholder
    under a made-up id past any a run makes, so it is still recorded."""
    if not capture.notes_on():
        return
    for index, note in enumerate(notes):
        placeholder = placeholder_id(note, config)
        note_id = placeholder if placeholder is not None else -(10**15) - index
        capture.snapshot_note(stage, note_id, partial(note_record, note))


def record_added(notes: Iterable["Note"], config: Mapping[str, Any]) -> None:
    """The notes the cleanup just added: which placeholder each replaces (`note.added`)."""
    if not capture.notes_on():
        return
    for note in notes:
        capture.note_added(placeholder_id(note, config), int(note.id))


def reference_notes(note_ids: Iterable[int]) -> None:
    """The run learned of these notes without fetching them; see `fetch_unread`."""
    if capture.notes_on():
        capture.reference_notes(note_ids)


def fetch_unread(col: "Collection") -> None:
    """Fetch and record as read every note the run referenced but never fetched, bar the notes
    it added.

    Called where what the collection holds of those notes is still what the run read: at the
    start of the cleanup, before its first write; after a cleanup lookup, for the notes it
    found that the run neither read nor wrote. An id no note has (a link to a deleted note) is
    recorded as a `note.missing` event: the replay must miss it too.
    """
    if not capture.notes_on():
        return
    for note_id in capture.unread_references():
        if note_id <= 0:
            continue
        try:
            note = col.get_note(note_id)  # type: ignore[arg-type]
        except Exception:
            capture.event("note.missing", {"note_id": note_id}, note_id=note_id)
            capture.snapshot_note("read", note_id, lambda: {"id": note_id, "missing": True})
            continue
        capture.snapshot_note("read", note_id, partial(note_record, note))


def record_final(
    col: "Collection", note_ids: Iterable[int], removed: Iterable[int] = ()
) -> None:
    """The end of the cleanup: every note the run saved or added as the collection now holds
    it (`final`), the notes it removed (`note.removed`), and the note types and decks of every
    note it recorded (`record_collection`).

    `removed` is what the cleanup asked to remove; a note the collection still holds is final,
    not removed. The removal is reported and the cleanup carries on when it fails (an addon's
    delete hook raising, before anything is deleted), and taken on trust, such a note was
    recorded gone while the collection kept it."""
    if not capture.notes_on():
        return
    asked = {int(nid) for nid in removed}
    gone: list[int] = []
    for note_id in sorted({int(nid) for nid in note_ids if int(nid) > 0} | asked):
        try:
            note = col.get_note(note_id)  # type: ignore[arg-type]
        except Exception:
            if note_id in asked:
                gone.append(note_id)
            else:
                capture.event("note.missing", {"note_id": note_id, "stage": "final"})
            continue
        capture.snapshot_note("final", note_id, partial(note_record, note))
    if gone:
        capture.event("note.removed", {"note_ids": gone})
    record_collection(col, capture.recorded_note_ids())


def record_collection(col: "Collection", note_ids: Iterable[int]) -> None:
    """Record the note types of these notes and the decks their cards are in, as events: what a
    replay needs to build them again beyond the notes themselves. One query for each."""
    if not capture.notes_on():
        return
    try:
        db = col.db
        ids = ",".join(str(nid) for nid in sorted({int(nid) for nid in note_ids if int(nid) > 0}))
        if db is None or not ids:
            return
        for (mid,) in db.all(f"select distinct mid from notes where id in ({ids})"):
            notetype = col.models.get(mid)
            if notetype is None:
                continue
            capture.event(
                "notetype",
                {
                    "mid": mid,
                    "name": notetype["name"],
                    "fields": [field["name"] for field in notetype["flds"]],
                    "sort_field": notetype.get("sortf", 0),
                    "templates": [template["name"] for template in notetype["tmpls"]],
                },
            )
        decks: dict[str, list[int]] = {}
        for nid, did in db.all(f"select distinct nid, did from cards where nid in ({ids})"):
            decks.setdefault(str(nid), []).append(did)
        names: dict[str, str] = {}
        for did in sorted({did for dids in decks.values() for did in dids}):
            deck = col.decks.get(did, default=False)  # type: ignore[arg-type]
            if deck is not None:
                names[str(did)] = deck["name"]
        capture.event("decks", {"notes": decks, "names": names})
    except Exception:
        logger.warning(
            "Capture: the collection's note types and decks were not recorded", exc_info=True
        )


def undo_step(col: "Collection") -> Optional[dict[str, Any]]:
    """The undo queue's head: what the next undo would be, and the step counter."""
    try:
        status = col.undo_status()
        return {"undo": status.undo, "last_step": status.last_step}
    except Exception:
        return None


def record_note_add(
    col: "Collection",
    note: "Note",
    config: Mapping[str, Any],
    seconds: float,
    merge_seconds: Optional[float],
    undo_before: Optional[dict[str, Any]],
    add_error: Optional[str] = None,
    merge_error: Optional[str] = None,
) -> None:
    """One note of the cleanup's adding (`note.add`): how long the add took, every addon's
    note_will_be_added hook included, and the merge into the run's undo entry, and the undo
    queue's head before and after them both. A failed add and a failed merge are told apart:
    an entry a hook makes of its own is what gets in the merge's way."""
    if not capture.notes_on():
        return
    capture.event(
        "note.add",
        {
            "placeholder": placeholder_id(note, config),
            "seconds": round(seconds, 5),
            "merge_seconds": None if merge_seconds is None else round(merge_seconds, 5),
            "undo_before": undo_before,
            "undo_after": undo_step(col),
            "add_error": add_error,
            "merge_error": merge_error,
        },
        note_id=int(note.id) or None,
    )


def record_undo_status(col: "Collection", when: str) -> None:
    """The undo queue's state at `when`: what the next undo and redo would be. The run's writes
    merge into one entry, so its label is what the user would undo."""
    if not capture.notes_on():
        return
    try:
        status = col.undo_status()
        capture.event(
            "undo",
            {"when": when, "undo": status.undo, "redo": status.redo, "last_step": status.last_step},
        )
    except Exception:
        logger.warning("Capture: the undo status was not recorded", exc_info=True)


def record_environment(col: "Collection", config: Mapping[str, Any]) -> None:
    """What the run reads besides its notes and its config, at its start: the dictionary files
    (`mdx_filenames`, under the addon's user_files) and the collection's size and last change,
    so a replay can tell whether it reads the same."""
    if not capture.notes_on():
        return
    try:
        from ..configuration import ADDON_USER_FILES_DIR

        filenames = config.get("mdx_filenames") or []
        dictionaries = file_facts(
            os.path.join(ADDON_USER_FILES_DIR, name) for name in filenames if isinstance(name, str)
        )
        collection: dict[str, Any] = {}
        if col.db is not None:
            count, last_mod = col.db.first("select count(), max(mod) from notes") or (None, None)
            collection = {"notes": count, "last_mod": last_mod}
        capture.event(
            "environment",
            {"dictionaries": dictionaries, "collection": collection, "records": RECORDS},
        )
    except Exception:
        logger.warning("Capture: the run's environment was not recorded", exc_info=True)


def file_facts(paths: Iterable[str]) -> list[dict[str, Any]]:
    """Name, size and modification time of each file: enough to tell whether a replay reads the
    files the run read, without hashing hundreds of MB of dictionary."""
    found: list[dict[str, Any]] = []
    for path in paths:
        try:
            stat = os.stat(path)
            found.append(
                {"name": os.path.basename(path), "size": stat.st_size, "mtime": int(stat.st_mtime)}
            )
        except OSError:
            found.append({"name": os.path.basename(path), "missing": True})
    return found


class MeaningsRecorder(dict):
    """The generated meanings file, as a run holds it: records the first value it read of each
    key (`meanings.read`, a missing key as null) and, at `record_final`, the value of each key
    it wrote (`meanings.final`).

    A dict, so the code that reads and writes it is unchanged; `get`, `in`, `[key]` and
    `[key] = ...` are all that code does with it, and those are what is recorded. Tasks on
    several threads use it at once, so the first read of a key is decided under a lock.
    """

    def __init__(self, data: Mapping[str, Any]) -> None:
        super().__init__(data)
        self._lock = threading.Lock()
        self._read: set[str] = set()
        self._written: set[str] = set()

    def _record_read(self, key: Any) -> None:
        if not isinstance(key, str):
            return
        with self._lock:
            if key in self._read or key in self._written:
                return
            self._read.add(key)
        capture.event("meanings.read", {"key": key, "value": dict.get(self, key)})

    def __getitem__(self, key: Any) -> Any:
        self._record_read(key)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._record_read(key)
        return super().get(key, default)

    def __contains__(self, key: Any) -> bool:
        self._record_read(key)
        return super().__contains__(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        # Read first if it was not: the replay starts from the value before this write
        self._record_read(key)
        with self._lock:
            self._written.add(key)
        super().__setitem__(key, value)

    def record_final(self) -> None:
        with self._lock:
            written = sorted(self._written)
        for key in written:
            capture.event("meanings.final", {"key": key, "value": dict.get(self, key)})


def record_meanings(data: dict) -> dict:
    """`data` as the run should hold it: a `MeaningsRecorder` in a run that records its notes,
    else `data` itself."""
    if not capture.notes_on():
        return data
    return MeaningsRecorder(data)


def record_final_meanings(data: Any) -> None:
    """The meanings a run wrote, just before they are saved; nothing for a plain dict."""
    if isinstance(data, MeaningsRecorder):
        data.record_final()


# The collection read points of a run, named for what collection_access and the caches do
def fetched(notes: Iterable["Note"]) -> None:
    """Notes a run fetched outside its cleanup: their state before the run."""
    snapshot_notes("read", notes)


def found(note_ids: Iterable[int]) -> None:
    """Notes a run's search or index lookup found, fetched or not: see `fetch_unread`."""
    reference_notes(note_ids)
