"""What the applier must refuse, and what it must leave alone when it writes.

This script rewrites whole fields - a word array is written back from the parsed structure, not
patched in place - so the risk is not that an edit fails but that it reaches further than it was
asked to. Each test here pins one edge of that: which elements an operation may touch, what a
stale value does, and that a note is written whole or not at all.

Nothing here talks to Anki. The furigana redraw is the only path that would, and the refusal it
guards is checked before it runs.
"""

import json
import sys
from pathlib import Path

import pytest

# See the note in test_vocab_morphology.py: the suite already has a `conftest` of its own, so
# the path is set here rather than in one more file by that name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vocab_apply as va  # noqa: E402

ARRAY = va.ARRAY_FIELD
SENTENCE = va.SENTENCE_FIELD


def element(raw, pos, dict_form, reading, match, subs=None):
    return [raw, pos, dict_form, reading, match, subs or []]


def fields(array=None, sentence="", **rest):
    out = dict(rest)
    out[ARRAY] = json.dumps(array if array is not None else [], ensure_ascii=False)
    out[SENTENCE] = sentence
    return out


def array_of(note_fields):
    return va.match_flags.decode_word_array(note_fields[ARRAY])


def readings(note_fields):
    return [e[3] for _, e in va.match_flags.iter_words(array_of(note_fields))]


# --- an operation reaches only as far as it names ------------------------------------------


def test_set_element_reading_touches_only_the_named_reading():
    """Two elements share a dict_form and only one carries the reading being repaired."""
    note = fields([
        element("凝りました", "verb", "凝る", "こごる", ["match"]),
        element("凝った", "verb", "凝る", "こる", ["match"]),
        element("肩", "noun", "肩", "かた", ["match"]),
    ])
    problem = va.apply_to_fields(
        {"op": "set_element_reading", "sentence": 1, "dict_form": "凝る",
         "from": "こごる", "to": "こる"},
        note,
    )
    assert problem == ""
    assert readings(note) == ["こる", "こる", "かた"]


def test_set_element_reading_changes_every_element_that_matches():
    """A sentence saying the word twice is wrong twice, so both move."""
    note = fields([
        element("％", "counter", "%", "きごう", ["dontmatch"]),
        element("％", "counter", "%", "きごう", ["dontmatch"]),
    ])
    assert va.apply_to_fields(
        {"op": "set_element_reading", "sentence": 1, "dict_form": "%",
         "from": "きごう", "to": "ぱーせんと"},
        note,
    ) == ""
    assert readings(note) == ["ぱーせんと", "ぱーせんと"]


def test_set_element_reading_refuses_a_reading_no_element_holds():
    note = fields([element("凝った", "verb", "凝る", "こる", ["match"])])
    before = note[ARRAY]
    problem = va.apply_to_fields(
        {"op": "set_element_reading", "sentence": 1, "dict_form": "凝る",
         "from": "こごる", "to": "こる"},
        note,
    )
    assert "no 凝る element reads" in problem
    assert note[ARRAY] == before  # refused, and nothing written


def test_a_sub_word_is_reachable_and_its_siblings_are_not():
    """Elements nest, and the repairs the plans ask for are often one level down."""
    note = fields([
        element("肩が凝りました", "expression", "肩が凝る", "かたがこごる", ["match"], [
            element("肩", "noun", "肩", "かた", ["match"]),
            element("凝りました", "verb", "凝る", "こごる", ["match"]),
        ]),
    ])
    assert va.apply_to_fields(
        {"op": "set_element_reading", "sentence": 1, "dict_form": "凝る",
         "from": "こごる", "to": "こる"},
        note,
    ) == ""
    assert readings(note) == ["かたがこごる", "かた", "こる"]


def test_repoint_changes_only_the_link():
    note = fields([element("件", "noun", "件", "けん", [111])])
    assert va.apply_to_fields(
        {"op": "repoint_element", "sentence": 1, "dict_form": "件",
         "from_nid": 111, "to_nid": 222},
        note,
    ) == ""
    word = array_of(note)[0]
    assert word[4] == [222]
    assert word[3] == "けん"  # the reading is not the link's business


def test_repoint_refuses_when_the_element_links_someone_else():
    note = fields([element("件", "noun", "件", "けん", [999])])
    problem = va.apply_to_fields(
        {"op": "repoint_element", "sentence": 1, "dict_form": "件",
         "from_nid": 111, "to_nid": 222},
        note,
    )
    assert "no 件 element links 111" in problem


@pytest.mark.parametrize("state, expected", [("match", ["match"]), ("unjudged", [])])
def test_unlink_sets_the_state(state, expected):
    note = fields([element("仇", "noun", "仇", "かたき", [111])])
    assert va.apply_to_fields(
        {"op": "unlink_element", "sentence": 1, "dict_form": "仇", "state": state}, note
    ) == ""
    assert array_of(note)[0][4] == expected


# --- the sentence field carries markup through its furigana --------------------------------


def test_sentence_furigana_needs_a_literal_match():
    """`<b>` sits between a group and its okurigana, so contiguous-looking text is not.

    Checking the cleaned text instead would accept this and the replacement would then do
    nothing at all, leaving a note the change list believes was repaired.
    """
    note = fields(sentence="少[すこ]し<b> 滑[ぬめり]</b>りも 有[あ]る")
    problem = va.apply_to_fields(
        {"op": "set_sentence_furigana", "sentence": 1, "from": "滑[ぬめり] り", "to": "滑[ぬめ] り"},
        note,
    )
    assert "does not contain" in problem
    assert va.apply_to_fields(
        {"op": "set_sentence_furigana", "sentence": 1, "from": "滑[ぬめり]", "to": "滑[ぬめ]"},
        note,
    ) == ""
    assert note[SENTENCE] == "少[すこ]し<b> 滑[ぬめ]</b>りも 有[あ]る"


def test_sentence_furigana_may_span_a_tag():
    """Where the run really does cross a tag, the tag goes in the operation."""
    note = fields(sentence="何[なん]<k> 箇[カ]</k> 所[かしょ]か")
    assert va.apply_to_fields(
        {"op": "set_sentence_furigana", "sentence": 1,
         "from": "<k> 箇[カ]</k> 所[かしょ]", "to": " 箇所[かしょ]"},
        note,
    ) == ""
    assert note[SENTENCE] == "何[なん] 箇所[かしょ]か"


# --- a stale value stops the write ---------------------------------------------------------


def test_a_reading_that_has_moved_on_refuses_before_anything_is_drawn():
    note = fields(**{va.READING_FIELD: "ひとばん", va.KANJIFIED_FIELD: "一晩"})
    problem = va.apply_to_fields(
        {"op": "set_note_reading", "nid": 1, "from": "いちばん", "to": "ひとばん"}, note
    )
    assert "vocab-kana holds 'ひとばん', not 'いちばん'" in problem


def test_a_note_field_is_compared_as_text_not_as_markup():
    """Field text carries markup and stray spacing that no plan should have to reproduce."""
    note = fields(**{va.FURIGANA_FIELD: "狡[ずる]&nbsp; 賢[かしこ]い"})
    problem = va.apply_to_fields(
        {"op": "set_note_field", "nid": 1, "field": va.FURIGANA_FIELD,
         "from": "狡[ずる] 賢[かしこ]い", "to": "狡[ずる] 賢[がしこ]い"},
        note,
    )
    assert problem == ""


# --- a note is written whole or not at all -------------------------------------------------


def test_one_stale_operation_refuses_the_whole_note():
    """Half of a plan applied is a state no plan asked for."""
    rows = [{
        "note_id": 7,
        "key": "test",
        "ops": [
            {"op": "set_element_reading", "sentence": 5, "dict_form": "凝る",
             "from": "こごる", "to": "こる"},
            {"op": "set_element_reading", "sentence": 5, "dict_form": "肩が凝る",
             "from": "stale", "to": "かたがこる"},
        ],
    }]
    live = {5: fields([
        element("凝りました", "verb", "凝る", "こごる", ["match"]),
        element("肩が凝りました", "expression", "肩が凝る", "かたがこごる", ["match"]),
    ])}
    writes, refused, _ = va.plan_writes(rows, live)
    assert writes == []
    assert len(refused) == 1 and "stale" in refused[0][1]


def test_operations_on_one_note_compose_into_a_single_write():
    rows = [{
        "note_id": 7,
        "key": "test",
        "ops": [
            {"op": "set_element_reading", "sentence": 5, "dict_form": "凝る",
             "from": "こごる", "to": "こる"},
            {"op": "set_sentence_furigana", "sentence": 5, "from": "凝[こご]", "to": "凝[こ]"},
        ],
    }]
    live = {5: fields([element("凝りました", "verb", "凝る", "こごる", ["match"])],
                      sentence="肩が 凝[こご]りました")}
    writes, refused, _ = va.plan_writes(rows, live)
    assert refused == []
    assert len(writes) == 1
    assert sorted(writes[0].after) == sorted([ARRAY, SENTENCE])
    assert len(writes[0].why) == 2


def test_an_operation_that_changes_nothing_produces_no_write():
    """The element already reads that way, so there is nothing to write and no undo entry."""
    rows = [{
        "note_id": 7,
        "key": "test",
        "ops": [{"op": "set_element_reading", "sentence": 5, "dict_form": "貫く",
                 "from": "ぬく", "to": "ぬく"}],
    }]
    live = {5: fields([element("貫いて", "verb", "貫く", "ぬく", ["match"])])}
    writes, refused, _ = va.plan_writes(rows, live)
    assert writes == [] and refused == []


def test_unwritable_operations_are_listed_and_never_written():
    rows = [{
        "note_id": 7,
        "key": "test",
        "ops": [
            {"op": "merge_notes", "keep": 1, "drop": 2},
            {"op": "none", "why": "nothing to do"},
        ],
    }]
    writes, refused, skipped = va.plan_writes(rows, {})
    assert writes == [] and refused == []
    assert sorted(skipped) == ["merge_notes", "none"]


def test_only_filters_to_one_kind():
    rows = [{
        "note_id": 7,
        "key": "test",
        "ops": [
            {"op": "set_element_reading", "sentence": 5, "dict_form": "凝る",
             "from": "こごる", "to": "こる"},
            {"op": "set_sentence_furigana", "sentence": 5, "from": "凝[こご]", "to": "凝[こ]"},
        ],
    }]
    live = {5: fields([element("凝りました", "verb", "凝る", "こごる", ["match"])],
                      sentence="肩が 凝[こご]りました")}
    writes, _, _ = va.plan_writes(rows, live, only="set_sentence_furigana")
    assert len(writes) == 1
    assert list(writes[0].after) == [SENTENCE]
