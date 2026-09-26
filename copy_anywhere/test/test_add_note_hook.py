"""Characterization tests for `run_copy_fields_on_add`: the `note_will_be_added` handler.

`init_note_hooks` registers this as `note_will_be_added.append(lambda _col, note, deck_id: ...)`,
and `Collection.add_note` fires that hook *before* it hands the note to the backend. So the
handler is a plain function over `(note, deck_id)` that needs `mw` and a collection but not a
running Anki, and every test here calls it directly and then, where the point is what the
database ends up with, performs the add itself with `col.add_note`. That ordering is not a
convenience -- it is the ordering the real code sees, and the undo assertions depend on it.

**No `CollectionOp` stand-in is needed here.** The sibling file `test_copy_fields_op.py` has
to swap `CollectionOp` out because `copy_fields` runs its work inside one. This handler
deliberately does not go through `copy_fields` -- the source comment says doing so would
raise "bug: run_in_background not called from main thread" -- and calls
`copy_for_single_trigger_note` directly for both its branches, doing its own
`add_custom_undo_entry` / `update_notes` / `merge_undo_entries` around the deferred one.

**Where the copy definitions come from.** `Config.load()` reads
`mw.addonManager.getConfig("copy_anywhere")`, the tag being the first dotted segment of the
addon module name. The `col` fixture puts a fresh `DEFAULT_CONFIG` dict at
`stub_mw.addonManager.configs["copy_anywhere"]` for every test, so a test supplies its
definitions by assigning that dict's `"copy_definitions"` key -- what `set_definitions` does.
The handler re-reads the config on every call, so definitions can be swapped mid-test.

Two things about the note being added shape almost everything below: its `id` is `0` and it
has no cards yet. The first makes a query stage's `nid:` search vacuous and interpolates
`__Note_ID` as `"0"` -- a search *condition*, which would be `nid:0` too, is judged against the
note itself instead; the second means the deck whitelist, and a condition's `deck:` term, have
nothing to read a deck from, which is what the `deck_id` argument exists to supply.
"""

from contextlib import contextmanager

import pytest
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import CLOZE, KANJI, VOCAB
from copy_anywhere.hooks import note_hooks
from copy_anywhere.hooks.note_hooks import (
    get_copy_definitions_for_add_note,
    run_copy_fields_on_add,
)
from copy_anywhere.logic.rename_reconcile import BROKEN_KEY

ADDON_TAG = "copy_anywhere"


@pytest.fixture
def set_definitions(col):
    """Put copy definitions where `Config.load()` will find them."""

    def apply(*definitions, log_level=None):
        config = mw.addonManager.configs[ADDON_TAG]
        config["copy_definitions"] = list(definitions)
        if log_level is not None:
            config["log_level"] = log_level

    return apply


@pytest.fixture
def hook_logger(logger, monkeypatch):
    """Capture the level the handler opens its operation log at.

    The handler reads `config.log_level` itself rather than taking a logger, so wrapping
    `operation_logging` in the module is the only way to see what level it asked for -- and
    replacing it keeps the test from writing a log file at all. What was logged is on the
    `logger` fixture's handler, which is attached to the same loggers the handler uses.
    """
    levels: list[str] = []

    @contextmanager
    def record(name, level):
        levels.append(level)
        yield None

    monkeypatch.setattr(note_hooks, "operation_logging", record)
    logger.levels = levels  # type: ignore[attr-defined]
    return logger


@pytest.fixture
def ran(monkeypatch):
    """Record which definitions reached `copy_for_single_trigger_note`, and how.

    Filtering is otherwise only observable through its effects, which conflates "the
    definition was filtered out" with "the definition ran and did nothing".
    """
    calls: list[dict] = []
    original = note_hooks.copy_for_single_trigger_note

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(note_hooks, "copy_for_single_trigger_note", spy)

    class Calls:
        def names(self) -> list[str]:
            return [call["copy_definition"]["definition_name"] for call in calls]

        def deck_ids(self) -> list:
            return [call.get("deck_id") for call in calls]

        def collects_into_notes(self) -> list[bool]:
            return [call.get("copied_into_notes") is not None for call in calls]

    return Calls()


def new_note(col, note_type=VOCAB, **fields):
    """A note that has not been added: `id` 0, no cards -- what the hook is handed."""
    model = col.models.by_name(note_type)
    assert model is not None
    note = col.new_note(model)
    for field_name, value in fields.items():
        note[field_name] = value
    return note


def deck(col, name="Other"):
    return col.decks.id_for_name(name)


def within(name="within", field="Note", value="{{Word}}", **extra):
    return d.within_note(
        definition_name=name,
        copy_on_add=True,
        field_to_field_defs=[d.field_to_field(field, value)],
        **extra,
    )


def to_destinations(name="s2d", field="Note", value="copied", query="Word:neko", **extra):
    """Across notes, trigger note as source: the query picks the notes that get written."""
    return d.source_to_destinations(
        definition_name=name,
        copy_on_add=True,
        copy_from_cards_query=query,
        # Every matching card, rather than one picked out of the result: a two-template note
        # type otherwise contributes one of its two cards and the count is a coin toss.
        select_card_count="0",
        field_to_field_defs=[d.field_to_field(field, value)],
        **extra,
    )


def to_sources(name="d2s", field="Note", value="{{Keyword}}", query="Kanji:neko", **extra):
    """Across notes, trigger note as destination: the query picks the notes read from."""
    return d.destination_to_sources(
        definition_name=name,
        copy_on_add=True,
        copy_from_cards_query=query,
        field_to_field_defs=[d.field_to_field(field, value)],
        **extra,
    )


class TestWhichDefinitionsRun:
    def test_a_definition_whose_note_type_matches_runs(self, col, set_definitions, ran):
        set_definitions(within())
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == ["within"]

    def test_a_definition_for_another_note_type_does_not_run(self, col, set_definitions, ran):
        set_definitions(within(note_types=[KANJI]))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == []

    def test_copy_on_add_false_does_not_run(self, col, set_definitions, ran):
        definition = within()
        definition["copy_on_add"] = False
        set_definitions(definition)
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == []

    def test_a_missing_copy_on_add_key_does_not_run(self, col, set_definitions, ran):
        definition = within()
        del definition["copy_on_add"]
        set_definitions(definition)
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == []

    def test_copy_on_add_is_read_for_truthiness_not_for_being_true(
        self, col, set_definitions, ran
    ):
        # The editor only ever writes a bool, but the gate is `if not copy_on_add`, so a
        # hand-edited config with a string in it runs rather than being rejected.
        definition = within()
        definition["copy_on_add"] = "yes"
        set_definitions(definition)
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == ["within"]

    def test_an_empty_note_type_list_does_not_run(self, col, set_definitions, ran):
        # An empty whitelist means "no note types", not "all note types" -- the opposite of
        # what `only_copy_into_decks` does with the same empty value.
        set_definitions(within(copy_into_note_types=""))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == []

    def test_a_none_note_type_list_does_not_run(self, col, set_definitions, ran):
        set_definitions(within(copy_into_note_types=None))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == []

    def test_a_missing_note_type_key_does_not_run(self, col, set_definitions, ran):
        definition = within()
        del definition["copy_into_note_types"]
        set_definitions(definition)
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == []

    def test_one_of_several_listed_note_types_is_enough(self, col, set_definitions, ran):
        set_definitions(within(note_types=[KANJI, VOCAB]))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == ["within"]

    def test_surrounding_quotes_are_stripped_off_the_stored_list(
        self, col, set_definitions, ran
    ):
        set_definitions(within(copy_into_note_types='"CA Vocab"'))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == ["within"]

    def test_a_plainly_comma_separated_list_matches_nothing(self, col, set_definitions, ran):
        # The split is on the exact three characters `", "`, so a list written the obvious
        # way stays one long name and can never match.
        set_definitions(within(copy_into_note_types="CA Vocab, CA Kanji"))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == []

    def test_a_leading_space_stops_the_name_matching(self, col, set_definitions, ran):
        # `strip('""')` strips quotes and nothing else; names are compared with `in`, exactly.
        set_definitions(within(copy_into_note_types=" CA Vocab"))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == []

    def test_a_note_with_no_note_type_stops_the_handler_before_any_definition(
        self, col, set_definitions, ran
    ):
        set_definitions(within())
        note = new_note(col, Word="neko")
        note.note_type = lambda: None  # type: ignore[method-assign]
        assert run_copy_fields_on_add(note, deck(col)) is None
        assert ran.names() == []

    def test_definitions_run_in_config_order(self, col, set_definitions, ran):
        set_definitions(within("first"), within("second", field="Meaning"))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == ["first", "second"]

    def test_the_handler_returns_none_whatever_happened(self, col, set_definitions):
        set_definitions(within())
        assert run_copy_fields_on_add(new_note(col, Word="neko"), deck(col)) is None


# The filtering corpus, shared by the handler and by the dead helper below. Each case is a
# definition and whether the rule lets it through for a `CA Vocab` note.
FILTER_CASES = [
    ("matching note type", within(), True),
    ("other note type", within(note_types=[KANJI]), False),
    ("copy_on_add false", {**within(), "copy_on_add": False}, False),
    ("copy_on_add absent", {k: v for k, v in within().items() if k != "copy_on_add"}, False),
    ("copy_on_add zero", {**within(), "copy_on_add": 0}, False),
    ("copy_on_add truthy string", {**within(), "copy_on_add": "yes"}, True),
    ("empty note types", within(copy_into_note_types=""), False),
    ("none note types", within(copy_into_note_types=None), False),
    (
        "note types absent",
        {k: v for k, v in within().items() if k != "copy_into_note_types"},
        False,
    ),
    ("two note types", within(note_types=[KANJI, VOCAB]), True),
    ("quoted note types", within(copy_into_note_types='"CA Vocab"'), True),
    ("plain comma list", within(copy_into_note_types="CA Vocab, CA Kanji"), False),
    ("leading space", within(copy_into_note_types=" CA Vocab"), False),
    ("across notes", to_destinations(), True),
    ("across notes not on add", {**to_destinations(), "copy_on_add": False}, False),
]


class TestTheHelperIsTheOnlyCopyOfTheRule:
    """`get_copy_definitions_for_add_note` is what `run_copy_fields_on_add` now selects with.

    It used to be dead code duplicating the handler's own filtering statement for statement.
    These tests were written against both rules at once, and the answers matched on every
    case below, which is what made the wiring safe to do. They are kept as they are so that
    the two can never drift apart again -- the handler is driven end to end, the helper
    directly, and each case asserts they still agree.
    """

    @pytest.mark.parametrize(
        "definition, expected", [(case, expected) for _, case, expected in FILTER_CASES],
        ids=[name for name, _, _ in FILTER_CASES],
    )
    def test_the_helper_selects_exactly_what_the_handler_runs(
        self, col, set_definitions, ran, definition, expected
    ):
        set_definitions(definition)
        note = new_note(col, Word="neko")
        helper = [
            selected["definition_name"] for selected in get_copy_definitions_for_add_note(note)
        ]
        run_copy_fields_on_add(note, deck(col))
        assert helper == ran.names()
        assert bool(helper) is expected

    def test_neither_rule_survives_a_note_type_list_that_is_not_a_string(
        self, col, set_definitions
    ):
        # Both reach for `.strip` unguarded, so a config holding a real list -- which is what
        # the stored format is trying to be -- takes down the add in both.
        set_definitions(within(copy_into_note_types=[VOCAB]))
        note = new_note(col, Word="neko")
        with pytest.raises(AttributeError):
            get_copy_definitions_for_add_note(note)
        with pytest.raises(AttributeError):
            run_copy_fields_on_add(note, deck(col))

    def test_both_rules_give_up_on_a_note_with_no_note_type(self, col, set_definitions, ran):
        set_definitions(within())
        note = new_note(col, Word="neko")
        note.note_type = lambda: None  # type: ignore[method-assign]
        assert get_copy_definitions_for_add_note(note) == []
        run_copy_fields_on_add(note, deck(col))
        assert ran.names() == []

    def test_the_helper_returns_one_flat_list_in_config_order(
        self, col, set_definitions, ran
    ):
        # The single behavioural difference: the helper does not partition on
        # `definition_modifies_other_notes`, so a caller that switched to it would run the
        # deferred definition first -- and outside the undo entry the handler builds for it.
        set_definitions(to_destinations("deferred"), within("direct"))
        note = new_note(col, Word="neko")
        helper = [
            selected["definition_name"] for selected in get_copy_definitions_for_add_note(note)
        ]
        run_copy_fields_on_add(note, deck(col))
        assert helper == ["deferred", "direct"]
        assert ran.names() == ["direct", "deferred"]

    def test_the_helper_does_not_apply_the_deck_whitelist(self, col, set_definitions):
        # It used to take a deck_id and never read it. The parameter is gone, but the
        # behaviour it implied never existed: selection is note type only, and the deck
        # whitelist is applied a layer down, inside copy_for_single_trigger_note.
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = new_note(col, Word="neko")
        assert len(get_copy_definitions_for_add_note(note)) == 1


class TestAWithinNoteDefinitionOnAdd:
    def test_the_note_is_mutated_in_place_before_the_add(self, col, set_definitions):
        set_definitions(within(value="{{Word}}-x"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert note["Note"] == "neko-x"
        assert note.id == 0

    def test_the_value_reaches_the_database_through_the_add_itself(self, col, set_definitions):
        # The handler never writes the trigger note. It works only because the object it
        # mutates is the same object `add_note` is about to hand to the backend.
        set_definitions(within())
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))
        assert col.get_note(note.id)["Note"] == "neko"

    def test_no_undo_entry_is_added(self, col, set_definitions):
        before = col.undo_status()
        set_definitions(within())
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        after = col.undo_status()
        assert (after.last_step, after.undo) == (before.last_step, before.undo)

    def test_add_note_is_the_only_undo_entry_the_whole_add_leaves(self, col, set_definitions):
        set_definitions(within())
        note = new_note(col, Word="neko")
        before = col.undo_status().last_step
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))
        assert col.undo_status().undo == "Add Note"
        assert col.undo_status().last_step == before + 1

    def test_tags_are_applied_in_place_and_survive_the_add(self, col, set_definitions):
        set_definitions(within(add_tags="added"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))
        assert col.get_note(note.id).tags == ["added"]

    def test_it_works_on_a_cloze_note_type_too(self, col, set_definitions):
        set_definitions(
            within(note_types=[CLOZE], field="Extra", value="{{Text}}")
        )
        note = new_note(col, CLOZE, Text="{{c1::neko}}")
        run_copy_fields_on_add(note, deck(col))
        assert note["Extra"] == "{{c1::neko}}"

    def test_a_failing_definition_does_not_stop_the_next_one(
        self, col, set_definitions, hook_logger
    ):
        # `copy_for_single_trigger_note` returns False here and the handler discards the
        # return value, so nothing about the add records that a definition failed.
        set_definitions(within("broken", field="Nonexistent"), within("ok", field="Meaning"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert note["Meaning"] == "neko"
        assert hook_logger.has_error("not found in note")

    def test_the_handler_opens_its_operation_log_at_the_configured_level(
        self, col, set_definitions, hook_logger
    ):
        set_definitions(within(), log_level="debug")
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert hook_logger.levels == ["debug"]


class TestADefinitionBrokenByARenameOnAdd:
    def test_it_is_not_run_and_the_note_is_still_added(self, col, set_definitions, hook_logger):
        # The refusal is the definition failing, which the handler already survives: only
        # that definition's changes are dropped, and the add goes ahead with the rest.
        broken = d.staged(
            "broken",
            stages=[d.edit_note("trigger", fields=[d.write("Note", d.text("broken ran"))])],
            on_add=True,
        )
        broken[BROKEN_KEY] = [{"field": "Word", "message": 'Field "Word" is no longer here'}]
        whole = d.staged(
            "whole",
            stages=[
                d.edit_note("trigger", fields=[d.write("Meaning", d.text("{{trigger.Word}}"))])
            ],
            on_add=True,
        )
        set_definitions(broken, whole)
        note = new_note(col, Word="neko")

        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))

        stored = col.get_note(note.id)
        assert (stored["Note"], stored["Meaning"]) == ("", "neko")
        assert hook_logger.has_error(
            "'broken' was not run: Field \"Word\" is no longer here."
        )


class TestTheDeckWhitelistNeedsTheDeckId:
    def test_the_deck_id_is_passed_to_every_definition(self, col, set_definitions, ran):
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(within("direct"), to_destinations("deferred"))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col, "JP vocab"))
        assert ran.deck_ids() == [deck(col, "JP vocab")] * 2

    def test_a_whitelisted_deck_lets_the_copy_through(self, col, set_definitions):
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col, "JP vocab"))
        assert note["Note"] == "neko"

    def test_a_deck_outside_the_whitelist_skips_the_copy(self, col, set_definitions):
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col, "Other"))
        assert note["Note"] == ""

    def test_include_subdecks_reaches_a_grandchild_deck(self, col, set_definitions):
        set_definitions(
            within(only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=True)
        )
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col, "JP vocab::10-80::x"))
        assert note["Note"] == "neko"

    def test_no_deck_id_at_all_defeats_the_whitelist_entirely(self, col, set_definitions):
        # With `deck_id` None the whitelist step falls back to the note's cards, and a note
        # being added has none -- so the empty list short-circuits the guard and every
        # definition passes. Nothing in the add path supplies None, but AnkiConnect and any
        # other caller of this handler could.
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, None)  # type: ignore[arg-type]
        assert note["Note"] == "neko"

    def test_the_whitelist_also_gates_the_deferred_branch(self, col, set_definitions):
        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(to_destinations(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col, "Other"))
        assert col.get_note(other.id)["Note"] == ""
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col, "JP vocab"))
        assert col.get_note(other.id)["Note"] == "copied"


class TestTheNoteIdIsZeroThroughout:
    def test_note_id_interpolates_as_zero(self, col, set_definitions):
        set_definitions(within(value="id={{__Note_ID}}"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert note["Note"] == "id=0"

    def test_a_format_1_condition_query_is_judged_against_the_note_being_added(
        self, col, set_definitions, hook_logger
    ):
        # Searched as `(<query>) nid:0` the condition could never match, and every
        # definition carrying one was dead on the add path. The migrated condition is judged
        # against the unsaved note instead, so one that plainly holds, as this one does, runs.
        set_definitions(within(copy_condition_query="Word:neko"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert hook_logger.errors == []
        assert note["Note"] == "neko"

    def test_a_format_1_condition_query_that_does_not_hold_still_skips(
        self, col, set_definitions, hook_logger
    ):
        set_definitions(within(copy_condition_query="Word:inu"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert hook_logger.errors == []
        assert note["Note"] == ""

    def test_the_same_condition_matches_once_the_note_really_exists(self, col, set_definitions):
        # The contrast: the collection's own search gives the same answer once the note is
        # saved, which is the answer the add path has to give before it is.
        from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note

        note = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        copy_for_single_trigger_note(within(copy_condition_query="Word:neko"), note)
        assert note["Note"] == "neko"

    def test_condition_only_on_sync_leaves_the_condition_unchecked_on_add(
        self, col, set_definitions
    ):
        # `is_sync` is left at its default False here, so this flag turns the check off and
        # the definition runs, although the condition does not hold for this note.
        set_definitions(within(copy_condition_query="Word:inu", condition_only_on_sync=True))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert note["Note"] == "neko"

    def test_condition_only_on_sync_is_also_how_to_keep_an_unjudgeable_condition(
        self, col, set_definitions, hook_logger
    ):
        # `is:new` needs the note's cards, which fails the definition on add (see
        # `TestASearchConditionOnTheNoteBeingAdded`); unchecked, it is never judged at all.
        set_definitions(within(copy_condition_query="is:new", condition_only_on_sync=True))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert hook_logger.errors == []
        assert note["Note"] == "neko"

    def test_a_cards_query_interpolating_the_note_id_searches_for_nid_zero(
        self, col, set_definitions
    ):
        # A query stage's search goes to the collection as written -- only a condition is
        # judged against the note itself -- and `-nid:0` is a real search that excludes
        # nothing, which is the only reason the usual "exclude myself" idiom still works.
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"}, deck_name="Other")
        set_definitions(to_sources(query="-nid:{{__Note_ID}} Kanji:neko"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert note["Note"] == "cat"


class TestASearchConditionOnTheNoteBeingAdded:
    """A format-2 search condition is judged against the note being added.

    `nid:0` finds nothing, so the executor answers the search from the unsaved note's fields,
    tags and note type and from the deck the add goes into -- the answer the collection gives
    once the note is saved. A term that needs what the note does not have yet (cards, review
    history, an id) fails the definition by name, and the note is left as it was typed.
    """

    @staticmethod
    def gated(search, *before, name="gated"):
        """Writes `Note` when `search` holds for the trigger, after the `before` stages."""
        return d.staged(
            name,
            on_add=True,
            stages=[
                *before,
                d.condition(
                    d.text(search),
                    [d.edit_note("trigger", [d.write("Note", d.text("matched"))])],
                    predicate_kind="note_query",
                    predicate_target={"binding": "trigger"},
                ),
            ],
        )

    def test_a_matching_search_condition_runs_on_add(self, col, set_definitions, hook_logger):
        set_definitions(self.gated("Word:neko"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col, "JP vocab"))
        assert hook_logger.errors == []
        assert note["Note"] == "matched"

    def test_the_answer_is_the_one_the_saved_note_gets(self, col, set_definitions):
        search = 'Word:ne* tag:jp note:"CA Vocab" -Meaning:dog'
        set_definitions(self.gated(search))
        note = new_note(col, Word="neko", Meaning="cat")
        note.tags = ["jp::n5"]
        run_copy_fields_on_add(note, deck(col, "JP vocab"))
        assert note["Note"] == "matched"

        col.add_note(note, deck(col, "JP vocab"))
        assert list(col.find_notes(f"({search}) nid:{note.id}")) == [note.id]

    def test_a_search_condition_that_does_not_hold_skips_on_add(
        self, col, set_definitions, hook_logger
    ):
        set_definitions(self.gated("Word:inu"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert hook_logger.errors == []
        assert note["Note"] == ""

    def test_a_term_that_needs_the_cards_fails_the_definition_by_name(
        self, col, set_definitions, hook_logger
    ):
        # The write before the condition is undone with the rest of the failed run, so the
        # add saves the note as it was typed.
        set_definitions(
            self.gated(
                "Word:neko is:due",
                d.edit_note(
                    "trigger",
                    [d.write("Meaning", d.text("written"))],
                    tags={"add": ["tagged"], "remove": ["kept"]},
                ),
            )
        )
        note = new_note(col, Word="neko")
        note.tags = ["kept"]
        run_copy_fields_on_add(note, deck(col))

        assert hook_logger.has_error(
            "the search term 'is:due' cannot be judged for a note that is not added yet"
        ), hook_logger.errors
        assert (note["Meaning"], note["Note"]) == ("", "")
        assert note.tags == ["kept"]

    def test_a_search_anki_would_refuse_fails_the_definition_saying_so(
        self, col, set_definitions, hook_logger
    ):
        set_definitions(self.gated("Word:neko)"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        assert hook_logger.has_error("Anki would refuse the search"), hook_logger.errors
        assert note["Note"] == ""

    def test_a_deck_condition_reads_the_deck_being_added_to(self, col, set_definitions):
        # Subdecks included, as Anki's `deck:` search does.
        set_definitions(self.gated('deck:"JP vocab"'))
        into_subdeck = new_note(col, Word="neko")
        run_copy_fields_on_add(into_subdeck, deck(col, "JP vocab::10-80::x"))
        elsewhere = new_note(col, Word="neko")
        run_copy_fields_on_add(elsewhere, deck(col, "Other"))

        assert into_subdeck["Note"] == "matched"
        assert elsewhere["Note"] == ""

    def test_a_deck_condition_with_no_deck_to_read_fails_the_definition(
        self, col, set_definitions, hook_logger
    ):
        # A caller of the handler that gives no deck (see
        # `test_no_deck_id_at_all_defeats_the_whitelist_entirely`): nothing says where the
        # note goes, so a `deck:` term has no answer. Other terms are still judged.
        set_definitions(self.gated('deck:"JP vocab"'), self.gated("Word:neko", name="other"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, None)  # type: ignore[arg-type]
        assert hook_logger.has_error("the deck the note goes into is not known")
        assert note["Note"] == "matched"

    def test_the_deck_reaches_a_definition_that_also_edits_other_notes(
        self, col, set_definitions
    ):
        # Such a definition runs in the handler's second, undoable pile, which is handed the
        # same `deck_id`.
        other = real_anki.add_note(col, VOCAB, {"Word": "inu"}, deck_name="Other")
        definition = d.staged(
            "reaching",
            on_add=True,
            stages=[
                d.condition(
                    d.text('deck:"JP vocab"'),
                    [
                        d.note_query("found", "Word:inu"),
                        d.for_each_note(
                            "found", [d.edit_note("note", [d.write("Note", d.text("x"))])]
                        ),
                    ],
                    predicate_kind="note_query",
                    predicate_target={"binding": "trigger"},
                ),
            ],
        )
        assert definition["effects"]["add_note_compatible"] is False
        set_definitions(definition)

        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col, "Other"))
        assert col.get_note(other.id)["Note"] == ""
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col, "JP vocab"))
        assert col.get_note(other.id)["Note"] == "x"


class TestTheDeferredOtherNotesBranch:
    def test_other_notes_are_written_to_the_database_by_the_handler(
        self, col, set_definitions
    ):
        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(to_destinations())
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert col.get_note(other.id)["Note"] == "copied"

    def test_the_deferred_definitions_run_after_the_direct_ones(
        self, col, set_definitions, ran
    ):
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(to_destinations("deferred"), within("direct"))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == ["direct", "deferred"]

    def test_only_the_deferred_definitions_collect_notes_to_update(
        self, col, set_definitions, ran
    ):
        # `copied_into_notes` is what later becomes the `update_notes` argument, and the
        # direct branch passes none -- which is why a direct definition can only ever affect
        # the trigger note.
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(within("direct"), to_destinations("deferred"))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.collects_into_notes() == [False, True]

    def test_it_creates_exactly_one_undo_entry_naming_the_definition(
        self, col, set_definitions
    ):
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(to_destinations())
        before = col.undo_status().last_step
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        status = col.undo_status()
        assert status.undo == "Copy fields (s2d) for 1 notes triggered by adding note"
        assert status.last_step == before + 1

    def test_several_deferred_definitions_share_one_undo_entry(self, col, set_definitions):
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(to_destinations("a"), to_destinations("b", field="Meaning"))
        before = col.undo_status().last_step
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        status = col.undo_status()
        assert status.undo == "Copy fields with 2 definitions for 1 notes triggered by adding note"
        assert status.last_step == before + 1

    def test_the_note_count_in_the_undo_text_is_always_one(self, col, set_definitions):
        # It is the trigger notes that are counted, not the notes written into: this path
        # only ever has the one being added, however many notes the query found.
        for word in ["neko", "inu"]:
            real_anki.add_note(col, VOCAB, {"Word": word}, deck_name="Other")
        set_definitions(to_destinations(query="Word:neko OR Word:inu"))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert "for 1 notes" in col.undo_status().undo

    def test_no_undo_entry_is_created_when_the_query_matched_nothing(
        self, col, set_definitions
    ):
        # The entry is only opened once `copied_into_notes` is known to hold a real note, so
        # a deferred definition that wrote nothing leaves no empty step on the undo stack.
        set_definitions(to_destinations(query="Word:nothing-matches-this"))
        before = col.undo_status()
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        after = col.undo_status()
        assert (after.last_step, after.undo) == (before.last_step, before.undo)

    def test_no_undo_entry_is_created_when_the_deck_whitelist_rejected_the_note(
        self, col, set_definitions
    ):
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(to_destinations(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        before = col.undo_status()
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col, "Other"))
        after = col.undo_status()
        assert (after.last_step, after.undo) == (before.last_step, before.undo)

    def test_the_undo_entry_lands_before_add_note_so_undoing_once_removes_the_new_note(
        self, col, set_definitions
    ):
        # The issue predicted one undo would revert the other notes and leave the new note's
        # own fields intact. It does not, and cannot: the hook runs before the note exists,
        # so its entry is *older* than "Add Note" and the first undo is the add. The source
        # comment at note_hooks.py:125 acknowledges the ordering; the user-facing cost is
        # that reverting the copy means first destroying the note that triggered it.
        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(to_destinations())
        note = new_note(col, Word="neko", Meaning="mine")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))
        assert col.undo_status().undo == "Add Note"

        col.undo()
        assert col.undo_status().undo == "Copy fields (s2d) for 1 notes triggered by adding note"
        assert col.get_note(other.id)["Note"] == "copied"

        col.undo()
        assert col.get_note(other.id)["Note"] == ""

    def test_a_within_note_definition_alongside_keeps_its_value_through_both_undos(
        self, col, set_definitions
    ):
        # What the ordering does buy: the trigger note's own fields are written by the add,
        # not by the copy entry, so the copy entry's undo cannot touch them.
        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(within("direct", field="Meaning"), to_destinations("deferred"))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))
        assert col.get_note(note.id)["Meaning"] == "neko"

        col.undo()
        col.undo()
        assert col.get_note(other.id)["Note"] == ""

    def test_two_definitions_writing_different_fields_of_one_other_note_both_survive(
        self, col, set_definitions
    ):
        # Definition "b" fetches the destination from the database, so "a"'s edit has to be
        # written before "b" runs, or "b"'s copy of the note overwrites it.
        other = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        set_definitions(
            to_destinations("a", field="Meaning", value="AAA"),
            to_destinations("b", field="Note", value="BBB"),
        )
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        reloaded = col.get_note(other.id)
        assert (reloaded["Meaning"], reloaded["Note"]) == ("AAA", "BBB")

    def test_the_per_definition_writes_still_undo_as_one_entry(self, col, set_definitions):
        other = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        set_definitions(
            to_destinations("a", field="Meaning", value="AAA"),
            to_destinations("b", field="Note", value="BBB"),
        )
        before = col.undo_status().last_step
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert col.undo_status().last_step == before + 1

        col.undo()
        reloaded = col.get_note(other.id)
        assert (reloaded["Meaning"], reloaded["Note"]) == ("cat", "")

    def test_an_across_notes_definition_with_no_field_copies_is_still_deferred_and_written(
        self, col, set_definitions, ran
    ):
        # A definition that only tags the notes its query finds edits them all the same, so
        # it has to take the deferred path: the direct one passes no `copied_into_notes` and
        # the tag would be set on an in-memory Note and thrown away.
        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        definition = d.source_to_destinations(
            definition_name="tags-only",
            copy_on_add=True,
            copy_from_cards_query="Word:neko",
            select_card_count="0",
            add_tags="tagged",
        )
        set_definitions(definition)
        before = col.undo_status().last_step
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.collects_into_notes() == [True]
        assert col.get_note(other.id).tags == ["tagged"]
        assert col.undo_status().last_step == before + 1


class TestDestinationToSourcesWritesOnlyTheTriggerNote:
    def test_destination_to_sources_runs_on_the_direct_path_like_a_within_note_definition(
        self, col, set_definitions, ran
    ):
        # Its only destination is the note being added, so there is nothing to wait for
        # and nothing for an undo entry of its own to revert: the add writes the value.
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"}, deck_name="Other")
        set_definitions(to_sources())
        note = new_note(col, Word="neko")
        before = col.undo_status().last_step
        run_copy_fields_on_add(note, deck(col))
        assert ran.collects_into_notes() == [False]
        assert note["Note"] == "cat"
        assert col.undo_status().last_step == before

    def test_the_value_reaches_the_database_through_the_add_itself(self, col, set_definitions):
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"}, deck_name="Other")
        set_definitions(to_sources())
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))
        assert col.get_note(note.id)["Note"] == "cat"

    def test_it_runs_before_a_source_to_destinations_definition_listed_ahead_of_it(
        self, col, set_definitions, ran
    ):
        # Classification, not config order, decides when a definition runs on add.
        real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"}, deck_name="Other")
        set_definitions(to_destinations("writes-out"), to_sources("reads-back", field="Reading"))
        run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))
        assert ran.names() == ["reads-back", "writes-out"]
        assert ran.collects_into_notes() == [False, True]

    def test_a_source_to_destinations_definition_alongside_still_writes_its_note(
        self, col, set_definitions
    ):
        # One of each kind: the trigger note's write goes through the add, the other note's
        # through the deferred `update_notes`.
        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"}, deck_name="Other")
        set_definitions(
            to_sources("reads-back", field="Reading"),
            to_destinations("writes-out"),
        )
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))
        assert col.get_note(other.id)["Note"] == "copied"
        assert col.get_note(note.id)["Reading"] == "cat"

    def test_the_first_undo_takes_the_trigger_note_with_it(self, col, set_definitions):
        # The trigger note's own write goes through the add, not the copy undo entry, so that
        # entry cannot revert it -- but reaching the entry means undoing "Add Note" first, and
        # that removes the note outright, write included.
        from anki.errors import NotFoundError

        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"}, deck_name="Other")
        set_definitions(
            to_sources("reads-back", field="Reading"),
            to_destinations("writes-out"),
        )
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))
        note_id = note.id

        col.undo()
        with pytest.raises(NotFoundError):
            col.get_note(note_id)
        assert col.get_note(other.id)["Note"] == "copied"

        col.undo()
        assert col.get_note(other.id)["Note"] == ""


class TestCardActionsWhileTheNoteHasNoCards:
    """A note being added has id 0 and no cards, so a card action on it has nothing to reach.

    That is not a reason to discard the definition: its field writes land on the note object
    the add is about to save, and its edits to other notes and their cards are real. The
    card action on the note being added is the one thing that cannot run, and the log says
    so rather than nothing.
    """

    def flag(self):
        return d.card_action(VOCAB, "Recognition", set_flag=3)

    def card_named(self, note, template_name):
        return next(card for card in note.cards() if card.template()["name"] == template_name)

    def filling_and_flagging(self, fmt):
        """The definition format 1 ran happily: fills `Meaning` and flags the Recognition card."""
        if fmt == 1:
            return within("fill-and-flag", field="Meaning", card_actions=[self.flag()])
        return d.staged(
            "fill-and-flag",
            on_add=True,
            stages=[
                d.edit_note(
                    "trigger",
                    [d.write("Meaning", d.text("{{trigger.Word}}"))],
                    card_actions=[self.flag()],
                )
            ],
        )

    @pytest.mark.parametrize("fmt", [1, 2])
    def test_the_field_is_filled_and_the_card_action_is_a_logged_skip(
        self, col, set_definitions, hook_logger, fmt
    ):
        set_definitions(self.filling_and_flagging(fmt))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))

        assert note["Meaning"] == "neko"
        assert hook_logger.errors == []
        assert any("no cards yet" in message for message in hook_logger.warnings), (
            hook_logger.warnings
        )

    @pytest.mark.parametrize("fmt", [1, 2])
    def test_the_add_saves_the_field_and_the_card_stays_unflagged(
        self, col, set_definitions, fmt
    ):
        set_definitions(self.filling_and_flagging(fmt))
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))

        assert col.get_note(note.id)["Meaning"] == "neko"
        assert self.card_named(note, "Recognition").user_flag() == 0
        # Nothing was written for the definition, so nothing needed an undo entry of its own
        assert col.undo_status().undo == "Add Note"

    def test_the_rest_of_the_definition_still_lands(self, col, set_definitions, hook_logger):
        # Field write on the trigger, card action on the trigger, and an edit to another
        # note in one definition: only the card action on the note being added is skipped.
        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(
            d.staged(
                "everything",
                on_add=True,
                stages=[
                    d.edit_note(
                        "trigger",
                        [d.write("Meaning", d.text("{{trigger.Word}}"))],
                        card_actions=[self.flag()],
                    ),
                    d.note_query("found", "Word:neko"),
                    d.for_each_note(
                        "found",
                        [
                            d.edit_note(
                                "note",
                                [d.write("Note", d.text("copied"))],
                                card_actions=[self.flag()],
                            )
                        ],
                    ),
                ],
            )
        )
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))

        assert note["Meaning"] == "neko"
        assert col.get_note(other.id)["Note"] == "copied"
        assert self.card_named(other, "Recognition").user_flag() == 3
        assert hook_logger.errors == []
        assert any("no cards yet" in message for message in hook_logger.warnings)

    def test_a_format_1_card_action_on_other_notes_cards_is_written(
        self, col, set_definitions, hook_logger
    ):
        # Card actions only, no field write and no tag: the format-1 fallback used to call
        # this add-note compatible, so the hook ran it under the trigger-only backstop, which
        # refused the queued card edits and discarded the run with an error.
        other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
        set_definitions(
            d.source_to_destinations(
                definition_name="flag-found",
                copy_on_add=True,
                copy_from_cards_query="Word:neko",
                select_card_count="0",
                card_actions=[self.flag()],
            )
        )
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))

        assert hook_logger.errors == []
        assert col.get_card(self.card_named(other, "Recognition").id).user_flag() == 3


class TestAFailedOrRefusedDefinitionLeavesTheNoteAlone:
    """A definition that does not commit leaves the note being added as it found it.

    The stages write into the very `Note` object the Add dialog is about to save, so throwing
    the run's plan away is not enough on this path: without putting the fields and tags back,
    a definition that failed half-way -- or that the add backstop refused, logging that
    nothing was written -- still had its trigger edits saved by the add.
    """

    def refused_claim(self):
        """Writes a file while its stored effects claim it is add-note compatible."""
        definition = d.staged(
            "refused",
            on_add=True,
            stages=[
                d.edit_note(
                    "trigger",
                    [d.write("Meaning", d.text("written"))],
                    tags={"add": ["tagged"], "remove": ["kept"]},
                ),
                d.write_file("log.txt", d.text("x")),
            ],
        )
        assert definition["effects"]["add_note_compatible"] is False
        # What a hand-edited or stale stored flag would say.
        definition["effects"]["add_note_compatible"] = True
        definition["effects"]["writes_files"] = False
        return definition

    def failing(self, name="failing", if_missing="error"):
        """Add-note compatible, and fails on a missing file after writing the trigger."""
        definition = d.staged(
            name,
            on_add=True,
            stages=[
                d.edit_note(
                    "trigger",
                    [d.write("Meaning", d.text("written"))],
                    tags={"add": ["tagged"], "remove": ["kept"]},
                ),
                d.read_file("x", "nope.txt", if_missing=if_missing),
            ],
        )
        assert definition["effects"]["add_note_compatible"] is True
        return definition

    def test_a_refused_definition_leaves_the_fields_and_tags_alone(
        self, col, set_definitions, hook_logger, media_dir
    ):
        set_definitions(self.refused_claim())
        note = new_note(col, Word="neko")
        note.tags = ["kept"]
        run_copy_fields_on_add(note, deck(col))

        assert hook_logger.has_error("nothing was written"), hook_logger.errors
        assert not (media_dir / "_log.txt").exists()
        assert note["Meaning"] == ""
        assert note.tags == ["kept"]

    def test_so_the_add_saves_the_note_as_it_was_typed(self, col, set_definitions, media_dir):
        set_definitions(self.refused_claim())
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))
        col.add_note(note, deck(col))

        saved = col.get_note(note.id)
        assert saved["Word"] == "neko"
        assert saved["Meaning"] == ""
        assert "tagged" not in saved.tags

    def test_a_failed_compatible_definition_leaves_the_fields_and_tags_alone(
        self, col, set_definitions, hook_logger
    ):
        set_definitions(self.failing())
        note = new_note(col, Word="neko")
        note.tags = ["kept"]
        run_copy_fields_on_add(note, deck(col))

        assert hook_logger.has_error("does not exist"), hook_logger.errors
        assert note["Meaning"] == ""
        assert note.tags == ["kept"]

    def test_an_earlier_definitions_write_survives_a_later_ones_failure(
        self, col, set_definitions, hook_logger
    ):
        # The failed run is put back to where *it* started, not to where the add started:
        # the definition before it committed, and undoing that would lose a write.
        set_definitions(within("ok", field="Note", add_tags="from-ok"), self.failing())
        note = new_note(col, Word="neko")
        run_copy_fields_on_add(note, deck(col))

        assert hook_logger.has_error("does not exist"), hook_logger.errors
        assert note["Note"] == "neko"
        assert note["Meaning"] == ""
        assert note.tags == ["from-ok"]

    def test_a_definition_that_succeeds_still_writes(self, col, set_definitions, hook_logger):
        # The same stages, but a missing file reads as empty instead of failing the run.
        set_definitions(self.failing("succeeding", if_missing="empty"))
        note = new_note(col, Word="neko")
        note.tags = ["kept"]
        run_copy_fields_on_add(note, deck(col))

        assert hook_logger.errors == []
        assert note["Meaning"] == "written"
        assert note.tags == ["tagged"]
