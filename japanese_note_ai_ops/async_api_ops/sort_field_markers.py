"""The markers of a word's sort fields, and how the cleanup tidies them after the adding.

A vocab note's sort field is its word followed by markers, in this order when the match op
writes them: `(kun)` or `(on)`, `(rN)`, `(mN)`, as in `言葉 (kun)(r2)(m1)`. A new meaning copied
from a note numbers the meanings of that reading (mN); a new reading of a word numbers the
word's readings (rN), within each of kun and on once those are marked apart.

The match op picks each marker as it prepares a new note, from the notes it can see, so
several things leave markers behind that nothing needs:

- a new note that is never added (a cancel of the adding, a failed add, a dedupe dropping it)
  leaves a gap in the numbering it was part of, or the one note it numbered alone with its
  `(m1)` or `(r1)`;
- a new reading whose meaning could not be made has already marked the word's other notes.

Nothing records the renames to take them back. Preparing a note only ever adds a marker a
note lacks, to tell it from the new one, so a marker whose new note is gone tells nothing
apart and is one `tidy_word_markers` takes off: given every note of one word, it says what
each sort field should be, meanings first and then readings, and the cleanup's last stage
applies it to every word a saved note carries markers for.

Pure: no collection, no Anki. The cleanup stage that reads the notes is
match_words_to_notes.tidy_sort_field_markers.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import NamedTuple, Optional, Sequence

_MARKER_AREA_RE = re.compile(r"(?:\s*\([^()]*\))*\s*")
_TOKEN_RE = re.compile(r"\(([^()]*)\)")
_NUMBERED_RE = re.compile(r"([rm])(\d+)")


@dataclass(frozen=True)
class SortMarkers:
    """A sort field taken apart: its word and the markers the match op writes."""

    base: str
    # "kun", "on" or ""
    kun_on: str = ""
    reading_number: Optional[int] = None
    meaning_number: Optional[int] = None

    @property
    def has_markers(self) -> bool:
        return (
            bool(self.kun_on)
            or self.reading_number is not None
            or self.meaning_number is not None
        )

    @property
    def reading(self) -> tuple[str, Optional[int]]:
        """Which reading of its word the note is, as its markers say: the notes of one reading
        are its meanings."""
        return self.kun_on, self.reading_number

    def format(self) -> str:
        markers = f"({self.kun_on})" if self.kun_on else ""
        if self.reading_number is not None:
            markers += f"(r{self.reading_number})"
        if self.meaning_number is not None:
            markers += f"(m{self.meaning_number})"
        return f"{self.base} {markers}" if markers else self.base


def parse_sort_field(value: str) -> Optional[SortMarkers]:
    """The word and markers of a sort field, or None when it holds anything else.

    None for a marker of another kind, such as the hand-written `(x1)` that takes a note out
    of matching, and for two markers of one kind: the match op leaves such a note out when it
    numbers a word's notes, so tidying has to leave it out too, neither counting nor renaming
    it. The markers are read in any order and with spaces between them, as a hand edit may
    leave them, and written back in the order above only if something changes.
    """
    value = value.strip()
    start = value.find("(")
    base = (value if start == -1 else value[:start]).strip()
    marker_area = "" if start == -1 else value[start:]
    if not base or not _MARKER_AREA_RE.fullmatch(marker_area):
        return None
    kun_on = ""
    numbers: dict[str, int] = {}
    for token in _TOKEN_RE.findall(marker_area):
        if token in ("kun", "on"):
            if kun_on:
                return None
            kun_on = token
            continue
        numbered = _NUMBERED_RE.fullmatch(token)
        if numbered is None or numbered.group(1) in numbers:
            return None
        numbers[numbered.group(1)] = int(numbered.group(2))
    return SortMarkers(base, kun_on, numbers.get("r"), numbers.get("m"))


def word_key(base: str) -> str:
    """The form two sort fields' words are compared in. As word_index.index_key, which is how
    the match op finds a word's other notes."""
    return unicodedata.normalize("NFC", base).casefold()


class WordNote(NamedTuple):
    """One note of a word, as tidying needs it."""

    nid: int
    sort_field: str
    # From the note's processed furigana: "kun", "on", or "" when it tells neither
    reading_type: str


def _numbering(created: Sequence[int]) -> list[Optional[int]]:
    """1..n for n >= 2 items in the order they were created, by `created` (note ids, which
    are creation times). None for one.

    Not in the order of the numbers they had: a note added by hand to a numbered word has none,
    and ordering the unnumbered first shifted every established note's number up by one, in
    every run that tidied the word. The match op numbers a new note after the ones it sees, so
    for its own numbering the two orders agree."""
    if len(created) < 2:
        return [None] * len(created)
    order = sorted(range(len(created)), key=lambda i: created[i])
    renumbered: list[Optional[int]] = [None] * len(created)
    for position, i in enumerate(order, start=1):
        renumbered[i] = position
    return renumbered


def tidy_word_markers(notes: Sequence[WordNote]) -> dict[int, str]:
    """The sort fields to rewrite among `notes`, by note id: only those that change.

    `notes` are every note whose sort field starts with one word, in any order; the answer does
    not depend on it. Notes of other words and notes parse_sort_field leaves out are ignored.

    Meanings first. The notes of one reading are its meanings. If any is numbered, one note
    alone loses its `(mN)` and two or more are numbered from 1 without gaps.

    Then readings. A reading is of the kind its marker says, or else of the kind its notes'
    furigana says, kun, on or neither. `(kun)`/`(on)` are kept while the word has readings of
    two kinds or more, neither counting as one: a (kun) reading and one of neither kind stay
    told apart by the marker, rather than numbered. With a single kind they are dropped, and
    the word's readings are numbered as one. Within each marker (or the word, when they are
    dropped) two or more readings are numbered from 1 without gaps, and a reading alone loses
    its `(rN)`.

    A marker is never added where the numbering does not need one: an unmarked kun reading
    of a word marked (kun)/(on) stays unmarked. Everything is renumbered in the order it was
    created, a reading by its oldest note, so tidying twice changes nothing more.
    """
    parsed: dict[int, SortMarkers] = {}
    by_word: dict[str, list[WordNote]] = {}
    for note in notes:
        markers = parse_sort_field(note.sort_field)
        if markers is None:
            continue
        parsed[note.nid] = markers
        by_word.setdefault(word_key(markers.base), []).append(note)

    tidied: dict[int, SortMarkers] = {}
    for word_notes in by_word.values():
        tidied.update(_tidy_meanings(word_notes, parsed))
        tidied.update(_tidy_readings(word_notes, parsed, tidied))
    return {nid: markers.format() for nid, markers in tidied.items() if markers != parsed[nid]}


def _by_reading(
    word_notes: Sequence[WordNote], markers: dict[int, SortMarkers]
) -> dict[tuple[str, Optional[int]], list[WordNote]]:
    readings: dict[tuple[str, Optional[int]], list[WordNote]] = {}
    for note in word_notes:
        readings.setdefault(markers[note.nid].reading, []).append(note)
    return readings


def _tidy_meanings(
    word_notes: Sequence[WordNote], parsed: dict[int, SortMarkers]
) -> dict[int, SortMarkers]:
    tidied: dict[int, SortMarkers] = {}
    for meanings in _by_reading(word_notes, parsed).values():
        if all(parsed[note.nid].meaning_number is None for note in meanings):
            # Unnumbered notes of one reading are no numbering of the match op's to tidy
            continue
        renumbered = _numbering([note.nid for note in meanings])
        for note, number in zip(meanings, renumbered):
            tidied[note.nid] = replace(parsed[note.nid], meaning_number=number)
    return tidied


def _tidy_readings(
    word_notes: Sequence[WordNote],
    parsed: dict[int, SortMarkers],
    meanings_tidied: dict[int, SortMarkers],
) -> dict[int, SortMarkers]:
    current = {note.nid: meanings_tidied.get(note.nid, parsed[note.nid]) for note in word_notes}
    readings = _by_reading(word_notes, current)

    def kind(reading: tuple[str, Optional[int]], notes: list[WordNote]) -> str:
        if reading[0]:
            return reading[0]
        return next(
            (note.reading_type for note in sorted(notes, key=lambda n: n.nid) if note.reading_type),
            "",
        )

    kinds = {kind(reading, notes) for reading, notes in readings.items()}
    # Neither is a kind of its own: counted as the kind it is marked apart from, a note the
    # match op marked (kun) for an on reading that was never added, next to one it left
    # unmarked for telling no kind, came out (r1) and (r2), markers neither had before
    keep_kun_on = len(kinds) > 1

    by_class: dict[str, list[tuple[str, Optional[int]]]] = {}
    for reading in readings:
        by_class.setdefault(reading[0] if keep_kun_on else "", []).append(reading)

    tidied: dict[int, SortMarkers] = {}
    for kun_on, class_readings in by_class.items():
        renumbered = _numbering(
            [min(note.nid for note in readings[reading]) for reading in class_readings]
        )
        for reading, number in zip(class_readings, renumbered):
            for note in readings[reading]:
                tidied[note.nid] = replace(
                    current[note.nid], kun_on=kun_on, reading_number=number
                )
    return tidied
