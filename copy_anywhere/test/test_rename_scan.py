"""The rename scanners: where a text spells a renamed or deleted name, and its replacement.

Pure logic (`logic/rename_scan.py`) except the round trips, which prove on a real
collection that a replaced search finds, after the rename, what the original found before
it -- the one place a wrong escape would silently change what a definition does.
"""

from __future__ import annotations

import pytest
from anki.collection import SearchNode

from note_types import VOCAB
from copy_anywhere.logic.definition_migration import reference_spans, rewrite_references
from copy_anywhere.logic.object_refs import KIND_CARD_TYPE, KIND_DECK, KIND_FIELD, KIND_NOTE_TYPE
from copy_anywhere.logic.query_terms import term_spans
from copy_anywhere.logic.rename_scan import (
    BINDING_TOKEN,
    CARD_TERM,
    CODE_LITERAL,
    DECK_SLOT,
    DECK_TERM,
    FIELD_TERM,
    NOTE_TERM,
    OTHER_SLOT,
    TRIGGER_SLOT,
    TRIGGER_TOKEN,
    Hit,
    Rename,
    apply,
    apply_with_spans,
    code_is_readable,
    find_in_code,
    find_in_deck_slot,
    find_in_query,
    find_in_search,
    find_in_slot,
    find_in_template,
    hit_blocks_run,
    object_term,
)

WORD = Rename(KIND_FIELD, "Word", "Term")
RECOGNITION = Rename(KIND_CARD_TYPE, "Recognition", "Reading Card")
JP = Rename(KIND_DECK, "JP Vocab", "JP Words")
VOCAB_NOTE = Rename(KIND_NOTE_TYPE, "CA Vocab", "CA Words")


def spelled(text: str, hits: list[Hit]) -> list[str]:
    return [text[hit.start : hit.end] for hit in hits]


class TestSpans:
    """The two span walkers the scanners stand on, and that they agree with their siblings."""

    def test_a_term_span_covers_its_quotes_and_its_minus(self):
        query = '-deck:"JP Vocab" ("note:CA Vocab" or tag:x)'
        assert [(query[s:e], term) for s, e, term in term_spans(query)] == [
            ('-deck:"JP Vocab"', "-deck:JP Vocab"),
            ('"note:CA Vocab"', "note:CA Vocab"),
            ("or", "or"),
            ("tag:x", "tag:x"),
        ]

    def test_a_term_span_keeps_its_escapes(self):
        query = r"deck:a\"b\*c x"
        assert [(query[s:e], term) for s, e, term in term_spans(query)] == [
            (r"deck:a\"b\*c", r"deck:a\"b\*c"),
            ("x", "x"),
        ]

    def test_an_empty_quoted_term_has_no_span(self):
        assert list(term_spans('"" x')) == [(3, 4, "x")]

    def test_reference_spans_cover_the_braces_and_skip_cloze_markers(self):
        text = "a {{trigger.Word}} {{c1::x {{note.Word}} y}} {{c2::z}}"
        spans = reference_spans(text)
        assert [(text[s:e], inside) for s, e, inside in spans] == [
            ("{{trigger.Word}}", "trigger.Word"),
            ("{{note.Word}}", "note.Word"),
        ]

    def test_reference_spans_find_what_rewrite_references_rewrites(self):
        text = "{{c1::{{a}}::{{b}}}} {{c}} {{c3::{{c4::{{d}}}}}}"
        seen: list[str] = []
        rewrite_references(text, lambda inside: seen.append(inside) or inside)
        assert sorted(inside for _s, _e, inside in reference_spans(text)) == sorted(seen)

    def test_a_text_holding_a_marker_character_gives_no_spans(self):
        assert reference_spans("\x02{{trigger.Word}}") == []


class TestTemplate:
    def test_a_trigger_field_token_is_found_in_any_case(self):
        text = "x {{trigger.word}} y"
        hits = find_in_template(text, WORD)
        assert spelled(text, hits) == ["{{trigger.word}}"]
        assert hits[0].kind == TRIGGER_TOKEN
        assert hits[0].replacement == "{{trigger.Term}}"

    def test_another_bindings_field_is_a_binding_token(self):
        hits = find_in_template("{{note.Word}}", WORD)
        assert [(hit.kind, hit.replacement) for hit in hits] == [(BINDING_TOKEN, "{{note.Term}}")]

    def test_a_longer_name_is_not_a_hit(self):
        assert find_in_template("{{trigger.Words}} {{trigger.Word2}} Word", WORD) == []

    def test_a_bare_reference_is_not_a_field(self):
        assert find_in_template("{{Word}}", WORD) == []

    def test_a_cloze_marker_is_not_a_reference_but_its_content_is_read(self):
        text = "{{c1::trigger.Word}} {{c2::{{trigger.Word}}}}"
        hits = find_in_template(text, WORD)
        assert [hit.start for hit in hits] == [text.index("{{trigger")]
        assert apply(text, hits) == "{{c1::trigger.Word}} {{c2::{{trigger.Term}}}}"

    def test_a_card_value_token_names_its_card_type_exactly(self):
        text = "{{trigger.Recognition__Card_Due}} {{trigger.recognition__Card_Due}}"
        hits = find_in_template(text, RECOGNITION)
        assert spelled(text, hits) == ["{{trigger.Recognition__Card_Due}}"]
        assert hits[0].replacement == "{{trigger.Reading Card__Card_Due}}"

    def test_a_card_value_with_an_argument_keeps_it(self):
        hits = find_in_template("{{q.Recognition__Card_Last_Reps==5}}", RECOGNITION)
        assert [(hit.kind, hit.replacement) for hit in hits] == [
            (BINDING_TOKEN, "{{q.Reading Card__Card_Last_Reps==5}}")
        ]

    def test_an_unknown_card_value_key_is_not_a_card_type(self):
        assert find_in_template("{{trigger.Recognition__Nonsense}}", RECOGNITION) == []

    def test_a_deleted_field_is_found_but_not_replaceable(self):
        hits = find_in_template("{{trigger.Word}}", Rename(KIND_FIELD, "Word"))
        assert [(hit.kind, hit.replaceable) for hit in hits] == [(TRIGGER_TOKEN, False)]

    def test_a_deck_or_note_type_is_never_spelled_in_a_reference(self):
        assert find_in_template("{{JP Vocab.x}} {{trigger.JP Vocab}}", JP) == []
        assert find_in_template("{{trigger.CA Vocab}}", VOCAB_NOTE) == []


class TestSearch:
    @pytest.mark.parametrize(
        "query, rename, written",
        [
            ('deck:"JP Vocab"', JP, 'deck:"JP Vocab"'),
            ('"deck:jp vocab"', JP, '"deck:jp vocab"'),
            ("Deck:JP\\ Vocab", JP, None),  # `\ ` is no escape Anki knows, not our business
            ('note:"CA Vocab"', VOCAB_NOTE, 'note:"CA Vocab"'),
            ("card:recognition", RECOGNITION, "card:recognition"),
            ("word:neko", WORD, "word"),
            ("Word:", WORD, "Word"),
            ("Word:re:ne.o", WORD, "Word"),
            ("Word:_*", WORD, "Word"),
        ],
    )
    def test_each_term_kind_is_found(self, query, rename, written):
        hits = find_in_search(query, rename)
        if written is None:
            assert [hit.kind for hit in hits] == [DECK_TERM]
        else:
            assert spelled(query, hits) == [written]

    def test_the_hit_kinds(self):
        query = 'deck:"JP Vocab" "note:CA Vocab" card:Recognition Word:x'
        assert [hit.kind for hit in find_in_search(query, JP)] == [DECK_TERM]
        assert [hit.kind for hit in find_in_search(query, VOCAB_NOTE)] == [NOTE_TERM]
        assert [hit.kind for hit in find_in_search(query, RECOGNITION)] == [CARD_TERM]
        assert [hit.kind for hit in find_in_search(query, WORD)] == [FIELD_TERM]

    @pytest.mark.parametrize(
        "query",
        [
            "deck:JP*",
            "deck:JP_Vocab",
            'deck:"JP Vocab::Sub"',
            'deck:"JP Vocabulary"',
            "deck:{{trigger.__Deck}}",
            'tag:"JP Vocab"',
            '"JP Vocab"',
            r"deck\:JP",
            "deck:current",
        ],
    )
    def test_not_a_deck_hit(self, query):
        assert find_in_search(query, JP) == []

    def test_a_pseudo_deck_name_is_not_the_deck_called_so(self):
        assert find_in_search("deck:current", Rename(KIND_DECK, "Current", "Now")) == []

    @pytest.mark.parametrize(
        "query",
        ["Words:x", "W*rd:x", "tag:Word", "re:Word", "Word", r"Word\:x", '"-Word:x"x', "nc:x"],
    )
    def test_not_a_field_hit(self, query):
        assert find_in_search(query, WORD) == []

    def test_a_card_ordinal_is_not_a_name(self):
        assert find_in_search("card:1", Rename(KIND_CARD_TYPE, "1", "One")) == []

    def test_an_escaped_name_is_found_and_matched_unescaped(self):
        rename = Rename(KIND_DECK, 'a"b*c_d', "e")
        query = r'deck:a\"b\*c\_d'
        assert spelled(query, find_in_search(query, rename)) == [query]

    def test_a_negated_term_keeps_its_minus(self):
        query = '-deck:"JP Vocab" x'
        assert apply(query, find_in_search(query, JP)) == '-"deck:JP Words" x'

    def test_a_minus_inside_quotes_is_a_field_search_not_a_negation(self):
        assert find_in_search('"-deck:JP Vocab"', JP) == []

    def test_terms_inside_groups_and_or(self):
        query = "(deck:Other OR deck:JP\\_x) and -(deck:JP_x)"
        rename = Rename(KIND_DECK, "JP_x", "JP y")
        assert apply(query, find_in_search(query, rename)) == (
            '(deck:Other OR "deck:JP y") and -(deck:JP_x)'
        )

    @pytest.mark.parametrize(
        "rename, expected",
        [
            (Rename(KIND_DECK, "JP Vocab", "JP"), "deck:JP"),
            (Rename(KIND_DECK, "JP Vocab", "JP Words"), '"deck:JP Words"'),
            (Rename(KIND_DECK, "JP Vocab", 'a"b*c_d:e'), r'deck:a\"b\*c\_d:e'),
            (Rename(KIND_DECK, "JP Vocab", "x(y)"), '"deck:x(y)"'),
            (Rename(KIND_DECK, "JP Vocab", "Parent::Child"), "deck:Parent::Child"),
            (Rename(KIND_DECK, "JP Vocab", "back\\slash"), r"deck:back\\slash"),
            (Rename(KIND_NOTE_TYPE, "JP Vocab", "CA Words"), '"note:CA Words"'),
            (Rename(KIND_CARD_TYPE, "JP Vocab", "Card_2"), r"card:Card\_2"),
            (Rename(KIND_CARD_TYPE, "JP Vocab", "Card 2"), '"card:Card 2"'),
        ],
    )
    def test_object_term_replacements(self, rename, expected):
        query = {KIND_DECK: "deck", KIND_NOTE_TYPE: "note", KIND_CARD_TYPE: "card"}[rename.kind]
        query += ':"JP Vocab"'
        assert apply(query, find_in_search(query, rename)) == expected

    @pytest.mark.parametrize(
        "query, new, expected",
        [
            ("Word:neko", "Term", "Term:neko"),
            ("-word:neko", "Term", "-Term:neko"),
            ('Word:"a b"', "Term", 'Term:"a b"'),
            ('Word:"a b"', "My Term", '"My Term:a b"'),
            ('"Word:a b"', "My Term", '"My Term:a b"'),
            ("Word:neko", "My Term", '"My Term:neko"'),
            ("-Word:neko", "My Term", '-"My Term:neko"'),
            ("Word:re:n.ko", "My_Term", r"My\_Term:re:n.ko"),
            (r"Word:a\*b\"c", "Term", r"Term:a\*b\"c"),
            ("Word:_*", "-Term", '"-Term:_*"'),
        ],
    )
    def test_a_field_term_keeps_its_value_as_written(self, query, new, expected):
        assert apply(query, find_in_search(query, Rename(KIND_FIELD, "Word", new))) == expected

    def test_a_field_term_holding_the_token_gives_two_hits_that_do_not_overlap(self):
        query = "Word:{{trigger.Word}}"
        hits = find_in_query(query, WORD)
        assert [(hit.kind, query[hit.start : hit.end]) for hit in hits] == [
            (FIELD_TERM, "Word"),
            (TRIGGER_TOKEN, "{{trigger.Word}}"),
        ]
        assert apply(query, hits) == "Term:{{trigger.Term}}"

    def test_a_deletion_is_found_and_not_replaceable(self):
        hits = find_in_search("deck:x Word:y", Rename(KIND_FIELD, "Word"))
        assert [(hit.kind, hit.replaceable) for hit in hits] == [(FIELD_TERM, False)]

    def test_the_writer_rules_match_ankis_own(self, col):
        # `object_term` is written out by hand because `card:` has no builder; the other two
        # kinds must come out exactly as Anki builds them, or the rules have drifted.
        for name in [
            "JP Vocab",
            'a"b*c_d:e',
            "x(y)",
            "-lead",
            "P::C",
            "back\\slash",
            "or",
            "AND",
            "ÄÖ vocab",
            'q"uote space',
            "a\u3000b",
            "x\\*",
        ]:
            assert object_term(KIND_DECK, name) == col.build_search_string(SearchNode(deck=name))
            assert object_term(KIND_NOTE_TYPE, name) == col.build_search_string(
                SearchNode(note=name)
            )


class TestSearchRoundTrips:
    """After the rename, the replaced search finds what the original found before it."""

    @staticmethod
    def add_note(col, note_type_name=VOCAB, deck_name="Default", **fields):
        note = col.new_note(col.models.by_name(note_type_name))
        for name, value in fields.items():
            note[name] = value
        col.add_note(note, col.decks.id(deck_name))
        return note.id

    @staticmethod
    def check(col, query, rename, rename_in_collection, find=None):
        find = find or col.find_notes
        before = list(find(query))
        assert before
        rename_in_collection()
        assert list(find(query)) != before
        replaced = apply(query, find_in_search(query, rename))
        assert replaced != query
        assert list(find(replaced)) == before

    @pytest.mark.parametrize(
        "old, new",
        [
            ("JP Vocab", "JP Words"),
            ("Old", 'we"ird*na_me'),
            ("Old", "x(y) z"),
            ("Old", "back\\slash"),
            ("Old", "or"),
            ("Old", "-dash"),
        ],
    )
    def test_deck(self, col, old, new):
        self.add_note(col, deck_name=old, Word="neko")
        self.add_note(col, deck_name="Other", Word="inu")
        query = f'"note:{VOCAB}" {object_term(KIND_DECK, old)}'
        deck = col.decks.by_name(old)
        self.check(col, query, Rename(KIND_DECK, old, new), lambda: col.decks.rename(deck, new))

    def test_a_negated_deck(self, col):
        self.add_note(col, deck_name="Keep", Word="a")
        self.add_note(col, deck_name="JP Vocab", Word="b")
        query = f'"note:{VOCAB}" -deck:"JP Vocab"'
        deck = col.decks.by_name("JP Vocab")
        self.check(col, query, JP, lambda: col.decks.rename(deck, "JP Words"))

    def test_a_child_deck_through_its_own_rename(self, col):
        # Renaming the parent renames the child's id too; the child's term follows that.
        self.add_note(col, deck_name="Top::Child", Word="a")
        self.add_note(col, deck_name="Top", Word="b")
        query = "deck:Top::Child"
        top = col.decks.by_name("Top")
        rename = Rename(KIND_DECK, "Top::Child", "New Top::Child")
        self.check(col, query, rename, lambda: col.decks.rename(top, "New Top"))

    # Anki drops a double quote from a note type or card type name, so none has one here.
    @pytest.mark.parametrize("new", ["CA Words", "n*o_t:e", "a(b)", "back\\slash"])
    def test_note_type(self, col, new):
        self.add_note(col, Word="neko")
        query = f'"note:{VOCAB}"'

        def rename_it():
            model = col.models.by_name(VOCAB)
            model["name"] = new
            col.models.update_dict(model)

        self.check(col, query, Rename(KIND_NOTE_TYPE, VOCAB, new), rename_it)

    @pytest.mark.parametrize("new", ["Reading Card", "Card_2", "C*rd", "x(y)", "back\\slash"])
    def test_card_type(self, col, new):
        self.add_note(col, Word="neko")
        query = "card:recognition"

        def rename_it():
            model = col.models.by_name(VOCAB)
            model["tmpls"][0]["name"] = new
            col.models.update_dict(model)

        rename = Rename(KIND_CARD_TYPE, "Recognition", new)
        self.check(col, query, rename, rename_it, find=col.find_cards)

    @pytest.mark.parametrize(
        "query, new",
        [
            ("Word:neko", "Term"),
            ("Word:neko", "My Term"),
            ("-Word:inu Word:_*", "My_Term"),
            ('"word:ne ko"', "Te*rm"),
            ("Word:re:^ne", "My Term"),
            ("(Word:neko or Word:inu)", "-Lead"),
        ],
    )
    def test_field(self, col, query, new):
        self.add_note(col, Word="neko")
        self.add_note(col, Word="ne ko")
        self.add_note(col, Word="inu")

        def rename_it():
            model = col.models.by_name(VOCAB)
            col.models.rename_field(model, model["flds"][0], new)
            col.models.update_dict(model)

        self.check(col, f'"note:{VOCAB}" {query}', Rename(KIND_FIELD, "Word", new), rename_it)


class TestCode:
    def test_a_literal_naming_the_field_is_found_in_any_case_and_replaced(self):
        code = "value = note['word']\n"
        hits = find_in_code(code, WORD)
        assert spelled(code, hits) == ["'word'"]
        assert hits[0].kind == CODE_LITERAL
        assert apply(code, hits) == "value = note['Term']\n"

    def test_a_comment_an_identifier_and_a_longer_name_are_not_hits(self):
        code = "# note['Word']\nWord = 1\nx = 'Words' + \"Word list\"\n"
        assert find_in_code(code, WORD) == []

    def test_a_card_type_literal_matches_exactly(self):
        assert find_in_code("x = 'recognition'", RECOGNITION) == []
        assert len(find_in_code("x = 'Recognition'", RECOGNITION)) == 1

    def test_a_deck_literal_matches_in_any_case(self):
        assert len(find_in_code("col.decks.by_name('jp vocab')", JP)) == 1

    def test_offsets_across_lines(self):
        code = "a = 1\n\nb = f(\n    \"Word\",\n)\n"
        assert spelled(code, find_in_code(code, WORD)) == ['"Word"']

    def test_a_literal_holding_a_search_gets_the_search_rule_inside(self):
        code = "ids = find_notes('deck:\"JP Vocab\" Word:x')"
        hits = find_in_code(code, JP)
        assert apply(code, hits) == "ids = find_notes('\"deck:JP Words\" Word:x')"

    @pytest.mark.parametrize(
        "code, new, expected",
        [
            ('"Word"', 'Te"rm', r'"Te\"rm"'),
            ("'Word'", 'Te"rm', "'Te\"rm'"),
            ("'Word'", "Te'rm", r"'Te\'rm'"),
            ("'Word'", "Te\\rm", r"'Te\\rm'"),
            ('"""Word"""', "Term", '"""Term"""'),
            ("r'Word'", "Te\\rm", r"r'Te\rm'"),
            ("u'Word'", "Term", "u'Term'"),
            ("R\"Word\"", "Term", 'R"Term"'),
        ],
    )
    def test_the_same_literal_with_the_new_name(self, code, new, expected):
        assert apply(code, find_in_code(code, Rename(KIND_FIELD, "Word", new))) == expected

    @pytest.mark.parametrize("code, new", [("r'Word'", "Te'rm"), ("r'Word'", "Term\\")])
    def test_a_raw_string_that_cannot_hold_the_new_name_is_not_replaceable(self, code, new):
        hits = find_in_code(code, Rename(KIND_FIELD, "Word", new))
        assert [hit.replaceable for hit in hits] == [False]

    def test_bytes_are_not_names(self):
        assert find_in_code("x = b'Word'", WORD) == []

    @pytest.mark.parametrize(
        "code",
        [
            'x = f"{note[\'Word\']}!"',
            'x = f"Word: {value}"',
            'x = f"{a} deck:\\"JP Vocab\\" {b}"',
            "x = F'{n}Word'",
        ],
    )
    def test_an_f_string_is_found_whole_and_never_replaced(self, code):
        rename = JP if "deck" in code else WORD
        hits = find_in_code(code, rename)
        assert spelled(code, hits) == [code[4:]]
        assert [hit.replaceable for hit in hits] == [False]

    def test_an_f_string_interpolating_a_name_holding_it_is_not_a_hit(self):
        assert find_in_code('x = f"{Word} {Words}"', WORD) == []

    @pytest.mark.parametrize(
        "code", ["x = 'Word", "x = ('Word'", "x = '''Word", "if x:\n  y = 'Word'\n z = 1\n"]
    )
    def test_code_that_does_not_tokenize_finds_nothing(self, code):
        assert find_in_code(code, WORD) == []
        # ... and says it could not read it, so "nothing found" is not taken for "gone".
        assert code_is_readable(code) is False

    @pytest.mark.parametrize("code", ["x = 'Term'", "", None, "# just a comment"])
    def test_code_that_tokenizes_is_readable(self, code):
        assert code_is_readable(code) is True

    def test_a_deletion_is_found_and_not_replaceable(self):
        hits = find_in_code("note['Word']", Rename(KIND_FIELD, "Word"))
        assert [hit.replaceable for hit in hits] == [False]


class TestSlotsAndBlocking:
    def test_a_slot_matches_a_field_in_any_case(self):
        assert find_in_slot("word", WORD) == [Hit(0, 4, TRIGGER_SLOT, "Term")]
        assert find_in_slot("Word", WORD, OTHER_SLOT)[0].kind == OTHER_SLOT
        assert find_in_slot("Words", WORD) == []
        assert find_in_slot("Word", RECOGNITION) == []

    @pytest.mark.parametrize(
        "kind, rename, multi, blocks",
        [
            (CODE_LITERAL, WORD, False, True),
            (DECK_TERM, JP, False, True),
            (NOTE_TERM, VOCAB_NOTE, False, True),
            (CARD_TERM, RECOGNITION, True, False),
            (FIELD_TERM, WORD, True, False),
            (BINDING_TOKEN, WORD, True, False),
            (OTHER_SLOT, WORD, True, False),
            (TRIGGER_TOKEN, WORD, True, True),
            (TRIGGER_SLOT, WORD, True, True),
            (TRIGGER_TOKEN, WORD, False, False),
            # A deletion blocks where the name can only mean the deleted object: the
            # trigger note type's own slots and tokens, and code.
            (TRIGGER_TOKEN, Rename(KIND_FIELD, "Word"), False, True),
            (TRIGGER_SLOT, Rename(KIND_FIELD, "Word"), False, True),
            (TRIGGER_TOKEN, Rename(KIND_CARD_TYPE, "Recognition"), False, True),
            (CODE_LITERAL, Rename(KIND_FIELD, "Word"), False, True),
            (DECK_TERM, Rename(KIND_DECK, "JP Vocab"), False, True),
            (NOTE_TERM, Rename(KIND_NOTE_TYPE, "CA Vocab"), False, True),
            # Elsewhere another note type may still have the name, as for a rename.
            (FIELD_TERM, Rename(KIND_FIELD, "Word"), False, False),
            (CARD_TERM, Rename(KIND_CARD_TYPE, "Recognition"), False, False),
            (BINDING_TOKEN, Rename(KIND_CARD_TYPE, "Recognition"), False, False),
            (BINDING_TOKEN, Rename(KIND_FIELD, "Word"), True, False),
            (OTHER_SLOT, Rename(KIND_FIELD, "Word"), False, False),
        ],
    )
    def test_what_blocks(self, kind, rename, multi, blocks):
        # In a definition that triggers on the renamed object's note type.
        assert hit_blocks_run(kind, rename, multi, True) is blocks

    @pytest.mark.parametrize(
        "kind, rename, blocks",
        [
            # Code naming a field or card type of a note type the definition does not
            # trigger on is a guess: another note type may have the name.
            (CODE_LITERAL, WORD, False),
            (CODE_LITERAL, Rename(KIND_FIELD, "Word"), False),
            (CODE_LITERAL, RECOGNITION, False),
            # A deck and a note type have unique names: code naming one is that object.
            (CODE_LITERAL, JP, True),
            (CODE_LITERAL, Rename(KIND_DECK, "JP Vocab"), True),
            (CODE_LITERAL, VOCAB_NOTE, True),
            (DECK_TERM, JP, True),
            (DECK_SLOT, JP, True),
            (DECK_SLOT, Rename(KIND_DECK, "JP Vocab"), True),
            (FIELD_TERM, WORD, False),
            (BINDING_TOKEN, WORD, False),
        ],
    )
    def test_what_blocks_in_a_definition_not_on_the_note_type(self, kind, rename, blocks):
        assert hit_blocks_run(kind, rename, False, False) is blocks

    @pytest.mark.parametrize("value", ["JP Vocab", "jp vocab"])
    def test_a_deck_slot_matches_the_deck_in_any_case(self, value):
        assert find_in_deck_slot(value, JP) == [Hit(0, len(value), DECK_SLOT, "JP Words")]

    @pytest.mark.parametrize("value", [None, "-", "", 0, 12345, "JP Vocab::Sub", "Other"])
    def test_what_a_deck_slot_holds_for_no_move_or_an_id_is_not_a_hit(self, value):
        assert find_in_deck_slot(value, JP) == []

    def test_a_deck_slot_is_only_about_decks(self):
        assert find_in_deck_slot("Word", WORD) == []
        assert find_in_deck_slot("CA Vocab", VOCAB_NOTE) == []

    def test_a_deleted_deck_in_a_slot_has_no_replacement(self):
        hits = find_in_deck_slot("JP Vocab", Rename(KIND_DECK, "JP Vocab"))
        assert [hit.replaceable for hit in hits] == [False]

    def test_a_rename_from_a_stored_entry(self):
        entry = {"kind": KIND_DECK, "old": "JP", "new": "JP2", "object_id": 1}
        assert Rename.from_entry(entry) == Rename(KIND_DECK, "JP", "JP2")
        assert Rename.from_entry({**entry, "new": None}) == Rename(KIND_DECK, "JP")
        assert Rename.from_entry({**entry, "kind": "tag"}) is None
        assert Rename.from_entry("junk") is None


class TestApply:
    def test_spans_of_the_replacements_in_the_result(self):
        text = "{{trigger.Word}} and {{note.Word}}"
        result, spans = apply_with_spans(text, find_in_template(text, WORD))
        assert [result[s:e] for s, e in spans] == ["{{trigger.Term}}", "{{note.Term}}"]

    def test_unreplaceable_and_overlapping_hits_are_left_alone(self):
        hits = [Hit(0, 3, FIELD_TERM, "X"), Hit(1, 2, FIELD_TERM, "Y"), Hit(4, 5, FIELD_TERM)]
        assert apply("abc d", hits) == "X d"
