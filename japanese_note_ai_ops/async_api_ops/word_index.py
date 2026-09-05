"""Every vocab note's word, reading and sort fields, read once per run.

The matching op used to answer three questions with three collection searches per word: which
notes carry this word, which of those carry this reading, and which sort fields already hold a
reading or meaning marker for it. Anki can only answer any of them with a scan of every note
of the notetype - the searched fields are note fields, and no index exists or can exist for
them - and the collection runs one query at a time, so several hundred concurrent tasks queue
behind each other for a table scan apiece.

Measured on a 33-minute run: about 2,400 scans at ~0.39s each, roughly half the collection's
entire busy time, to retrieve 4,347 notes in total - under two notes per scan.

All three questions are answerable from four dicts built by reading the same table once. The
build is a single turn with the collection (~0.55s over 18,855 notes, ~9 MB resident) and a
lookup is tens of microseconds, so the scans stop being the run's binding constraint.

This is sound for the same reason the search memoisation it replaces was: a run does not write
to the collection. Every update_notes, remove_notes and add_note happens in base_ops' cleanup
phase after the ops have finished, so what the notes table says at the start of a run is what
it says throughout. Notes an op creates live in notes_to_add_dict, which every call site
already layers on top of query results rather than searching for.

The one field an op does rewrite in memory is the sort field, and that does not affect the
index either: the queries served here are the ones that ran against the collection, which
holds exactly what the index holds, and the call sites that want the edited object go on
taking it from notes_to_update_dict by id the way they already did.
"""

import asyncio
import logging
import re
import time
import unicodedata
from typing import TYPE_CHECKING, Iterable, NamedTuple, Optional, Sequence, cast

from aqt import mw

from .collection_access import run_on_collection_async

if TYPE_CHECKING:
    from anki.notes import NoteId

logger = logging.getLogger(__name__)

# How Anki packs a note's fields into the one `flds` column
FIELD_SEPARATOR = "\x1f"

# The marker that takes a note out of matching entirely, written by hand rather than by the
# add-on. The searches spell it as a negated `re:` term on the sort field, and Anki's re: is
# case-insensitive unless told otherwise.
X_MARKER_RE = re.compile(r"\(x\d\)", re.IGNORECASE)

# The two terms the meaning-group query puts on the sort field, as this module's own regexes.
# Deliberately not X_MARKER_RE: that query spells the exclusion `x\d+`, without the brackets
# and with more than one digit allowed, and an index that answers a query has to answer the
# query as written rather than as it might have been.
MEANING_MARKER_RE = re.compile(r"m\d+", re.IGNORECASE)
GROUP_EXCLUDED_RE = re.compile(r"x\d+", re.IGNORECASE)


def index_key(value: str) -> str:
    """The form a field value is indexed and looked up under.

    Anki's `field:value` compares case-insensitively, under a collation that also treats the
    two Unicode compositions of a character as equal, so the keys have to be folded the same
    way or a lookup would miss a note the search would have found. For Japanese text both
    steps are no-ops; they matter for the fields that hold romaji or mixed scripts.
    """
    return unicodedata.normalize("NFC", value).casefold()


def sort_field_base(sort_value: str) -> str:
    """The word a sort field value starts with, before any (kun)/(rN)/(mN) markers.

    Every site that rewrites a sort field rebuilds it as base + markers and never touches the
    base, so this is stable for the length of a run even though the markers are not.
    """
    marker_start = sort_value.find("(")
    return (sort_value if marker_start == -1 else sort_value[:marker_start]).strip()


class WordFields(NamedTuple):
    """The four field names one notetype's config points the word queries at.

    Doubles as the index's cache key: two notetypes configured with the same field names are
    searched by the same queries and so share one index.
    """

    kanjified: str
    normal: str
    reading: str
    sort: str


class FieldOrds(NamedTuple):
    """Where those four sit in one notetype's `flds`, or None where it has no such field."""

    kanjified: Optional[int]
    normal: Optional[int]
    reading: Optional[int]
    sort: Optional[int]


def _value_at(values: "Sequence[str]", field_ord: int) -> str:
    # A note saved before a field was added to its notetype can be short of the ordinal
    return values[field_ord] if field_ord < len(values) else ""


class WordIndex:
    """The four maps, built from one pass over the notes table."""

    __slots__ = ("fields", "by_kanjified", "by_normal", "reading_of", "sort_of", "by_sort_base")

    def __init__(self, fields: WordFields):
        self.fields = fields
        # word -> notes carrying it, one map per field a word query looks in
        self.by_kanjified: "dict[str, list[NoteId]]" = {}
        self.by_normal: "dict[str, list[NoteId]]" = {}
        # Present for every note whose notetype has the field, the empty string included: the
        # matching code distinguishes "no reading field" from "an empty one", so this has to
        # as well
        self.reading_of: "dict[NoteId, str]" = {}
        self.sort_of: "dict[NoteId, str]" = {}
        # base word -> notes whose sort field starts with it, for the marker query
        self.by_sort_base: "dict[str, list[NoteId]]" = {}

    @classmethod
    def from_rows(
        cls,
        fields: WordFields,
        ords_by_mid: "dict[int, FieldOrds]",
        rows: "Iterable[tuple[int, int, str]]",
    ) -> "WordIndex":
        """Build from `select id, mid, flds from notes` rows. Pure - no collection needed."""
        index = cls(fields)
        for raw_note_id, mid, flds in rows:
            ords = ords_by_mid.get(mid)
            if ords is None:
                continue
            note_id = cast("NoteId", raw_note_id)
            values = flds.split(FIELD_SEPARATOR)
            if ords.kanjified is not None:
                value = _value_at(values, ords.kanjified)
                if value:
                    index.by_kanjified.setdefault(index_key(value), []).append(note_id)
            if ords.normal is not None:
                value = _value_at(values, ords.normal)
                if value:
                    index.by_normal.setdefault(index_key(value), []).append(note_id)
            if ords.reading is not None:
                index.reading_of[note_id] = _value_at(values, ords.reading)
            if ords.sort is not None:
                value = _value_at(values, ords.sort)
                index.sort_of[note_id] = value
                if value:
                    base = index_key(sort_field_base(value))
                    index.by_sort_base.setdefault(base, []).append(note_id)
        return index

    def reading(self, note_id: "NoteId") -> Optional[str]:
        """This note's reading, or None if its notetype has no reading field.

        None is the case the matching code used to express as `word_reading_field not in note`,
        and it means the note cannot be matched by reading at all.
        """
        return self.reading_of.get(note_id)

    def is_excluded(self, note_id: "NoteId") -> bool:
        """Whether the negated `(xN)` term on the sort field drops this note.

        A note whose notetype has no sort field is not dropped: that term is a negated field
        search, and a field search only ever matches notes that have the field.
        """
        return bool(X_MARKER_RE.search(self.sort_of.get(note_id, "")))

    def matching_note_ids(
        self,
        kanjified_values: "Iterable[str]",
        normal_values: "Iterable[str]",
        only_note_id: "Optional[NoteId]" = None,
    ) -> "list[NoteId]":
        """The notes a word query would have found, minus the ones its (xN) term excludes.

        The values are the alternatives the query ORed together per field: the word itself, its
        する form, the honorific spelled with kana, and for a kana-only word its reading.

        Returned in id order, which is the order a table scan produced them in.
        """
        found: "set[NoteId]" = set()
        for value in kanjified_values:
            found.update(self.by_kanjified.get(index_key(value), ()))
        for value in normal_values:
            found.update(self.by_normal.get(index_key(value), ()))
        if only_note_id is not None:
            found &= {only_note_id}
        return sorted(note_id for note_id in found if not self.is_excluded(note_id))

    def marker_note_ids(self, word: str, marker_regex: str) -> "list[NoteId]":
        """The notes whose sort field is `word` plus nothing but reading/meaning markers."""
        pattern = re.compile(marker_regex, re.IGNORECASE)
        if re.escape(word) == word:
            # The regex is anchored on the word and a sort field value is its base word
            # followed only by parenthesised markers, so everything the regex can match is in
            # this one bucket.
            candidates: "Iterable[NoteId]" = self.by_sort_base.get(index_key(word), ())
        else:
            # The caller interpolates the word into the regex unescaped, so a metacharacter in
            # it could match a sort field whose base is something else entirely. Rare enough
            # to be worth paying a pass over the index for rather than getting wrong.
            candidates = self.sort_of.keys()
        return sorted(
            note_id for note_id in candidates if pattern.search(self.sort_of.get(note_id, ""))
        )

    def covers(self, kanjified: str, normal: str, reading: str, sort: str) -> bool:
        """Whether this index was built over exactly these four fields.

        A caller with its own field names has to ask before it trusts a lookup: two notetypes
        can be configured differently, and an index built over one set of fields cannot answer
        a query written against another. The matching op happens to configure `word_field` and
        `word_kanjified_field` to the same field, which is what lets one index serve both, but
        nothing makes that true of every profile.
        """
        return self.fields == WordFields(
            kanjified=kanjified, normal=normal, reading=reading, sort=sort
        )

    def meaning_group_note_ids(
        self,
        reading: str,
        normal_value: str,
        kanjified_value: str,
        exclude_note_id: "Optional[NoteId]" = None,
    ) -> "Optional[list[NoteId]]":
        """The other notes in one note's meaning group, or None if it cannot be answered here.

        The search this replaces is `get_other_meaning_notes`'s, a whole-collection regex over
        the sort field run once per note that needed cleaning - 898 of them in one run, to
        retrieve six notes in total. Spelled out, it asks for notes that carry a meaning marker
        and no exclusion marker, share this note's reading, and match it in either word field:

            sort:re:m\\d+ -sort:re:x\\d+ -nid:N "reading:R" ("normal:V" OR "kanjified:W")

        Each of those terms is a field this index already holds. The `re:` terms become the two
        module regexes above, the field terms become the same collation-folded exact match the
        rest of the index uses, and the OR becomes a union of the two word maps.

        None means the caller has to fall back to the search: both word values being empty is
        the one case this cannot express, because `field:` with nothing after it asks for notes
        whose field is empty and the word maps deliberately hold no empty keys. It is not a
        case any real note reaches - a vocab note with neither word field filled in has nothing
        to group by - but answering it wrongly would silently split a meaning group.

        The one place a lookup can differ from the search is a word or reading containing an
        Anki search metacharacter (`*` and `_` are wildcards inside a quoted field term). The
        index matches such a value literally, which is what the caller passing a note's own
        field value means by it.
        """
        normal_key = index_key(normal_value) if normal_value else ""
        kanjified_key = index_key(kanjified_value) if kanjified_value else ""
        if not normal_key and not kanjified_key:
            return None

        found: "set[NoteId]" = set()
        if normal_key:
            found.update(self.by_normal.get(normal_key, ()))
        if kanjified_key:
            found.update(self.by_kanjified.get(kanjified_key, ()))
        if exclude_note_id is not None:
            found.discard(exclude_note_id)

        reading_key = index_key(reading)
        matching: "list[NoteId]" = []
        for note_id in found:
            note_reading = self.reading_of.get(note_id)
            # None is a notetype with no reading field at all, which a `reading:R` term cannot
            # match however R is spelled
            if note_reading is None or index_key(note_reading) != reading_key:
                continue
            sort_value = self.sort_of.get(note_id, "")
            if not MEANING_MARKER_RE.search(sort_value):
                continue
            if GROUP_EXCLUDED_RE.search(sort_value):
                continue
            matching.append(note_id)
        return sorted(matching)


def _read_notes(fields: WordFields) -> "tuple[dict[int, FieldOrds], list[tuple[int, int, str]]]":
    """One turn with the collection: the notetype ordinals, then every note that can match.

    Runs on the collection worker, so both statements happen under a single turn rather than
    letting every other waiting caller in between.
    """
    ords_by_mid: "dict[int, FieldOrds]" = {}
    for notetype in mw.col.models.all():
        ord_by_name = {field["name"]: field["ord"] for field in notetype["flds"]}
        ords = FieldOrds(
            kanjified=ord_by_name.get(fields.kanjified),
            normal=ord_by_name.get(fields.normal),
            reading=ord_by_name.get(fields.reading),
            sort=ord_by_name.get(fields.sort),
        )
        # A field search matches notes of any notetype that has a field of that name, so the
        # scan has to span all of them - but a notetype with none of the three searched fields
        # is invisible to every query this replaces.
        if ords.kanjified is None and ords.normal is None and ords.sort is None:
            continue
        ords_by_mid[notetype["id"]] = ords
    if not ords_by_mid:
        return {}, []
    db = mw.col.db
    if db is None:
        # Only true of a closed collection, which an op cannot be running against. Raising
        # rather than answering with an empty index matters: an empty one says every word is
        # unmatched, and the run would create a duplicate note for each of them.
        raise RuntimeError("Cannot build the word index, the collection is closed")
    mids = ",".join(str(mid) for mid in ords_by_mid)
    rows = cast(
        "list[tuple[int, int, str]]",
        db.all(f"select id, mid, flds from notes where mid in ({mids})"),
    )
    return ords_by_mid, rows


async def build_word_index(fields: WordFields) -> WordIndex:
    started = time.perf_counter()
    ords_by_mid, rows = await run_on_collection_async(
        f"word_index: {fields.kanjified}/{fields.normal}", lambda: _read_notes(fields)
    )
    read = time.perf_counter()
    index = WordIndex.from_rows(fields, ords_by_mid, rows)
    logger.debug(
        "Built word index over %d notes of %d notetypes in %.3fs, %.3fs of it the collection",
        len(rows),
        len(ords_by_mid),
        time.perf_counter() - started,
        read - started,
    )
    return index


class WordIndexCache:
    """One index per field set, built at most once for a run.

    Built on first use rather than up front because the field names come from the notetype of
    the note being processed, which is not known until the run has notes in hand. Every task
    that arrives while the build is in flight waits on the same lock and then finds the answer
    already there, so a run scans the notes table once however many tasks start at once.
    """

    def __init__(self):
        self._indexes: "dict[WordFields, WordIndex]" = {}
        self._lock = asyncio.Lock()

    async def get(self, fields: WordFields) -> WordIndex:
        index = self._indexes.get(fields)
        if index is not None:
            return index
        async with self._lock:
            # Checked again under the lock: everything that queued behind a build wants the
            # answer it produced, not another build.
            index = self._indexes.get(fields)
            if index is None:
                index = await build_word_index(fields)
                self._indexes[fields] = index
            return index
