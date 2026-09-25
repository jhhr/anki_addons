"""Judge an Anki search against a note that is not in the collection yet (finding 9).

A search condition is normally asked of the collection: `find_notes(f"({search}) nid:{id}")`.
A note being added -- the `note_will_be_added` hook, or the Add dialog's unfocus -- has id 0
and no row, so that search can never match it, and every definition with a search condition
would be skipped on add. This module answers the same question in Python instead, from the
unsaved note's fields and tags, its note type, and the deck it is being added to.

The answer has to be the one Anki would give once the note is saved, so everything here is a
port of Anki 25.9's own search code (`rslib/src/search/parser.rs`, `sqlwriter.rs`, `text.rs`)
rather than a reading of the manual, and each rule was established against a real collection;
`test_unsaved_note_search.py` holds the differential tests that prove it. What cannot be ported
faithfully is refused: a term that needs cards, review history, ids or collection state
(`is:`, `card:`, `prop:`, `rated:`, `nid:`, ...), a regular expression or accent-folding form
(`re:`, `nc:`, `w:`, `sc:`, `field:re:`), and a few things only this note makes unknowable (a
`deck:` search with no deck, a tag Anki would rewrite on save), all raise `UnjudgeableSearch`
naming the term. A guessed answer would silently run or skip a definition; a refusal fails it
where the user can see why.

What Anki does, in short, and therefore what this does:

- Parsing: terms separated by spaces (and U+3000), implicit AND, `and`/`or` in any case, AND
  binding tighter than OR, `-` negating one term or group, `"..."` and `key:"..."` quoting,
  and backslash escapes `\\ \\" \\: \\( \\) \\- \\* \\_`; any other escape is an error.
- Bare text: a substring match, `*` and `_` as wildcards, ASCII letters case-insensitive and
  nothing else, against all fields joined by U+001F (so a wildcard can cross fields) *or*
  against the sort field with its HTML stripped (so `neko` finds `ne<b>ko</b>` in the sort
  field only; a sort field that reads as a number is searched as that number). With "ignore
  accents in search" on, both sides are compatibility-decomposed and lose their combining
  marks first.
- `field:value`: a whole-field match with the same wildcard and case rules. The field name is
  matched case-insensitively (with Unicode full case folding, or as a glob when it has
  wildcards); `*`, `_*` and `*_` as the name mean "any field", which Anki matches with a
  Unicode case-insensitive regex instead.
- `tag:`, `note:`, `deck:`: Unicode case-insensitive; `tag:a` also finds `a::b`; `deck:a`
  also finds `a::b`, with each level of the searched name trimmed; `note:` compares whole
  names.

The functions and patterns below keep the names of the Rust ones they port, so a difference
found later can be traced to the line it came from.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional, Union

from anki.card_rendering_pb2 import StripHtmlRequest
from anki.collection import Config
from anki.notes import Note


class UnsavedNoteSearchError(ValueError):
    """The search could not be answered for this note; the message says why."""


class SearchSyntaxError(UnsavedNoteSearchError):
    """Anki itself would refuse this search, so there is no answer to give.

    Raised wherever `find_notes` would raise for the same text, so a malformed condition
    fails the same way on the add path as on a saved note.
    """


class UnjudgeableSearch(UnsavedNoteSearchError):
    """The search has a term whose answer depends on something an unsaved note does not have.

    `term` is the term as it was written in the search, so the message can point at it.
    """

    def __init__(self, term: str, reason: str) -> None:
        super().__init__(
            f"the search term '{term}' cannot be judged for a note that is not saved yet:"
            f" {reason}"
        )
        self.term = term
        self.reason = reason


# Keys Anki reads as something other than a field name. Every other `key:value` is a field
# search. The list was checked against 25.9 by searching every one- to three-letter key and
# every word in the compiled backend, both with and without a field of that name.
_UNJUDGEABLE_KEYS = {
    "added": "it needs the card's creation time",
    "card": "it needs the note's cards",
    "cid": "it needs card ids",
    "did": "it needs the cards' deck ids",
    "dupe": "it needs the other notes in the collection",
    "edited": "it needs the note's modification time",
    "flag": "it needs the note's cards",
    "has-cd": "it needs the cards' custom data",
    "introduced": "it needs review history",
    "is": "it needs the note's cards",
    "mid": "it needs note type ids",
    "nid": "it needs the note's id",
    "preset": "it needs the cards' deck options",
    "prop": "it needs the note's cards",
    "rated": "it needs review history",
    "resched": "it needs review history",
    "re": "regular expressions are not supported here",
    "nc": "accent-insensitive search is not supported here",
    "w": "word-boundary search is not supported here",
    "sc": "cloze-stripped search is not supported here",
}

# Rust's `str::trim`, which the parser applies to the whole search, trims Unicode White_Space;
# Python's `strip` also trims U+001C..U+001F, which Rust keeps.
_RUST_WHITESPACE = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)

# Between terms the parser skips only these two, so a tab or a newline inside a search is
# part of a term, not a separator.
_TERM_SEPARATORS = " \u3000"


# Anki's text helpers (rslib/src/text.rs and parser.rs), ported rule for rule -------------

_INVALID_ESCAPE = re.compile(r'(^|[^\\])(\\\\)*(\\[^\\":*_()-])')
_PARSER_ESCAPE = re.compile(r'\\[\\":()-]')
_SQL_WILDCARDS = re.compile(r"\\[\\*]|[*%]")
_GLOB_CHARS = re.compile(r"\\?.")
_IS_GLOB = re.compile(r"(^|[^\\])(\\\\)*[*_]")
_ANY_ESCAPE = re.compile(r"\\(.)")


def _unescape(text: str, term: str) -> str:
    """Resolve the escapes only the parser cares about; `\\*`, `\\_` and `\\\\` stay for the
    wildcard conversion that follows. Any other escape is Anki's "not defined" error."""
    if _INVALID_ESCAPE.search(text):
        raise SearchSyntaxError(f"the search term '{term}' has an unknown escape sequence")
    return _PARSER_ESCAPE.sub(lambda m: "\\\\" if m.group(0) == "\\\\" else m.group(0)[1], text)


def _to_sql(text: str) -> str:
    """Anki wildcards to a LIKE pattern with `\\` as the escape character."""
    return _SQL_WILDCARDS.sub(
        lambda m: {"\\\\": "\\\\", "\\*": "*", "*": "%", "%": "\\%"}[m.group(0)], text
    )


def _to_custom_re(text: str, wildcard: str) -> str:
    """Anki wildcards to a regular expression, `_` becoming `wildcard` and `*` its repeat."""

    def convert(match: re.Match) -> str:
        piece = match.group(0)
        if piece in ("\\\\", "\\*"):
            return piece
        if piece == "\\_":
            return "_"
        if piece == "*":
            return f"{wildcard}*"
        if piece == "_":
            return wildcard
        return re.escape(piece)

    return _GLOB_CHARS.sub(convert, text)


def _is_glob(text: str) -> bool:
    return bool(_IS_GLOB.search(text))


def _to_text(text: str) -> str:
    return _ANY_ESCAPE.sub(r"\1", text)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _without_combining(text: str) -> str:
    """What "ignore accents in search" compares: the text decomposed, marks dropped.

    The decomposition is the compatibility one (NFKD), as Anki's is: with the option on, the
    "fi" ligature is found by "fi", full-width letters by their plain forms, and a Japanese
    voiced kana by its unvoiced one, since the voicing mark is a combining mark.
    """
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.category(char).startswith("M")
    )


def _unicase_equal(a: str, b: str) -> bool:
    # Anki compares a non-wildcard field or note type name with the `unicase` crate, which
    # folds case fully, so an upper-case "SS" names a field spelled with a sharp s.
    return a.casefold() == b.casefold()


def _glob_matcher(pattern: str) -> Callable[[str], bool]:
    """How Anki matches a field or note type name against a search: a case-insensitive
    regex when it has wildcards, full-case-folded equality when it does not."""
    if _is_glob(pattern):
        regex = re.compile(_to_custom_re(pattern, "."), re.IGNORECASE)
        return lambda name: regex.fullmatch(name) is not None
    literal = _to_text(pattern)
    return lambda name: _unicase_equal(name, literal)


def _ascii_lower(text: str) -> str:
    return "".join(char.lower() if char.isascii() else char for char in text)


# The parsed search ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Term:
    """One search term: `kind` says how it is judged, `source` is the text it came from."""

    kind: str
    source: str
    value: str = ""
    name: str = ""


@dataclass(frozen=True)
class _Not:
    node: "_Node"


@dataclass(frozen=True)
class _Group:
    # Terms and "and"/"or" alternating, as Anki's parser leaves them; SQL precedence (AND
    # before OR) is applied when the group is judged.
    items: tuple


_AND = "and"
_OR = "or"
_Node = Union[_Term, _Not, _Group, str]


class _Backtrack(Exception):
    """A parser alternative did not apply; the next one is tried (nom's `Err::Error`)."""


class _EscapedError(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind


def _escaped(text: str, stop: str, escapable: Callable[[str], bool]) -> tuple[str, str]:
    """nom's `escaped(is_not(stop), '\\\\', escapable)`: runs of ordinary characters and
    backslash pairs. Returns (consumed, rest); raises `_EscapedError` where nom errors."""
    i = 0
    length = len(text)
    while i < length:
        j = i
        while j < length and text[j] not in stop:
            j += 1
        if j > i:
            i = j
            continue
        if text[i] == "\\":
            if i + 1 >= length:
                raise _EscapedError("trailing backslash")
            if not escapable(text[i + 1]):
                raise _EscapedError("none of")
            i += 2
            continue
        if i == 0:
            raise _EscapedError("nothing consumed")
        return text[:i], text[i:]
    return text, ""


class _Parser:
    """A port of Anki's nom parser: the same alternatives, tried in the same order, with the
    same split between "try the next alternative" and "the whole search is invalid"."""

    def __init__(self) -> None:
        # The first term found that cannot be judged. It is raised only once the whole
        # search has parsed, so a search Anki would refuse is reported as that instead.
        self.unjudgeable: Optional[UnjudgeableSearch] = None

    def parse(self, search: str) -> _Node:
        search = search.strip(_RUST_WHITESPACE)
        if not search:
            # `find_notes("")` matches every note.
            return _Term("all", "")
        nodes, rest = self._group_inner(search)
        if rest:
            raise SearchSyntaxError("a closing bracket `)` has no opening bracket")
        if self.unjudgeable is not None:
            raise self.unjudgeable
        return _Group(tuple(nodes))

    def _group_inner(self, text: str) -> tuple[list, str]:
        rest = text
        nodes: list = []
        while True:
            try:
                node, rest = self._node(rest)
            except _Backtrack:
                break
            if len(nodes) % 2 == 0:
                if node in (_AND, _OR):
                    raise SearchSyntaxError(f"an `{node}` is not connecting two search terms")
            elif node not in (_AND, _OR):
                nodes.append(_AND)
            nodes.append(node)
        if nodes and nodes[-1] in (_AND, _OR):
            raise SearchSyntaxError(f"an `{nodes[-1]}` is not connecting two search terms")
        return nodes, rest.lstrip(_TERM_SEPARATORS)

    def _node(self, text: str) -> tuple[_Node, str]:
        text = text.lstrip(_TERM_SEPARATORS)
        for alternative in (self._negated, self._group, self._text):
            try:
                return alternative(text)
            except _Backtrack:
                continue
        raise _Backtrack

    def _negated(self, text: str) -> tuple[_Node, str]:
        if not text.startswith("-"):
            raise _Backtrack
        for alternative in (self._group, self._text):
            try:
                node, rest = alternative(text[1:])
            except _Backtrack:
                continue
            if node in (_AND, _OR):
                # Anki accepts `-or`, then writes `not or` into its SQL, which SQLite rejects.
                raise SearchSyntaxError(f"`-{node}` is not a valid search")
            return _Not(node), rest
        raise _Backtrack

    def _group(self, text: str) -> tuple[_Node, str]:
        if not text.startswith("("):
            raise _Backtrack
        inner, tail = self._group_inner(text[1:])
        if not tail.startswith(")"):
            raise SearchSyntaxError("an opening bracket `(` has no closing bracket")
        if not inner:
            raise SearchSyntaxError("a group `()` has nothing in it")
        return _Group(tuple(inner)), tail[1:]

    def _text(self, text: str) -> tuple[_Node, str]:
        for alternative in (self._quoted_term, self._partially_quoted_term, self._unquoted_term):
            try:
                return alternative(text)
            except _Backtrack:
                continue
        raise _Backtrack

    def _quoted_term_str(self, text: str) -> tuple[str, str]:
        if not text.startswith('"'):
            raise _Backtrack
        opened = text[1:]
        try:
            inner, tail = _escaped(opened, '"\\', lambda char: True)
        except _EscapedError:
            if (opened[:1] or '"') == '"':
                raise SearchSyntaxError('a pair of double quotes `""` has nothing in it')
            raise SearchSyntaxError('an opening double quote `"` is not closed')
        if not tail.startswith('"'):
            raise SearchSyntaxError('an opening double quote `"` is not closed')
        return inner, tail[1:]

    def _quoted_term(self, text: str) -> tuple[_Node, str]:
        inner, rest = self._quoted_term_str(text)
        return self._search_node_for_text(inner, text[: len(text) - len(rest)]), rest

    def _partially_quoted_term(self, text: str) -> tuple[_Node, str]:
        # `key:"quoted value"`: the quotes have to come after the colon.
        try:
            key, rest = _escaped(text, '"(): \u3000\\', lambda char: char not in _TERM_SEPARATORS)
        except _EscapedError:
            raise _Backtrack
        if not rest.startswith(":"):
            raise _Backtrack
        value, rest = self._quoted_term_str(rest[1:])
        source = text[: len(text) - len(rest)]
        return self._search_node_with_argument(key, value, source), rest

    def _unquoted_term(self, text: str) -> tuple[_Node, str]:
        try:
            term, rest = _escaped(
                text, '"() \u3000\\', lambda char: char not in _TERM_SEPARATORS
            )
        except _EscapedError as error:
            if error.kind == "nothing consumed":
                raise _Backtrack
            raise SearchSyntaxError(f"the search term '{text}' has an unknown escape sequence")
        if not term:
            raise _Backtrack
        lowered = _ascii_lower(term)
        if lowered == "and":
            return _AND, rest
        if lowered == "or":
            return _OR, rest
        return self._search_node_for_text(term, term), rest

    def _search_node_for_text(self, text: str, source: str) -> _Term:
        # The key is everything before the first unescaped colon; with no colon the whole
        # term is text to find in any field.
        try:
            head, tail = _escaped(text, ":\\", lambda char: True)
        except _EscapedError:
            head, tail = "", ""
        if not head:
            raise SearchSyntaxError(
                f"the search term '{source}' has a `:` with no keyword before it"
            )
        if not tail:
            return _Term("text", source, value=_unescape(head, source))
        return self._search_node_with_argument(head, tail[1:], source)

    def _search_node_with_argument(self, key: str, value: str, source: str) -> _Term:
        # Anki compares the key as written, escapes and all, ASCII-lowercased.
        lowered = _ascii_lower(key)
        if lowered == "deck":
            return _Term("deck", source, value=_unescape(value, source))
        if lowered == "note":
            return _Term("note", source, value=_unescape(value, source))
        if lowered == "tag":
            if value.startswith("re:"):
                return self._refuse(source, "regular expressions are not supported here")
            return _Term("tag", source, value=_unescape(value, source))
        if lowered in _UNJUDGEABLE_KEYS:
            return self._refuse(source, _UNJUDGEABLE_KEYS[lowered])
        if value.startswith("re:"):
            return self._refuse(source, "regular expressions are not supported here")
        if value.startswith("nc:"):
            return self._refuse(source, "accent-insensitive search is not supported here")
        return _Term(
            "field", source, value=_unescape(value, source), name=_unescape(key, source)
        )

    def _refuse(self, source: str, reason: str) -> _Term:
        if self.unjudgeable is None:
            self.unjudgeable = UnjudgeableSearch(source, reason)
        return _Term("refused", source)


# Judging -----------------------------------------------------------------------------------


class _NoteView:
    """The unsaved note as the collection would hold it once added.

    Anki normalises a note's text to NFC when it saves it (unless the collection says not
    to), keeps the sort field as HTML-stripped text in a column with integer affinity, and
    stores the tags as one space-padded string. Searching the saved note searches those, so
    this builds them the same way.
    """

    def __init__(self, note: Note, deck_id: Optional[int]) -> None:
        col = note.col
        self.col = col
        self.normalize = col.get_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT)
        self.ignore_accents = col.get_config_bool(Config.Bool.IGNORE_ACCENTS_IN_SEARCH)
        self.note_type = note.note_type()
        fields = [self.norm_note(value) for value in note.fields]
        self.fields = fields
        self.flds = "\x1f".join(fields)
        self.deck_id = deck_id
        self.tags = list(note.tags)
        self.sql = sqlite3.connect(":memory:")
        self.sfld = self._sort_field_text(fields)

    def close(self) -> None:
        self.sql.close()

    def norm_note(self, text: str) -> str:
        return _nfc(text) if self.normalize else text

    def like(self, text: str, pattern: str) -> bool:
        # SQLite's own LIKE, as Anki's search runs it: `_` is one character, and only ASCII
        # letters match regardless of case.
        return bool(self.sql.execute("select ? like ? escape '\\'", (text, pattern)).fetchone()[0])

    def _sort_field_text(self, fields: list[str]) -> str:
        index = self.note_type.get("sortf", 0) or 0
        raw = fields[index] if index < len(fields) else ""
        # The Rust function Anki strips the saved sort field with (it drops comments, styles
        # and scripts, decodes entities, and keeps an image's file name). It is what
        # `anki.utils.strip_html_media` calls too, but through `anki.lang`, which is only set
        # up inside a running Anki; the note's own collection has the same backend.
        stripped = self.col._backend.strip_html(
            text=raw, mode=StripHtmlRequest.PRESERVE_MEDIA_FILENAMES
        )
        # The notes table declares `sfld integer`, so a sort field that reads as a number is
        # stored as one and searched as its text form: "<b>0123</b>" is searched as "123".
        self.sql.execute("create table note (sfld integer)")
        self.sql.execute("insert into note values (?)", (stripped,))
        return self.sql.execute("select cast(sfld as text) from note").fetchone()[0]


def _judge_text(term: _Term, view: _NoteView) -> bool:
    pattern = _to_sql(view.norm_note(term.value))
    sfld, flds = view.sfld, view.flds
    if view.ignore_accents:
        pattern = _without_combining(pattern)
        sfld, flds = _without_combining(sfld), _without_combining(flds)
    pattern = f"%{pattern}%"
    return view.like(sfld, pattern) or view.like(flds, pattern)


def _judge_field(term: _Term, view: _NoteView) -> bool:
    name = _nfc(term.name)
    value = view.norm_note(term.value)
    if name in ("*", "_*", "*_"):
        # "Any field" is a regex over each field, not LIKE: Unicode case-insensitive, and `.`
        # crosses newlines.
        regex = re.compile(_to_custom_re(value, "."), re.IGNORECASE | re.DOTALL)
        return any(regex.fullmatch(field) for field in view.fields)

    matches_name = _glob_matcher(name)
    indices = [
        field["ord"] for field in view.note_type["flds"] if matches_name(field["name"])
    ]
    if not indices:
        return False
    # Anki builds one LIKE over the whole joined field list per run of consecutive matching
    # fields: the value at the run's first position, the run's other fields left out, `%`
    # everywhere else. For one field that is an exact whole-field match; for a run it is the
    # value matched against the run's fields joined, which is Anki's behaviour, not ours.
    runs: list[range] = []
    for index in sorted(indices):
        if runs and runs[-1].stop == index:
            runs[-1] = range(runs[-1].start, index + 1)
        else:
            runs.append(range(index, index + 1))
    sql_value = _to_sql(value)
    total = len(view.note_type["flds"])
    for run in runs:
        parts = []
        for position in range(total):
            if position == run.start:
                parts.append(sql_value)
            elif position not in run:
                parts.append("%")
        if view.like(view.flds, "\x1f".join(parts)):
            return True
    return False


def _judge_tag(term: _Term, view: _NoteView) -> bool:
    tag = _nfc(term.value)
    if tag == "*":
        return True
    tags = _saved_tags(term, view)
    if tag == "none":
        return not tags
    if " " in tag:
        return False
    stored = f" {' '.join(tags)} " if tags else ""
    # A tag matches itself or any tag below it; `*` and `_` do not cross a space.
    tag_regex = _to_custom_re(tag, r"\S")
    regex = re.compile(f".* {tag_regex}(::| ).*", re.IGNORECASE)
    return regex.search(stored) is not None


def _saved_tags(term: _Term, view: _NoteView) -> list[str]:
    """The note's tags as Anki would store them, or a refusal when saving would change them.

    Anki rewrites some tags on save -- it splits on whitespace, drops control characters, and
    fills empty `::` parts with "blank" -- and a tag search sees the rewritten ones. Those
    rules are not ported; a note whose tags would be touched by them cannot be judged.
    """
    for tag in view.tags:
        parts = tag.split("::")
        if (
            not tag
            or tag != _nfc(tag)
            or any(char.isspace() or unicodedata.category(char).startswith("C") for char in tag)
            or any(not part or part.endswith(":") for part in parts)
        ):
            raise UnjudgeableSearch(
                term.source, f"the note's tag {tag!r} would be rewritten when it is saved"
            )
    return view.tags


def _judge_note_type(term: _Term, view: _NoteView) -> bool:
    return _glob_matcher(_nfc(term.value))(view.note_type["name"])


def _judge_deck(term: _Term, view: _NoteView) -> bool:
    deck = _nfc(term.value)
    if deck == "*":
        return True
    if deck in ("current", "filtered"):
        raise UnjudgeableSearch(term.source, "it depends on the collection's state")
    if view.deck_id is None:
        raise UnjudgeableSearch(term.source, "the deck the note goes into is not known")
    target = view.col.decks.get(view.deck_id, default=False)
    if not target or target.get("dyn"):
        raise UnjudgeableSearch(term.source, "the deck the note goes into is not a normal deck")
    if any(template.get("did") for template in view.note_type["tmpls"]):
        raise UnjudgeableSearch(
            term.source, "the note type sends some cards to a deck of its own"
        )
    # Deck names are held with U+001F between levels, and Anki cleans each level of the
    # searched name the way it cleans a new deck's: control characters dropped, the ends
    # trimmed, an empty level named "blank". So `deck:JP vocab :: 10-80` finds "JP
    # vocab::10-80", and `deck:` finds a deck called "blank". The search matches the deck
    # itself or any deck below it.
    native_search = "\x1f".join(
        _to_custom_re(_deck_name_component(level), ".") for level in deck.split("::")
    )
    regex = re.compile(f"{native_search}(?:\\Z|\x1f)", re.IGNORECASE)
    return regex.match(target["name"].replace("::", "\x1f")) is not None


def _deck_name_component(level: str) -> str:
    level = "".join(char for char in level if not (char < " " or char == "\x7f"))
    return level.strip(_RUST_WHITESPACE) or "blank"


_JUDGES = {
    "text": _judge_text,
    "field": _judge_field,
    "tag": _judge_tag,
    "note": _judge_note_type,
    "deck": _judge_deck,
    "all": lambda term, view: True,
}


def _judge(node: _Node, view: _NoteView) -> bool:
    # Every term is judged, even where AND or OR has already decided: a term that cannot be
    # judged must fail the search whatever sits next to it, not only when it is reached.
    if isinstance(node, _Term):
        return _JUDGES[node.kind](node, view)
    if isinstance(node, _Not):
        return not _judge(node.node, view)
    values = [item if isinstance(item, str) else _judge(item, view) for item in node.items]
    # Anki writes the group into SQL as it stands, so AND binds tighter than OR: the group is
    # true when any OR-separated run of ANDed terms is.
    alternatives = [True]
    for value in values:
        if value == _OR:
            alternatives.append(True)
        elif value != _AND:
            alternatives[-1] = alternatives[-1] and value
    return any(alternatives)


@dataclass(frozen=True)
class ParsedSearch:
    """A search parsed once, to be judged against any number of unsaved notes."""

    search: str
    _root: _Node

    def matches(self, note: Note, deck_id: Optional[int]) -> bool:
        """Whether `note`, added to `deck_id`, would be found by this search.

        Raises `UnjudgeableSearch` when a term depends on something this note or deck makes
        unknowable (see `_judge_deck` and `_saved_tags`).
        """
        view = _NoteView(note, deck_id)
        try:
            return _judge(self._root, view)
        finally:
            view.close()


def parse_search(search: str) -> ParsedSearch:
    """Parse `search` as Anki 25.9 would.

    Raises `SearchSyntaxError` where Anki would refuse the search, and `UnjudgeableSearch`
    for the first term that needs cards, history, ids or collection state.
    """
    return ParsedSearch(search, _Parser().parse(search))


def matches(search: str, note: Note, deck_id: Optional[int]) -> bool:
    """Whether the unsaved `note`, added to `deck_id`, would be found by `search`.

    To ask exactly what `find_notes(f"({search}) nid:{id}")` asks of a saved note, pass the
    search in the same parentheses: a search with an unbalanced `)` or an inner newline reads
    differently inside them.
    """
    return parse_search(search).matches(note, deck_id)


__all__ = [
    "ParsedSearch",
    "SearchSyntaxError",
    "UnjudgeableSearch",
    "UnsavedNoteSearchError",
    "matches",
    "parse_search",
]
