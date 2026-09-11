"""Characterization tests for `interpolate_from_text`: note fields, special note values,
the destination prefix, and variables.

Everything a copy definition does eventually goes through this function, so it is pinned
first and in the most detail. It is pure apart from the note it reads, which makes it the
cheapest part of the suite and the part a refactor is most likely to change by accident --
the case-sensitivity rules in particular are not a design, they are what falls out of
lowercasing the text inside the braces while matching the special-value regex against the
original casing.
"""

import pytest

from anki_shared.testing import real_anki
from anki_shared.interpolate.interpolate_fields import (
    get_fields_from_text,
    interpolate_from_text,
)
from anki_shared.interpolate.to_lowercase_dict import to_lowercase_dict
from conftest import VOCAB


@pytest.fixture
def note(col):
    return real_anki.add_note(
        col,
        VOCAB,
        {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat", "Freq": "12", "Note": ""},
        tags=["animal", "n5"],
    )


class TestNoteFields:
    def test_field_substitutes_its_value(self, note):
        assert interpolate_from_text("{{Word}}", note) == ("neko", [])

    @pytest.mark.parametrize("written", ["{{Word}}", "{{word}}", "{{WORD}}", "{{WoRd}}"])
    def test_field_names_are_case_insensitive(self, note, written):
        assert interpolate_from_text(written, note) == ("neko", [])

    def test_every_casing_in_one_text_resolves(self, note):
        # The text is lowercased inside the braces before a single str.replace pass, so all
        # three spellings collapse onto the same key and are replaced together.
        assert interpolate_from_text("{{Word}} {{WORD}} {{word}}", note) == (
            "neko neko neko",
            [],
        )

    def test_an_empty_field_is_not_an_invalid_field(self, note):
        # Only a None lookup is invalid. "" is a value the note really holds.
        assert interpolate_from_text("{{Note}}", note) == ("", [])

    def test_a_missing_field_is_reported_lowercased(self, note):
        assert interpolate_from_text("{{Missing}}", note) == ("", ["missing"])

    def test_the_invalid_field_list_is_de_duplicated(self, note):
        # One entry, and the text between the two occurrences is left as it was.
        assert interpolate_from_text("{{Missing}} {{missing}}", note) == (" ", ["missing"])

    def test_two_fields_differing_only_in_case_collapse_and_the_last_wins(self):
        # Pinned on to_lowercase_dict rather than on a real note: Anki renames the second of
        # two fields whose names differ only in case ("word" is saved as "word+"), so the
        # collision cannot reach a collection any more. The collapse rule still applies to
        # anything dict-like handed to the interpolator, variables included.
        assert to_lowercase_dict({"Word": "upper", "word": "lower"}) == {"word": "lower"}

    def test_triple_braces_take_the_inner_brace_as_part_of_the_name(self, note):
        # The non-greedy (.+?) grabs "{Word", leaving the third "}" as literal text. Pinned
        # because it is exactly the kind of thing a regex tweak silently changes.
        assert interpolate_from_text("{{{Word}}}", note) == ("}", ["{word"])

    def test_empty_text_passes_through(self, note):
        assert interpolate_from_text("", note) == ("", [])

    def test_text_with_no_fields_passes_through(self, note):
        assert interpolate_from_text("plain text", note) == ("plain text", [])

    def test_fields_are_substituted_inside_surrounding_text(self, note):
        assert interpolate_from_text("a {{Word}} b {{Meaning}} c", note) == (
            "a neko b cat c",
            [],
        )


class TestSpecialNoteValues:
    def test_note_id(self, note):
        assert interpolate_from_text("{{__Note_ID}}", note) == (str(note.id), [])

    def test_note_type_id(self, note, col):
        note_type = col.models.by_name(VOCAB)
        assert note_type is not None
        assert interpolate_from_text("{{__Note_Type_ID}}", note) == (str(note_type["id"]), [])

    def test_note_tags_are_space_joined(self, note):
        assert interpolate_from_text("{{__Note_Tags}}", note) == ("animal n5", [])

    def test_note_card_count(self, note):
        assert interpolate_from_text("{{__Note_Card_Count}}", note) == ("2", [])

    def test_special_values_are_case_sensitive(self, note):
        # NOTE_VALUE_RE.match runs on the original-cased name while the note-field lookup
        # uses field.lower(), so note *fields* are case-insensitive and special values are
        # not. A trap for anyone touching that regex, hence its own test.
        assert interpolate_from_text("{{__note_id}}", note) == ("", ["__note_id"])

    def test_has_tag_returns_the_tag_when_present(self, note):
        assert interpolate_from_text("{{__Note_Has_Tag==animal}}", note) == ("animal", [])

    def test_has_tag_returns_a_dash_when_absent(self, note):
        assert interpolate_from_text("{{__Note_Has_Tag==plant}}", note) == ("-", [])

    def test_has_tag_without_an_argument_is_an_invalid_field(self, note):
        # "__Note_Has_Tag==" is the registered key, so the bare name matches nothing.
        assert interpolate_from_text("{{__Note_Has_Tag}}", note) == ("", ["__note_has_tag"])


class TestDestinationPrefix:
    @pytest.fixture
    def destination(self, col):
        return real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})

    def test_source_and_destination_are_read_in_the_same_text(self, note, destination):
        assert interpolate_from_text(
            "{{Word}} -> {{__Dest__Word}}", note, destination_note=destination
        ) == ("neko -> inu", [])

    def test_a_special_value_through_the_prefix_keeps_its_own_underscores(
        self, note, destination
    ):
        # Four underscores: the prefix's two plus the special value's two. This is the real
        # shape used by live file definitions.
        assert interpolate_from_text(
            "{{__Dest____Note_ID}}", note, destination_note=destination
        ) == (str(destination.id), [])

    def test_the_prefix_is_case_sensitive(self, note, destination):
        # startswith(DESTINATION_PREFIX) runs before the lowercasing that makes field names
        # case-insensitive, so a lowercased prefix never matches.
        assert interpolate_from_text(
            "{{__dest__Word}}", note, destination_note=destination
        ) == ("", ["__dest__word"])

    def test_without_a_destination_note_it_does_not_fall_back_to_the_source(self, note):
        assert interpolate_from_text("{{__Dest__Word}}", note, destination_note=None) == (
            "",
            ["__dest__word"],
        )


class TestVariables:
    def test_a_variable_is_substituted(self, note):
        assert interpolate_from_text(
            "{{MyVar}}", note, variable_values_dict={"MyVar": "value"}
        ) == ("value", [])

    @pytest.mark.parametrize("written", ["{{MyVar}}", "{{myvar}}", "{{MYVAR}}"])
    def test_variable_names_are_case_insensitive(self, note, written):
        assert interpolate_from_text(
            written, note, variable_values_dict={"MyVar": "value"}
        ) == ("value", [])

    def test_a_note_field_shadows_a_variable_of_the_same_name(self, note):
        # The variable is only consulted after the note-field lookup returns None, so a
        # variable can never override a field. Easy to break by reordering those two.
        assert interpolate_from_text(
            "{{Word}}", note, variable_values_dict={"Word": "from variable"}
        ) == ("neko", [])

    def test_non_string_variable_values_are_stringified(self, note):
        assert interpolate_from_text(
            "{{__Target_Notes_Count}}", note, variable_values_dict={"__Target_Notes_Count": 3}
        ) == ("3", [])

    def test_a_variable_absent_from_the_dict_is_an_invalid_field(self, note):
        assert interpolate_from_text("{{MyVar}}", note, variable_values_dict={}) == (
            "",
            ["myvar"],
        )


class TestCloze:
    def test_the_inner_field_is_interpolated_and_the_wrapper_preserved(self, note):
        assert interpolate_from_text("{{c1::{{Word}}}}", note) == ("{{c1::neko}}", [])

    def test_a_hint_survives(self, note):
        assert interpolate_from_text("{{c1::{{Word}}::hint}}", note) == (
            "{{c1::neko::hint}}",
            [],
        )

    def test_an_unclosed_cloze_is_returned_verbatim(self, note):
        assert interpolate_from_text("{{c1::unclosed", note) == ("{{c1::unclosed", [])

    def test_two_clozes_in_one_text(self, note):
        assert interpolate_from_text("{{c1::{{Word}}}} {{c2::{{Meaning}}}}", note) == (
            "{{c1::neko}} {{c2::cat}}",
            [],
        )

    def test_nested_cloze(self, note):
        assert interpolate_from_text("{{c1::a {{c2::{{Word}}}} b}}", note) == (
            "{{c1::a {{c2::neko}} b}}",
            [],
        )

    def test_an_invalid_field_inside_cloze_content_surfaces(self, note):
        text, invalid = interpolate_from_text("{{c1::{{Missing}}}}", note)
        assert text == "{{c1::}}"
        assert invalid == ["missing"]

    def test_get_fields_from_text_puts_inner_cloze_fields_first_and_excludes_the_cloze(self):
        # Cloze regions are stripped in reverse order and their inner fields collected as
        # they go, so the inner ones come out ahead of the plain ones.
        assert get_fields_from_text("a {{X}} {{c1::{{Y}}}} {{c2::z}}") == ["Y", "X"]
