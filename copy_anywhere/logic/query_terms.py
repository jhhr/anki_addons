"""The names a query spells, checked against the collection -- and never rewritten.

A query stage holds an ordinary Anki search, and a search names decks, note types, card
types and fields by name alone. When Anki renames one of those the query goes on saying
the old name and quietly matches nothing, which is the one failure mode the reconcile pass
(`rename_reconcile.py`) cannot fix for the user: `col.replace_in_search_node` swaps *every*
term of a kind at once, so it cannot rename one deck inside a query naming two, and nothing
else in the Python API turns a search string into nodes. So the only honest thing to do is
say which name went stale and let the user decide what the query meant.

That is also why this scan checks the exact-name case only. Anki's search grammar is not
being reimplemented here: a term carrying a wildcard, a regex or a `{{...}}` reference that
is not resolved yet is skipped rather than guessed at, because a scan that cried wolf over
a working query would teach the user to ignore it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Optional

from .object_refs import KIND_CARD_TYPE, KIND_DECK, KIND_FIELD, KIND_NOTE_TYPE

#: Search keys that are Anki's own rather than a field name. Everything else before a colon
#: is read as a field search, which is what Anki does with it too.
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

#: `deck:` values that name a state rather than a deck.
DECK_PSEUDO_NAMES = frozenset({"current", "filtered"})

#: The three search keys that name an object a definition also references by id.
OBJECT_KEYS = frozenset({"deck", "note", "card"})


@dataclass(frozen=True)
class StaleTerm:
    """One search term naming something this collection does not have."""

    kind: str
    name: str

    def as_text(self) -> str:
        return f"{self.kind} '{self.name}'"


def term_spans(query: str) -> Iterator[tuple[int, int, str]]:
    """`(start, end, term)` for each of the query's terms, in order.

    The term is what `_split_terms` gives; `start` and `end` cover it as it was written, a
    leading `-` and every quote included, so the rename scanner (`rename_scan.py`) can
    replace a whole term without leaving a stray quote behind. Grouping parentheses and the
    whitespace between terms are outside every span.
    """
    term: list[str] = []
    start: Optional[int] = None
    quoted = False
    escaped = False
    for index, character in enumerate(query):
        if escaped:
            term.append("\\")
            term.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
            start = index if start is None else start
        elif character == '"':
            quoted = not quoted
            start = index if start is None else start
        elif not quoted and (character.isspace() or character in "()"):
            if term and start is not None:
                yield start, index, "".join(term)
            term = []
            start = None
        else:
            term.append(character)
            start = index if start is None else start
    if escaped:
        term.append("\\")
    if term and start is not None:
        yield start, len(query), "".join(term)


def _split_terms(query: str) -> Iterator[str]:
    """The query's terms, with quoting honoured and grouping punctuation dropped.

    Anki lets a quote start anywhere, so `deck:"JP vocab"` and `"deck:JP vocab"` are the
    same term; the quotes come out here and what is left is the term as it was meant.
    Escapes stay in, for `split_at_colon` and `is_skipped` to read.
    """
    for _start, _end, term in term_spans(query):
        yield term


def is_skipped(text: str) -> bool:
    """Whether this term is one the scan deliberately does not judge.

    A wildcard or a regex means the term was never an exact name, and a `{{...}}` reference
    means the name is not known until the definition runs.
    """
    if "{{" in text:
        return True
    unescaped = text.replace("\\\\", "")
    for index, character in enumerate(unescaped):
        if character in "*_" and (index == 0 or unescaped[index - 1] != "\\"):
            return True
    return False


def split_at_colon(text: str) -> Optional[tuple[str, str]]:
    """The term's key and value, or None when the term holds no key/value colon.

    Only an *unescaped* colon separates a key from a value: `foo\\:bar` is a plain-text
    search in Anki, not a search of a field called `foo`, and splitting it would report a
    working query as naming a field the collection does not have.
    """
    escaped = False
    for index, character in enumerate(text):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            return text[:index], text[index + 1 :]
    return None


def unescape(text: str) -> str:
    result: list[str] = []
    escaped = False
    for character in text:
        if escaped:
            result.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        else:
            result.append(character)
    return "".join(result)


class CollectionNames:
    """The name lists a scan needs, read once rather than once per term.

    Built lazily and never re-read, so one object may be shared by every scan made while
    the collection's names cannot change: a whole pass, or an open picker or definition
    editor, during which Manage Note Types cannot be reached. A scan given none builds its
    own, which is always correct.

    Both lists are folded to lower case: Anki matches a field search and `card:` without
    regard to case, so a query spelling a live name in another case works and must not be
    reported. `deck:` and `note:` need no list -- `decks.id_for_name` and `models.by_name`
    already fold for themselves.
    """

    def __init__(self, col: Any) -> None:
        self._col = col
        self._fields: Optional[set[str]] = None
        self._templates: Optional[set[str]] = None

    @property
    def fields(self) -> set[str]:
        if self._fields is None:
            self._fields = {
                field["name"].lower()
                for model in self._col.models.all()
                for field in model.get("flds") or []
            }
        return self._fields

    @property
    def templates(self) -> set[str]:
        if self._templates is None:
            self._templates = {
                template["name"].lower()
                for model in self._col.models.all()
                for template in model.get("tmpls") or []
            }
        return self._templates


def _stale_term(key: str, value: str, col: Any, names: CollectionNames):
    if key == "deck":
        if value.lower() in DECK_PSEUDO_NAMES or col.decks.id_for_name(value) is not None:
            return None
        return StaleTerm(KIND_DECK, value)
    if key == "note":
        if col.models.by_name(value) is not None:
            return None
        return StaleTerm(KIND_NOTE_TYPE, value)
    if key == "card":
        # `card:2` names the second template of whatever note type the note has, so it is
        # not a name at all.
        if value.isdigit() or value.lower() in names.templates:
            return None
        return StaleTerm(KIND_CARD_TYPE, value)
    if key.lower() in names.fields:
        return None
    return StaleTerm(KIND_FIELD, key)


def stale_search_terms(
    query_text: Any, col: Any, names: Optional[CollectionNames] = None
) -> list[StaleTerm]:
    """Every exact name in this search that the collection does not have, in order.

    `names` is the caller's shared `CollectionNames` for `col`, if it keeps one.
    """
    if not isinstance(query_text, str) or not query_text.strip():
        return []
    if names is None:
        names = CollectionNames(col)
    found: list[StaleTerm] = []
    for raw in _split_terms(query_text):
        text = raw.lstrip("-")
        split = split_at_colon(text)
        if split is None or is_skipped(text):
            continue
        key = unescape(split[0])
        value = unescape(split[1])
        # `re:...` is a regex over the whole note and `Word:re:neko` one over a field;
        # neither is a name. An empty value is `deck:` with nothing after it.
        if not value or key.lower() == "re" or value.lower().startswith("re:"):
            continue
        if key.lower() in OBJECT_KEYS:
            key = key.lower()
        elif key.lower() in SEARCH_KEYWORDS:
            continue
        stale = _stale_term(key, value, col, names)
        if stale is not None and stale not in found:
            found.append(stale)
    return found


__all__ = [
    "DECK_PSEUDO_NAMES",
    "SEARCH_KEYWORDS",
    "CollectionNames",
    "StaleTerm",
    "is_skipped",
    "split_at_colon",
    "stale_search_terms",
    "term_spans",
    "unescape",
]
