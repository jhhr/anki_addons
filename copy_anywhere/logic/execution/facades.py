"""Immutable note and card views for code mode.

Code mode never receives a raw Anki object. It receives a facade: everything readable about
a note or card, nothing writable, and no way to reach the collection or the note type
through it. That is not only about protecting the objects -- it is what keeps every mutation
in a definition visible. A stage is the only way to change anything, so the preview can show
what a run would do and the commit planner knows what it is committing.

Reads go through the session's overlays, so a facade shows the edits earlier stages made
even though nothing has been saved yet.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional, Sequence, Union

from anki.cards import Card
from anki.consts import MODEL_CLOZE
from anki.notes import Note

from ...shared.interpolate.interpolate_fields import (
    CardTimeValues,
    get_card_time_values,
    get_card_type_as_string,
    get_formatted_average_time,
    get_formatted_card_created_time,
    get_formatted_first_review_time,
    get_formatted_latest_review_time,
    get_formatted_total_time,
)
from .context import ExecutionSession

READ_ONLY_MESSAGE = (
    "notes and cards are read-only inside code; use a stage to change one"
)


class _Immutable:
    """Shared refusal to be written to, by attribute or by item."""

    __slots__ = ()

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError(READ_ONLY_MESSAGE)

    def __delattr__(self, name: str) -> None:
        raise TypeError(READ_ONLY_MESSAGE)

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError(READ_ONLY_MESSAGE)

    def __delitem__(self, key: Any) -> None:
        raise TypeError(READ_ONLY_MESSAGE)


class NoteFacade(_Immutable):
    """Every read-only attribute of a note, plus its cards."""

    __slots__ = ("_note", "_session")

    def __init__(self, note: Note, session: ExecutionSession) -> None:
        object.__setattr__(self, "_note", note)
        object.__setattr__(self, "_session", session)

    # -- fields -----------------------------------------------------------------------

    def __getitem__(self, key: str) -> str:
        return object.__getattribute__(self, "_note")[key]

    def keys(self) -> list[str]:
        return list(object.__getattribute__(self, "_note").keys())

    def values(self) -> list[str]:
        return list(object.__getattribute__(self, "_note").values())

    def items(self) -> list[tuple[str, str]]:
        return list(object.__getattribute__(self, "_note").items())

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, "_note").keys()

    # -- metadata ---------------------------------------------------------------------

    @property
    def id(self) -> int:
        return object.__getattribute__(self, "_note").id

    @property
    def guid(self) -> str:
        return object.__getattribute__(self, "_note").guid

    @property
    def mid(self) -> int:
        return object.__getattribute__(self, "_note").mid

    @property
    def note_type_id(self) -> int:
        return object.__getattribute__(self, "_note").mid

    @property
    def note_type_name(self) -> str:
        note_type = object.__getattribute__(self, "_note").note_type()
        return note_type["name"] if note_type else ""

    @property
    def is_cloze(self) -> bool:
        note_type = object.__getattribute__(self, "_note").note_type()
        return bool(note_type and note_type.get("type") == MODEL_CLOZE)

    @property
    def tags(self) -> list[str]:
        return list(object.__getattribute__(self, "_note").tags)

    def has_tag(self, tag: str) -> bool:
        return object.__getattribute__(self, "_note").has_tag(tag)

    @property
    def mod(self) -> int:
        return object.__getattribute__(self, "_note").mod

    @property
    def usn(self) -> int:
        return object.__getattribute__(self, "_note").usn

    @property
    def sort_field(self) -> str:
        note = object.__getattribute__(self, "_note")
        note_type = note.note_type()
        index = note_type.get("sortf", 0) if note_type else 0
        values = note.values()
        return values[index] if index < len(values) else ""

    @property
    def cards(self) -> "CardListFacade":
        note = object.__getattribute__(self, "_note")
        session = object.__getattribute__(self, "_session")
        return CardListFacade(session.cards_of_note(note), session)

    def __repr__(self) -> str:
        return f"<NoteFacade id={self.id}>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NoteFacade):
            return self.id == other.id and self.id != 0
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("note", self.id))


class CardFacade(_Immutable):
    """Every read-only attribute of a card, including its note."""

    __slots__ = ("_card", "_session", "_time_values")

    def __init__(self, card: Card, session: ExecutionSession) -> None:
        object.__setattr__(self, "_card", card)
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_time_values", None)

    def _raw(self) -> Card:
        return object.__getattribute__(self, "_card")

    def _times(self) -> CardTimeValues:
        # One revlog aggregate answers all four review-time properties, so it is read on
        # the first of them and kept for the rest.
        time_values = object.__getattribute__(self, "_time_values")
        if time_values is None:
            card_id = self._raw().id
            time_values = (
                get_card_time_values(card_id) if card_id else (None, None, None, None)
            )
            object.__setattr__(self, "_time_values", time_values)
        return time_values

    @property
    def id(self) -> int:
        return self._raw().id

    @property
    def nid(self) -> int:
        return self._raw().nid

    @property
    def note(self) -> NoteFacade:
        session = object.__getattribute__(self, "_session")
        return NoteFacade(session.note_by_id(self._raw().nid), session)

    @property
    def did(self) -> int:
        return self._raw().did

    @property
    def odid(self) -> int:
        return self._raw().odid

    @property
    def deck_id(self) -> int:
        # The deck the card belongs to, which for a card in a filtered deck is the deck it
        # came from rather than the filtered one.
        card = self._raw()
        return card.odid or card.did

    @property
    def deck_name(self) -> str:
        from aqt import mw

        return mw.col.decks.name(self.deck_id)

    @property
    def original_deck_name(self) -> str:
        from aqt import mw

        return mw.col.decks.name(self._raw().odid) if self._raw().odid else ""

    @property
    def ord(self) -> int:
        return self._raw().ord

    @property
    def template_name(self) -> str:
        # A cloze note's cards all share one template, so its name alone cannot tell them
        # apart: the cloze number goes after it, as in the note's card values ("Cloze 2")
        # and as format 1's code saw it.
        card = self._raw()
        template = card.template()
        name = template["name"] if template else ""
        note_type = card.note_type()
        if note_type and note_type.get("type") == MODEL_CLOZE:
            return f"{name} {card.ord + 1}"
        return name

    @property
    def type(self) -> str:
        return get_card_type_as_string(self._raw().type)

    @property
    def queue(self) -> int:
        return self._raw().queue

    @property
    def due(self) -> int:
        return self._raw().due

    @property
    def odue(self) -> int:
        return self._raw().odue

    @property
    def ivl(self) -> int:
        return self._raw().ivl

    @property
    def factor(self) -> int:
        return self._raw().factor

    @property
    def ease(self) -> float:
        return (self._raw().factor / 10) or 0

    @property
    def reps(self) -> int:
        return self._raw().reps

    @property
    def lapses(self) -> int:
        return self._raw().lapses

    @property
    def left(self) -> int:
        return self._raw().left

    @property
    def flag(self) -> int:
        return self._raw().user_flag()

    @property
    def custom_data(self) -> str:
        return self._raw().custom_data

    @property
    def desired_retention(self) -> Optional[float]:
        return getattr(self._raw(), "desired_retention", None)

    @property
    def stability(self) -> float:
        state = getattr(self._raw(), "memory_state", None)
        return round(state.stability, 1) if state else 0

    @property
    def difficulty(self) -> float:
        state = getattr(self._raw(), "memory_state", None)
        return round(state.difficulty, 1) if state else 0

    @property
    def mod(self) -> int:
        return self._raw().mod

    @property
    def created(self) -> str:
        return get_formatted_card_created_time(self._raw().id)

    @property
    def first_review_time(self) -> str:
        return get_formatted_first_review_time(self._times()[0])

    @property
    def latest_review_time(self) -> str:
        return get_formatted_latest_review_time(self._times()[1])

    @property
    def average_review_time(self) -> str:
        _first, _latest, count, total = self._times()
        return get_formatted_average_time(total, count)

    @property
    def total_review_time(self) -> str:
        return get_formatted_total_time(self._times()[3])

    @property
    def suspended(self) -> bool:
        return self._raw().queue == -1

    @property
    def buried(self) -> bool:
        return self._raw().queue in (-2, -3)

    def __repr__(self) -> str:
        return f"<CardFacade id={self.id}>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CardFacade):
            return self.id == other.id and self.id != 0
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("card", self.id))


class _ListFacade(_Immutable):
    """A read-only sequence whose slices keep their type."""

    __slots__ = ("_items", "_session")
    _member: Any = None

    def __init__(self, items: Sequence[Any], session: ExecutionSession) -> None:
        object.__setattr__(self, "_items", list(items))
        object.__setattr__(self, "_session", session)

    def _raw_items(self) -> list:
        return object.__getattribute__(self, "_items")

    def __len__(self) -> int:
        return len(self._raw_items())

    def __iter__(self) -> Iterator:
        session = object.__getattribute__(self, "_session")
        return (type(self)._member(item, session) for item in self._raw_items())

    def __getitem__(self, index: Union[int, slice]):
        session = object.__getattribute__(self, "_session")
        items = self._raw_items()
        if isinstance(index, slice):
            return type(self)(items[index], session)
        return type(self)._member(items[index], session)

    def __bool__(self) -> bool:
        return bool(self._raw_items())

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {len(self)} items>"


class NoteListFacade(_ListFacade):
    __slots__ = ()
    _member = NoteFacade


class CardListFacade(_ListFacade):
    __slots__ = ()
    _member = CardFacade


class NoteCardsFacade(CardListFacade):
    """One note's working cards, fetched the first time something reads them.

    This is code mode's `cards`. Most code never reads it, and building it is a query -- in a
    loop, one per iteration -- so nothing is fetched until the code asks for its length, an
    item or an iteration, and the list is then kept for the rest of that evaluation.
    """

    __slots__ = ("_note",)

    def __init__(self, note: Note, session: ExecutionSession) -> None:
        object.__setattr__(self, "_items", None)
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_note", note)

    def _raw_items(self) -> list:
        items = object.__getattribute__(self, "_items")
        if items is None:
            session = object.__getattribute__(self, "_session")
            items = list(session.cards_of_note(object.__getattribute__(self, "_note")))
            object.__setattr__(self, "_items", items)
        return items

    def __getitem__(self, index: Union[int, slice]):
        if isinstance(index, slice):
            # A slice is a list already in hand, not a note to fetch.
            session = object.__getattribute__(self, "_session")
            return CardListFacade(self._raw_items()[index], session)
        return super().__getitem__(index)


def to_facade(value: Any, session: ExecutionSession) -> Any:
    """Wrap notes, cards and lists of them; leave anything else alone."""
    if isinstance(value, Note):
        return NoteFacade(value, session)
    if isinstance(value, Card):
        return CardFacade(value, session)
    if isinstance(value, list):
        if value and all(isinstance(item, Note) for item in value):
            return NoteListFacade(value, session)
        if value and all(isinstance(item, Card) for item in value):
            return CardListFacade(value, session)
        return [to_facade(item, session) for item in value]
    return value


def from_facade(value: Any) -> Any:
    """Unwrap a facade a code expression returned, back to the object the runtime holds.

    A mixed list is rejected: a `List[NoteRef]` and a `List[CardRef]` are different types and
    the consuming action has to be told which one it got.
    """
    if isinstance(value, NoteFacade):
        return object.__getattribute__(value, "_note")
    if isinstance(value, CardFacade):
        return object.__getattribute__(value, "_card")
    if isinstance(value, (NoteListFacade, CardListFacade)):
        return list(value._raw_items())
    if isinstance(value, (list, tuple)):
        unwrapped = [from_facade(item) for item in value]
        kinds = {type(item) for item in unwrapped}
        if len(kinds) > 1 and kinds <= {Note, Card}:
            raise TypeError("a list of notes and cards mixed together is not a value type")
        return unwrapped
    return value


def code_helpers(session: ExecutionSession) -> dict:
    """`find_notes`/`find_cards`/`get_note`/`get_card` as code mode sees them (§4.1).

    The finders return raw id lists from the persisted collection, the same rule query
    stages follow; the getters turn an id into a facade through the overlays.
    """

    def get_note(note_id: int) -> NoteFacade:
        try:
            note = session.note_by_id(int(note_id))
        except Exception as error:  # noqa: BLE001 -- surfaced to the user as a code error
            raise KeyError(f"no note with id {note_id}: {error}") from error
        return NoteFacade(note, session)

    def get_card(card_id: int) -> CardFacade:
        try:
            card = session.card_by_id(int(card_id))
        except Exception as error:  # noqa: BLE001
            raise KeyError(f"no card with id {card_id}: {error}") from error
        return CardFacade(card, session)

    return {
        "find_notes": session.find_notes,
        "find_cards": session.find_cards,
        "get_note": get_note,
        "get_card": get_card,
    }
