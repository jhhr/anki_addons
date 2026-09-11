"""Characterization tests for the card half of `interpolate_from_text`.

Card values are the part of interpolation that needs a real collection rather than a note
object: several of them read `revlog` directly, the defaults depend on which templates
actually produced a card, and the multi-note-type path raises rather than guessing. All of
that is invisible to a stub.
"""

import pytest

from anki_shared.testing import real_anki
from anki_shared.interpolate.interpolate_fields import interpolate_from_text
from conftest import CLOZE, KANJI, ODD_TEMPLATE, SENTENCE, VOCAB


@pytest.fixture
def note(col):
    return real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})


@pytest.fixture
def recognition_card(col, note):
    """The card of the note's first template, which the tests below read values off."""
    return next(card for card in note.cards() if card.template()["name"] == "Recognition")


class TestCardValues:
    def test_card_id(self, note, recognition_card):
        assert interpolate_from_text("{{Recognition__Card_ID}}", note) == (
            str(recognition_card.id),
            [],
        )

    def test_card_nid(self, note):
        assert interpolate_from_text("{{Recognition__Card_NID}}", note) == (str(note.id), [])

    def test_other_card_ids_excludes_the_card_itself(self, note, recognition_card):
        recall = next(c for c in note.cards() if c.template()["name"] == "Recall")
        assert interpolate_from_text("{{Recognition__Other_Card_IDs}}", note) == (
            str([recall.id]),
            [],
        )

    def test_card_due_and_interval_of_a_new_card(self, note):
        text, invalid = interpolate_from_text(
            "{{Recognition__Card_Due}}|{{Recognition__Card_Interval}}", note
        )
        assert invalid == []
        due, ivl = text.split("|")
        assert due.isdigit()  # a new card's due is its position in the new queue
        assert ivl == "0"

    def test_card_type_is_a_string_not_an_int(self, note):
        assert interpolate_from_text("{{Recognition__Card_Type}}", note) == ("New", [])

    def test_card_ease_is_the_factor_over_ten(self, col, note, recognition_card):
        recognition_card.factor = 2500
        col.update_card(recognition_card)
        assert interpolate_from_text("{{Recognition__Card_Ease}}", note) == ("250.0", [])

    def test_card_ease_falls_back_to_zero_when_the_factor_is_zero(self, note, recognition_card):
        # A new card's factor is 0, and `card.factor / 10 or 0` turns the resulting 0.0 into
        # a plain 0, so the rendered value is "0" rather than "0.0".
        assert recognition_card.factor == 0
        assert interpolate_from_text("{{Recognition__Card_Ease}}", note) == ("0", [])

    def test_stability_and_difficulty_are_zero_with_fsrs_off(self, note, recognition_card):
        # memory_state is None without FSRS, and both values short-circuit to 0 rather than
        # raising or rendering "None".
        assert recognition_card.memory_state is None
        assert interpolate_from_text(
            "{{Recognition__Card_Stability}}/{{Recognition__Card_Difficulty}}", note
        ) == ("0/0", [])

    def test_rep_and_lapse_counts_of_an_unreviewed_card(self, note):
        assert interpolate_from_text(
            "{{Recognition__Card_Rep_Count}}/{{Recognition__Card_Lapse_Count}}", note
        ) == ("0/0", [])

    def test_review_times_of_an_unreviewed_card_are_dashes(self, note):
        assert interpolate_from_text(
            "{{Recognition__Card_First_Review}}|{{Recognition__Card_Latest_Review}}"
            "|{{Recognition__Card_Average_Time}}|{{Recognition__Card_Total_Time}}",
            note,
        ) == ("-|-|-|-", [])

    def test_custom_data_of_a_card_that_has_none(self, note):
        assert interpolate_from_text("{{Recognition__Card_Custom_Data}}", note) == ("{}", [])


class TestCustomDataProp:
    def test_a_present_property_is_returned(self, col, note, recognition_card):
        real_anki.set_custom_data(col, recognition_card.id, '{"dr": 0.85}')
        assert interpolate_from_text("{{Recognition__Card_Custom_Data_Prop==dr}}", note) == (
            "0.85",
            [],
        )

    def test_a_missing_property_is_empty(self, col, note, recognition_card):
        real_anki.set_custom_data(col, recognition_card.id, '{"other": 1}')
        assert interpolate_from_text("{{Recognition__Card_Custom_Data_Prop==dr}}", note) == (
            "",
            [],
        )

    def test_empty_custom_data_is_empty(self, note):
        assert interpolate_from_text("{{Recognition__Card_Custom_Data_Prop==dr}}", note) == (
            "",
            [],
        )

    def test_the_prop_getter_is_a_partial_so_its_argument_reaches_it(self, note, col, recognition_card):
        # The contrast that matters: get_from_note_fields only passes the argument on to a
        # value it recognises as a functools.partial. Custom_Data_Prop has always been one;
        # __Note_Has_Tag was a plain nested def and rendered its own repr into the field
        # until it was made a partial too. Both are asserted so the pair cannot drift apart.
        real_anki.set_custom_data(col, recognition_card.id, '{"dr": 0.85}')
        note.add_tag("mytag")
        text, invalid = interpolate_from_text(
            "{{Recognition__Card_Custom_Data_Prop==dr}}/{{__Note_Has_Tag==mytag}}", note
        )
        assert text == "0.85/mytag"
        assert invalid == []
        assert "<function" not in text


class TestLastReps:
    """`__Card_Last_Reps` and friends read revlog directly, newest first, skipping the
    ease-0 rows Anki writes for a manual reschedule."""

    @pytest.fixture
    def reviewed(self, col, note, recognition_card):
        # Ascending ids, so "ORDER BY id DESC" returns 5, 4, 3, 2, 1.
        for i, ease in enumerate([1, 2, 3, 4, 1], start=1):
            real_anki.add_revlog(
                col, recognition_card.id, count=1, ease=ease, first_id=recognition_card.id + i
            )
        return recognition_card

    def test_a_count_returns_that_many_newest_first(self, note, reviewed):
        assert interpolate_from_text("{{Recognition__Card_Last_Reps==3}}", note) == (
            str([1, 4, 3]),
            [],
        )

    def test_all_returns_every_rep_but_oldest_first(self, note, reviewed):
        # "all" and a count disagree about order, and this is where that shows: the SQL only
        # appends "DESC LIMIT n" when a count was given, so "all" comes back ascending while
        # "==5" over the same five rows comes back descending. Pinned as-is; a definition
        # reading rep 0 means different things under the two spellings.
        assert interpolate_from_text("{{Recognition__Card_Last_Reps==all}}", note) == (
            str([1, 2, 3, 4, 1]),
            [],
        )
        assert interpolate_from_text("{{Recognition__Card_Last_Reps==5}}", note) == (
            str([1, 4, 3, 2, 1]),
            [],
        )

    def test_zero_returns_nothing(self, note, reviewed):
        assert interpolate_from_text("{{Recognition__Card_Last_Reps==0}}", note) == ("[]", [])

    def test_a_non_numeric_count_returns_nothing(self, note, reviewed):
        assert interpolate_from_text("{{Recognition__Card_Last_Reps==abc}}", note) == ("[]", [])

    def test_manual_reschedules_are_excluded(self, col, note, reviewed):
        # ease = 0 is how Anki records a manual reschedule; it is not a review and the query
        # filters it out rather than returning a 0 in the middle of the list.
        real_anki.add_revlog(col, reviewed.id, count=1, ease=0, first_id=reviewed.id + 99)
        assert interpolate_from_text("{{Recognition__Card_Last_Reps==all}}", note) == (
            str([1, 2, 3, 4, 1]),
            [],
        )

    def test_intervals_and_factors_come_from_their_own_columns(self, col, note, recognition_card):
        real_anki.add_revlog(
            col, recognition_card.id, count=1, ivl=21, factor=2350, first_id=recognition_card.id + 1
        )
        assert interpolate_from_text(
            "{{Recognition__Card_Last_Intervals==1}}|{{Recognition__Card_Last_Factors==1}}", note
        ) == ("[21]|[2350]", [])


class TestMissingCardTypes:
    def test_a_card_type_not_on_the_note_gets_a_type_appropriate_default(self, note):
        # Not an invalid field: CARD_VALUE_DEFAULTS answers for a template that produced no
        # card, so a definition spanning note types does not report every one of them.
        assert interpolate_from_text("{{Nonexistent__Card_Due}}", note) == ("0", [])
        assert interpolate_from_text("{{Nonexistent__Card_Created}}", note) == ("-", [])
        assert interpolate_from_text("{{Nonexistent__Card_Custom_Data}}", note) == ("{}", [])

    def test_a_new_standard_note_answers_from_the_fake_card_not_from_nothing(self, col):
        # Not "" -- the standard branch fills the dict with a fake `Card(mw.col)` for every
        # template that has no card yet, so a note being added still resolves card values.
        # The empty-dict path below is the only one that yields "".
        model = col.models.by_name(VOCAB)
        assert model is not None
        unadded = col.new_note(model)
        unadded["Word"] = "not added yet"
        assert interpolate_from_text("{{Recognition__Card_Due}}", unadded) == ("0", [])

    def test_a_new_cloze_note_with_no_cards_is_empty_and_not_invalid(self, col):
        # The cloze branch only walks the cards that exist, so a note with none leaves the
        # dict empty and every card value returns "" rather than being flagged invalid.
        model = col.models.by_name(CLOZE)
        assert model is not None
        unadded = col.new_note(model)
        unadded["Text"] = "no cloze deletions here"
        assert interpolate_from_text("{{Cloze 1__Card_Due}}", unadded) == ("", [])

    def test_a_conditional_template_with_no_card_uses_the_fake_card_defaults(self, col):
        # The second template renders nothing when its field is empty, so the note has one
        # card and the other template goes through the `Card(mw.col)` branch rather than
        # through CARD_VALUE_DEFAULTS.
        real_anki.make_note_type(
            col,
            "CA Conditional",
            ["Front", "Back"],
            [
                ("Always", "{{Front}}", "{{Back}}"),
                ("Sometimes", "{{#Back}}{{Front}}{{/Back}}", "{{Back}}"),
            ],
        )
        note = real_anki.add_note(col, "CA Conditional", {"Front": "f", "Back": ""})
        assert len(note.cards()) == 1
        assert interpolate_from_text("{{Sometimes__Card_ID}}", note) == ("0", [])
        assert interpolate_from_text("{{Sometimes__Card_Type}}", note) == ("New", [])


class TestClozeCardValues:
    def test_cloze_cards_key_by_ordinal(self, col):
        note = real_anki.add_note(
            col, CLOZE, {"Text": "{{c1::alpha}} and {{c2::beta}}", "Extra": ""}
        )
        cards = {card.ord: card for card in note.cards()}
        assert set(cards) == {0, 1}
        assert interpolate_from_text("{{Cloze 1__Card_ID}}", note) == (str(cards[0].id), [])
        assert interpolate_from_text("{{Cloze 2__Card_ID}}", note) == (str(cards[1].id), [])

    def test_an_ordinal_with_no_card_falls_back_to_the_defaults(self, col):
        note = real_anki.add_note(col, CLOZE, {"Text": "{{c1::alpha}}", "Extra": ""})
        assert interpolate_from_text("{{Cloze 2__Card_Due}}", note) == ("0", [])


class TestCardTypeNameParsing:
    def test_a_template_name_containing_a_double_underscore_still_splits(self, col):
        # CARD_VALUE_RE's greedy (.+) has to give back just enough for (__\w+) to match the
        # value key, which means the template name keeps its own double underscore.
        note = real_anki.add_note(col, ODD_TEMPLATE, {"Front": "f", "Back": "b"})
        card = note.cards()[0]
        assert card.template()["name"] == "Card__Front"
        assert interpolate_from_text("{{Card__Front__Card_ID}}", note) == (str(card.id), [])


class TestMultipleNoteTypes:
    def test_the_card_type_name_is_omitted_and_resolved(self, col):
        note = real_anki.add_note(col, SENTENCE, {"Sentence": "s", "Vocab": "v"})
        card = note.cards()[0]
        assert interpolate_from_text("{{__Card_ID}}", note, multiple_note_types=True) == (
            str(card.id),
            [],
        )

    def test_more_than_one_card_type_raises(self, col):
        # A definition spanning several note types assumes each has a single card type;
        # there is no way to pick between two, so this is a ValueError rather than a guess.
        # get_field_values_from_notes catches it and breaks out of the source-note loop.
        note = real_anki.add_note(col, VOCAB, {"Word": "w"})
        with pytest.raises(ValueError, match="single card type"):
            interpolate_from_text("{{__Card_ID}}", note, multiple_note_types=True)

    def test_a_second_single_card_note_type_resolves_the_same_way(self, col):
        note = real_anki.add_note(col, KANJI, {"Kanji": "k", "Keyword": "kw"})
        assert interpolate_from_text("{{__Card_Type}}", note, multiple_note_types=True) == (
            "New",
            [],
        )
