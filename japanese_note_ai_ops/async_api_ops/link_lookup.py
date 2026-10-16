"""Link IDs read from the collection: a note type's index, and the notes holding given IDs.

A note holds its link ID field's value, or its note id when the field holds none
(link_ids.py), so no one search finds the note holding an ID: a field search finds the notes
whose field holds it, `nid:` the note that may hold it as its note id, and only the field read
back says which of them do. Two ways to ask, for two kinds of caller:

- `link_index`, one SQL pass over a type's notes, for an op that resolves many IDs. A run
  builds it once (`LinkIndexCache`, which the match op's tasks share): a run never writes to
  the collection before its cleanup, so the index holds for the whole run, as word_index's
  does. Notes the run creates are not in it: it hands out their IDs (`LinkIndex.new_link_id`),
  one for the whole run per type, so no two get one.
- `find_note_ids` and `find_note_id`, a search and a read of the notes found, in one turn with
  the collection, for a few IDs.

Both answer as `link_ids.LinkIndex`: of two notes holding one ID, the one keeping it. On a
run's worker threads the collection is read through collection_access, which a cancel abandons;
a sync op or a hook passes `col` (its `mw.col`, or the one a CollectionOp hands it). Every note
an answer names is noted for a run that records its notes (`capture_notes.found`), as the word
index's are: a replay has to find it too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, Optional, cast

from anki.utils import ids2str
from aqt import mw

from ..link_ids import SEARCH_KEYWORDS, LinkIndex, field_search, is_link_id, link_ids_search
from ..note_roles import LayoutError, link_id_field, note_type_search
from . import capture_notes
from .collection_access import run_on_collection, run_on_collection_async

if TYPE_CHECKING:
    from anki.collection import Collection
    from anki.dbproxy import DBProxy
    from anki.notes import NoteId

logger = logging.getLogger(__name__)


class _LinkField(NamedTuple):
    """Where a type's link IDs are: its notetype id, and its link ID field's name and ord, both
    None when its block names none and every note's ID is its note id."""

    mid: int
    name: Optional[str]
    ord: Optional[int]


def _link_field(col: Collection, config: Mapping[str, Any], note_type_name: str) -> _LinkField:
    """Raises LayoutError when the settings name a type or a field the collection lacks, or a
    field no search can name. Answering from the note ids alone instead would read every ID
    the field holds as held by no note, and a run would unlink words or duplicate notes."""
    notetype = col.models.by_name(note_type_name)
    if notetype is None:
        raise LayoutError(
            f'The settings name note type "{note_type_name}", which is not in the collection.'
        )
    field = link_id_field(config, note_type_name)
    if field is None:
        return _LinkField(notetype["id"], None, None)
    ords = col.models.field_map(notetype)
    if field not in ords:
        raise LayoutError(
            f'Note type "{note_type_name}" has no field "{field}", which the settings name as'
            " its link ID field."
        )
    if field.lower() in SEARCH_KEYWORDS:
        raise LayoutError(
            f'The link ID field "{field}" of note type "{note_type_name}" cannot be searched:'
            f' Anki reads "{field}:" as a search of its own. Rename the field and the settings.'
        )
    return _LinkField(notetype["id"], field, ords[field][0])


def _db(col: Collection) -> DBProxy:
    db = col.db
    if db is None:
        # Only true of a closed collection, which nothing can be running against. Raising
        # rather than answering that no note holds anything: a run would unlink every word
        raise RuntimeError("Cannot read link IDs, the collection is closed")
    return db


def _index_rows(
    col: Collection, config: Mapping[str, Any], note_type_name: str
) -> list[tuple[int, str]]:
    """(note id, link ID field value) of every note of the type, in one SQL pass."""
    field = _link_field(col, config, note_type_name)
    db = _db(col)
    if field.ord is None:
        note_ids = db.list("select id from notes where mid = ?", field.mid)
        return [(note_id, "") for note_id in note_ids]
    # field_at_index is Anki's own function on the collection's database: the field alone, ""
    # when a note's fields are fewer. Reading whole `flds` rows, as the word index does, would
    # bring every sentence note's word array into memory to read one short field.
    return cast(
        "list[tuple[int, str]]",
        db.all(
            "select id, field_at_index(flds, ?) from notes where mid = ?", field.ord, field.mid
        ),
    )


def _built(note_type_name: str, rows: list[tuple[int, str]], started: float) -> LinkIndex:
    index = LinkIndex.from_rows(rows, on_found=capture_notes.found)
    logger.debug(
        "Built the link index of %s over %d notes in %.3fs: %d IDs held by more than one note,"
        " %d fields holding no ID",
        note_type_name,
        len(rows),
        time.perf_counter() - started,
        len(index.duplicates),
        len(index.invalid),
    )
    return index


def link_index(
    config: Mapping[str, Any], note_type_name: str, col: Optional[Collection] = None
) -> LinkIndex:
    """Every link ID of the type's notes, read now: for a sync op over many notes, built once
    before them. Through collection_access without `col`."""
    started = time.perf_counter()
    if col is not None:
        rows = _index_rows(col, config, note_type_name)
    else:
        rows = run_on_collection(
            f"link_lookup: index of {note_type_name}",
            lambda: _index_rows(mw.col, config, note_type_name),
        )
    return _built(note_type_name, rows, started)


class LinkIndexCache:
    """One index per note type, built at most once for a run, through collection_access.

    As word_index.WordIndexCache: built on first use, and every task that asks while it is
    being built waits for that build, so a run reads a type once however many tasks start
    together.
    """

    def __init__(self) -> None:
        self._indexes: dict[tuple[str, Optional[str]], LinkIndex] = {}
        # Made inside the run's event loop, on first use: on Python 3.9 a Lock is bound to the
        # event loop current when it is made, and the cache may be made before the run's
        self._lock: Optional[asyncio.Lock] = None

    async def get(self, config: Mapping[str, Any], note_type_name: str) -> LinkIndex:
        key = (note_type_name, link_id_field(config, note_type_name))
        index = self._indexes.get(key)
        if index is not None:
            return index
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # Again under the lock: whatever queued behind a build wants that build's answer
            index = self._indexes.get(key)
            if index is None:
                started = time.perf_counter()
                rows = await run_on_collection_async(
                    f"link_lookup: index of {note_type_name}",
                    lambda: _index_rows(mw.col, config, note_type_name),
                )
                index = _built(note_type_name, rows, started)
                self._indexes[key] = index
            return index


# The most IDs one search looks for. Each search reads every note of the type once, whatever it
# asks; a cap keeps one regex, and the `nid:` list beside it, a size Anki takes in its stride
# (3000 sixteen-digit IDs took 0.1s against Anki 26.09). Past a few hundred IDs the index is
# the better way anyway.
_IDS_PER_SEARCH = 500


def _find(
    col: Collection, config: Mapping[str, Any], note_type_name: str, wanted: list[int]
) -> dict[int, int]:
    field = _link_field(col, config, note_type_name)
    type_term = note_type_search(note_type_name)
    candidates: set[int] = set()
    for start in range(0, len(wanted), _IDS_PER_SEARCH):
        chunk = wanted[start : start + _IDS_PER_SEARCH]
        nid_term = "nid:" + ",".join(str(number) for number in chunk)
        if field.name is None:
            query = f"{type_term} {nid_term}"
        else:
            if len(chunk) == 1:
                field_term = field_search(field.name, str(chunk[0]))
            else:
                field_term = link_ids_search(field.name, chunk)
            query = f"{type_term} ({field_term} OR {nid_term})"
        candidates.update(col.find_notes(query))
    if not candidates:
        return {}
    # The notes found, their fields read back: the searches find every note holding a wanted
    # ID, and also a note found by `nid:` whose field holds another ID. An index of just these
    # answers for the wanted IDs exactly as one of the whole type does.
    if field.ord is None:
        rows = [(note_id, "") for note_id in candidates]
    else:
        rows = cast(
            "list[tuple[int, str]]",
            _db(col).all(
                f"select id, field_at_index(flds, ?) from notes where id in {ids2str(candidates)}",
                field.ord,
            ),
        )
    return LinkIndex.from_rows(rows).note_ids(wanted)


def find_note_ids(
    config: Mapping[str, Any],
    note_type_name: str,
    link_ids: Iterable[int],
    col: Optional[Collection] = None,
) -> dict[int, NoteId]:
    """The note of the type holding each of these link IDs, the one keeping it when several do
    (link_ids.LinkIndex); an ID no note holds, or a number no note can hold (0, a legacy
    negative placeholder), is left out. One turn with the collection, through
    collection_access without `col`."""
    wanted = sorted({number for number in link_ids if is_link_id(number)})
    if not wanted:
        return {}
    if col is not None:
        found = _find(col, config, note_type_name, wanted)
    else:
        found = run_on_collection(
            f"link_lookup: {len(wanted)} link IDs of {note_type_name}",
            lambda: _find(mw.col, config, note_type_name, wanted),
        )
    # On the calling thread, which has the run's capture context; the collection's worker has
    # none
    capture_notes.found(found.values())
    return {number: cast("NoteId", note_id) for number, note_id in found.items()}


def find_note_id(
    config: Mapping[str, Any],
    note_type_name: str,
    held: int,
    col: Optional[Collection] = None,
) -> Optional[NoteId]:
    """The note of the type holding this link ID, as `find_note_ids`; None when none does."""
    return find_note_ids(config, note_type_name, [held], col=col).get(held)
