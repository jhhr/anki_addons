"""Characterization tests for `copy_fields`: the `CollectionOp` entry point.

This is the top of the stack and the only layer that writes: everything below it mutates
`Note` and `Card` objects in memory and hands them back, and `copy_fields`'s `op` is what
calls `update_notes` / `update_cards` and folds the whole run into one undo entry. So this
is the one file whose assertions are on re-fetched notes, on card flags, and on `col.db`.

**Driving the op headless.** `copy_fields` ends with
`CollectionOp(parent, op).success(...).failure(...).run_in_background()`, and it returns
whatever `run_in_background` returns -- `None` in a real Anki. `run_in_background` needs
`mw._increase_background_ops` and a taskman with `with_progress`, neither of which the stub
`mw` has, so the seam is the `CollectionOp` name in the module: `run_copy_fields` swaps in a
stand-in that keeps the `op` closure, runs it inline as `op(mw.col)`, and returns its
`CacheResults` straight out of `copy_fields`. The `success` / `failure` callbacks are
recorded but deliberately not called -- `on_success` builds a `tooltip`, which needs a real
main window -- so an exception raised inside `op` reaches the test directly instead of going
through `on_failure`. That also leaves the run's log file open, which the `conftest` fixture
closes again; `test_operation_logging.py` is where the callbacks are driven too.

Four behaviours here are load-bearing and easy to break:

* `merge_undo_entries` runs once before the loop and once after *every* definition -- the
  comment in the source warns that skipping it causes "target undo op not found";
* `copied_into_notes` is appended to and never cleared, so definition *n*'s `update_notes`
  re-writes every note definitions 1..n-1 touched;
* in Source-to-destinations that same list can hold one destination note several times, once
  per trigger note, and each of those is a separate fetch of the same row -- so the earlier
  trigger notes' writes are lost (see `TestOneDestinationFromSeveralTriggerNotes`);
* the sync tail is the only place `fc` is written back to `1`, and it has to clear the flag
  on cards the definitions never looked at as well as on the ones they did.
"""

import json

import pytest
from anki.collection import OpChanges
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import KANJI, SENTENCE, VOCAB
from copy_anywhere.logic import copy_fields as copy_fields_module
from copy_anywhere.logic.copy_fields import copy_fields, make_copy_fields_undo_text


@pytest.fixture
def run_copy_fields(monkeypatch):
    """Call `copy_fields` with its `CollectionOp` driven inline, and hand back the results.

    See the module docstring: the stand-in returns the `op`'s `CacheResults` from
    `run_in_background`, which `copy_fields` returns unchanged.
    """
    captured: dict = {}

    class InlineCollectionOp:
        def __init__(self, parent, op):
            captured["parent"] = parent
            captured["op"] = op

        def success(self, callback):
            captured["success"] = callback
            return self

        def failure(self, callback):
            captured["failure"] = callback
            return self

        def run_in_background(self, **kwargs):
            return captured["op"](mw.col)

    monkeypatch.setattr(copy_fields_module, "CollectionOp", InlineCollectionOp)

    def run(**kwargs):
        return copy_fields(**kwargs)

    run.captured = captured  # type: ignore[attr-defined]
    return run


class UpdateLog:
    """What `update_notes` / `update_cards` were handed, call by call.

    Fields are snapshotted at call time rather than kept as references, because the same
    note id arrives more than once carrying different values and the whole point is which
    of those the database ends up with.
    """

    def __init__(self) -> None:
        self.notes: list[list[tuple[int, dict]]] = []
        self.cards: list[list[tuple[int, object]]] = []

    def clear(self) -> None:
        self.notes.clear()
        self.cards.clear()

    def note_ids(self) -> list[list[int]]:
        return [[nid for nid, _ in call] for call in self.notes]

    def note_values(self, field: str) -> list[list[str]]:
        return [[fields[field] for _, fields in call] for call in self.notes]

    def card_ids(self) -> list[list[int]]:
        return [[cid for cid, _ in call] for call in self.cards]

    def card_edited_flags(self) -> list[list[object]]:
        return [[edited for _, edited in call] for call in self.cards]


ABSENT = "<no edited attribute>"


@pytest.fixture
def updates(col, monkeypatch):
    """Record every `update_notes` / `update_cards` call, then let it through."""
    log = UpdateLog()
    original_notes = col.update_notes
    original_cards = col.update_cards

    def update_notes(notes, **kwargs):
        log.notes.append([(note.id, dict(note.items())) for note in notes])
        return original_notes(notes, **kwargs)

    def update_cards(cards, **kwargs):
        log.cards.append([(card.id, getattr(card, "edited", ABSENT)) for card in cards])
        return original_cards(cards, **kwargs)

    monkeypatch.setattr(col, "update_notes", update_notes)
    monkeypatch.setattr(col, "update_cards", update_cards)
    return log


def write_into_note(name="within", value="{{Word}}", field="Note", **extra):
    """A Within-note definition over CA Vocab, so a run leaves a trace in one field."""
    return d.within_note(
        definition_name=name,
        field_to_field_defs=[d.field_to_field(field, value)],
        **extra,
    )


def flag(col, note, fc, extra=None):
    """Set the custom-data field-changed flag the sync selection and tail read."""
    data = {"fc": fc}
    if extra:
        data.update(extra)
    for card in note.cards():
        real_anki.set_custom_data(col, card.id, json.dumps(data))


def custom_data(col, card_id) -> dict:
    raw = col.get_card(card_id).custom_data
    return json.loads(raw) if raw else {}


class TestUndoText:
    """`make_copy_fields_undo_text`, which names the entry the whole run merges into."""

    def test_a_single_definition_is_named_in_the_undo_text(self):
        assert (
            make_copy_fields_undo_text([d.within_note(definition_name="alpha")])
            == "Copy fields (alpha)"
        )

    def test_several_definitions_are_counted_rather_than_named(self):
        definitions = [d.within_note(definition_name=f"d{i}") for i in range(3)]

        assert make_copy_fields_undo_text(definitions) == "Copy fields with 3 definitions"

    def test_an_empty_list_still_produces_a_count_of_zero(self):
        # Unreachable through `copy_fields`, which returns before building any undo text for
        # an empty list -- but the function itself is public and answers for it.
        assert make_copy_fields_undo_text([]) == "Copy fields with 0 definitions"

    def test_a_note_count_is_appended_when_one_is_given(self):
        definitions = [d.within_note(definition_name="alpha")]

        assert (
            make_copy_fields_undo_text(definitions, note_count=5)
            == "Copy fields (alpha) for 5 notes"
        )

    def test_a_note_count_of_zero_is_left_out_rather_than_shown(self):
        # `if note_count:` rather than `is not None`, so "for 0 notes" can never appear.
        # Harmless here -- a run over no notes has nothing to undo -- but it means the undo
        # text cannot distinguish "no notes given" from "an empty list of notes given".
        definitions = [d.within_note(definition_name="alpha")]

        assert make_copy_fields_undo_text(definitions, note_count=0) == "Copy fields (alpha)"

    def test_a_suffix_lands_after_the_note_count(self):
        definitions = [d.within_note(definition_name="alpha")]

        assert (
            make_copy_fields_undo_text(definitions, note_count=2, suffix="on add")
            == "Copy fields (alpha) for 2 notes on add"
        )

    def test_a_suffix_alone_is_appended_directly(self):
        definitions = [d.within_note(definition_name="alpha")]

        assert (
            make_copy_fields_undo_text(definitions, suffix="on add") == "Copy fields (alpha) on add"
        )


class TestNoDefinitions:
    def test_an_empty_definition_list_returns_an_empty_result_with_empty_changes(
        self, col, run_copy_fields
    ):
        results = run_copy_fields(copy_definitions=[])

        assert results.get_result_text() == ""
        assert results.get_count() == 0
        assert results.changes == OpChanges()

    def test_an_empty_definition_list_never_opens_an_undo_entry(self, col, run_copy_fields):
        add_calls = real_anki.counting_wrapper(col, "add_custom_undo_entry")
        merge_calls = real_anki.counting_wrapper(col, "merge_undo_entries")

        run_copy_fields(copy_definitions=[])

        assert add_calls() == 0
        assert merge_calls() == 0

    def test_an_empty_definition_list_is_reported_as_an_error(self, col, run_copy_fields, logger):
        # `copy_fields` logs through the module's logger rather than taking one, so the
        # `logger` fixture's handler -- attached to the addon's logger for the test -- is
        # where a test sees what it reported.
        run_copy_fields(copy_definitions=[])

        assert logger.has_error("Error in copy fields: No definitions given")


class TestUndoEntry:
    def test_a_custom_undo_entry_is_created_when_none_is_passed(self, col, run_copy_fields):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        add_calls = real_anki.counting_wrapper(col, "add_custom_undo_entry")

        run_copy_fields(copy_definitions=[write_into_note()], note_ids=[note.id])

        assert add_calls() == 1
        assert col.undo_status().undo == "Copy fields (within) for 1 notes"

    def test_the_note_count_in_the_undo_text_comes_from_note_ids(self, col, run_copy_fields):
        first = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        second = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})

        run_copy_fields(copy_definitions=[write_into_note()], note_ids=[first.id, second.id])

        assert col.undo_status().undo == "Copy fields (within) for 2 notes"

    def test_the_note_count_is_summed_across_note_ids_per_definition(self, col, run_copy_fields):
        first = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        second = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})

        run_copy_fields(
            copy_definitions=[write_into_note("a"), write_into_note("b")],
            note_ids_per_definition=[[first.id], [second.id]],
        )

        # Summed, not deduplicated: the same note in both lists would be counted twice.
        assert col.undo_status().undo == "Copy fields with 2 definitions for 2 notes"

    def test_note_ids_wins_over_note_ids_per_definition_for_the_count_alone(
        self, col, run_copy_fields
    ):
        # The count is taken from `note_ids` when it is given, but the ids each definition
        # actually runs over come from `note_ids_per_definition` -- so passing both makes
        # the undo text disagree with what was copied. Nothing calls it that way today.
        first = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        second = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})

        run_copy_fields(
            copy_definitions=[write_into_note()],
            note_ids=[first.id],
            note_ids_per_definition=[[first.id, second.id]],
        )

        assert col.undo_status().undo == "Copy fields (within) for 1 notes"
        assert col.get_note(second.id)["Note"] == "inu"

    def test_no_note_ids_at_all_leaves_the_count_out_of_the_undo_text(self, col, run_copy_fields):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        run_copy_fields(copy_definitions=[write_into_note()])

        assert col.undo_status().undo == "Copy fields (within)"

    def test_an_explicit_undo_entry_is_merged_into_rather_than_replaced(self, col, run_copy_fields):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        entry = col.add_custom_undo_entry("Outer thing")
        add_calls = real_anki.counting_wrapper(col, "add_custom_undo_entry")

        run_copy_fields(
            copy_definitions=[write_into_note()],
            note_ids=[note.id],
            undo_entry=entry,
            # Documented as useless when an undo_entry is passed, because the text it would
            # decorate is never built.
            undo_text_suffix="on add",
        )

        assert add_calls() == 0
        assert col.undo_status().undo == "Outer thing"

    def test_the_whole_run_collapses_into_one_undo_step(self, col, run_copy_fields):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definitions = [
            write_into_note("a", value="written", field="Note"),
            write_into_note("b", value="7", field="Freq"),
        ]

        run_copy_fields(copy_definitions=definitions, note_ids=[note.id])
        assert (col.get_note(note.id)["Note"], col.get_note(note.id)["Freq"]) == ("written", "7")

        col.undo()

        # Both definitions' writes go in one step: that is what merging every definition
        # into the same custom entry buys.
        assert (col.get_note(note.id)["Note"], col.get_note(note.id)["Freq"]) == ("", "")

    def test_merge_runs_once_before_the_loop_and_once_after_every_definition(
        self, col, run_copy_fields
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        merge_calls = real_anki.counting_wrapper(col, "merge_undo_entries")
        definitions = [write_into_note("a"), write_into_note("b"), write_into_note("c")]

        run_copy_fields(copy_definitions=definitions, note_ids=[note.id])

        # Three definitions, four merges: the issue predicted one per definition, but there
        # is also the merge that builds the initial `CacheResults.changes` before the loop.
        assert merge_calls() == 4

    def test_the_sync_tail_merges_twice_more_on_top_of_that(self, col, run_copy_fields):
        note = real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": ""})
        flag(col, note, 0)
        merge_calls = real_anki.counting_wrapper(col, "merge_undo_entries")
        definition = d.within_note(
            note_types=[KANJI],
            field_to_field_defs=[d.field_to_field("Keyword", "{{Kanji}}")],
            copy_on_sync=True,
            copy_on_review=True,
        )

        run_copy_fields(copy_definitions=[definition], update_sync_result=lambda text, count: None)

        # One before the loop, one after the definition, then one after each of the tail's
        # two `update_cards` calls.
        assert merge_calls() == 4


class TestNoteIdsPerDefinition:
    """The `PickCopyDefinitionsDialog` path: definition *i* runs over list *i*."""

    def test_each_definition_sees_only_its_own_note_ids(self, col, run_copy_fields):
        first = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        second = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})

        run_copy_fields(
            copy_definitions=[
                write_into_note("a", value="from-a"),
                write_into_note("b", value="from-b"),
            ],
            note_ids_per_definition=[[first.id], [second.id]],
        )

        assert col.get_note(first.id)["Note"] == "from-a"
        assert col.get_note(second.id)["Note"] == "from-b"

    def test_an_empty_list_for_one_definition_makes_that_definition_a_no_op(
        self, col, run_copy_fields, logger
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        run_copy_fields(
            copy_definitions=[
                write_into_note("a", value="from-a"),
                write_into_note("b", value="from-b", field="Freq"),
            ],
            note_ids_per_definition=[[note.id], []],
        )

        assert col.get_note(note.id)["Note"] == "from-a"
        assert col.get_note(note.id)["Freq"] == ""
        # An empty id list reaches the bulk loop's "found no notes of this note type" error
        # rather than being recognised as "this definition was given nothing to do".
        assert logger.has_error("Did not find any notes of note type(s)")

    def test_a_list_shorter_than_the_definitions_is_rejected_before_anything_is_written(
        self, col, run_copy_fields, logger
    ):
        # Checked before the loop: indexing list i per definition would otherwise raise
        # IndexError only after the earlier definitions had been written and merged.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        add_calls = real_anki.counting_wrapper(col, "add_custom_undo_entry")

        results = run_copy_fields(
            copy_definitions=[write_into_note("a"), write_into_note("b", field="Freq")],
            note_ids_per_definition=[[note.id]],
        )

        assert col.get_note(note.id)["Note"] == ""
        assert add_calls() == 0
        assert results.get_result_text() == ""
        assert results.changes == OpChanges()
        assert logger.has_error("Got 1 note id lists for 2 definitions")

    def test_a_list_longer_than_the_definitions_is_rejected_too(self, col, run_copy_fields, logger):
        # A surplus list would never be indexed, but it means the caller's lists and
        # definitions have drifted apart, so which list belongs to which is unknowable.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        results = run_copy_fields(
            copy_definitions=[write_into_note("a")],
            note_ids_per_definition=[[note.id], [note.id]],
        )

        assert col.get_note(note.id)["Note"] == ""
        assert results.changes == OpChanges()
        assert logger.has_error("Got 2 note id lists for 1 definitions")

    @pytest.mark.parametrize("entry", [None, 5, "123"], ids=["none", "int", "str"])
    def test_an_entry_that_is_not_a_list_is_rejected_before_anything_is_written(
        self, col, run_copy_fields, logger, entry
    ):
        # A str is a Sequence, but of characters rather than note ids.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        results = run_copy_fields(
            copy_definitions=[write_into_note("a"), write_into_note("b", field="Freq")],
            note_ids_per_definition=[[note.id], entry],
        )

        assert col.get_note(note.id)["Note"] == ""
        assert results.changes == OpChanges()
        assert logger.has_error("Note ids for definition 2 are not a list")

    def test_find_notes_results_pass_the_check_as_the_dialog_hands_them_over(
        self, col, run_copy_fields
    ):
        # `show_copy_dialog` passes `find_notes`' return value as it is: a protobuf
        # container, which is a Sequence but not a `list`.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        found = col.find_notes(f"nid:{note.id}")
        assert not isinstance(found, list)

        run_copy_fields(
            copy_definitions=[write_into_note("a", value="from-a")],
            note_ids_per_definition=[found],
        )

        assert col.get_note(note.id)["Note"] == "from-a"


class TestDefinitionOrder:
    """`update_notes` between definitions is what makes the order visible."""

    def test_the_last_definition_to_write_a_field_wins(self, col, run_copy_fields):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        run_copy_fields(
            copy_definitions=[
                write_into_note("a", value="first"),
                write_into_note("b", value="second"),
            ],
            note_ids=[note.id],
        )

        assert col.get_note(note.id)["Note"] == "second"

    def test_a_later_definition_reads_what_an_earlier_one_wrote(self, col, run_copy_fields):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        run_copy_fields(
            copy_definitions=[
                write_into_note("a", value="{{Word}}"),
                write_into_note("b", value="[{{Note}}]"),
            ],
            note_ids=[note.id],
        )

        # The second definition re-fetches every note from the database, so it sees the
        # first's write rather than the value the run started with.
        assert col.get_note(note.id)["Note"] == "[neko]"

    def test_reversing_the_definitions_reverses_the_result(self, col, run_copy_fields):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        run_copy_fields(
            copy_definitions=[
                write_into_note("b", value="[{{Note}}]"),
                write_into_note("a", value="{{Word}}"),
            ],
            note_ids=[note.id],
        )

        assert col.get_note(note.id)["Note"] == "neko"


class TestCopiedIntoNotesAccumulates:
    def test_update_notes_runs_once_per_definition_even_with_nothing_to_write(
        self, col, run_copy_fields, updates
    ):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definitions = [
            d.within_note(definition_name="a", field_to_field_defs=[]),
            d.within_note(definition_name="b", field_to_field_defs=[]),
        ]

        run_copy_fields(copy_definitions=definitions)

        assert updates.note_ids() == [[], []]

    def test_the_list_is_never_cleared_so_earlier_notes_are_written_again(
        self, col, run_copy_fields, updates
    ):
        first = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        second = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})

        run_copy_fields(
            copy_definitions=[
                write_into_note("a", value="from-a"),
                write_into_note("b", value="from-b"),
            ],
            note_ids_per_definition=[[first.id], [second.id]],
        )

        # The second call re-writes the note the first definition already saved. Harmless
        # today -- the stale object carries the same values the database holds -- but it is
        # O(definitions x notes) writes, and it is what makes the duplicate case below bite.
        assert updates.note_ids() == [[first.id], [first.id, second.id]]

    def test_the_same_note_arrives_twice_and_the_later_object_wins(
        self, col, run_copy_fields, updates
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        run_copy_fields(
            copy_definitions=[
                write_into_note("a", value="{{Word}}"),
                write_into_note("b", value="[{{Note}}]"),
            ],
            note_ids=[note.id],
        )

        # One id, listed twice, carrying different values: `update_notes` applies them in
        # order, so the freshly-fetched object at the end of the list is what survives.
        assert updates.note_ids()[1] == [note.id, note.id]
        assert updates.note_values("Note")[1] == ["neko", "[neko]"]
        assert col.get_note(note.id)["Note"] == "[neko]"


class TestOneDestinationFromSeveralTriggerNotes:
    """Source-to-destinations, where the duplicates in `copied_into_notes` do damage."""

    def test_two_trigger_notes_append_the_same_destination_twice(
        self, col, run_copy_fields, updates
    ):
        destination = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.add_note(col, SENTENCE, {"Sentence": "s1", "Vocab": "neko"})
        real_anki.add_note(col, SENTENCE, {"Sentence": "s2", "Vocab": "neko"})
        definition = d.source_to_destinations(
            note_types=[SENTENCE],
            copy_from_cards_query='note:"CA Vocab"',
            select_card_count="0",
            field_to_field_defs=[d.field_to_field("Note", "{{__Dest__Note}}<{{Sentence}}>")],
        )

        run_copy_fields(copy_definitions=[definition])

        assert updates.note_ids() == [[destination.id, destination.id]]

    def test_the_first_trigger_notes_write_is_lost(self, col, run_copy_fields):
        # KNOWN LIMITATION: each trigger note fetches the destination note fresh from the database,
        # but `update_notes` only runs once the whole definition has finished, so the second
        # trigger note starts from the unmodified row and its object -- the last in
        # `copied_into_notes` -- is the one that lands.
        #
        # This is intentional because the alternative is calling update_notes + merge_undo_entries
        # per each trigger note, which was done in the past and found to be massively worse in
        # performance compared to one update_notes call at the end of each definition.
        #
        # Additionally, updating after each trigger note would make Source-to-destinations whose
        # destination notes can be an upcoming trigger note in the loop, leading to the final
        # state depends on the order of trigger notes, which is extremely difficult to predict.
        #
        # Thus, the current approach, while not perfect, is a deliberate trade-off for performance
        # and predictability.
        #
        # Copying into the same destination from several sources within one definition is also
        # not the primary purpose of Source-to-destinations, that's better served
        # by a Destination-to-sources definition, so this limitation is not so bad.
        destination = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.add_note(col, SENTENCE, {"Sentence": "s1", "Vocab": "neko"})
        real_anki.add_note(col, SENTENCE, {"Sentence": "s2", "Vocab": "neko"})
        definition = d.source_to_destinations(
            note_types=[SENTENCE],
            copy_from_cards_query='note:"CA Vocab"',
            select_card_count="0",
            field_to_field_defs=[d.field_to_field("Note", "{{__Dest__Note}}<{{Sentence}}>")],
        )

        run_copy_fields(copy_definitions=[definition])

        assert col.get_note(destination.id)["Note"] == "<s2>"

    @staticmethod
    def flag_only_for_s1():
        """Every Sentence writes into the one Vocab note; only trigger `s1` flags a card."""
        return d.staged(
            note_types=[SENTENCE],
            stages=[
                d.note_query("dests", 'note:"CA Vocab"'),
                d.for_each_note("dests", [
                    d.condition(
                        d.code("return trigger['Sentence'] == 's1'"),
                        [d.edit_note(
                            "note",
                            [d.write("Note", d.text("x"))],
                            card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)],
                        )],
                        [d.edit_note("note", [d.write("Note", d.text("x"))])],
                    ),
                ]),
            ],
        )

    @pytest.mark.parametrize("order", [("s1", "s2"), ("s2", "s1")])
    def test_a_later_trigger_note_that_leaves_a_card_alone_keeps_the_earlier_ones_edit(
        self, col, run_copy_fields, logger, order
    ):
        # Unlike the note above, this is not the known limitation: the second trigger note
        # writes the destination note but never edits its card, so it has no copy of the
        # card to hand over (decision 5). When every card of a written note went into
        # `copied_into_cards_dict`, `s2`'s fresh, unflagged copy replaced `s1`'s flagged one
        # whenever `s1` ran first, and the flag was lost. Notes are walked in id order, so
        # the order they are added in is the order they run in.
        destination = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        for sentence in order:
            real_anki.add_note(col, SENTENCE, {"Sentence": sentence})

        run_copy_fields(copy_definitions=[self.flag_only_for_s1()])

        assert not logger.errors, logger.errors
        flags = {card.template()["name"]: card.user_flag() for card in destination.cards()}
        assert flags == {"Recognition": 2, "Recall": 0}


class TestEditedCards:
    def test_the_edited_flag_is_already_gone_when_update_cards_sees_the_card(
        self, col, run_copy_fields, updates
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = write_into_note(card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)])

        run_copy_fields(copy_definitions=[definition], note_ids=[note.id])

        # `del card.edited` happens between selecting the edited cards and saving them, so
        # nothing downstream can tell an edited card from an untouched one.
        assert updates.card_edited_flags() == [[ABSENT]]

    def test_only_the_cards_a_definition_edited_are_saved(self, col, run_copy_fields, updates):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        recognition, recall = note.cards()
        definitions = [
            write_into_note("a", card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)]),
            write_into_note(
                "b", field="Freq", card_actions=[d.card_action(VOCAB, "Recall", set_flag=3)]
            ),
        ]

        run_copy_fields(copy_definitions=definitions, note_ids=[note.id])

        # `copied_into_cards_dict` still holds the Recognition card the first definition
        # flagged, but saving it took its `edited` attribute off, and the second definition
        # leaves the entry alone because it did not edit that card. So it is saved once.
        assert updates.card_ids() == [[recognition.id], [recall.id]]
        assert [card.flags for card in col.get_note(note.id).cards()] == [2, 3]

    def test_a_definition_with_no_card_action_saves_no_cards_at_all(
        self, col, run_copy_fields, updates
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definitions = [
            write_into_note("a", card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)]),
            write_into_note("b", field="Freq"),
        ]

        run_copy_fields(copy_definitions=definitions, note_ids=[note.id])

        assert updates.card_ids()[1] == []


class TestSyncTail:
    """`update_sync_result` is what turns a run into a sync run, and adds the `fc` tail."""

    @pytest.fixture
    def sync_definition(self):
        # copy_on_review together with copy_on_sync makes `copy_on_sync_after_review` False,
        # so the bulk loop selects only `fc = 0` cards and leaves the `fc = -1` ones for the
        # tail -- which is the only way to have a card the tail has to clean up on its own.
        return d.within_note(
            note_types=[KANJI],
            field_to_field_defs=[d.field_to_field("Keyword", "{{Kanji}}")],
            copy_on_sync=True,
            copy_on_review=True,
        )

    @pytest.fixture
    def three_kanji(self, col):
        """A note the definition copies into, one only the tail reaches, one with no flag."""
        copied = real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": ""})
        stale = real_anki.add_note(col, KANJI, {"Kanji": "inu", "Keyword": ""})
        unflagged = real_anki.add_note(col, KANJI, {"Kanji": "tori", "Keyword": ""})
        flag(col, copied, 0)
        flag(col, stale, -1, extra={"s": 3})
        return copied, stale, unflagged

    def test_only_the_flagged_notes_are_copied_into(
        self, col, run_copy_fields, sync_definition, three_kanji
    ):
        copied, stale, unflagged = three_kanji

        run_copy_fields(
            copy_definitions=[sync_definition], update_sync_result=lambda text, count: None
        )

        assert col.get_note(copied.id)["Keyword"] == "neko"
        assert col.get_note(stale.id)["Keyword"] == ""
        assert col.get_note(unflagged.id)["Keyword"] == ""

    def test_every_copied_card_is_flagged_as_handled(
        self, col, run_copy_fields, sync_definition, three_kanji
    ):
        copied, _, _ = three_kanji

        run_copy_fields(
            copy_definitions=[sync_definition], update_sync_result=lambda text, count: None
        )

        assert custom_data(col, copied.cards()[0].id) == {"fc": 1}

    def test_a_card_the_definitions_never_looked_at_is_flagged_too(
        self, col, run_copy_fields, sync_definition, three_kanji
    ):
        _, stale, _ = three_kanji

        run_copy_fields(
            copy_definitions=[sync_definition], update_sync_result=lambda text, count: None
        )

        # The second sweep is a search, not a walk of what was copied: `fc = -1` was never
        # selected by the definition, and other custom-data keys survive the rewrite.
        assert custom_data(col, stale.cards()[0].id) == {"fc": 1, "s": 3}

    def test_nothing_is_left_at_zero_or_minus_one(
        self, col, run_copy_fields, sync_definition, three_kanji
    ):
        run_copy_fields(
            copy_definitions=[sync_definition], update_sync_result=lambda text, count: None
        )

        assert list(col.find_cards("prop:cdn:fc=-1 OR prop:cdn:fc=0")) == []
        assert len(col.find_cards("prop:cdn:fc=1")) == 2

    def test_a_card_with_no_custom_data_is_not_given_an_fc_key(
        self, col, run_copy_fields, sync_definition, three_kanji
    ):
        _, _, unflagged = three_kanji

        run_copy_fields(
            copy_definitions=[sync_definition], update_sync_result=lambda text, count: None
        )

        # The sweep only searches for `fc` values of 0 and -1, so a card that never had the
        # key stays without it rather than being initialised to 1.
        assert col.get_card(unflagged.cards()[0].id).custom_data == ""

    def test_the_db_row_holds_the_flag_under_the_cd_key(
        self, col, run_copy_fields, sync_definition, three_kanji
    ):
        copied, _, _ = three_kanji

        run_copy_fields(
            copy_definitions=[sync_definition], update_sync_result=lambda text, count: None
        )

        data = col.db.scalar("SELECT data FROM cards WHERE id = ?", copied.cards()[0].id)
        # Custom data is JSON nested inside JSON in the `cards.data` column, which is why
        # the search prefix is `prop:cdn:` rather than a plain column comparison.
        assert json.loads(json.loads(data)["cd"]) == {"fc": 1}

    def test_an_edited_card_keeps_its_edit_and_its_unedited_sibling_is_flagged_too(
        self, col, run_copy_fields
    ):
        # Only the edited Recognition card comes back in `copied_into_cards_dict`. The first
        # pass flags it on the object that carries the edit; Recall, whose note was written
        # but which no action changed, is left to the sweep.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        flag(col, note, 0)
        definition = write_into_note(
            copy_on_sync=True, card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)]
        )

        run_copy_fields(copy_definitions=[definition], update_sync_result=lambda text, count: None)

        recognition, recall = col.get_note(note.id).cards()
        assert col.get_note(note.id)["Note"] == "neko"
        assert (recognition.user_flag(), custom_data(col, recognition.id)) == (2, {"fc": 1})
        assert (recall.user_flag(), custom_data(col, recall.id)) == (0, {"fc": 1})

    def test_the_tail_does_not_run_without_update_sync_result(
        self, col, run_copy_fields, sync_definition, three_kanji
    ):
        copied, stale, _ = three_kanji

        # No `update_sync_result`, so `is_sync` is False: the bulk loop selects every note
        # of the type regardless of its flag, and no flag is written back.
        run_copy_fields(copy_definitions=[sync_definition])

        assert custom_data(col, copied.cards()[0].id) == {"fc": 0}
        assert custom_data(col, stale.cards()[0].id) == {"fc": -1, "s": 3}
        assert col.get_note(stale.id)["Keyword"] == "inu"

    def test_a_sync_run_that_matched_nothing_reports_nothing(self, col, run_copy_fields):
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": ""})
        definition = d.within_note(
            note_types=[KANJI],
            field_to_field_defs=[d.field_to_field("Keyword", "{{Kanji}}")],
            copy_on_sync=True,
            copy_on_review=True,
        )

        results = run_copy_fields(
            copy_definitions=[definition], update_sync_result=lambda text, count: None
        )

        # An empty result text is what stops `on_success` showing a tooltip after every
        # sync that had nothing to do.
        assert results.get_result_text() == ""
        assert results.get_count() == 0


class TestCancellation:
    @pytest.fixture
    def cancel_after(self, monkeypatch):
        """Replace `want_cancel` with one that answers True from the nth question onward."""

        def install(nth):
            calls = {"n": 0}

            def want_cancel():
                calls["n"] += 1
                return calls["n"] >= nth

            monkeypatch.setattr(mw.progress, "want_cancel", want_cancel)
            return lambda: calls["n"]

        return install

    def test_cancelling_between_definitions_skips_the_later_ones(
        self, col, run_copy_fields, cancel_after
    ):
        first = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        second = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        # Two notes, so the bulk loop asks twice, and the third question is the one
        # `copy_fields` itself asks after the first definition has been merged.
        cancel_after(3)

        run_copy_fields(
            copy_definitions=[
                write_into_note("a", value="written"),
                write_into_note("b", value="7", field="Freq"),
            ],
            note_ids=[first.id, second.id],
        )

        assert col.get_note(first.id)["Note"] == "written"
        assert col.get_note(second.id)["Note"] == "written"
        assert col.get_note(first.id)["Freq"] == ""

    def test_only_the_definitions_that_ran_are_in_the_result_text(
        self, col, run_copy_fields, cancel_after
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        cancel_after(2)

        results = run_copy_fields(
            copy_definitions=[write_into_note("a"), write_into_note("b", field="Freq")],
            note_ids=[note.id],
        )

        assert "<i>a:</i>" in results.get_result_text()
        assert "<i>b:</i>" not in results.get_result_text()
        assert results.get_count() == 1

    def test_the_undo_entry_of_a_cancelled_run_is_still_the_custom_one(
        self, col, run_copy_fields, cancel_after
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        cancel_after(2)

        run_copy_fields(
            copy_definitions=[write_into_note("a"), write_into_note("b", field="Freq")],
            note_ids=[note.id],
        )

        # The break comes after the merge, so the entry the earlier definitions were folded
        # into is still the current undo step rather than a half-finished one.
        assert col.undo_status().undo == "Copy fields with 2 definitions for 1 notes"

    def test_what_a_cancelled_run_already_wrote_can_still_be_undone(
        self, col, run_copy_fields, cancel_after
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        cancel_after(2)

        run_copy_fields(
            copy_definitions=[write_into_note("a"), write_into_note("b", field="Freq")],
            note_ids=[note.id],
        )
        assert col.get_note(note.id)["Note"] == "neko"

        col.undo()

        assert col.get_note(note.id)["Note"] == ""

    def test_cancelling_before_the_first_definition_still_runs_it(
        self, col, run_copy_fields, cancel_after
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        # The very first question comes from the bulk loop, *after* the first note has been
        # processed -- there is no check before any work is done.
        cancel_after(1)

        run_copy_fields(copy_definitions=[write_into_note("a")], note_ids=[note.id])

        assert col.get_note(note.id)["Note"] == "neko"
