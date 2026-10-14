"""Where a text still spells a renamed or deleted name, and what it would say replaced.

The reconcile pass (`rename_reconcile.py`) files a rename warning under each location that
spells the old name, and the definition editor shows it there, hides it once the live text
no longer spells it, drops it on save, and offers to replace it. All of them have to agree
on what "spells" means, or a warning the pass filed would be hidden by an editor that
cannot see it, or never cleared by a save that finds it gone when it is not. So they all
ask the scanners here, and nothing else looks for a name in a text.

Three kinds of text, each read the way what runs it reads it:

- **Template text**: the `{{...}}` references `definition_migration.rewrite_references`
  walks (clozes left alone), read as `_rewrite_reference` in the pass reads them: a field
  or a card type of `trigger`, or a field or card type of another binding.
- **Search text**: the terms `query_terms` splits an Anki search into. A term with a
  wildcard, a regex or an unresolved `{{...}}` is not an exact name and is never a hit,
  which is the stated limitation: `deck:JP*` is not found by a rename of `JP`.
- **Code**: the string literals Python's own `tokenize` finds, so a comment, an identifier
  or a longer name holding the old one is never a hit. A literal is a hit when its value
  is the name (`code_literal`), or when it reads as a search with a hit in it
  (`code_search`). An f-string's literal parts are held to the same rule, each part on its
  own; one is found but never replaced: what it spells depends on what it interpolates,
  and only the user can say what it meant.

A scanner reports what a hit *is* (`Hit.kind`), not whether it stops the definition from
running: that depends on the definition (one trigger note type or several), which the
text does not know. `hit_blocks_run` is the one rule -- the block/warn table under "Where it
warns" in `docs/staged-definitions.md` -- applied by the pass. Which hits a stored warning
counts is the entry's to say (`entry_hits`): a definition's own trigger tokens and slots
spell its own trigger note type's names, which a rename in another note type is not about.

Each text is split once however many renames are looked for in it (`_search_terms`,
`_code_strings`, `_references` remember their last few texts): renaming a parent deck
renames every deck under it, one rename each, and the pass looks for all of them in every
location of every definition on the main thread.

Search replacements are quoted and escaped here by the rules Anki's own search writer
uses (`rslib/src/search/writer.rs`) rather than through `col.build_search_string`: a
`card:` term has no `SearchNode` to build it from, so the rules have to be written out once
anyway, and one set of rules for every term kind keeps the scanners free of a collection
(the editor asks them on every keystroke). The tests pin these rules to
`build_search_string`'s output and round-trip each replacement through `find_notes`.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Optional

from ..shared.interpolate.interpolate_fields import CARD_VALUE_RE, CARD_VALUES_DICT
from .definition_migration import reference_spans
from .flow_analysis import TRIGGER_BINDING
from .object_refs import KIND_CARD_TYPE, KIND_DECK, KIND_FIELD, KIND_NOTE_TYPE
from .query_terms import (
    DECK_PSEUDO_NAMES,
    SEARCH_KEYWORDS,
    is_skipped,
    split_at_colon,
    term_spans,
    unescape,
)

# What a hit is. The template scanner gives the first two, the search scanner the four
# `_term`s, the code scanner the two `code_`s, `find_in_slot` the two field slots and
# `find_in_deck_slot` the deck slot.

#: `{{trigger.Word}}` or `{{trigger.Recognition__Card_Due}}`.
TRIGGER_TOKEN = "trigger_token"
#: The same spelled through another binding: `{{note.Word}}` for notes from a query.
BINDING_TOKEN = "binding_token"
DECK_TERM = "deck_term"
NOTE_TERM = "note_term"
CARD_TERM = "card_term"
#: A field search, `Word:neko`: only its key is the name.
FIELD_TERM = "field_term"
#: A string in code whose value is the name: `note["Word"]`, `by_name("JP")`.
CODE_LITERAL = "code_literal"
#: A string in code that reads as a search naming it: `find_notes("deck:JP")`.
CODE_SEARCH = "code_search"
#: A slot naming a field of the trigger note: the unfocus lists, `write_if_field`, a field
#: write's target on the trigger note.
TRIGGER_SLOT = "trigger_slot"
#: A slot naming a field of notes whose note types are not known: a query's sort field, a
#: field write's target on a note from a query.
OTHER_SLOT = "other_slot"
#: A slot naming a deck: a card action's `change_deck`, which moves the card by name.
DECK_SLOT = "deck_slot"

#: The hits that spell a name through the definition's own trigger note type.
TRIGGER_HIT_KINDS = frozenset({TRIGGER_TOKEN, TRIGGER_SLOT})

#: The search key each object term kind is spelled with.
_TERM_KEYS = {KIND_DECK: "deck", KIND_NOTE_TYPE: "note", KIND_CARD_TYPE: "card"}
_TERM_KINDS = {"deck": DECK_TERM, "note": NOTE_TERM, "card": CARD_TERM}


@dataclass(frozen=True)
class Rename:
    """One renamed or deleted object, by kind and its names.

    `kind` is one of `object_refs`' kinds; `new` is None when the object was deleted. Ids are
    not here: a text spells names only, and the warning entry the caller files keeps them.
    """

    kind: str
    old: str
    new: Optional[str] = None

    @classmethod
    def from_entry(cls, entry: Any) -> Optional["Rename"]:
        """The rename a stored warning entry is about, or None when the entry is unusable."""
        if not isinstance(entry, dict):
            return None
        kind, old, new = entry.get("kind"), entry.get("old"), entry.get("new")
        if kind not in _NAME_MATCHERS or not isinstance(old, str) or not old:
            return None
        return cls(kind, old, new if isinstance(new, str) and new else None)


@dataclass(frozen=True)
class Hit:
    """One place a text spells the old name: `text[start:end]`, and what could replace it.

    `replacement` is the text for that span with the new name in it, or None when there is
    no new name (a deletion) or the spelling cannot be rewritten mechanically (an f-string,
    a raw string the new name cannot go into).
    """

    start: int
    end: int
    kind: str
    replacement: Optional[str] = None

    @property
    def replaceable(self) -> bool:
        return self.replacement is not None


def hit_blocks_run(hit_kind: str, rename: Rename, multi_trigger: bool, uses_object: bool) -> bool:
    """Whether a warning for this hit stops the definition from running.

    `uses_object` says whether the definition is known to use the renamed object: it
    triggers on the note type a field or card type belongs to, or it references the deck
    or note type by id.

    Blocks: a `deck:` or `note:` term, in a search or in a string of code, and a deck slot
    -- a search term names its object unmistakably, and deck and note type names are unique
    in the collection; a string of code that is the name, or a search naming a field or card
    type, in a definition that uses the object (the user's choice); a trigger token or slot
    of a definition on several trigger note types, or naming a field or card type that was
    deleted. Warns only: `card:` and field terms, a sort field or a query note's field, a
    field or card type spelled through another binding, and a string of code that is the
    name in a definition not known to use the object -- `"default"` is a deck's name and
    also any setting's -- so the hit is a guess. That holds for a deletion too: another note
    type the search, the binding or the code reaches may still have the name.

    A trigger token or slot of a one-trigger definition is followed by the pass rather than
    warned about, so it only reaches here as a deletion.
    """
    if hit_kind in (DECK_TERM, NOTE_TERM, DECK_SLOT):
        return True
    if hit_kind == CODE_SEARCH and rename.kind in (KIND_DECK, KIND_NOTE_TYPE):
        return True
    if hit_kind in (CODE_LITERAL, CODE_SEARCH):
        return uses_object
    if hit_kind in (TRIGGER_TOKEN, TRIGGER_SLOT):
        return multi_trigger or rename.new is None
    return False


# Matching names ---------------------------------------------------------------------------


def _folded_equal(a: str, b: str) -> bool:
    return a.lower() == b.lower()


def _exactly_equal(a: str, b: str) -> bool:
    return a == b


#: How each kind's name is compared, as what reads it compares it: a field without regard to
#: case (the interpolation and a field search both do), a deck and a note type too (Anki's
#: names are case-insensitive), and a card type exactly, since the card values dict keys it
#: exactly. A `card:` search term is the exception, matched as Anki matches it (`_search_hit`).
_NAME_MATCHERS = {
    KIND_FIELD: _folded_equal,
    KIND_DECK: _folded_equal,
    KIND_NOTE_TYPE: _folded_equal,
    KIND_CARD_TYPE: _exactly_equal,
}


def names_equal(kind: str, one: str, other: str) -> bool:
    """Whether two names of this kind name the same object, as what reads them matches them.

    The one rule, for the pass too: what it follows, what it counts as a rename and what it
    reads as a rename undone have to agree with what the scanners find.
    """
    return _NAME_MATCHERS[kind](one, other)


def _names_equal(rename: Rename, name: str) -> bool:
    return names_equal(rename.kind, name, rename.old)


# Slots -------------------------------------------------------------------------------------


def find_in_slot(value: Any, rename: Rename, kind: str = TRIGGER_SLOT) -> list[Hit]:
    """A slot holding one field name (`kind` says whose): a hit over all of it, or none."""
    if rename.kind != KIND_FIELD or not isinstance(value, str) or not value:
        return []
    if not _names_equal(rename, value):
        return []
    return [Hit(0, len(value), kind, rename.new)]


def find_in_deck_slot(value: Any, rename: Rename) -> list[Hit]:
    """A slot holding one deck name: a hit over all of it when it names the old deck.

    Without regard to case, as Anki looks a deck up by name. What a card action stores for
    "no move" (`None`, `"-"`, `0`) and a deck id are not names, and never a hit.
    """
    if rename.kind != KIND_DECK or not isinstance(value, str) or value in ("", "-"):
        return []
    if not _names_equal(rename, value):
        return []
    return [Hit(0, len(value), DECK_SLOT, rename.new)]


# Template text -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reference:
    """The inside of one `{{binding.name}}` reference, read as the interpolation reads it.

    `field` is everything after the binding, as a field name. `card_type` is set when that
    also reads as a card value -- a card type name followed by a card value key the
    interpolation knows, `Recognition__Card_Due` -- and `card_key` is the rest of it, so a
    field whose own name holds `__` is not read as one.
    """

    binding: str
    field: str
    card_type: Optional[str] = None
    card_key: str = ""

    def with_field(self, name: str) -> str:
        return f"{self.binding}.{name}"

    def with_card_type(self, name: str) -> str:
        return f"{self.binding}.{name}{self.card_key}"


def read_reference(inside: str) -> Optional[Reference]:
    """The reference inside `{{...}}`, or None when it names no binding.

    The one reading both the pass's rewrite (`rename_reconcile._rewrite_reference`) and the
    scanner use, so what is followed and what is warned about cannot part ways.
    """
    head, dot, rest = inside.partition(".")
    if not dot or not head:
        return None
    match = CARD_VALUE_RE.match(rest)
    if match and match.group(2) in CARD_VALUES_DICT:
        return Reference(head, rest, match.group(1), rest[len(match.group(1)) :])
    return Reference(head, rest)


def _reference_replacement(inside: str, rename: Rename) -> Optional[tuple[str, str]]:
    """`(hit kind, new inside)` when this reference spells the old name, else None."""
    reference = read_reference(inside)
    if reference is None:
        return None
    kind = TRIGGER_TOKEN if reference.binding == TRIGGER_BINDING else BINDING_TOKEN
    new = rename.new or ""
    if rename.kind == KIND_FIELD and _names_equal(rename, reference.field):
        return kind, reference.with_field(new)
    if (
        rename.kind == KIND_CARD_TYPE
        and reference.card_type is not None
        and _names_equal(rename, reference.card_type)
    ):
        return kind, reference.with_card_type(new)
    return None


@lru_cache(maxsize=256)
def _references(text: str) -> tuple[tuple[int, int, str], ...]:
    return tuple(reference_spans(text))


def find_in_template(text: Any, rename: Rename) -> list[Hit]:
    """The `{{...}}` references spelling a renamed or deleted field or card type.

    A deck or a note type is never spelled in a reference -- references to them are ids,
    followed silently -- so a rename of one finds nothing here.
    """
    if not isinstance(text, str) or rename.kind not in (KIND_FIELD, KIND_CARD_TYPE):
        return []
    hits = []
    for start, end, inside in _references(text):
        found = _reference_replacement(inside, rename)
        if found is not None:
            kind, new_inside = found
            replacement = "{{" + new_inside + "}}" if rename.new else None
            hits.append(Hit(start, end, kind, replacement))
    return hits


# Search text ---------------------------------------------------------------------------------

#: When Anki's search writer puts a term in quotes: a bare `and` / `or`, a leading `-`, a
#: space (ASCII or ideographic) or a parenthesis.
_NEEDS_QUOTES = re.compile("(?i)^and$|^or$|^-.| |\u3000|\\(|\\)")


def _maybe_quote(term: str) -> str:
    return f'"{term}"' if _NEEDS_QUOTES.search(term) else term


def _escape_search_text(name: str) -> str:
    """A name as a search spells it: backslash, quote and the two wildcards escaped."""
    return re.sub(r'([\\"*_])', r"\\\1", name)


def _escape_field_key(name: str) -> str:
    """A field name as the key of a field search: also its colon, or it would end the key."""
    return _escape_search_text(name).replace(":", "\\:")


def object_term(kind: str, name: str) -> str:
    """The `deck:` / `note:` / `card:` term finding the object of this kind named `name`.

    Spelled as `col.build_search_string(SearchNode(deck=name))` spells it, and the same for
    `card:`, which has no `SearchNode` by name.
    """
    return _maybe_quote(f"{_TERM_KEYS[kind]}:{_escape_search_text(name)}")


def _field_term_hit(
    query: str, start: int, end: int, negated: bool, raw_key: str, raw_value: str, new: str
) -> Hit:
    """A field term with its key renamed and its value kept exactly as written.

    Where it can, the hit covers only the key, so that a `{{trigger.Word}}` in the value --
    `Word:{{trigger.Word}}` is a common query -- is a separate hit that does not overlap it.
    It covers the whole term when the new key needs quotes the term does not have.
    """
    new_key = _escape_field_key(new)
    key_start = start + (1 if negated else 0)
    in_quotes = query[key_start : key_start + 1] == '"'
    key_start += 1 if in_quotes else 0
    key_end = key_start + len(raw_key)
    if query[key_start:key_end] == raw_key and query[key_end : key_end + 1] == ":":
        if in_quotes or not _NEEDS_QUOTES.search(new_key):
            return Hit(key_start, key_end, FIELD_TERM, new_key)
    prefix = "-" if negated else ""
    return Hit(start, end, FIELD_TERM, prefix + _maybe_quote(f"{new_key}:{raw_value}"))


@lru_cache(maxsize=512)
def _search_terms(query: str) -> tuple[tuple[int, int, bool, str, str, str], ...]:
    """Each `key:value` term of a search: `(start, end, negated, raw key, raw value, key)`,
    the last unescaped and lower-cased."""
    terms = []
    for start, end, text in term_spans(query):
        # Only a `-` outside quotes negates: `"-deck:x"` is a search of a field `-deck`.
        negated = query[start] == "-"
        split = split_at_colon(text[1:] if negated else text)
        if split is None:
            continue
        raw_key, raw_value = split
        terms.append((start, end, negated, raw_key, raw_value, unescape(raw_key).lower()))
    return tuple(terms)


def find_in_search(query: Any, rename: Rename) -> list[Hit]:
    """The terms of an Anki search that name the renamed or deleted object.

    `deck:` and `note:` by name without regard to case, `card:` too (Anki matches it so),
    and a field search by its key without regard to case. A leading `-` is kept on the
    replacement; quotes around the term or its value are rewritten as Anki would write them.
    """
    if not isinstance(query, str) or not query or rename.kind not in _NAME_MATCHERS:
        return []
    hits: list[Hit] = []
    for start, end, negated, raw_key, raw_value, key in _search_terms(query):
        if key in _TERM_KINDS:
            if _TERM_KEYS.get(rename.kind) != key or is_skipped(raw_value):
                continue
            value = unescape(raw_value)
            if not value or value.lower().startswith("re:"):
                continue
            if key == "deck" and value.lower() in DECK_PSEUDO_NAMES:
                continue
            # `card:2` is an ordinal, not a name.
            if key == "card" and value.isdigit():
                continue
            if value.lower() != rename.old.lower():
                continue
            replacement = None
            if rename.new:
                replacement = ("-" if negated else "") + object_term(rename.kind, rename.new)
            hits.append(Hit(start, end, _TERM_KINDS[key], replacement))
        elif key not in SEARCH_KEYWORDS and rename.kind == KIND_FIELD:
            if is_skipped(raw_key) or key != rename.old.lower():
                continue
            if not rename.new:
                hits.append(Hit(start, end, FIELD_TERM))
                continue
            hits.append(
                _field_term_hit(query, start, end, negated, raw_key, raw_value, rename.new)
            )
    return hits


def find_in_query(query: Any, rename: Rename) -> list[Hit]:
    """A query's text: an Anki search that may also hold `{{...}}` references."""
    return sorted(
        find_in_template(query, rename) + find_in_search(query, rename),
        key=lambda hit: (hit.start, hit.end),
    )


# Code ------------------------------------------------------------------------------------------

#: Only on Python 3.12+, where an f-string is a run of tokens rather than one STRING.
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)

_STRING_PREFIX = re.compile(r"^[rRbBuUfF]*")


def _literal_parts(token_text: str) -> Optional[tuple[str, str, str]]:
    """`(prefix, quote, body)` of a string literal's source, or None if it is not one."""
    prefix = _STRING_PREFIX.match(token_text)
    assert prefix is not None
    rest = token_text[prefix.end() :]
    for quote in ('"""', "'''", '"', "'"):
        if len(rest) >= 2 * len(quote) and rest.startswith(quote) and rest.endswith(quote):
            return prefix.group(), quote, rest[len(quote) : -len(quote)]
    return None


def _encode_literal(prefix: str, quote: str, value: str) -> Optional[str]:
    """`value` as a literal with this prefix and quote, or None when it cannot be one.

    A raw string cannot escape, so a value holding its quote character or ending in a
    backslash does not go into one. Whatever is built is read back and compared, so an
    escaping rule missed here gives no replacement rather than a wrong one.
    """
    if "r" in prefix.lower():
        if quote[0] in value or value.endswith("\\") or "\n" in value or "\r" in value:
            return None
        literal = f"{prefix}{quote}{value}{quote}"
    else:
        body = value.replace("\\", "\\\\").replace(quote[0], "\\" + quote[0])
        body = body.replace("\n", "\\n").replace("\r", "\\r")
        literal = f"{prefix}{quote}{body}{quote}"
    try:
        read_back = ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return None
    return literal if read_back == value else None


def _value_replacement(value: str, rename: Rename) -> Optional[tuple[str, Optional[str]]]:
    """How a string value spells the old name, and the value with the new one.

    None when it does not; `(hit kind, None)` when it does but has no replacement.
    """
    if _names_equal(rename, value):
        return CODE_LITERAL, rename.new
    hits = find_in_search(value, rename)
    if not hits:
        return None
    if not all(hit.replaceable for hit in hits):
        return CODE_SEARCH, None
    return CODE_SEARCH, apply(value, hits)


@lru_cache(maxsize=1024)
def _plain_literal(token_text: str) -> Optional[tuple[str, str, str]]:
    """`(prefix, quote, value)` of a string literal that is not an f-string, else None."""
    parts = _literal_parts(token_text)
    if parts is None or "f" in parts[0].lower():
        return None
    try:
        value = ast.literal_eval(token_text)
    except (SyntaxError, ValueError):
        return None
    return (parts[0], parts[1], value) if isinstance(value, str) else None


def _string_hit(token_text: str, start: int, end: int, rename: Rename) -> Optional[Hit]:
    literal = _plain_literal(token_text)
    if literal is None:
        return None
    prefix, quote, value = literal
    found = _value_replacement(value, rename)
    if found is None:
        return None
    kind, new_value = found
    replacement = _encode_literal(prefix, quote, new_value) if new_value is not None else None
    return Hit(start, end, kind, replacement)


@lru_cache(maxsize=256)
def _fstring_strings(source: str) -> tuple[str, ...]:
    """Every string an f-string holds: its literal parts, and strings inside its `{...}`.

    Read the same on every Python: the f-string's source is parsed whole, rather than read
    off the tokens, which split it differently from 3.12 on.
    """
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError:
        return ()
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _fstring_hit(source: str, start: int, end: int, rename: Rename) -> Optional[Hit]:
    """An f-string with a string in it that spells the name as a plain literal would.

    A literal part is held to the same rule as a whole literal -- the name, or a search
    naming it -- so `f"Wordlist {x}"` is not a hit for `Word`, nor `f"JPN {x}"` for a deck
    `JP`. Never replaced.
    """
    kinds = {
        found[0]
        for found in (_value_replacement(value, rename) for value in _fstring_strings(source))
        if found is not None
    }
    if not kinds:
        return None
    return Hit(start, end, CODE_SEARCH if CODE_SEARCH in kinds else CODE_LITERAL)


def _line_starts(code: str) -> list[int]:
    return [0] + [index + 1 for index, character in enumerate(code) if character == "\n"]


def _tokens(code: str) -> Optional[list[tokenize.TokenInfo]]:
    """Every token of the code, or None when it does not tokenize (it is being typed).

    Before 3.12 an unterminated string is an ERRORTOKEN rather than an exception, so that
    counts as not tokenizing too, and the scan finds the same on every Python.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    if any(token.type == tokenize.ERRORTOKEN and token.string.strip() for token in tokens):
        return None
    return tokens


def code_is_readable(code: Any) -> bool:
    """Whether `find_in_code` can read this code, so that finding nothing in it means the
    name is not there.

    Code that does not tokenize finds nothing too, and a caller dropping a warning because
    its text no longer spells the name must not read "cannot tell" as "gone". No code at
    all is readable: there is nothing in it to spell a name.
    """
    if not isinstance(code, str) or not code:
        return True
    return _code_strings(code) is not None


@lru_cache(maxsize=256)
def _code_strings(code: str) -> Optional[tuple[tuple[int, int, str, bool], ...]]:
    """Each string literal in the code, `(start, end, source, is an f-string)`, or None when
    the code does not tokenize."""
    tokens = _tokens(code)
    if tokens is None:
        return None
    line_starts = _line_starts(code)

    def offset(position: tuple[int, int]) -> int:
        return line_starts[position[0] - 1] + position[1]

    strings: list[tuple[int, int, str, bool]] = []
    fstring_depth = 0
    fstring_start = 0
    for token in tokens:
        if _FSTRING_START is not None and token.type == _FSTRING_START:
            if fstring_depth == 0:
                fstring_start = offset(token.start)
            fstring_depth += 1
        elif _FSTRING_END is not None and token.type == _FSTRING_END:
            fstring_depth -= 1
            if fstring_depth == 0:
                end = offset(token.end)
                strings.append((fstring_start, end, code[fstring_start:end], True))
        elif token.type == tokenize.STRING and fstring_depth == 0:
            parts = _literal_parts(token.string)
            is_fstring = parts is not None and "f" in parts[0].lower()
            strings.append((offset(token.start), offset(token.end), token.string, is_fstring))
    return tuple(strings)


def find_in_code(code: Any, rename: Rename) -> list[Hit]:
    """The string literals in Python code that spell the old name, or a search naming it.

    Code that does not tokenize finds nothing: it is mid-edit, and a guess at where its
    strings are would put a hit inside a comment.
    """
    if not isinstance(code, str) or not code or rename.kind not in _NAME_MATCHERS:
        return []
    strings = _code_strings(code)
    if strings is None:
        return []
    hits: list[Hit] = []
    for start, end, source, is_fstring in strings:
        found = (
            _fstring_hit(source, start, end, rename)
            if is_fstring
            else _string_hit(source, start, end, rename)
        )
        if found is not None:
            hits.append(found)
    return hits


# Replacing -------------------------------------------------------------------------------------


def applied_hits(hits: Iterable[Hit]) -> list[Hit]:
    """The hits `apply_with_spans` replaces, in text order.

    Hits are applied in text order; one overlapping a hit already applied, and one with no
    replacement, are left as they are (a second scan still finds them). The editor's diff
    dialog asks this rather than guessing, so it shows exactly what Apply will write.
    """
    applied: list[Hit] = []
    position = 0
    for hit in sorted(hits, key=lambda hit: (hit.start, hit.end)):
        if hit.replacement is None or hit.start < position:
            continue
        applied.append(hit)
        position = hit.end
    return applied


def apply_with_spans(text: str, hits: Iterable[Hit]) -> tuple[str, list[tuple[int, int]]]:
    """The text with every replaceable hit replaced, and where each replacement now stands,
    one span per hit of `applied_hits`, in the same order.
    """
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    position = 0
    length = 0
    for hit in applied_hits(hits):
        assert hit.replacement is not None
        pieces.append(text[position : hit.start])
        length += hit.start - position
        pieces.append(hit.replacement)
        spans.append((length, length + len(hit.replacement)))
        length += len(hit.replacement)
        position = hit.end
    pieces.append(text[position:])
    return "".join(pieces), spans


def apply(text: str, hits: Iterable[Hit]) -> str:
    """The text with every replaceable hit replaced (see `apply_with_spans`)."""
    return apply_with_spans(text, hits)[0]


# Reading a location ----------------------------------------------------------------------------
#
# How a location's value is read, which picks the scanner. The pass names each location's
# with one of these as it walks a definition (`rename_reconcile._locations`), and the editor
# part showing a location names the same one, so the part hides a warning exactly when a
# save would drop it.

READ_AS_TEXT = "text"
#: An Anki search that can also hold `{{...}}` references: a query, a note query predicate.
READ_AS_QUERY = "query"
READ_AS_CODE = "code"
#: Field names of the trigger note, one or a list: the unfocus lists, the migrated gates,
#: a field write's target when it writes the trigger note.
READ_AS_TRIGGER_SLOT = "trigger slot"
#: A field name of notes whose note types the pass cannot know: a sort field, a field
#: write's target on a note from a query.
READ_AS_OTHER_SLOT = "other slot"
#: A deck name: a card action's `change_deck`, which moves the card by name.
READ_AS_DECK_SLOT = "deck slot"


def find_at(read_as: str, value: Any, rename: Rename) -> list[Hit]:
    """Where a location's value spells the old name, read the way `read_as` says."""
    if read_as == READ_AS_CODE:
        return find_in_code(value, rename)
    if read_as == READ_AS_QUERY:
        return find_in_query(value, rename)
    if read_as == READ_AS_TEXT:
        return find_in_template(value, rename)
    if read_as == READ_AS_OTHER_SLOT:
        return find_in_slot(value, rename, OTHER_SLOT)
    if read_as == READ_AS_DECK_SLOT:
        return find_in_deck_slot(value, rename)
    names = value if isinstance(value, list) else [value]
    return [hit for name in names for hit in find_in_slot(name, rename, TRIGGER_SLOT)]


def counted_hits(hits: Iterable[Hit], through_trigger: bool) -> list[Hit]:
    """The hits a warning counts: without the trigger's own tokens and slots unless
    `through_trigger` (`TRIGGER_HIT_KINDS`)."""
    return [hit for hit in hits if through_trigger or hit.kind not in TRIGGER_HIT_KINDS]


def entry_hits(read_as: str, value: Any, entry: Any) -> list[Hit]:
    """The hits a stored warning is about in the location's value, as the pass counted them.

    An entry filed for a definition whose trigger does not spell the renamed object's names
    says so (`through_trigger: false`), and its own `{{trigger.Word}}` is then not a
    spelling of another note type's `Word`: the indicator does not show it, a save does not
    keep the warning for it, and Replace leaves it alone. An entry that does not say counts
    every hit.
    """
    rename = Rename.from_entry(entry)
    if rename is None:
        return []
    through_trigger = not (isinstance(entry, dict) and entry.get("through_trigger") is False)
    return counted_hits(find_at(read_as, value, rename), through_trigger)


def still_spelled(read_as: str, value: Any, entry: Any) -> bool:
    """Whether a stored warning still has its old name to point at in the location's value.

    What an editor part asks to decide whether to show a warning, and what a save asks to
    decide whether to keep it. Both lean towards keeping: code that does
    not tokenize finds nothing, but that is "cannot tell" and not "gone" -- code is mid-edit
    far more often than it is fixed -- and an entry that does not say which rename it is
    about cannot be checked at all, so it stays for the user to dismiss.
    """
    if Rename.from_entry(entry) is None:
        return True
    if read_as == READ_AS_CODE and not code_is_readable(value):
        return True
    return bool(entry_hits(read_as, value, entry))


__all__ = [
    "READ_AS_CODE",
    "READ_AS_DECK_SLOT",
    "READ_AS_OTHER_SLOT",
    "READ_AS_QUERY",
    "READ_AS_TEXT",
    "READ_AS_TRIGGER_SLOT",
    "BINDING_TOKEN",
    "CARD_TERM",
    "CODE_LITERAL",
    "CODE_SEARCH",
    "DECK_SLOT",
    "DECK_TERM",
    "FIELD_TERM",
    "NOTE_TERM",
    "OTHER_SLOT",
    "TRIGGER_SLOT",
    "TRIGGER_HIT_KINDS",
    "TRIGGER_TOKEN",
    "Hit",
    "Reference",
    "Rename",
    "apply",
    "applied_hits",
    "apply_with_spans",
    "code_is_readable",
    "counted_hits",
    "entry_hits",
    "find_in_code",
    "find_in_deck_slot",
    "find_in_query",
    "find_in_search",
    "find_in_slot",
    "find_at",
    "find_in_template",
    "hit_blocks_run",
    "names_equal",
    "object_term",
    "read_reference",
    "still_spelled",
]
