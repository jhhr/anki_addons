"""Link IDs: the numbers notes link each other by.

A word array's element links its vocab note by that note's link ID, and a vocab note's
`example_sentence_id_field` its example sentence note by that note's. A note's link ID is the
value of its type's link ID field (`note_roles.link_id_field`) when that is a positive integer,
else its Anki note id. The note id alone cannot be the link: an .apkg import keeps it only when
the target collection has no note with that id, otherwise the note gets a new one and an array
still holding the old id names the target's other note, without an error; and a text export
carries no note id at all. A field travels with its note. The fallback to the note id is what
leaves every existing note and link valid with nothing written.

A note the addon creates gets a generated ID before it is added, so a link to it can be written
while it is prepared, and the same input makes the same ID on a rerun or a replay.

Free of anki and aqt, like note_roles: the index is built from plain rows, and what reads the
collection is `async_api_ops/link_lookup.py`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Container, Iterable
from typing import Callable, Optional, Protocol

# Card scripts parse the arrays as JSON, and a JavaScript number is exact only below 2**53: a
# larger value would name another number there
LINK_ID_LIMIT = 2**53
# Generated IDs have 16 digits, so one is never a note id (13 digits): code that takes it for
# a note id finds no note rather than a wrong one
GENERATED_FLOOR = 10**15

# The plain spelling only, as the searches see it: Anki's field search compares the field's
# raw text, so it never finds " 123", "0123" or "123<br>" as 123 (checked against Anki 26.09),
# and the index must answer what a lookup by search answers. Sixteen digits at most (any more
# is past LINK_ID_LIMIT), which also keeps int() off a long run of digits: newer Pythons refuse
# to convert more than 4300 of them. `[0-9]`, since `\d` is any script's digits.
_LINK_ID_TEXT = re.compile(r"[1-9][0-9]{0,15}")


def parse_link_id(value: str) -> Optional[int]:
    """The link ID a link ID field's value holds, None when it holds none: anything but a
    positive integer below LINK_ID_LIMIT written plainly, so "", "0", a legacy negative
    placeholder, spaces around the digits and any other text."""
    if not _LINK_ID_TEXT.fullmatch(value):
        return None
    number = int(value)
    return number if number < LINK_ID_LIMIT else None


def is_link_id(number: object) -> bool:
    """Whether a note could hold this number as its link ID; a link to anything else, such as
    a legacy negative placeholder, leads to no note."""
    return isinstance(number, int) and 0 < number < LINK_ID_LIMIT


def link_id(value: str, note_id: int) -> int:
    """A note's link ID from its link ID field's value (`""` for a type with no such field) and
    its note id: the value's when it holds one, else the note id.

    A note not added yet (id 0) whose field holds no ID has none: raises ValueError rather than
    give 0, which an array would read as a link to no note."""
    parsed = parse_link_id(value)
    if parsed is not None:
        return parsed
    if note_id <= 0:
        raise ValueError(
            f"A note not added yet (id {note_id}) has no link ID until its field holds one:"
            f" {value!r}"
        )
    return note_id


class NoteInHand(Protocol):
    """What `note_link_id` reads of a note: an Anki note, or a test's stand-in for one."""

    @property
    def id(self) -> int: ...

    def __contains__(self, key: str, /) -> bool: ...

    def __getitem__(self, key: str, /) -> str: ...


def note_link_id(note: NoteInHand, field: Optional[str]) -> Optional[int]:
    """The link ID of a note in hand, `field` being its type's link ID field
    (note_roles.link_id_field, None for a type that names none): `link_id` of what the field
    holds and the note's id.

    None for a note not added yet whose field holds no ID, where `link_id` raises: nothing can
    link such a note, and the readers that meet one (the match op's pending notes, a note being
    added by hand) look for no link to it."""
    value = note[field] if field and field in note else ""
    if note.id <= 0 and parse_link_id(value) is None:
        return None
    return link_id(value, note.id)


# A bound on the rehashing, for a `taken` that would hold every candidate: a raise beats a
# hang. A rehash comes from a rerun making the same key again, or from a hash landing on a taken
# ID by chance (with 10**5 IDs taken, once in about 10**11 keys)
_REHASH_LIMIT = 1000


def _hashed(text: str) -> int:
    digest = hashlib.sha1(text.encode("utf-8")).digest()
    return GENERATED_FLOOR + int.from_bytes(digest, "big") % (LINK_ID_LIMIT - GENERATED_FLOOR)


def new_link_id(key: str, taken: Container[int]) -> int:
    """A generated link ID for a note made from what `key` names, one `taken` does not hold.

    The sha1 of the key's UTF-8 bytes, read as one big-endian number, taken modulo the size of
    [GENERATED_FLOOR, LINK_ID_LIMIT) and added to GENERATED_FLOOR. When `taken` holds that ID,
    the same is done for `<key>#1`, then `<key>#2`, and so on until one is free. So the same key
    with the same `taken` always gives the same ID, and two notes never get one ID as long as
    the caller puts every ID it hands out into `taken` (two keys can collide, `a#1` with `a`'s
    first rehash among them: `taken` sorts that out too).
    """
    candidate = key
    for attempt in range(1, _REHASH_LIMIT + 1):
        number = _hashed(candidate)
        if number not in taken:
            return number
        candidate = f"{key}#{attempt}"
    raise RuntimeError(f"No free link ID for {key!r} in {_REHASH_LIMIT} tries")


class LinkIndex:
    """The link IDs of one note type's notes, from rows of (note id, link ID field value).

    Two notes hold one ID when both fields hold it (Anki's Create Copy copies the field, and so
    does a CopyAnywhere duplicate), or when one's field holds the other's note id while the
    other's holds nothing, which gives it its note id. Such an ID answers with the older note
    (the lower note id): the newer one is the copy, and it is the newer that gets a fresh ID
    when the duplicate is repaired, so a link resolves to the same note before and after.

    `on_found` is told the note ids of every answer that names a note: a run that records its
    notes passes `capture_notes.found`, since a replay has to find those notes too.
    """

    __slots__ = ("_holders", "duplicates", "invalid", "_on_found")

    def __init__(self, on_found: Optional[Callable[[Iterable[int]], None]] = None) -> None:
        self._holders: dict[int, int] = {}
        # Each ID more than one note holds, to those notes, the oldest (the one it names) first
        self.duplicates: dict[int, list[int]] = {}
        # Each note whose field holds something but no link ID, to that value: its ID is its
        # note id, and the value is a legacy negative placeholder, a "0", or stray text
        self.invalid: dict[int, str] = {}
        self._on_found = on_found

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[tuple[int, str]],
        on_found: Optional[Callable[[Iterable[int]], None]] = None,
    ) -> LinkIndex:
        index = cls(on_found)
        for note_id, value in rows:
            parsed = parse_link_id(value)
            if parsed is None and value:
                index.invalid[note_id] = value
            index._add(note_id if parsed is None else parsed, note_id)
        return index

    def _add(self, held: int, note_id: int) -> None:
        holder = self._holders.get(held)
        if holder is None:
            self._holders[held] = note_id
            return
        holders = self.duplicates.setdefault(held, [holder])
        holders.append(note_id)
        holders.sort()
        self._holders[held] = holders[0]

    def _found(self, note_ids: Iterable[int]) -> None:
        if self._on_found is not None:
            self._on_found(note_ids)

    def note_id(self, held: int) -> Optional[int]:
        """The note holding this link ID, the oldest when several do; None when none does."""
        holder = self._holders.get(held)
        if holder is not None:
            self._found((holder,))
        return holder

    def note_ids(self, held: Iterable[int]) -> dict[int, int]:
        """`note_id` of each of these link IDs that a note holds."""
        found = {number: self._holders[number] for number in held if number in self._holders}
        if found:
            self._found(found.values())
        return found

    def __contains__(self, held: object) -> bool:
        """Whether a note holds this link ID. An answer like `note_id`'s, so a found holder is
        told to `on_found` too: whether a link leads anywhere has to replay alike."""
        return isinstance(held, int) and self.note_id(held) is not None


# The names Anki reads as a search of its own before a colon, in any case and whatever follows,
# a quoted term included, with no escape for them: Anki's own build_search_string writes `nid:1`
# for a field named "nid", which is a `nid:` search. These 22 of about 150 names tried against
# Anki 26.09; test_replay/test_link_lookup.py pins them.
SEARCH_KEYWORDS = frozenset(
    {
        "added",
        "card",
        "cid",
        "deck",
        "did",
        "dupe",
        "edited",
        "flag",
        "has-cd",
        "introduced",
        "is",
        "mid",
        "nc",
        "nid",
        "note",
        "preset",
        "prop",
        "rated",
        "re",
        "resched",
        "tag",
        "w",
    }
)

# What Anki reads as syntax in a field's name: `_` and `*` are wildcards (`a_b:1` also searches
# a field "axb") and a lone backslash makes the search invalid. Escaped with a backslash, as
# Anki's build_search_string does and as note_roles.note_type_search does for a note type's
# name; `"` too, though Anki removes it from a field's name, as it removes `:`.
_FIELD_NAME_SPECIAL = re.compile(r'[\\"*_]')


def field_search(field_name: str, text: str) -> str:
    """`"<field>:<text>"`, a search term for the notes whose field `field_name` matches `text`.

    `text` is search syntax as given: digits find a field holding exactly those digits, and a
    `*` in it stays a wildcard. The term is quoted, since field names have spaces. A field no
    search can name (SEARCH_KEYWORDS) raises ValueError: its term would search something else.
    """
    if field_name.lower() in SEARCH_KEYWORDS:
        raise ValueError(
            f'No search can name the field "{field_name}": Anki reads "{field_name}:" as a'
            " search of its own."
        )
    return '"' + _FIELD_NAME_SPECIAL.sub(r"\\\g<0>", field_name) + ":" + text + '"'


def link_ids_search(field_name: str, link_ids: Iterable[int]) -> str:
    """A search term for the notes whose field holds one of these link IDs, exactly:
    `"<field>:re:^(?:1|2)$"`.

    Anchored, since a regex matches anywhere in the field (`re:123` also finds 91234); a regex,
    since it is the one way to look for several values in one search. It finds the IDs held in
    the field only: a note whose field holds none holds its note id, which `nid:` finds.
    """
    ids = sorted(set(link_ids))
    if not ids:
        # `^(?:)$` would find every note whose field is empty
        raise ValueError("No link IDs to search for")
    not_ids = [number for number in ids if not is_link_id(number)]
    if not_ids:
        raise ValueError(f"Not link IDs: {not_ids}")
    return field_search(field_name, "re:^(?:" + "|".join(str(number) for number in ids) + ")$")
