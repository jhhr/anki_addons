"""Differential tests for `unsaved_note_search`: the matcher against Anki's own search.

A search condition on a note being added cannot ask the collection (the note has id 0 and no
row), so `unsaved_note_search.matches` answers it in Python. It is only right if it gives the
answer Anki would give once the note is saved, and the only authority on that is Anki. So each
case here adds a note to a real collection, asks `find_notes(f"({search}) nid:{id}")` -- the
search a condition runs on a saved note -- and then asks the matcher the same question of an
unsaved copy: a `col.new_note` with the same fields and tags, id 0, never added.

The table also states what Anki answers, so it reads as a record of Anki's search rules and a
case cannot quietly become one where both sides say False for an uninteresting reason. The
seeded random searches at the end go wider than any table: they mix every kind of term,
negation, grouping and quoting, and compare refusals too (a search Anki rejects must be
rejected by the matcher).

The table shares one collection, built once for the module, because the notes are only read;
the cases that change a collection option get a fresh `col` of their own.
"""

import random
import unicodedata
from dataclasses import dataclass, field

import pytest
from anki.collection import Config

from anki_shared.testing import real_anki
from conftest import (
    CLOZE,
    CLOZE_FIELDS,
    SENTENCE,
    SENTENCE_FIELDS,
    SENTENCE_TEMPLATES,
    VOCAB,
    VOCAB_FIELDS,
    VOCAB_TEMPLATES,
)
from copy_anywhere.logic.unsaved_note_search import (
    SearchSyntaxError,
    UnjudgeableSearch,
    UnsavedNoteSearchError,
    matches,
    parse_search,
)

# A note type whose names need Unicode case folding: "GRÖSSE" names "Größe" (full folding, the
# way Anki compares a plain field or note type name), but only "ẞ" matches "ß" in a wildcard.
GERMAN = "CA Maße"
GERMAN_FIELDS = ["Größe", "Straße"]

# A note type whose sort field is its second field, because bare text also searches the sort
# field with its HTML stripped, and that must follow the note type's choice.
SORTED = "CA Sorted"
SORTED_FIELDS = ["Front", "Back"]

DECKS = [
    "JP vocab",
    "JP vocab::10-80",
    "JP vocab::10-80::x",
    "Other",
    "Straße",
    "Ärger::Öl",
    "blank",
]


@dataclass(frozen=True)
class NoteSpec:
    note_type: str
    fields: dict
    tags: tuple = ()
    deck: str = "JP vocab"


NOTES = {
    "neko": NoteSpec(
        VOCAB,
        {
            "Word": "ne<b>ko</b>",
            "Reading": "ねこ",
            "Meaning": "a cat",
            "Freq": "",
            "Note": "50%_off*",
        },
        ("JLPT::N5", "animal"),
        "JP vocab::10-80",
    ),
    "cafe": NoteSpec(
        VOCAB,
        {
            "Word": "Café",
            "Meaning": "crème brûlée",
            "Freq": "12",
            "Note": "line one\nline two",
        },
        ("Ärger", "food::fr"),
        "Other",
    ),
    "numbers": NoteSpec(VOCAB, {"Word": "<b>0</b>123", "Meaning": "q"}),
    "real": NoteSpec(VOCAB, {"Word": "1<i>.</i>50", "Meaning": "q"}),
    "html": NoteSpec(
        VOCAB,
        {"Word": 'x<img src="p.jpg">y', "Meaning": "a&amp;b<br>c"},
        deck="JP vocab::10-80::x",
    ),
    "german": NoteSpec(
        GERMAN, {"Größe": "Äpfel", "Straße": "ΣΊΣΥΦΟΣ"}, ("Straße", "Σοφία"), "Straße"
    ),
    "cloze": NoteSpec(CLOZE, {"Text": "ne{{c1::ko}} desu", "Extra": ""}, deck="Ärger::Öl"),
    "sentence": NoteSpec(
        SENTENCE, {"Sentence": "猫が好き", "Vocab": "猫", "Audio": "[sound:neko.mp3]"}
    ),
    "quotes": NoteSpec(
        VOCAB,
        {"Word": 'say "hi"', "Reading": "-dash", "Meaning": "a:b (c) back\\slash"},
        ("x::y::z",),
        "blank",
    ),
    "sorted": NoteSpec(SORTED, {"Front": "plain", "Back": "so<b>rt</b>"}),
    "fields": NoteSpec(
        VOCAB, {"Word": "a", "Reading": "b", "Meaning": "c", "Freq": "d", "Note": "e"}
    ),
}

ERROR = "error"

# (note, search, what Anki answers). Grouped by the rule each group shows.
CASES = [
    # Bare text: a substring of any field, ASCII letters only case-insensitive.
    ("neko", "cat a", True),  # two terms, each found somewhere
    ("neko", "cat zzz", False),
    ("neko", '"a cat"', True),
    ("neko", "cat", True),
    ("neko", "CAT", True),
    ("neko", "Cat", True),
    ("neko", "dog", False),
    ("neko", "ねこ", True),
    ("neko", "ね", True),
    ("cafe", "café", True),
    ("cafe", "CAFÉ", False),
    ("cafe", "cafe", False),
    ("cafe", "CRÈME", False),
    ("cafe", "crème", True),
    ("german", "äpfel", False),
    ("german", "ÄPFEL", True),  # "Ä" is the same letter; only "PFEL" needed folding
    ("german", "äPFEL", False),
    ("german", "Äpfel", True),
    ("german", "σίσυφος", False),
    # Bare text and HTML: the fields as stored, or the sort field with the HTML stripped.
    ("neko", "neko", True),  # only through the stripped sort field
    ("neko", "ne<b>ko", True),
    ("neko", "<b>", True),
    ("html", "xy", False),
    ("html", '"x p.jpg y"', True),  # the stripped sort field keeps the image's file name
    ("html", "p.jpg", True),
    ("html", "a&b", False),  # Meaning is not the sort field, so its entity stays one
    ("html", "a&amp;b", True),
    ("html", "b<br>c", True),
    ("sorted", "sort", True),  # the sort field is Back here
    ("sorted", "so<b>rt", True),
    ("sorted", "plain", True),
    # Bare text over the joined fields: wildcards cross from one field into the next.
    ("neko", "ne*cat", True),
    ("neko", "ko</b>_ねこ", True),  # `_` is the separator between Word and Reading
    ("neko", "ko</b>ねこ", False),
    ("neko", "c_t", True),
    ("neko", "c\\_t", False),
    ("neko", "50%", True),
    ("neko", "50\\_", False),
    ("neko", "%_off", True),
    ("neko", "off\\*", True),
    ("neko", "off*", True),
    ("neko", "*", True),
    ("neko", "_", True),
    # Sort fields that read as numbers are searched as numbers.
    ("numbers", "0123", False),  # the stripped sort field "0123" is stored as 123
    ("numbers", "123", True),
    ("numbers", "<b>0</b>123", True),
    ("real", "1.50", False),  # stored as 1.5
    ("real", "1.5", True),
    ("real", "1<i>.</i>50", True),
    # Negation, OR, AND, grouping and precedence.
    ("neko", "-dog", True),
    ("neko", "-cat", False),
    ("neko", "cat OR dog", True),
    ("neko", "dog OR cat", True),
    ("neko", "dog or cat", True),
    ("neko", "dog Or cat", True),
    ("neko", "cat and dog", False),
    ("neko", "cat AND ねこ", True),
    ("neko", "cat OR dog zzz", True),  # OR binds looser: cat OR (dog zzz)
    ("neko", "cat OR zzz dog", True),
    ("neko", "cat ねこ OR zzz dog", True),
    ("neko", "dog zzz OR cat", True),
    ("neko", "(cat OR dog) zzz", False),
    ("neko", "zzz cat OR dog", False),
    ("neko", "-(dog OR cat)", False),
    ("neko", "-(dog OR zzz)", True),
    ("neko", "((cat))", True),
    ("neko", "( cat )", True),
    ("neko", "(dog OR cat) ねこ", True),
    ("neko", "dog OR (cat zzz)", False),
    ("neko", "ねこ -(-cat)", True),
    ("neko", "ねこ\u3000cat", True),  # the ideographic space separates terms too
    ("neko", "(dog)cat", False),
    ("neko", "(cat)ねこ", True),
    ("neko", "cat(ねこ)", True),
    ("neko", "ne\"ko\"", True),  # "ne" and "ko"
    ("quotes", "--dash", False),  # not a double negation: NOT the text "-dash"
    ("quotes", "- dash", True),  # a lone "-" is text, and "-dash" contains it
    ("neko", "- neko", False),
    ("quotes", "-\\-dash", False),
    ("quotes", "\\-dash", True),
    ("neko", "-\\-dash", True),
    # Quoting and escapes.
    ("quotes", 'say \\"hi\\"', True),
    ("quotes", '"say \\"hi\\""', True),
    ("quotes", '"a:b"', False),  # a field search: field "a", value "b"
    ("quotes", '"a\\:b"', True),
    ("quotes", "a\\:b", True),
    ("quotes", "\\(c\\)", True),
    ("quotes", '"(c)"', True),
    ("quotes", "back\\\\slash", True),
    ("quotes", "back\\slash", ERROR),  # `\s` is not an escape Anki knows
    ("quotes", 'Word:"say \\"hi\\""', True),
    ("quotes", '"Word:say \\"hi\\""', True),
    ("quotes", 'Meaning:"a:b (c)*"', True),
    ("quotes", "Meaning:a\\:b*", True),
    ("quotes", "Meaning:a:b*", True),  # only the first colon splits the key off
    # Field searches: the whole field, same wildcards and case rules as bare text.
    ("neko", "Word:neko", False),
    ("neko", "Word:ne<b>ko</b>", True),
    ("neko", "word:NE<B>KO</B>", True),
    ("neko", "WORD:ne*", True),
    ("neko", "Meaning:cat", False),
    ("neko", "Meaning:*cat", True),
    ("neko", "Meaning:a_cat", True),
    ("neko", 'Meaning:"a cat"', True),
    ("neko", '"Meaning:a cat"', True),
    ("neko", "Meaning:a\\ cat", ERROR),
    ("neko", "Meaning:A CAT", False),  # two terms, "CAT" being bare text
    ("neko", 'Meaning:"A CAT"', True),
    ("neko", "Freq:", True),
    ("neko", "Word:", False),
    ("neko", "Freq:*", True),
    ("neko", "Freq:_*", False),
    ("neko", "Word:_*", True),
    ("neko", "nofield:*", False),
    ("neko", "nofield:", False),
    ("neko", "-nofield:x", True),
    ("neko", "Reading:ねこ", True),
    ("neko", "Reading:ね", False),
    ("cafe", "Word:café", True),  # "C" folds, "é" is the same letter
    ("cafe", "Word:Café", True),
    ("cafe", "Word:CAFÉ", False),
    ("cafe", "Word:cAFé", True),
    ("cafe", "Note:line*", False),  # "note:" is the note type, whatever the field is called
    ("cafe", "Note:*", True),
    ("cafe", '"*:line one*"', True),
    ("cafe", "Freq:12", True),
    ("german", "Größe:äpfel", False),
    ("german", "Größe:ÄPFEL", True),  # only the ASCII letters fold
    ("german", "Straße:σίσυφος", False),
    # Field names: case-insensitive with full Unicode folding; wildcards are a regex.
    ("german", "größe:Äpfel", True),
    ("german", "GRÖSSE:Äpfel", True),
    ("german", "grösse:Äpfel", True),
    ("german", "gröss*:Äpfel", False),
    ("german", "grö*:Äpfel", True),
    ("german", "STRAẞE:ΣΊΣΥΦΟΣ", True),
    ("neko", "W*:ne*", True),
    ("neko", "w_rd:ne*", True),
    ("neko", "*d:ne*", True),
    ("neko", "*g:ねこ", True),
    ("neko", "w\\*rd:ne*", False),
    # `*`, `_*` and `*_` as a field name mean "any field": a Unicode case-insensitive regex.
    ("neko", "*:a cat", False),
    ("neko", '"*:a cat"', True),
    ("neko", "*:cat", False),
    ("neko", "*:*cat", True),
    ("neko", "_*:ねこ", True),
    ("neko", "*_:ねこ", True),
    ("german", "*:äpfel", True),
    ("german", "*:σίσυφος", True),
    ("german", "*:σίσυφοσ", True),
    ("cafe", "*:line one*", False),  # "one*" is bare text
    ("cafe", '"*:line*two"', True),  # `*` crosses the newline
    ("cafe", '"*:line one"', False),
    # A wildcard field name matching several fields in a row: Anki matches the value against
    # the run of fields joined, not each field.
    ("fields", "*e*:b", False),  # Reading, Meaning, Freq, Note
    ("fields", "*e*:b*", True),
    ("fields", "*e*:b_c_d_e", True),
    ("fields", "*d*:a", True),  # Word, Reading
    ("fields", "*d*:b", False),
    ("fields", "*r*:a", True),
    ("fields", "**:a", False),
    ("fields", "**:a*", True),
    ("fields", "*:c", True),
    # Tags: Unicode case-insensitive, a parent finds its children, wildcards within a tag.
    ("neko", "tag:jlpt", True),
    ("neko", "tag:JLPT::n5", True),
    ("neko", "tag:N5", False),
    ("neko", "tag:jlpt::", False),
    ("neko", "tag:jlpt::*", True),
    ("neko", "tag:*n5", True),
    ("neko", "tag:*::n5", True),
    ("neko", "tag:j*", True),
    ("neko", "tag:jl*::n5", True),
    ("neko", "tag:anim_l", True),
    ("neko", "tag:ani", False),
    ("neko", "tag:animal*", True),
    ("neko", "tag:none", False),
    ("neko", "-tag:none", True),
    ("numbers", "tag:none", True),
    ("numbers", "tag:*", True),
    ("neko", "tag:", False),
    ("neko", '"tag:jlpt animal"', False),
    ("cafe", "tag:ärger", True),
    ("cafe", "tag:ÄRGER", True),
    ("cafe", "tag:food", True),
    ("cafe", "tag:fr", False),
    ("german", "tag:straße", True),
    ("german", "tag:STRAẞE", True),
    ("german", "tag:strasse", False),
    ("german", "tag:ΣΟΦΊΑ", True),
    ("german", "tag:σοφια", False),
    ("quotes", "tag:x::y", True),
    ("quotes", "tag:y", False),
    ("quotes", "tag:*y*", True),
    ("quotes", "tag:x::*::z", True),
    ("quotes", "tag:x*z", True),
    # Note types: the whole name; plain names fold fully, wildcard names are a regex.
    ("neko", "note:CA Vocab", False),  # "note:CA" and bare text "Vocab"
    ("neko", '"note:CA Vocab"', True),
    ("neko", '"note:ca vocab"', True),
    ("neko", "note:ca_vocab", True),
    ("neko", "note:CA*", True),
    ("neko", '"note:CA Voc"', False),
    ("neko", "note:*Sentence", False),
    ("sentence", "note:*Sentence", True),
    ("german", '"note:CA MASSE"', True),
    ("german", '"note:CA MASSE*"', False),
    ("german", '"note:CA MAẞE*"', True),
    ("cloze", "note:*cloze", True),
    # Decks: the deck or any below it, Unicode case-insensitive, `::` between levels.
    ("neko", '"deck:JP vocab"', True),
    ("neko", '"deck:jp vocab::10-80"', True),
    ("neko", '"deck:JP vocab::10-80::x"', False),
    ("html", '"deck:JP vocab::10-80::x"', True),
    ("html", '"deck:JP vocab::10-80"', True),
    ("neko", "deck:JP*", True),
    ("neko", "deck:*10-80", True),
    ("neko", "deck:10-80", False),
    ("neko", "deck:*::10-80", True),
    ("neko", "deck:*", True),
    ("neko", "deck:Other", False),
    ("neko", "-deck:Other", True),
    ("neko", '"deck:JP_vocab"', True),
    ("neko", '"deck:JP vocab::10"', False),
    ("neko", '"deck:JP vocab :: 10-80"', True),  # each level is trimmed
    ("neko", '"deck: JP vocab"', True),
    ("neko", '"deck:JP vocab::10-80::"', False),  # an empty level is "blank"
    ("quotes", "deck:", True),  # the deck called "blank"
    ("german", "deck:straße", True),
    ("german", "deck:STRAẞE", True),
    ("german", "deck:STRASSE", False),
    ("cloze", "deck:ärger", True),
    ("cloze", "deck:ÄRGER::ÖL", True),
    ("cloze", "deck:ärger::", False),
    ("cloze", "deck:Ärger::Ö_", True),
    # Keys are case-insensitive; a quoted key:value is the same term.
    ("neko", "Deck:*", True),
    ("neko", "TAG:animal", True),
    ("neko", "NOTE:CA*", True),
    ("neko", '"tag:animal"', True),
    ("neko", 'tag:"animal"', True),
    # Mixed.
    ("neko", '"deck:JP vocab" tag:jlpt* -Freq:_* (Word:*ko* OR cat)', True),
    ("cafe", "-tag:none (tag:food::* OR deck:JP*) -café", False),
    ("german", "Größe:äpfel OR Größe:Äpfel", True),
    # Searches Anki refuses; the matcher refuses them too.
    ("neko", "neko)", ERROR),
    ("neko", "(neko", ERROR),
    ("neko", "()", ERROR),
    ("neko", '"neko', ERROR),
    ("neko", '""', ERROR),
    ("neko", "neko OR", ERROR),
    ("neko", "OR neko", ERROR),
    ("neko", "neko OR OR cat", ERROR),
    ("neko", "neko AND OR cat", ERROR),
    ("neko", ":neko", ERROR),
    ("neko", '":neko"', ERROR),
    ("neko", "neko -or", ERROR),
    ("neko", "-and", ERROR),
    ("neko", "neko\\", ERROR),
    ("neko", "wo\\rd:x", ERROR),
    ("neko", "Word:50\\%", ERROR),
]


def _new_note_type(col, name, field_names, sort_field=0):
    model = real_anki.make_note_type(col, name, field_names)
    if sort_field:
        model["sortf"] = sort_field
        col.models.update_dict(model)
    return model


def _unsaved_copy(col, spec: NoteSpec):
    note = col.new_note(col.models.by_name(spec.note_type))
    for name, value in spec.fields.items():
        note[name] = value
    note.tags = list(spec.tags)
    return note


def _anki_answer(col, search: str, note_id: int):
    """What a search condition on the saved note answers, or ERROR if Anki refuses it."""
    try:
        return bool(col.find_notes(f"({search}) nid:{note_id}"))
    except Exception:  # SearchError, or DBError for a search it accepts but cannot run
        return ERROR


def _matcher_answer(search: str, note, deck_id):
    try:
        return matches(search, note, deck_id)
    except UnjudgeableSearch:
        raise
    except SearchSyntaxError:
        return ERROR


@dataclass
class Pool:
    col: object
    saved: dict = field(default_factory=dict)
    unsaved: dict = field(default_factory=dict)
    deck_ids: dict = field(default_factory=dict)


@pytest.fixture(scope="module")
def pool(tmp_path_factory, stub_mw):
    col = real_anki.open_collection(tmp_path_factory.mktemp("search") / "collection.anki2")
    real_anki.make_note_type(col, VOCAB, VOCAB_FIELDS, VOCAB_TEMPLATES)
    real_anki.make_note_type(col, SENTENCE, SENTENCE_FIELDS, SENTENCE_TEMPLATES)
    real_anki.make_note_type(col, CLOZE, CLOZE_FIELDS, is_cloze=True)
    _new_note_type(col, GERMAN, GERMAN_FIELDS)
    _new_note_type(col, SORTED, SORTED_FIELDS, sort_field=1)
    for deck in DECKS:
        col.decks.id(deck)
    result = Pool(col)
    for key, spec in NOTES.items():
        result.saved[key] = real_anki.add_note(
            col, spec.note_type, spec.fields, spec.deck, list(spec.tags)
        )
        result.unsaved[key] = _unsaved_copy(col, spec)
        result.deck_ids[key] = col.decks.id(spec.deck)
    try:
        yield result
    finally:
        col.close()


def _case_id(case):
    key, search, _ = case
    return f"{key}:{search!r}"


class TestAgainstAnki:
    @pytest.mark.parametrize("case", CASES, ids=[_case_id(case) for case in CASES])
    def test_the_matcher_answers_what_anki_answers(self, pool, case):
        key, search, stated = case
        unsaved = pool.unsaved[key]
        assert unsaved.id == 0
        anki = _anki_answer(pool.col, search, pool.saved[key].id)
        assert anki == stated, "the table misstates what Anki answers"
        assert _matcher_answer(search, unsaved, pool.deck_ids[key]) == anki

    def test_the_table_has_both_answers_for_every_kind_of_term(self):
        # A table of nothing but False would pass against a matcher that always says no.
        answers = [stated for _, _, stated in CASES]
        assert answers.count(True) > 100 and answers.count(False) > 60

    def test_a_search_parsed_once_judges_several_notes(self, pool):
        parsed = parse_search("tag:jlpt* OR note:*Sentence")
        judged = {key: parsed.matches(pool.unsaved[key], pool.deck_ids[key]) for key in NOTES}
        assert {key for key, value in judged.items() if value} == {"neko", "sentence"}

    def test_the_whole_search_without_parentheses_reads_as_anki_reads_it(self, pool):
        # `matches(search)` parses the search as it stands, as `find_notes(search)` does:
        # surrounding whitespace is trimmed and an unbalanced `)` is refused. Asked of the
        # whole collection, the saved note is found exactly when the matcher says so.
        searches = [
            "  neko  ",
            "\tneko\n",
            "\u3000neko",
            "neko\x1f",
            "neko)",
            "",
            "   ",
            "a) OR (cat",
        ]
        saved, unsaved = pool.saved["neko"], pool.unsaved["neko"]
        for search in searches:
            try:
                anki = saved.id in pool.col.find_notes(search)
            except Exception:
                anki = ERROR
            assert _matcher_answer(search, unsaved, pool.deck_ids["neko"]) == anki, search


class TestCollectionOptions:
    """Two collection options change what a search finds, so the matcher reads them too."""

    def _compare(self, col, spec: NoteSpec, searches):
        saved = real_anki.add_note(col, spec.note_type, spec.fields, spec.deck, list(spec.tags))
        unsaved = _unsaved_copy(col, spec)
        deck_id = col.decks.id(spec.deck)
        answers = {}
        for search in searches:
            anki = _anki_answer(col, search, saved.id)
            assert _matcher_answer(search, unsaved, deck_id) == anki, search
            answers[search] = anki
        return answers

    def test_ignore_accents_folds_bare_text_but_not_fields(self, col):
        col.set_config_bool(Config.Bool.IGNORE_ACCENTS_IN_SEARCH, True)
        spec = NoteSpec(
            VOCAB,
            {"Word": "Café", "Reading": "xがy", "Meaning": "ｎｅｋｏ ﬁx", "Note": "x한y"},
        )
        answers = self._compare(
            col,
            spec,
            [
                "cafe",
                "CAFE",
                "café",
                "Word:cafe",
                "Word:Café",
                "xかy",  # the voicing mark is a combining mark
                "x_y",
                "neko",  # full-width letters decompose to plain ones
                "fix",  # so does the ligature
                "x___y",  # a Hangul syllable decomposes into three letters
                "x_y OR x__y",
            ],
        )
        assert answers["cafe"] and answers["xかy"] and answers["neko"] and answers["fix"]
        assert not answers["Word:cafe"]

    def test_text_is_compared_in_nfc_when_the_collection_normalises(self, col):
        decomposed = unicodedata.normalize("NFD", "café")
        spec = NoteSpec(VOCAB, {"Word": decomposed, "Meaning": "q"})
        searches = ["café", decomposed, f"Word:{decomposed}", "Word:café"]
        answers = self._compare(col, spec, searches)
        assert all(answers.values())

    def test_text_is_compared_as_written_when_the_collection_does_not(self, col):
        col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        decomposed = unicodedata.normalize("NFD", "café")
        spec = NoteSpec(VOCAB, {"Word": decomposed, "Meaning": "q"})
        searches = ["café", decomposed, f"Word:{decomposed}", "Word:café"]
        answers = self._compare(col, spec, searches)
        assert answers == {
            "café": False,
            decomposed: True,
            f"Word:{decomposed}": True,
            "Word:café": False,
        }


# Terms the matcher refuses, and why they cannot be judged before the note is saved.
UNJUDGEABLE_TERMS = [
    "is:new",
    "IS:new",
    '"is:new"',
    'is:"new"',
    "card:1",
    "card:Recognition",
    "flag:0",
    "prop:ivl>1",
    "rated:1",
    "introduced:1",
    "added:1",
    "edited:1",
    "resched:1",
    "nid:0",
    "cid:1",
    "mid:1",
    "did:1",
    "dupe:1,neko",
    "has-cd:v",
    "preset:Default",
    "re:neko",
    "nc:neko",
    "w:neko",
    "sc:neko",
    "Word:re:ne",
    "Word:nc:neko",
    "tag:re:jl",
    "deck:current",
    "deck:filtered",
]


def _unsaved_neko(col):
    return _unsaved_copy(col, NOTES["neko"])


class TestUnjudgeableTerms:
    @pytest.mark.parametrize("term", UNJUDGEABLE_TERMS)
    @pytest.mark.parametrize(
        "template",
        ["{}", "neko OR {}", "-{}", "cat -(dog OR {})", "{} OR zzz"],
        ids=["alone", "after-a-matching-or", "negated", "nested", "before-an-or"],
    )
    def test_the_term_is_refused_by_name_wherever_it_stands(self, col, term, template):
        # Even where the rest of the search decides the answer ("neko OR is:new" is true
        # whatever `is:new` says), the term is refused: a condition that sometimes works and
        # sometimes fails on the same term would be harder to understand than one that says
        # up front it cannot be used on add.
        search = template.format(term)
        with pytest.raises(UnjudgeableSearch) as raised:
            matches(search, _unsaved_neko(col), col.decks.id("JP vocab"))
        assert raised.value.term == term
        assert f"'{term}'" in str(raised.value)

    def test_terms_that_need_cards_are_refused_before_any_note_is_seen(self):
        with pytest.raises(UnjudgeableSearch):
            parse_search("tag:x is:due")

    def test_a_search_anki_would_refuse_is_reported_as_that_first(self):
        with pytest.raises(SearchSyntaxError):
            parse_search("is:new (neko")

    def test_the_first_unjudgeable_term_is_the_one_named(self):
        with pytest.raises(UnjudgeableSearch) as raised:
            parse_search("neko prop:ivl>1 is:new")
        assert raised.value.term == "prop:ivl>1"

    def test_both_errors_are_value_errors_under_one_base(self):
        assert issubclass(UnjudgeableSearch, UnsavedNoteSearchError)
        assert issubclass(SearchSyntaxError, UnsavedNoteSearchError)
        assert issubclass(UnsavedNoteSearchError, ValueError)

    def test_a_deck_search_needs_a_deck(self, col):
        with pytest.raises(UnjudgeableSearch) as raised:
            matches("neko deck:Other", _unsaved_neko(col), None)
        assert raised.value.term == "deck:Other"
        # Without a deck term, no deck is needed; `deck:*` matches every note regardless.
        assert matches("neko", _unsaved_neko(col), None) is True
        assert matches("deck:*", _unsaved_neko(col), None) is True

    def test_a_filtered_deck_is_refused(self, col):
        filtered = col.decks.new_filtered("Filtered")
        with pytest.raises(UnjudgeableSearch):
            matches("deck:Filtered", _unsaved_neko(col), filtered)

    def test_a_note_type_with_its_own_target_deck_is_refused_for_deck_terms(self, col):
        # Anki puts such a template's cards in the template's deck, not the one the note is
        # added to, so which decks the note ends up in is not known here.
        model = col.models.by_name(VOCAB)
        model["tmpls"][1]["did"] = col.decks.id("Other")
        col.models.update_dict(model)
        with pytest.raises(UnjudgeableSearch):
            matches("deck:Other", _unsaved_neko(col), col.decks.id("JP vocab"))
        assert matches("tag:jlpt", _unsaved_neko(col), col.decks.id("JP vocab")) is True

    @pytest.mark.parametrize("tag", ["b::", "::c", "a::::b", "tab\tx", "two words", ""])
    def test_a_tag_anki_would_rewrite_on_save_is_refused_for_tag_terms(self, col, tag):
        note = _unsaved_neko(col)
        note.tags = ["animal", tag]
        with pytest.raises(UnjudgeableSearch) as raised:
            matches("tag:animal", note, col.decks.id("JP vocab"))
        assert raised.value.term == "tag:animal"
        assert matches("neko", note, col.decks.id("JP vocab")) is True


# Seeded random searches ---------------------------------------------------------------------

_TEXTS = [
    "neko", "NEKO", "ne*", "*ko", "n_ko", "ne<b>ko", "cat", "ca*", "a_cat", "ねこ", "猫",
    "äpfel", "Äpfel", "ÄPFEL", "café", "cafe", "x*y", "50%", "a\\_b", "a\\*b", "0123", "123",
    "1.5", "a&b", "&amp;", "\\(c\\)", '\\"hi\\"', "a\\:b", "\\\\", "σίσυφοσ", "*", "_",
    "p.jpg", "-", "\\-dash", "e", "c",
]  # fmt: skip
_FIELD_NAMES = [
    "Word", "word", "W*", "w_rd", "*", "_*", "**", "*e*", "*r*", "Freq", "Note", "nofield",
    "Text", "Größe", "GRÖSSE", "gröss*", "Straße", "Meaning", "Back", "*g",
]  # fmt: skip
_TAG_SEARCHES = [
    "jlpt", "JLPT::n5", "n5", "*n5", "*::n5", "j*", "jlpt::", "jlpt::*", "anim_l", "ärger",
    "straße", "STRASSE", "x::y", "*y*", "none", "*", "", "σοφία", "food",
]  # fmt: skip
_DECK_SEARCHES = [
    "JP vocab", "jp vocab", "JP*", "*10-80", "JP vocab::*", "Other", "*", "JP_vocab", "10-80",
    "straße", "STRASSE", "ärger::öl", "", "JP vocab :: 10-80", "Other\x1f", "blank", "*x",
]  # fmt: skip
_NOTE_TYPE_SEARCHES = ["CA Vocab", "ca vocab", "CA*", "ca_vocab", "*Cloze", "CA MASSE", "*"]


def _random_term(rnd: random.Random) -> str:
    roll = rnd.random()
    if roll < 0.3:
        text = rnd.choice(_TEXTS)
        return f'"{text}"' if rnd.random() < 0.2 else text
    if roll < 0.6:
        name, value = rnd.choice(_FIELD_NAMES), rnd.choice(_TEXTS + [""])
        if rnd.random() < 0.3:
            return f'{name}:"{value}"' if value else f"{name}:"
        return f"{name}:{value}"
    if roll < 0.75:
        return f"tag:{rnd.choice(_TAG_SEARCHES)}"
    if roll < 0.87:
        return f'"deck:{rnd.choice(_DECK_SEARCHES)}"'
    if roll < 0.95:
        return f'"note:{rnd.choice(_NOTE_TYPE_SEARCHES)}"'
    return rnd.choice(["OR", "and", "-", "(", ")", '"', ":", "-or", "--dash", "\u3000", "\t"])


def _random_search(rnd: random.Random, depth: int = 0) -> str:
    parts = []
    for _ in range(rnd.randint(1, 4)):
        negation = "-" if rnd.random() < 0.2 else ""
        if rnd.random() < 0.15 and depth < 2:
            parts.append(f"{negation}({_random_search(rnd, depth + 1)})")
        else:
            parts.append(negation + _random_term(rnd))
        if rnd.random() < 0.3:
            parts.append(rnd.choice(["OR", "or", "AND"]))
    if len(parts) > 1 and parts[-1] in ("OR", "or", "AND") and rnd.random() < 0.9:
        parts.pop()
    return " ".join(parts)


# Whitespace other than a space, which Anki 26.8 started reading as a space everywhere in a
# search: before it, a tab or a newline is text, inside quotes and out. Anki's answer is asked
# live, so these hold against whichever version is installed; the random searches found the
# change, and the pieces suite pins both readings whatever is installed.
WHITESPACE_SEARCHES = [
    ("cafe", "\t"),
    ("cafe", "(\t)"),
    ("cafe", "-\t"),
    ("cafe", '"one\nline"'),  # the Note field holds "line one\nline two"
    ("cafe", "one\nline"),
    ("cafe", "Word:Caf*\tcrème"),
    ("cafe", "Note:line*\ttwo"),
    ("cafe", "a\\\tb"),  # a backslash before the tab
    ("cafe", "Word:Café\u00a0"),
    ("cafe", "\u3000"),
    ("cafe", '"crème\u3000brûlée"'),
    ("neko", "cat\tor\tzzz"),
    ("neko", "zzz\u2003OR\u2003cat"),
    ("neko", '"deck:JP vocab::10-80\x0b"'),
    ("neko", "tag:\tanimal"),
    ("neko", "tag:animal\x1f"),  # U+001F is not whitespace to Rust, in either version
    ("neko", "cat\u200b"),  # nor is a zero-width space
    ("real", '-\t "_" or Straße:äpfel'),
    ("quotes", "deck:\t"),
]


class TestWhitespace:
    @pytest.mark.parametrize("key, search", WHITESPACE_SEARCHES)
    def test_whitespace_reads_as_the_installed_anki_reads_it(self, pool, key, search):
        anki = _anki_answer(pool.col, search, pool.saved[key].id)
        mine = _matcher_answer(f"({search})", pool.unsaved[key], pool.deck_ids[key])
        assert mine == anki


class TestRandomSearches:
    def test_random_searches_get_anki_s_answer(self, pool):
        rnd = random.Random(20260925)
        keys = list(NOTES)
        mismatches = []
        judged = 0
        for _ in range(1500):
            search = _random_search(rnd)
            key = rnd.choice(keys)
            anki = _anki_answer(pool.col, search, pool.saved[key].id)
            # Wrapped as the condition wraps it, so what the matcher parses is exactly what
            # Anki parsed, refusals included.
            mine = _matcher_answer(f"({search})", pool.unsaved[key], pool.deck_ids[key])
            judged += mine is True
            if mine != anki:
                mismatches.append((key, search, anki, mine))
        assert mismatches == []
        assert judged > 150  # enough of them match for "False everywhere" not to pass
