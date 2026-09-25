"""Each piece of `unsaved_note_search`, one rule at a time.

`test_unsaved_note_search.py` compares whole searches with Anki's own answer, and that is the
authority on whether the matcher is right. This file pins the pieces the answer is built from
-- the escape and wildcard conversions, the name matching, the parser's shapes, precedence,
SQLite's LIKE, the saved sort field, each judge, and which of the costly steps a search asks
for -- so a change to one of them fails here, named, rather than as one mismatch among a
table of hundreds.

Where a piece reproduces something Anki stores, the test asks Anki: the sort field is compared
with the `sfld` column of the same note saved, not with a value written down here.
"""

import threading
import unicodedata

import pytest
from anki.collection import Config

from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic import unsaved_note_search as search_module
from copy_anywhere.logic.unsaved_note_search import (
    SearchSyntaxError,
    UnjudgeableSearch,
    _AND,
    _OR,
    _ascii_lower,
    _deck_name_component,
    _escaped,
    _EscapedError,
    _glob_matcher,
    _Group,
    _is_glob,
    _Not,
    _NoteView,
    _Parser,
    _Term,
    _to_custom_re,
    _to_sql,
    _to_text,
    _unescape,
    _unicase_equal,
    _without_combining,
    matches,
    parse_search,
)

NEKO = {"Word": "ne<b>ko</b>", "Reading": "ねこ", "Meaning": "a cat", "Note": "50%_off*"}


def unsaved(col, fields=None, tags=("JLPT::N5", "animal"), note_type=VOCAB):
    note = col.new_note(col.models.by_name(note_type))
    for name, value in (NEKO if fields is None else fields).items():
        note[name] = value
    note.tags = list(tags)
    return note


# Escapes and wildcards -----------------------------------------------------------------------


class TestUnescape:
    """The parser's own escapes are resolved; the wildcard ones are left for later."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("a\\:b", "a:b"),
            ('\\"', '"'),
            ("\\(x\\)", "(x)"),
            ("\\-x", "-x"),
            ("a\\\\b", "a\\\\b"),
            ("a\\*b", "a\\*b"),
            ("a\\_b", "a\\_b"),
            ("a\\\\s", "a\\\\s"),  # an escaped backslash, then a plain `s`
            ("plain", "plain"),
        ],
    )
    def test_known_escapes(self, text, expected):
        assert _unescape(text, text) == expected

    @pytest.mark.parametrize("text", ["a\\sb", "\\a", "x\\%", "\\\\\\s"])
    def test_any_other_escape_is_anki_s_error(self, text):
        with pytest.raises(SearchSyntaxError, match="unknown escape"):
            _unescape(text, text)


class TestToSql:
    """Anki wildcards into a LIKE pattern escaped with a backslash."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("*", "%"),
            ("%", "\\%"),
            ("\\*", "*"),
            ("\\\\", "\\\\"),
            ("_", "_"),
            ("\\_", "\\_"),
            ("a*b%c", "a%b\\%c"),
        ],
    )
    def test_conversion(self, text, expected):
        assert _to_sql(text) == expected


class TestToCustomRe:
    """Anki wildcards into a regular expression over a chosen single-character class."""

    @pytest.mark.parametrize(
        "text, wildcard, expected",
        [
            ("*", ".", ".*"),
            ("_", ".", "."),
            ("\\_", ".", "_"),
            ("\\*", ".", "\\*"),
            ("\\\\", ".", "\\\\"),
            ("a.b", ".", "a\\.b"),
            ("ö", ".", "ö"),
            ("_", "\\S", "\\S"),
            ("*", "\\S", "\\S*"),
        ],
    )
    def test_conversion(self, text, wildcard, expected):
        assert _to_custom_re(text, wildcard) == expected


class TestIsGlob:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("a*", True),
            ("_x", True),
            ("\\\\*", True),  # an escaped backslash leaves the `*` a wildcard
            ("\\*", False),
            ("\\_", False),
            ("\\\\\\*", False),  # an escaped backslash, then an escaped `*`
            ("plain", False),
        ],
    )
    def test_an_unescaped_star_or_underscore_is_a_wildcard(self, text, expected):
        assert _is_glob(text) is expected


class TestToText:
    @pytest.mark.parametrize(
        "text, expected", [("a\\*b", "a*b"), ("\\\\", "\\"), ("\\_", "_"), ("x", "x")]
    )
    def test_every_escape_becomes_its_character(self, text, expected):
        assert _to_text(text) == expected


# Folding and names ---------------------------------------------------------------------------


class TestAccentFolding:
    """What "ignore accents in search" compares: NFKD, combining marks dropped."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("café", "cafe"),
            ("ﬁ", "fi"),
            ("ｎｅｋｏ", "neko"),
            ("が", "か"),
            ("plain", "plain"),
        ],
    )
    def test_folding(self, text, expected):
        assert _without_combining(text) == expected

    def test_a_hangul_syllable_becomes_its_three_letters(self):
        assert len(_without_combining("한")) == 3


class TestNameMatching:
    """Field and note type names: full case folding when plain, a regex when a glob."""

    @pytest.mark.parametrize(
        "a, b, expected",
        [
            ("GRÖSSE", "Größe", True),
            ("STRAẞE", "straße", True),
            ("word", "Word", True),
            ("Word", "Words", False),
        ],
    )
    def test_plain_names_fold_fully(self, a, b, expected):
        assert _unicase_equal(a, b) is expected

    @pytest.mark.parametrize(
        "pattern, name, expected",
        [
            ("GRÖSSE", "Größe", True),  # plain: full folding
            ("gröss*", "Größe", False),  # a glob: `ss` does not match `ß`
            ("grö*", "Größe", True),
            ("w_rd", "Word", True),
            ("W*", "word", True),
            ("w\\*rd", "Word", False),
            ("w\\*rd", "w*rd", True),
            ("*d", "Word", True),
            ("*d", "Words", False),  # a glob matches the whole name
        ],
    )
    def test_glob_matcher(self, pattern, name, expected):
        assert _glob_matcher(pattern)(name) is expected

    def test_only_ascii_letters_are_lowered_for_keywords(self):
        assert _ascii_lower("ÄBC and OR") == "Äbc and or"


class TestDeckNameComponent:
    """Each level of a searched deck name is cleaned the way a new deck's name is."""

    @pytest.mark.parametrize(
        "level, expected",
        [
            (" JP vocab ", "JP vocab"),
            ("", "blank"),
            ("  ", "blank"),
            ("a\x01b", "ab"),
            ("a\x7fb", "ab"),
            ("　x　", "x"),
        ],
    )
    def test_cleaning(self, level, expected):
        assert _deck_name_component(level) == expected


# The parser ----------------------------------------------------------------------------------


class TestEscapedRuns:
    """nom's `escaped`: runs of ordinary characters and backslash pairs."""

    def test_stops_at_the_first_stop_character(self):
        assert _escaped("abc def", " ", lambda char: True) == ("abc", " def")

    def test_a_backslash_pair_is_consumed_whole(self):
        assert _escaped("a\\ b c", " \\", lambda char: True) == ("a\\ b", " c")

    def test_the_whole_text_when_nothing_stops_it(self):
        assert _escaped("abc", " ", lambda char: True) == ("abc", "")

    @pytest.mark.parametrize(
        "text, stop, escapable, kind",
        [
            ("a\\", " \\", lambda char: True, "trailing backslash"),
            ("\\x", " \\", lambda char: False, "none of"),
            (" x", " ", lambda char: True, "nothing consumed"),
        ],
    )
    def test_errors(self, text, stop, escapable, kind):
        with pytest.raises(_EscapedError) as raised:
            _escaped(text, stop, escapable)
        assert raised.value.kind == kind


def shape(node) -> str:
    """A parsed search as compact text: `kind:name=value`, `-` for NOT, brackets for groups."""
    if isinstance(node, _Term):
        name = f"{node.name}=" if node.name else ""
        return f"{node.kind}:{name}{node.value}"
    if isinstance(node, _Not):
        return f"-{shape(node.node)}"
    if isinstance(node, _Group):
        items = (item if item in (_AND, _OR) else shape(item) for item in node.items)
        return "(" + " ".join(items) + ")"
    raise AssertionError(node)


class TestParserShapes:
    @pytest.mark.parametrize(
        "search, expected",
        [
            ("", "all:"),
            ("   ", "all:"),
            ("a", "(text:a)"),
            ("a b", "(text:a and text:b)"),
            ("a or b", "(text:a or text:b)"),
            ("a AnD b", "(text:a and text:b)"),
            ("a OR b c", "(text:a or text:b and text:c)"),
            ("-a", "(-text:a)"),
            ("-(a b)", "(-(text:a and text:b))"),
            ("(a)b", "((text:a) and text:b)"),
            ("--x", "(-text:-x)"),  # not a double negation: NOT the text "-x"
            ("- x", "(text:- and text:x)"),  # a lone "-" is text
            ('"a b"', "(text:a b)"),
            ('ne"ko"', "(text:ne and text:ko)"),
            ('Word:"a b"', "(field:Word=a b)"),
            ('"Word:a b"', "(field:Word=a b)"),
            ("Meaning:a:b", "(field:Meaning=a:b)"),  # only the first colon splits
            ("a\\:b", "(text:a:b)"),
            ("w\\*rd:x", "(field:w\\*rd=x)"),
            ("TAG:x", "(tag:x)"),
            ("Deck:x", "(deck:x)"),
            ('NOTE:"a b"', "(note:a b)"),
            ("a　b", "(text:a and text:b)"),  # the ideographic space separates terms
            ("a\tb", "(text:a\tb)"),  # a tab does not
        ],
    )
    def test_shapes(self, search, expected):
        assert shape(_Parser().parse(search)) == expected

    @pytest.mark.parametrize(
        "search",
        [
            "a)",
            "(a",
            "()",
            '"a',
            '""',
            "a OR",
            "OR a",
            "a OR OR b",
            "a AND OR b",
            ":a",
            '":a"',
            "a -or",
            "a\\",
            "wo\\rd:x",
        ],
    )
    def test_searches_anki_refuses(self, search):
        with pytest.raises(SearchSyntaxError):
            _Parser().parse(search)

    def test_a_term_needing_cards_is_refused_only_after_the_whole_search_parsed(self):
        with pytest.raises(SearchSyntaxError):
            _Parser().parse("is:new (a")
        with pytest.raises(UnjudgeableSearch) as raised:
            _Parser().parse("a is:new b")
        assert raised.value.term == "is:new"

    @pytest.mark.parametrize(
        "search, term",
        [("tag:re:x", "tag:re:x"), ("Word:re:x", "Word:re:x"), ("Word:nc:x", "Word:nc:x")],
    )
    def test_value_prefixes_that_are_refused(self, search, term):
        with pytest.raises(UnjudgeableSearch) as raised:
            _Parser().parse(search)
        assert raised.value.term == term


class TestPrecedence:
    """AND binds tighter than OR, as the SQL Anki writes does; every term is still judged."""

    T = '"note:CA Vocab"'
    F = '"note:Nope"'

    @pytest.mark.parametrize(
        "template, expected",
        [
            ("{T} OR {F} {F}", True),  # T OR (F AND F)
            ("{F} {F} OR {T}", True),
            ("({T} OR {F}) {F}", False),
            ("{F} OR {F}", False),
            ("-({F} OR {F})", True),
            ("{T} -{T}", False),
            ("{T} {T} OR {F}", True),
        ],
    )
    def test_grouping(self, col, template, expected):
        search = template.format(T=self.T, F=self.F)
        assert matches(search, unsaved(col), None) is expected

    def test_a_term_is_judged_even_where_the_answer_is_already_decided(self, col):
        # A tag Anki would rewrite on save makes every tag term unjudgeable, and the `OR`
        # already true on its left does not spare it.
        note = unsaved(col, tags=["two words"])
        with pytest.raises(UnjudgeableSearch):
            matches(f"{self.T} OR tag:x", note, None)


# The note as it would be saved ---------------------------------------------------------------


class TestLike:
    """SQLite's own LIKE, which is what Anki's search runs for text and field terms."""

    @pytest.mark.parametrize(
        "text, pattern, expected",
        [
            ("neko", "n_ko", True),
            ("ねこ", "_こ", True),  # `_` is one character, not one byte
            ("ねこ", "__", True),
            ("ねこ", "_", False),
            ("NEKO", "neko", True),  # ASCII letters ignore case
            ("Äpfel", "äpfel", False),  # nothing else does
            ("a%b", "a\\%b", True),
            ("axb", "a\\%b", False),
            ("a_b", "a\\_b", True),
            ("axb", "a\\_b", False),
            ("a\\b", "a\\\\b", True),
            ("line one\nline two", "line%two", True),  # `%` crosses a newline
            ("", "%", True),
        ],
    )
    def test_like(self, col, text, pattern, expected):
        assert _NoteView(unsaved(col), None).like(text, pattern) is expected


class TestSavedSortField:
    """The stripped sort field, as the notes table's integer-affinity column stores it."""

    @pytest.mark.parametrize(
        "value",
        [
            "ne<b>ko</b>",
            "<b>0</b>123",
            "1<i>.</i>50",
            "3.0e+5",
            "1e3",
            "-7",
            "+5",
            "00",
            " 42 ",
            "12abc",
            "0x10",
            "9223372036854775808",
            "a&amp;b",
            'x<img src="p.jpg">y',
            "<!-- c -->d",
            "",
        ],
    )
    def test_the_view_holds_what_anki_stores(self, col, value):
        fields = {"Word": value, "Meaning": "q"}
        saved = real_anki.add_note(col, VOCAB, fields)
        stored = col.db.scalar("select cast(sfld as text) from notes where id = ?", saved.id)
        assert _NoteView(unsaved(col, fields), None).sfld == stored

    def test_the_sort_field_follows_the_note_type(self, col):
        model = col.models.by_name(VOCAB)
        model["sortf"] = 2
        col.models.update_dict(model)
        assert _NoteView(unsaved(col), None).sfld == "a cat"

    def test_one_note_s_sort_field_does_not_linger_for_the_next(self, col):
        # The table the sort field goes through is kept between matches, so what it held
        # for the last note must not answer for this one.
        assert matches("123", unsaved(col, {"Word": "0123"}), None) is True
        assert matches("123", unsaved(col, {"Word": "abc", "Meaning": "q"}), None) is False


class TestNoteText:
    def test_fields_are_joined_by_the_unit_separator(self, col):
        view = _NoteView(unsaved(col, {"Word": "a", "Reading": "b"}), None)
        assert view.flds == "a\x1fb\x1f\x1f\x1f"

    def test_fields_are_nfc_when_the_collection_normalises(self, col):
        decomposed = unicodedata.normalize("NFD", "café")
        view = _NoteView(unsaved(col, {"Word": decomposed}), None)
        assert view.fields[0] == "café"

    def test_fields_are_as_written_when_it_does_not(self, col):
        col.set_config_bool(Config.Bool.NORMALIZE_NOTE_TEXT, False)
        decomposed = unicodedata.normalize("NFD", "café")
        view = _NoteView(unsaved(col, {"Word": decomposed}), None)
        assert view.fields[0] == decomposed


# The judges ----------------------------------------------------------------------------------


class TestTextJudge:
    def test_the_sort_field_is_searched_without_its_html(self, col):
        assert matches("neko", unsaved(col), None) is True

    def test_other_fields_are_searched_as_stored(self, col):
        note = unsaved(col, {"Word": "x", "Meaning": "ne<b>ko</b>"})
        assert matches("neko", note, None) is False
        assert matches("ne<b>ko", note, None) is True

    def test_a_wildcard_crosses_from_one_field_into_the_next(self, col):
        assert matches("ko</b>_ねこ", unsaved(col), None) is True
        assert matches("ko</b>ねこ", unsaved(col), None) is False

    def test_ignore_accents_folds_both_sides(self, col):
        col.set_config_bool(Config.Bool.IGNORE_ACCENTS_IN_SEARCH, True)
        note = unsaved(col, {"Word": "Café", "Meaning": "x"})
        assert matches("cafe", note, None) is True
        assert matches("Word:cafe", note, None) is False  # a field term does not fold


class TestFieldJudge:
    def test_the_whole_field_has_to_match(self, col):
        assert matches("Meaning:cat", unsaved(col), None) is False
        assert matches("Meaning:*cat", unsaved(col), None) is True

    def test_an_empty_value_matches_an_empty_field(self, col):
        assert matches("Freq:", unsaved(col), None) is True
        assert matches("Word:", unsaved(col), None) is False

    def test_a_field_the_note_type_does_not_have_matches_nothing(self, col):
        assert matches("nofield:*", unsaved(col), None) is False

    def test_a_run_of_matching_fields_is_matched_joined(self, col):
        fields = {"Word": "a", "Reading": "b", "Meaning": "c", "Freq": "d", "Note": "e"}
        note = unsaved(col, fields)
        # `*e*` names Reading, Meaning, Freq and Note, one run: the value has to cover them all.
        assert matches("*e*:b", note, None) is False
        assert matches("*e*:b_c_d_e", note, None) is True

    def test_any_field_is_a_regex_over_each_field(self, col):
        note = unsaved(col, {"Word": "x", "Note": "line one\nline two"})
        assert matches('"*:line*two"', note, None) is True  # `*` crosses the newline
        assert matches('"*:line one"', note, None) is False
        assert matches("_*:x", note, None) is True


class TestTagJudge:
    @pytest.mark.parametrize(
        "search, expected",
        [
            ("tag:jlpt", True),  # a parent finds its children
            ("tag:n5", False),
            ("tag:*::n5", True),
            ("tag:jlpt::*", True),
            ("tag:anim_l", True),
            ("tag:ani", False),
            ("tag:ANIMAL", True),
            ("tag:*", True),
            ("tag:none", False),
            ('"tag:jlpt animal"', False),  # a space is never inside a tag
        ],
    )
    def test_tag_rules(self, col, search, expected):
        assert matches(search, unsaved(col), None) is expected

    def test_none_matches_a_note_without_tags(self, col):
        assert matches("tag:none", unsaved(col, tags=()), None) is True

    def test_tags_fold_case_beyond_ascii(self, col):
        assert matches("tag:ÄRGER", unsaved(col, tags=["ärger"]), None) is True

    @pytest.mark.parametrize(
        "tag",
        [
            "",
            "b::",
            "a::::b",
            "tab\tx",
            "two words",
            "ctl\x01x",
            unicodedata.normalize("NFD", "café"),
        ],
    )
    def test_a_tag_anki_would_rewrite_on_save_cannot_be_judged(self, col, tag):
        note = unsaved(col, tags=["animal", tag])
        with pytest.raises(UnjudgeableSearch) as raised:
            matches("tag:animal", note, None)
        assert raised.value.term == "tag:animal"
        assert matches("neko", note, None) is True  # only tag terms need the tags


class TestNoteTypeJudge:
    @pytest.mark.parametrize(
        "search, expected",
        [
            ('"note:CA Vocab"', True),
            ('"note:ca vocab"', True),
            ("note:ca_vocab", True),
            ("note:CA*", True),
            ('"note:CA Voc"', False),  # the whole name
        ],
    )
    def test_note_type_rules(self, col, search, expected):
        assert matches(search, unsaved(col), None) is expected


class TestDeckJudge:
    @pytest.fixture
    def child(self, col):
        col.decks.id("JP vocab")
        return col.decks.id("JP vocab::10-80")

    @pytest.mark.parametrize(
        "search, expected",
        [
            ('"deck:JP vocab"', True),  # a deck finds the decks below it
            ('"deck:jp vocab::10-80"', True),
            ('"deck:JP vocab :: 10-80"', True),  # each level is trimmed
            ("deck:10-80", False),
            ("deck:*::10-80", True),
            ('"deck:JP vocab::10"', False),
        ],
    )
    def test_deck_rules(self, col, child, search, expected):
        assert matches(search, unsaved(col), child) is expected

    def test_every_deck_matches_star_without_knowing_the_deck(self, col):
        assert matches("deck:*", unsaved(col), None) is True

    @pytest.mark.parametrize("search", ["deck:current", "deck:filtered"])
    def test_collection_state_cannot_be_judged(self, col, child, search):
        with pytest.raises(UnjudgeableSearch):
            matches(search, unsaved(col), child)

    def test_an_unknown_deck_cannot_be_judged(self, col):
        with pytest.raises(UnjudgeableSearch):
            matches("deck:x", unsaved(col), None)


# What a search costs -------------------------------------------------------------------------


@pytest.fixture
def costly(col, monkeypatch):
    """Counts of the three costly steps a match can take, each through the note's collection.

    Collection options are read with a backend call each, the sort field is stripped with
    another, and the scratch database is SQLite. A search that needs none of them should
    take none of them.
    """
    counts = {"options": 0, "strip": 0, "sql": 0}
    get_config_bool = col.get_config_bool
    strip_html = col._backend.strip_html
    scratch = search_module._sql

    def counting(key, original):
        def wrapper(*args, **kwargs):
            counts[key] += 1
            return original(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(col, "get_config_bool", counting("options", get_config_bool))
    monkeypatch.setattr(col._backend, "strip_html", counting("strip", strip_html))
    monkeypatch.setattr(search_module, "_sql", counting("sql", scratch))
    return counts


class TestWhatASearchCosts:
    @pytest.mark.parametrize(
        "search",
        [
            "tag:jlpt",
            '"note:CA Vocab"',
            "deck:*",
            "tag:jlpt OR -tag:x",
            '(tag:a OR "note:CA*") -tag:b',
        ],
    )
    def test_tags_and_note_types_take_none_of_the_costly_steps(self, col, costly, search):
        matches(search, unsaved(col), None)
        assert costly == {"options": 0, "strip": 0, "sql": 0}

    def test_a_deck_term_takes_none_either(self, col, costly):
        assert matches('"deck:JP vocab"', unsaved(col), col.decks.id("JP vocab")) is True
        assert costly == {"options": 0, "strip": 0, "sql": 0}

    def test_a_field_term_needs_the_options_and_like_but_not_the_sort_field(self, col, costly):
        matches("Word:ne* Meaning:*cat", unsaved(col), None)
        assert costly["strip"] == 0
        assert costly["options"] == 1  # "normalise note text" only, read once
        assert costly["sql"] >= 1

    def test_any_field_needs_no_database(self, col, costly):
        matches('"*:a cat"', unsaved(col), None)
        assert (costly["strip"], costly["sql"]) == (0, 0)

    def test_the_sort_field_is_stripped_once_however_many_text_terms(self, col, costly):
        # Unprefixed text can appear anywhere in a search, beside anything else.
        assert matches("tag:jlpt neko cat -dog", unsaved(col), None) is True
        assert costly["strip"] == 1
        assert costly["options"] == 2  # both options, each read once


class TestTheScratchDatabase:
    def test_one_connection_serves_every_match_on_a_thread(self, col, monkeypatch):
        opened = []
        connect = search_module.sqlite3.connect
        monkeypatch.setattr(search_module, "_THREAD", threading.local())
        monkeypatch.setattr(
            search_module.sqlite3, "connect", lambda *a, **k: opened.append(1) or connect(*a, **k)
        )
        for _ in range(5):
            assert matches("neko Word:ne*", unsaved(col), None) is True
        assert len(opened) == 1

    def test_another_thread_gets_its_own(self, col):
        # A connection may only be used on the thread that opened it, and a bulk run judges
        # on a background thread while the editor judges on the main one.
        matches("neko", unsaved(col), None)
        answers = []
        note = unsaved(col)
        worker = threading.Thread(target=lambda: answers.append(matches("neko", note, None)))
        worker.start()
        worker.join()
        assert answers == [True]


class TestParsingOnce:
    def test_the_same_search_is_parsed_once(self):
        assert parse_search("tag:x OR neko") is parse_search("tag:x OR neko")

    def test_a_refused_search_is_refused_every_time(self):
        for _ in range(2):
            with pytest.raises(UnjudgeableSearch):
                parse_search("is:new")
