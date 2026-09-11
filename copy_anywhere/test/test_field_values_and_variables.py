"""Characterization tests for `get_field_values_from_notes` and `get_variable_values_for_note`.

These two sit either side of interpolation: the first joins one text across many source
notes, the second resolves the variables that text may refer to. Both have failure modes
that are silent -- a value dropped without its separator, an entire variable dict discarded
-- so the assertions here are as much about what is *missing* from the output as about what
is in it.
"""

import pytest

from anki_shared.testing import real_anki
from anki_shared.interpolate.interpolate_fields import QUERY_NOTE_INDEX
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import (
    CopyFailedException,
    get_field_values_from_notes,
    get_variable_values_for_note,
)


@pytest.fixture
def notes(col):
    return [
        real_anki.add_note(col, VOCAB, {"Word": "one", "Meaning": "1"}),
        real_anki.add_note(col, VOCAB, {"Word": "two", "Meaning": "2"}),
        real_anki.add_note(col, VOCAB, {"Word": "three", "Meaning": "3"}),
    ]


class TestGetFieldValuesFromNotes:
    def test_none_copy_from_text_logs_and_returns_empty(self, notes, logger):
        assert (
            get_field_values_from_notes(
                copy_from_text=None, notes=notes, dest_note=None, logger=logger
            )
            == ""
        )
        assert logger.has_error("'copy_from_text' was missing")

    def test_no_notes_returns_empty(self, logger):
        # The wipe path: an empty result is what clears a target field when a definition is
        # allowed to run with no sources found.
        assert (
            get_field_values_from_notes(
                copy_from_text="{{Word}}", notes=[], dest_note=None, logger=logger
            )
            == ""
        )

    def test_values_are_joined_with_the_separator(self, notes, logger):
        assert (
            get_field_values_from_notes(
                copy_from_text="{{Word}}",
                notes=notes,
                dest_note=None,
                select_card_separator=" | ",
                logger=logger,
            )
            == "one | two | three"
        )

    def test_a_none_separator_defaults_to_comma_space(self, notes, logger):
        assert (
            get_field_values_from_notes(
                copy_from_text="{{Word}}",
                notes=notes,
                dest_note=None,
                select_card_separator=None,
                logger=logger,
            )
            == "one, two, three"
        )

    def test_an_empty_separator_stays_empty(self, notes, logger):
        # Only None is defaulted. "" is a separator someone chose, and the difference is
        # invisible without a test.
        assert (
            get_field_values_from_notes(
                copy_from_text="{{Word}}",
                notes=notes,
                dest_note=None,
                select_card_separator="",
                logger=logger,
            )
            == "onetwothree"
        )

    def test_the_separator_never_leads(self, notes, logger):
        assert (
            get_field_values_from_notes(
                copy_from_text="{{Word}}",
                notes=notes[:1],
                dest_note=None,
                select_card_separator=", ",
                logger=logger,
            )
            == "one"
        )

    def test_query_note_index_increments_across_several_notes(self, notes, logger):
        variables = {}
        assert (
            get_field_values_from_notes(
                copy_from_text="{{__Query_Note_Index}}",
                notes=notes,
                dest_note=None,
                variable_values_dict=variables,
                select_card_separator="-",
                logger=logger,
            )
            == "1-2-3"
        )
        assert variables[QUERY_NOTE_INDEX] == 3

    def test_query_note_index_is_left_alone_for_a_single_note(self, notes, logger):
        # The guard is `len(notes) > 1`, so with exactly one source note the index keeps
        # whatever the outer destination loop put there. That is what makes
        # Source-to-destinations able to number its destinations.
        variables = {QUERY_NOTE_INDEX: 7}
        assert (
            get_field_values_from_notes(
                copy_from_text="{{__Query_Note_Index}}",
                notes=notes[:1],
                dest_note=None,
                variable_values_dict=variables,
                logger=logger,
            )
            == "7"
        )
        assert variables[QUERY_NOTE_INDEX] == 7

    def test_code_returning_nothing_drops_the_value_and_its_separator(self, notes, logger):
        # execute_code_for_field returns None when the code has no return, and the append is
        # guarded on `interpolated_value is not None`, so the separator that would have gone
        # in front of it is dropped too -- the gap between the surviving values doubles.
        code = "if note['Word'] == 'two':\n    pass\nelse:\n    return note['Word']"
        assert (
            get_field_values_from_notes(
                copy_from_text=code,
                notes=notes,
                dest_note=None,
                use_code=True,
                select_card_separator="-",
                logger=logger,
            )
            == "one-three"
        )

    def test_a_code_error_aborts_the_whole_definition(self, notes, logger):
        # Not just this field: CopyFailedException unwinds to copy_for_single_trigger_note,
        # which returns False and stops the bulk loop.
        with pytest.raises(CopyFailedException):
            get_field_values_from_notes(
                copy_from_text="return 1 / 0",
                notes=notes,
                dest_note=None,
                use_code=True,
                logger=logger,
            )

    def test_the_multi_note_type_value_error_breaks_and_returns_a_partial_string(
        self, col, logger
    ):
        # A note with two card types raises out of interpolation when multiple_note_types is
        # set. The handler breaks rather than continuing, so the notes after it are dropped
        # and a *partial* joined string comes back -- not "" and not an exception.
        from conftest import SENTENCE

        single = real_anki.add_note(col, SENTENCE, {"Sentence": "s", "Vocab": "v"})
        two_card = real_anki.add_note(col, VOCAB, {"Word": "boom"})
        another = real_anki.add_note(col, SENTENCE, {"Sentence": "s2", "Vocab": "v2"})
        result = get_field_values_from_notes(
            copy_from_text="{{__Card_Type}}",
            notes=[single, two_card, another],
            dest_note=None,
            multiple_note_types=True,
            select_card_separator="-",
            logger=logger,
        )
        assert result == "New"
        assert logger.has_error("Error in text interpolation")


class TestGetVariableValuesForNote:
    def _variable(self, name, text, **extra):
        return {"copy_into_variable": name, "copy_from_text": text, **extra}

    def test_a_variable_is_interpolated_from_the_note(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        assert get_variable_values_for_note(
            [self._variable("MyVar", "{{Word}}!")], note, logger=logger
        ) == {"MyVar": "neko!"}

    def test_one_variable_cannot_reference_another(self, col, logger):
        # Interpolation here is given neither a destination note nor the dict being built, so
        # a variable naming another variable is simply an invalid field. Several live
        # definitions would change meaning if this were "improved".
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        values = get_variable_values_for_note(
            [self._variable("First", "{{Word}}"), self._variable("Second", "{{First}}")],
            note,
            logger=logger,
        )
        assert values == {"First": "neko", "Second": ""}
        assert logger.has_error("Invalid fields in copy_from_text: first")

    def test_an_invalid_field_logs_but_keeps_the_partial_value(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        values = get_variable_values_for_note(
            [self._variable("MyVar", "{{Word}}-{{Missing}}")], note, logger=logger
        )
        assert values == {"MyVar": "neko-"}
        assert logger.has_error("Invalid fields in copy_from_text: missing")

    def test_a_code_variable_runs(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        values = get_variable_values_for_note(
            [
                self._variable(
                    "MyVar", "", copy_as_code="return '{{Word}}'.upper()", use_code=True
                )
            ],
            note,
            logger=logger,
        )
        assert values == {"MyVar": "NEKO"}

    def test_a_code_error_raises_copy_failed(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        with pytest.raises(CopyFailedException, match="MyVar"):
            get_variable_values_for_note(
                [self._variable("MyVar", "", copy_as_code="return 1 / 0", use_code=True)],
                note,
                logger=logger,
            )

    def test_a_process_chain_runs_on_a_variable(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        values = get_variable_values_for_note(
            [
                self._variable(
                    "MyVar",
                    "{{Word}}",
                    process_chain=[
                        {"name": "Regex replace", "regex": "e", "replacement": "3", "flags": ""}
                    ],
                )
            ],
            note,
            logger=logger,
        )
        assert values == {"MyVar": "n3ko"}

    def test_a_failing_process_chain_discards_every_variable(self, col, logger):
        # The early `return {}` throws away the variables that already resolved, not just the
        # one whose chain failed. It looks like an accident; pinned so that changing it is a
        # deliberate act with a visible diff.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        values = get_variable_values_for_note(
            [
                self._variable("Good", "{{Word}}"),
                self._variable(
                    "Bad",
                    "{{Word}}",
                    process_chain=[
                        {
                            "name": "Fonts check",
                            "fonts_dict_file": "does_not_exist.json",
                            "limit_to_fonts": [],
                            "character_limit_regex": "",
                        }
                    ],
                ),
                self._variable("AlsoGood", "{{Meaning}}"),
            ],
            note,
            logger=logger,
        )
        assert values == {}
