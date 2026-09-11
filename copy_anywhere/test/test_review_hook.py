"""Characterization tests for `run_copy_fields_on_review`: the `reviewer_did_answer_card`
handler.

`init_note_hooks` registers it as
`reviewer_did_answer_card.append(lambda reviewer, card, ease: run_copy_fields_on_review(card))`,
so the handler is a plain function over one `Card` that needs `mw` and a collection but not
a running Anki -- every test here calls it directly.

**The review is real.** The hook fires after Anki has rescheduled the card, so the "Answer
Card" undo entry already exists and the card's queue, due and reps have already moved. That
ordering is the whole point of this section: the wrapped `Scheduler.answer_card` records the
answer's `undo_status().last_step`, and the handler folds everything it does into that entry. Faking it with `add_custom_undo_entry` would
pin the folding but not what is being folded into. So `answer_card` below answers the card
through the v3 scheduler for real -- `get_queued_cards` for the scheduling states,
`build_answer`, `sched.answer_card` -- which works headless, and then reloads the `Card`,
because `Reviewer._answerCard` calls `self.card.load()` before firing the hook whenever a
listener is registered. All of that costs under a millisecond; no Qt, no reviewer.

**Where the copy definitions come from.** `Config.load()` reads
`mw.addonManager.getConfig("copy_anywhere")`; the `col` fixture installs a fresh config dict
per test, so a test supplies its definitions by assigning that dict's `"copy_definitions"`
key. Filtering is otherwise invisible, so `ran` spies on
`note_hooks.copy_for_single_trigger_note` to tell "the definition was filtered out" from
"the definition ran and did nothing". Both seams are the ones `test_add_note_hook.py`
established.

**Two things separate this handler from its siblings.** It writes the `fc` custom-data flag
that the `copy_on_sync` pipeline selects on -- and writes it only on the path where at least
one definition matched, which is why `TestNoMatchingDefinitionRunsAtAll` exists. And it is
the only path that has to cope with a copy editing the card currently being reviewed, which
is what `merge_cards` is for: the handler's own closing `update_card(card)` would otherwise
write the pre-copy row back over it.
"""

import json
from types import SimpleNamespace

import pytest
from anki.errors import InvalidInput
from anki.scheduler.v3 import CardAnswer
from anki.scheduler.v3 import Scheduler as V3Scheduler
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import KANJI, VOCAB
from copy_anywhere.hooks import note_hooks
from copy_anywhere.hooks.note_hooks import run_copy_fields_on_review

ADDON_TAG = "copy_anywhere"

ANSWER_CARD = "Answer Card"


# Driving a real review -------------------------------------------------------------------


def answer_card(col, card, rating=CardAnswer.GOOD):
    """Answer `card` for real and return the `Card` the hook would be handed.

    `get_queued_cards` only offers cards from the current deck, hence the `select`; it is
    done before the answer so that "Answer Card" is still the newest undo entry afterwards,
    exactly as it is when the reviewer fires the hook. `start_timer` is not decoration --
    `build_answer` asks the card how long it was shown, and a card that was never displayed
    has no start time to subtract from.
    """
    col.decks.select(card.did)
    queued = col.sched.get_queued_cards(fetch_limit=50)
    states = next(entry.states for entry in queued.cards if entry.card.id == card.id)
    card.start_timer()
    col.sched.answer_card(col.sched.build_answer(card=card, states=states, rating=rating))
    return col.get_card(card.id)


def vocab_note(col, deck="Other", **fields):
    """A CA Vocab note with both of its cards.

    The second template only produces a card while `Meaning` is non-empty, and the cases
    about a card action on another card of the same note need that second card.
    """
    fields.setdefault("Word", "neko")
    fields.setdefault("Meaning", "cat")
    return real_anki.add_note(col, VOCAB, fields, deck_name=deck)


def kanji_note(col, keyword="cat", kanji="neko"):
    """A CA Kanji note the copies read from or write into.

    It goes in a different deck from the reviewed note on purpose: `get_queued_cards` serves
    the current deck, and the v3 scheduler refuses to answer a card that is not at the top of
    that queue, so a new card added before the one under review would take its place.
    """
    return real_anki.add_note(
        col, KANJI, {"Kanji": kanji, "Keyword": keyword}, deck_name="JP vocab"
    )


def review(col, note=None, index=0, rating=CardAnswer.GOOD):
    """Add a CA Vocab note if none was given, answer one of its cards, return both."""
    if note is None:
        note = vocab_note(col)
    return note, answer_card(col, note.cards()[index], rating)


def custom_data(col, card_id) -> dict:
    raw = col.get_card(card_id).custom_data
    return json.loads(raw) if raw else {}


# Fixtures --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def answers_are_tracked(monkeypatch):
    """Wrap `Scheduler.answer_card` as `init_note_hooks` does, and unwrap it afterwards.

    The handler merges into the step the wrapper recorded for the answer, so without it
    every test here would silently run on the newest-step fallback instead. Registering the
    original with `monkeypatch` first is what puts the class back when the test ends.
    """
    monkeypatch.setattr(V3Scheduler, "answer_card", V3Scheduler.answer_card)
    monkeypatch.setattr(note_hooks, "last_answer_undo_step", None)
    note_hooks.track_answer_undo_steps()


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
    """Capture the `Logger` the handler builds for itself.

    The handler constructs `Logger(config.log_level)` rather than taking one, so replacing
    the name in the module is the only way to see what it reported -- and the only way to
    keep a failing definition from printing over the test's output.
    """
    levels: list[str] = []

    def build(level):
        levels.append(level)
        return logger

    monkeypatch.setattr(note_hooks, "Logger", build)
    logger.levels = levels  # type: ignore[attr-defined]
    return logger


@pytest.fixture
def ran(monkeypatch):
    """Record which definitions reached `copy_for_single_trigger_note`, and with what."""
    calls: list[dict] = []
    original = note_hooks.copy_for_single_trigger_note

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(note_hooks, "copy_for_single_trigger_note", spy)

    class Calls:
        def names(self) -> list[str]:
            return [call["copy_definition"]["definition_name"] for call in calls]

        def kwarg_names(self) -> list[list[str]]:
            return [sorted(call) for call in calls]

        def trigger_note_ids(self) -> list[int]:
            return [call["trigger_note"].id for call in calls]

    return Calls()


@pytest.fixture
def updates(col, monkeypatch):
    """Record every `update_notes` / `update_cards` call, then let it through.

    `Collection.update_card` funnels into `update_cards`, so the handler's closing write of
    the reviewed card shows up as the last entry in `cards`.
    """
    log = SimpleNamespace(notes=[], cards=[])
    original_notes = col.update_notes
    original_cards = col.update_cards

    def update_notes(notes, **kwargs):
        log.notes.append([note.id for note in notes])
        return original_notes(notes, **kwargs)

    def update_cards(cards, **kwargs):
        log.cards.append([(card.id, hasattr(card, "edited")) for card in cards])
        return original_cards(cards, **kwargs)

    monkeypatch.setattr(col, "update_notes", update_notes)
    monkeypatch.setattr(col, "update_cards", update_cards)
    return log


# Definition builders ----------------------------------------------------------------------


def within(name="within", field="Note", value="{{Word}}", **extra):
    return d.within_note(
        definition_name=name,
        copy_on_review=True,
        field_to_field_defs=[d.field_to_field(field, value)],
        **extra,
    )


def to_destinations(name="s2d", field="Keyword", value="copied", query="Kanji:neko", **extra):
    """Across notes, the reviewed note as source: the query picks the notes written into."""
    return d.source_to_destinations(
        definition_name=name,
        copy_on_review=True,
        copy_from_cards_query=query,
        # Every matching card rather than one picked out of the result, so a two-template
        # note type does not make the destination a coin toss.
        select_card_count="0",
        field_to_field_defs=[d.field_to_field(field, value)],
        **extra,
    )


def to_sources(name="d2s", field="Note", value="{{Keyword}}", query="Kanji:neko", **extra):
    """Across notes, the reviewed note as destination: the query picks the notes read."""
    return d.destination_to_sources(
        definition_name=name,
        copy_on_review=True,
        copy_from_cards_query=query,
        field_to_field_defs=[d.field_to_field(field, value)],
        **extra,
    )


def on_sync(name="sync", note_types=None, **extra):
    """A definition the handler will not run, but which counts towards the `fc` branch."""
    return d.within_note(
        definition_name=name,
        note_types=note_types or [KANJI],
        copy_on_sync=True,
        field_to_field_defs=[d.field_to_field("Keyword", "synced")],
        **extra,
    )


# The review itself -------------------------------------------------------------------------


class TestTheReviewTheHandlerIsHandedIsReal:
    """A guard on the harness: if these stop holding, nothing below is pinning anything."""

    def test_answering_reschedules_the_card_and_leaves_an_answer_card_undo_entry(self, col):
        note = vocab_note(col)
        before = note.cards()[0]
        assert (before.queue, before.type, before.reps) == (0, 0, 0)

        reviewed = answer_card(col, before)

        assert (reviewed.queue, reviewed.type, reviewed.reps) == (1, 1, 1)
        assert col.undo_status().undo == ANSWER_CARD

    def test_the_review_is_written_to_the_database_not_just_to_the_object(self, col):
        _, reviewed = review(col)
        assert col.get_card(reviewed.id).reps == 1

    def test_undoing_the_answer_puts_the_card_back(self, col):
        _, reviewed = review(col)
        col.undo()
        restored = col.get_card(reviewed.id)
        assert (restored.queue, restored.type, restored.reps) == (0, 0, 0)


# Which definitions run ----------------------------------------------------------------------


class TestWhichDefinitionsRun:
    def test_a_definition_whose_note_type_matches_runs(self, col, set_definitions, ran):
        _, reviewed = review(col)
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert ran.names() == ["within"]

    def test_a_definition_for_another_note_type_does_not_run(self, col, set_definitions, ran):
        _, reviewed = review(col)
        set_definitions(within(note_types=[KANJI]))
        run_copy_fields_on_review(reviewed)
        assert ran.names() == []

    def test_copy_on_review_false_does_not_run(self, col, set_definitions, ran):
        _, reviewed = review(col)
        definition = within()
        definition["copy_on_review"] = False
        set_definitions(definition)
        run_copy_fields_on_review(reviewed)
        assert ran.names() == []

    def test_a_missing_copy_on_review_key_does_not_run(self, col, set_definitions, ran):
        _, reviewed = review(col)
        definition = within()
        del definition["copy_on_review"]
        set_definitions(definition)
        run_copy_fields_on_review(reviewed)
        assert ran.names() == []

    def test_copy_on_review_is_read_for_truthiness_not_for_being_true(
        self, col, set_definitions, ran
    ):
        # The editor only ever writes a bool, but the gate is `if not copy_on_review`, so a
        # hand-edited config with a string in it runs rather than being rejected.
        _, reviewed = review(col)
        definition = within()
        definition["copy_on_review"] = "yes"
        set_definitions(definition)
        run_copy_fields_on_review(reviewed)
        assert ran.names() == ["within"]

    def test_an_empty_note_type_list_does_not_run(self, col, set_definitions, ran):
        _, reviewed = review(col)
        set_definitions(within(copy_into_note_types=""))
        run_copy_fields_on_review(reviewed)
        assert ran.names() == []

    def test_a_none_note_type_list_does_not_run(self, col, set_definitions, ran):
        _, reviewed = review(col)
        set_definitions(within(copy_into_note_types=None))
        run_copy_fields_on_review(reviewed)
        assert ran.names() == []

    def test_one_of_several_listed_note_types_is_enough(self, col, set_definitions, ran):
        _, reviewed = review(col)
        set_definitions(within(note_types=[KANJI, VOCAB]))
        run_copy_fields_on_review(reviewed)
        assert ran.names() == ["within"]

    def test_a_plainly_comma_separated_list_matches_nothing(self, col, set_definitions, ran):
        # The split is on the exact three characters `", "`, so a list written the obvious
        # way stays one long name and can never match. Same rule as the add path.
        _, reviewed = review(col)
        set_definitions(within(copy_into_note_types="CA Vocab, CA Kanji"))
        run_copy_fields_on_review(reviewed)
        assert ran.names() == []

    def test_a_note_type_list_that_is_not_a_string_is_logged_and_skipped(
        self, col, set_definitions, ran, hook_logger
    ):
        # A config holding a real list -- which is what the stored quoted-and-joined format
        # is trying to be -- can't be split, and the answer has already been committed by
        # the time the hook fires. So that definition is logged and skipped rather than
        # raised out of `reviewer_did_answer_card`, and the ones after it still run.
        # (`run_copy_fields_on_add` has the identical hole, pinned in test_add_note_hook.py.)
        _, reviewed = review(col)
        set_definitions(
            within("listed", copy_into_note_types=[VOCAB]),
            within("joined", field="Freq"),
        )
        run_copy_fields_on_review(reviewed)
        assert ran.names() == ["joined"]
        assert hook_logger.has_error("not a string")

    def test_definitions_run_in_config_order(self, col, set_definitions, ran):
        _, reviewed = review(col)
        set_definitions(within("first"), within("second", field="Freq"))
        run_copy_fields_on_review(reviewed)
        assert ran.names() == ["first", "second"]

    def test_a_note_with_no_note_type_stops_the_handler_before_any_definition(
        self, col, set_definitions, ran
    ):
        note, reviewed = review(col)
        broken = col.get_note(note.id)
        broken.note_type = lambda: None  # type: ignore[method-assign]
        reviewed.note = lambda: broken  # type: ignore[method-assign]
        set_definitions(within())
        assert run_copy_fields_on_review(reviewed) is None
        assert ran.names() == []
        assert col.get_card(reviewed.id).custom_data == ""

    def test_the_note_is_read_back_from_the_database_rather_than_taken_from_the_card(
        self, col, set_definitions, ran
    ):
        # `card.note()` is a fresh `col.get_note`, so the handler never sees whatever the
        # reviewer happened to be holding -- which is why an edit made in the reviewer's own
        # editor is picked up here without anything having to invalidate a cache.
        note, reviewed = review(col)
        rewritten = col.get_note(note.id)
        rewritten["Word"] = "inu"
        col.update_note(rewritten)
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "inu"
        assert ran.trigger_note_ids() == [note.id]

    def test_the_handler_passes_neither_a_deck_id_nor_a_sync_flag(
        self, col, set_definitions, ran
    ):
        # Unlike the add path there is no `deck_id` to pass -- the note has real cards, so
        # the deck whitelist reads their decks itself. And `is_sync` is left at its default,
        # which is what makes `condition_only_on_sync` definitions skip their condition here.
        _, reviewed = review(col)
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert ran.kwarg_names() == [
            [
                "copied_into_cards_dict",
                "copied_into_notes",
                "copy_definition",
                "logger",
                "trigger_note",
            ]
        ]

    def test_the_handler_returns_none_whatever_happened(self, col, set_definitions):
        _, reviewed = review(col)
        set_definitions(within())
        assert run_copy_fields_on_review(reviewed) is None


# No matching definitions --------------------------------------------------------------------


class TestNoMatchingDefinitionRunsAtAll:
    """The early return at note_hooks.py:177-178, which skips the `fc` write entirely.

    Worth pinning on its own: the `copy_on_sync` pipeline selects cards by their `fc` value,
    and this path leaves whatever was there untouched without recording that a review
    happened at all.
    """

    def test_an_empty_config_writes_no_custom_data_at_all(self, col, set_definitions):
        _, reviewed = review(col)
        set_definitions()
        run_copy_fields_on_review(reviewed)
        assert col.get_card(reviewed.id).custom_data == ""

    def test_a_definition_for_another_note_type_writes_no_custom_data(
        self, col, set_definitions
    ):
        _, reviewed = review(col)
        set_definitions(within(note_types=[KANJI]))
        run_copy_fields_on_review(reviewed)
        assert col.get_card(reviewed.id).custom_data == ""

    def test_an_existing_flag_is_left_exactly_as_it_was(self, col, set_definitions):
        # `fc = 0` is what the custom scheduler writes to say "a field changed, copy on the
        # next sync". Reviewing a card with no matching definition neither clears it nor
        # downgrades it to -1, so the sync sweep still picks the note up.
        note = vocab_note(col)
        real_anki.set_custom_data(col, note.cards()[0].id, json.dumps({"fc": 0}))
        _, reviewed = review(col, note)
        set_definitions(on_sync())
        run_copy_fields_on_review(reviewed)
        assert custom_data(col, reviewed.id) == {"fc": 0}

    def test_nothing_is_written_and_no_undo_entry_is_touched(self, col, set_definitions):
        _, reviewed = review(col)
        merges = real_anki.counting_wrapper(col, "merge_undo_entries")
        card_updates = real_anki.counting_wrapper(col, "update_card")
        note_updates = real_anki.counting_wrapper(col, "update_notes")
        set_definitions(within(note_types=[KANJI]), on_sync())
        before = col.undo_status()

        run_copy_fields_on_review(reviewed)

        after = col.undo_status()
        assert (merges(), card_updates(), note_updates()) == (0, 0, 0)
        assert (after.undo, after.last_step) == (before.undo, before.last_step)

    def test_the_fc_write_is_gated_on_a_matching_definition_not_on_work_being_done(
        self, col, set_definitions
    ):
        # The mirror image: a definition that matches the note type but is then rejected
        # deeper down -- here by the deck whitelist -- still reaches the `fc` write, because
        # the gate is the note-type filter and nothing else.
        note, reviewed = review(col)
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == ""
        assert custom_data(col, reviewed.id) == {"fc": 1}


# The fc flag ----------------------------------------------------------------------------------


class TestTheSyncFlag:
    """`fc`, in the reviewed card's custom data, is what the `copy_on_sync` sweep selects on.

    `copy_fields` takes `fc = 0` for a plain on-sync definition and `fc IN (0, -1)` for one
    that is on-sync-but-not-on-review, so -1 means "reviewed, and something still has to run
    on the next sync" while 1 means "done".
    """

    def test_no_on_sync_definition_anywhere_gives_the_card_fc_one(self, col, set_definitions):
        _, reviewed = review(col)
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert custom_data(col, reviewed.id) == {"fc": 1}

    def test_an_on_sync_definition_for_a_different_note_type_still_gives_fc_minus_one(
        self, col, set_definitions
    ):
        # The flag is computed from the whole config before any note-type filtering, so a
        # card is marked "still to be processed on sync" on account of a definition that
        # will never look at it. It washes out in the end -- the sync tail resets every
        # stray -1 to 1 -- but `fc` here describes the config, not the card.
        _, reviewed = review(col)
        set_definitions(within(), on_sync(note_types=[KANJI]))
        run_copy_fields_on_review(reviewed)
        assert custom_data(col, reviewed.id) == {"fc": -1}

    def test_an_on_sync_definition_with_no_note_types_at_all_still_gives_fc_minus_one(
        self, col, set_definitions
    ):
        # The `copy_on_sync` branch `continue`s before the note-type list is even read, so a
        # definition that could not match anything still flips the flag.
        _, reviewed = review(col)
        set_definitions(within(), on_sync(copy_into_note_types=None))
        run_copy_fields_on_review(reviewed)
        assert custom_data(col, reviewed.id) == {"fc": -1}

    def test_a_definition_that_is_both_on_review_and_on_sync_gives_fc_one(
        self, col, set_definitions
    ):
        # The issue predicted "any `copy_on_sync` definition in the config". It is narrower
        # than that: the flag is only ever set inside the `if not copy_on_review` branch, so
        # a definition carrying both flags does not count. That turns out to agree with the
        # sync side exactly -- `copy_on_sync_after_review` in copy_fields.py:544 is
        # `not copy_on_review and copy_on_sync`, and only those definitions select on -1.
        _, reviewed = review(col)
        set_definitions(within(copy_on_sync=True))
        run_copy_fields_on_review(reviewed)
        assert custom_data(col, reviewed.id) == {"fc": 1}

    def test_the_on_sync_definition_may_come_before_or_after_the_running_one(
        self, col, set_definitions
    ):
        _, reviewed = review(col)
        set_definitions(on_sync(), within())
        run_copy_fields_on_review(reviewed)
        assert custom_data(col, reviewed.id) == {"fc": -1}

    def test_an_existing_flag_value_is_overwritten_and_other_keys_survive(
        self, col, set_definitions
    ):
        note = vocab_note(col)
        real_anki.set_custom_data(col, note.cards()[0].id, json.dumps({"fc": 0, "s": 3}))
        _, reviewed = review(col, note)
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert custom_data(col, reviewed.id) == {"fc": 1, "s": 3}

    def test_only_the_reviewed_card_is_flagged_not_its_sibling(self, col, set_definitions):
        # The sync sweep joins notes to cards and takes a note if *any* of its cards carries
        # the flag, so a sibling left at 0 keeps the note in the sweep after the reviewed
        # card has been marked done.
        note = vocab_note(col)
        for card in note.cards():
            real_anki.set_custom_data(col, card.id, json.dumps({"fc": 0}))
        _, reviewed = review(col, note)
        sibling = note.cards()[1]
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert custom_data(col, reviewed.id) == {"fc": 1}
        assert custom_data(col, sibling.id) == {"fc": 0}

    def test_the_db_row_holds_the_flag_under_the_cd_key(self, col, set_definitions):
        _, reviewed = review(col)
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        data = col.db.scalar("SELECT data FROM cards WHERE id = ?", reviewed.id)
        # Custom data is JSON nested inside JSON in the `cards.data` column, which is why
        # the sweep's search prefix is `prop:cdn:` rather than a plain column comparison.
        assert json.loads(json.loads(data)["cd"]) == {"fc": 1}

    def test_custom_data_too_large_for_the_flag_is_logged_not_raised_out_of_the_hook(
        self, col, set_definitions, hook_logger
    ):
        # `write_custom_data` raises ValueError once the compressed JSON passes 100 bytes --
        # Anki's own limit. By then the copies have been written and merged, so the handler
        # logs the failure instead of throwing it into Anki's hook dispatch at the reviewer.
        # The copy stands and, with no flag written, the note stays queued for the sync sweep.
        note = vocab_note(col)
        real_anki.set_custom_data(
            col, note.cards()[0].id, json.dumps({"x": "y" * 90}, separators=(",", ":"))
        )
        _, reviewed = review(col, note)
        set_definitions(within())

        run_copy_fields_on_review(reviewed)

        assert hook_logger.has_error("exceeds 100 bytes")
        assert col.get_note(note.id)["Note"] == "neko"
        assert "fc" not in custom_data(col, reviewed.id)


# Undo merging ------------------------------------------------------------------------------


class TestEverythingMergesIntoTheAnswerCardEntry:
    def test_one_undo_reverts_both_the_answer_and_the_copy(self, col, set_definitions):
        note, reviewed = review(col)
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "neko"

        col.undo()

        restored = col.get_card(reviewed.id)
        assert (restored.queue, restored.reps) == (0, 0)
        assert restored.custom_data == ""
        assert col.get_note(note.id)["Note"] == ""

    def test_the_undo_entry_keeps_its_answer_card_name(self, col, set_definitions):
        # `update_notes` and `update_cards` each open an entry of their own -- "Update Note",
        # "Update Card" -- and the merges fold them back in, so the user never sees the
        # addon's writes named in the undo menu at all.
        kanji_note(col)
        _, reviewed = review(col)
        set_definitions(within(), to_destinations())
        before = col.undo_status().last_step

        run_copy_fields_on_review(reviewed)

        after = col.undo_status()
        assert after.undo == ANSWER_CARD
        assert after.last_step == before

    def test_a_copy_into_another_note_is_reverted_by_the_same_single_undo(
        self, col, set_definitions
    ):
        target = kanji_note(col, keyword="old")
        _, reviewed = review(col)
        set_definitions(to_destinations())
        run_copy_fields_on_review(reviewed)
        assert col.get_note(target.id)["Keyword"] == "copied"

        col.undo()

        assert col.get_note(target.id)["Keyword"] == "old"
        assert col.get_card(reviewed.id).reps == 0

    def test_the_merge_runs_once_per_definition_and_once_more_at_the_end(
        self, col, set_definitions
    ):
        # The source comment in `copy_fields` warns that skipping a merge produces "target
        # undo op not found" on the next one, which is why it is re-issued every iteration
        # rather than only after the loop.
        _, reviewed = review(col)
        merges = real_anki.counting_wrapper(col, "merge_undo_entries")
        set_definitions(within("a"), within("b", field="Freq"), within("c", field="Reading"))
        run_copy_fields_on_review(reviewed)
        assert merges() == 4

    def test_the_merge_target_is_the_answer_entry_even_when_it_is_not_the_newest(
        self, col, set_definitions
    ):
        # `reviewer_did_answer_card` listeners run in registration order, so another addon's
        # listener may have added an undo entry before this one runs. The step recorded when
        # the card was answered is still the target, so one undo takes back the review and
        # the copies together. Anki merges every step newer than the target, so the other
        # addon's entry is folded in as well.
        note, reviewed = review(col)
        col.add_custom_undo_entry("Some other addon")
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert col.undo_status().undo == ANSWER_CARD

        col.undo()

        assert col.get_note(note.id)["Note"] == ""
        assert col.get_card(reviewed.id).reps == 0

    def test_without_a_recorded_answer_the_newest_entry_is_the_target(
        self, col, set_definitions, monkeypatch
    ):
        # The fallback, for a hook fired without the wrapped scheduler seeing the answer --
        # as the real-Anki suite does when it fires `reviewer_did_answer_card` directly.
        note, reviewed = review(col)
        monkeypatch.setattr(note_hooks, "last_answer_undo_step", None)
        col.add_custom_undo_entry("Some other addon")
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert col.undo_status().undo == "Some other addon"

        col.undo()

        assert col.get_note(note.id)["Note"] == ""
        assert col.get_card(reviewed.id).reps == 1

    def test_an_answer_recorded_for_another_card_is_not_used(self, col, set_definitions):
        note, reviewed = review(col)
        col.add_custom_undo_entry("Some other addon")
        set_definitions(within())
        run_copy_fields_on_review(col.get_card(note.cards()[1].id))
        assert col.undo_status().undo == "Some other addon"

    def test_no_undo_entry_at_all_makes_the_merge_raise(
        self, col, set_definitions, monkeypatch
    ):
        # There is no guard on the captured step. Anki always leaves an entry behind the
        # answer, so this is not reachable through the reviewer; it is pinned because it is
        # what the handler does once the step it is handed does not exist. The patch goes in
        # before the review because the step is recorded when the card is answered.
        monkeypatch.setattr(col, "undo_status", lambda: SimpleNamespace(last_step=0))
        _, reviewed = review(col)
        set_definitions(within())
        with pytest.raises(InvalidInput, match="target undo op not found"):
            run_copy_fields_on_review(reviewed)


# The reviewed card itself ---------------------------------------------------------------------


class TestACardActionOnTheReviewedCard:
    """`merge_cards` exists for exactly this case.

    The copy loads the destination note's cards fresh from the database and mutates those;
    the handler is holding a different `Card` object for the same row, and its closing
    `update_card(card)` writes that one. Without the merge, the copy's changes are written by
    `update_cards` and then immediately written back over.
    """

    def test_a_flag_set_on_the_reviewed_card_survives_the_closing_update(
        self, col, set_definitions
    ):
        _, reviewed = review(col)
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)]))
        run_copy_fields_on_review(reviewed)
        assert col.get_card(reviewed.id).user_flag() == 2

    def test_the_review_survives_alongside_it(self, col, set_definitions):
        _, reviewed = review(col)
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)]))
        run_copy_fields_on_review(reviewed)
        after = col.get_card(reviewed.id)
        assert (after.queue, after.type, after.reps) == (1, 1, 1)
        assert (after.due, after.ivl) == (reviewed.due, reviewed.ivl)

    def test_a_deck_change_on_the_reviewed_card_survives_too(self, col, set_definitions):
        _, reviewed = review(col)
        set_definitions(
            within(card_actions=[d.card_action(VOCAB, "Recognition", change_deck="JP vocab")])
        )
        run_copy_fields_on_review(reviewed)
        after = col.get_card(reviewed.id)
        assert col.decks.name(after.did) == "JP vocab"
        assert after.reps == 1

    def test_without_the_merge_the_closing_update_takes_the_flag_back(
        self, col, set_definitions, monkeypatch
    ):
        # The negative control, and the reason this bullet is in the issue at all: neutering
        # `merge_cards` leaves the review intact and silently drops the card action.
        _, reviewed = review(col)
        monkeypatch.setattr(note_hooks, "merge_cards", lambda card, other: None)
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)]))
        run_copy_fields_on_review(reviewed)
        after = col.get_card(reviewed.id)
        assert after.user_flag() == 0
        assert after.reps == 1

    def test_one_undo_takes_back_the_answer_the_field_and_the_flag_together(
        self, col, set_definitions
    ):
        note, reviewed = review(col)
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)]))
        run_copy_fields_on_review(reviewed)

        col.undo()

        after = col.get_card(reviewed.id)
        assert (after.user_flag(), after.queue, after.reps) == (0, 0, 0)
        assert col.get_note(note.id)["Note"] == ""

    def test_the_flag_and_the_fc_value_end_up_on_the_same_card_row(self, col, set_definitions):
        # `merge_cards` copies `custom_data` across as well, so the merge has to happen
        # before the `fc` write -- which it does, the write being after the whole loop.
        _, reviewed = review(col)
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)]))
        run_copy_fields_on_review(reviewed)
        after = col.get_card(reviewed.id)
        assert (after.user_flag(), custom_data(col, after.id)) == (2, {"fc": 1})

    def test_a_card_action_reaches_the_reviewed_card_through_a_destination_to_sources_copy(
        self, col, set_definitions
    ):
        # The other mode whose destination is the reviewed note, so the same merge path is
        # taken. Across-notes source-to-destinations can never get here: the reviewed note is
        # the source there, and only destination notes' cards are collected.
        kanji_note(col)
        note, reviewed = review(col)
        set_definitions(
            to_sources(card_actions=[d.card_action(VOCAB, "Recognition", set_flag=4)])
        )
        run_copy_fields_on_review(reviewed)
        after = col.get_card(reviewed.id)
        assert (col.get_note(note.id)["Note"], after.user_flag(), after.reps) == ("cat", 4, 1)


class TestACardActionOnAnotherCard:
    """No merge is needed here: the handler's closing write only touches the reviewed row."""

    def test_a_sibling_card_of_the_same_note_is_flagged(self, col, set_definitions):
        note, reviewed = review(col)
        sibling = note.cards()[1]
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recall", set_flag=3)]))
        run_copy_fields_on_review(reviewed)
        assert col.get_card(sibling.id).user_flag() == 3
        assert col.get_card(reviewed.id).user_flag() == 0

    def test_the_sibling_is_not_given_an_fc_value(self, col, set_definitions):
        note, reviewed = review(col)
        sibling = note.cards()[1]
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recall", set_flag=3)]))
        run_copy_fields_on_review(reviewed)
        assert col.get_card(sibling.id).custom_data == ""

    def test_one_undo_takes_the_sibling_flag_back_with_the_answer(self, col, set_definitions):
        note, reviewed = review(col)
        sibling = note.cards()[1]
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recall", set_flag=3)]))
        run_copy_fields_on_review(reviewed)

        col.undo()

        assert col.get_card(sibling.id).user_flag() == 0
        assert col.get_card(reviewed.id).reps == 0

    def test_a_card_of_another_note_entirely_is_flagged_and_undone_together(
        self, col, set_definitions
    ):
        target = kanji_note(col, keyword="old")
        _, reviewed = review(col)
        set_definitions(
            to_destinations(card_actions=[d.card_action(KANJI, "Card 1", set_flag=5)])
        )
        run_copy_fields_on_review(reviewed)
        target_card = target.cards()[0]
        assert col.get_card(target_card.id).user_flag() == 5

        col.undo()

        assert col.get_card(target_card.id).user_flag() == 0
        assert col.get_note(target.id)["Keyword"] == "old"
        assert col.get_card(reviewed.id).reps == 0

    def test_only_cards_a_definition_actually_edited_are_written(
        self, col, set_definitions, updates
    ):
        # Every destination note's cards land in `copied_into_cards_dict`, edited or not; the
        # `edited` attribute the card actions set is what narrows the write down. The last
        # entry is the handler's own closing `update_card`, which arrives without one.
        note, reviewed = review(col)
        sibling = note.cards()[1]
        set_definitions(
            within("plain"),
            within(
                "acts",
                field="Freq",
                card_actions=[d.card_action(VOCAB, "Recall", set_flag=3)],
            ),
        )
        run_copy_fields_on_review(reviewed)
        assert updates.cards == [[], [(sibling.id, False)], [(reviewed.id, False)]]

    def test_the_edited_marker_is_removed_before_the_card_is_written(
        self, col, set_definitions, updates
    ):
        # `edited` is not a real card attribute; leaving it on would carry it to the backend.
        _, reviewed = review(col)
        set_definitions(within(card_actions=[d.card_action(VOCAB, "Recall", set_flag=3)]))
        run_copy_fields_on_review(reviewed)
        assert all(not had_marker for call in updates.cards for _, had_marker in call)


# Between definitions ---------------------------------------------------------------------------


class TestEachDefinitionSeesTheLastOnesWrites:
    def test_a_later_definition_reads_a_note_an_earlier_one_wrote(self, col, set_definitions):
        # Neither definition shares a `Note` object with the other: the writer's destination
        # and the reader's source are both fetched from the database by the query. The only
        # thing that carries the value across is the `update_notes` at the end of each round.
        kanji_note(col, keyword="old")
        note, reviewed = review(col)
        set_definitions(
            to_destinations("writer", field="Keyword", value="new"),
            to_sources("reader"),
        )
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "new"

    def test_the_same_two_definitions_in_the_other_order_read_the_old_value(
        self, col, set_definitions
    ):
        kanji_note(col, keyword="old")
        note, reviewed = review(col)
        set_definitions(
            to_sources("reader"),
            to_destinations("writer", field="Keyword", value="new"),
        )
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "old"

    def test_two_definitions_writing_different_fields_of_one_other_note_both_survive(
        self, col, set_definitions
    ):
        # The contrast with `run_copy_fields_on_add`, where the same two definitions lose the
        # first one's write (test_add_note_hook.py's
        # `test_the_second_of_two_definitions_writing_one_note_discards_the_first`). Here the
        # per-definition flush means the second definition re-reads a note that already has
        # the first definition's value in it.
        target = kanji_note(col, keyword="old")
        _, reviewed = review(col)
        set_definitions(
            to_destinations("a", field="Keyword", value="AAA"),
            to_destinations("b", field="Kanji", value="BBB"),
        )
        run_copy_fields_on_review(reviewed)
        written = col.get_note(target.id)
        assert (written["Keyword"], written["Kanji"]) == ("AAA", "BBB")

    def test_a_later_definition_sees_an_earlier_ones_card_action(self, col, set_definitions):
        # Card writes are flushed per definition too, so a query on `flag:` -- or a
        # `set_desired_retention` reading a custom-data key -- sees what came before it.
        note, reviewed = review(col)
        sibling = note.cards()[1]
        set_definitions(
            within("flagger", card_actions=[d.card_action(VOCAB, "Recall", set_flag=3)]),
            to_destinations(
                "reader",
                field="Note",
                value="found",
                query=f"flag:3 nid:{note.id}",
                note_types=[VOCAB],
            ),
        )
        run_copy_fields_on_review(reviewed)
        assert col.get_card(sibling.id).user_flag() == 3
        assert col.get_note(note.id)["Note"] == "found"

    def test_the_note_list_is_never_cleared_so_earlier_notes_are_rewritten_every_round(
        self, col, set_definitions, updates
    ):
        # `copied_into_notes` accumulates across the loop and the whole list is handed to
        # `update_notes` each time, so N definitions over one note produce N(N+1)/2 note
        # writes. Only a cost here -- the values written are current, unlike on the add path
        # -- but it is why the write count grows quadratically with the definition count.
        note, reviewed = review(col)
        set_definitions(within("a"), within("b", field="Freq"), within("c", field="Reading"))
        run_copy_fields_on_review(reviewed)
        assert updates.notes == [[note.id], [note.id] * 2, [note.id] * 3]
        written = col.get_note(note.id)
        assert (written["Note"], written["Freq"], written["Reading"]) == ("neko",) * 3


# Odds and ends ------------------------------------------------------------------------------


class TestTheRestOfTheDefinition:
    def test_a_condition_query_is_evaluated_against_the_real_note(self, col, set_definitions):
        # Unlike the add path, where the note has no id yet and `nid:0` makes every condition
        # dead, the reviewed note is in the database and its conditions work.
        note, reviewed = review(col)
        set_definitions(within(copy_condition_query="Word:neko"))
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "neko"

    def test_a_condition_that_does_not_hold_skips_the_copy_but_not_the_flag(
        self, col, set_definitions
    ):
        note, reviewed = review(col)
        set_definitions(within(copy_condition_query="Word:inu"))
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == ""
        assert custom_data(col, reviewed.id) == {"fc": 1}

    def test_condition_only_on_sync_turns_the_condition_off_here(self, col, set_definitions):
        # `is_sync` is never passed, so it defaults to False and this flag makes the
        # condition unreachable on review however plainly it fails.
        note, reviewed = review(col)
        set_definitions(within(copy_condition_query="Word:inu", condition_only_on_sync=True))
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "neko"

    def test_the_deck_whitelist_reads_the_decks_of_the_notes_own_cards(
        self, col, set_definitions
    ):
        note = vocab_note(col, deck="JP vocab::10-80")
        _, reviewed = review(col, note)
        set_definitions(
            within(only_copy_into_decks=d.quoted_list(["JP vocab"]), include_subdecks=True)
        )
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "neko"

    def test_tags_are_written_through_the_same_update(self, col, set_definitions):
        note, reviewed = review(col)
        set_definitions(within(add_tags="reviewed"))
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id).tags == ["reviewed"]

        col.undo()

        assert col.get_note(note.id).tags == []

    def test_a_failing_definition_does_not_stop_the_next_one(
        self, col, set_definitions, hook_logger
    ):
        # `copy_for_single_trigger_note` returns False and the handler discards it, so a
        # broken definition costs nothing but a log line -- the review still gets its flag.
        note, reviewed = review(col)
        set_definitions(within("broken", field="Nonexistent"), within("ok", field="Freq"))
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Freq"] == "neko"
        assert hook_logger.has_error("not found in note")
        assert custom_data(col, reviewed.id) == {"fc": 1}

    def test_the_handler_builds_its_logger_from_the_configured_level(
        self, col, set_definitions, hook_logger
    ):
        _, reviewed = review(col)
        set_definitions(within(), log_level="debug")
        run_copy_fields_on_review(reviewed)
        assert hook_logger.levels == ["debug"]

    def test_the_config_is_re_read_on_every_review(self, col, set_definitions):
        # Nothing is cached between calls, so a definition edited in the addon's dialog takes
        # effect on the very next answered card.
        note, reviewed = review(col)
        set_definitions(within(value="first"))
        run_copy_fields_on_review(reviewed)
        set_definitions(within(value="second"))
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "second"

    def test_answering_again_rather_than_good_changes_nothing_for_the_handler(
        self, col, set_definitions
    ):
        # The `ease` argument is dropped by the lambda in `init_note_hooks`, so no definition
        # can react to how the card was answered.
        note, reviewed = review(col, rating=CardAnswer.AGAIN)
        set_definitions(within())
        run_copy_fields_on_review(reviewed)
        assert col.get_note(note.id)["Note"] == "neko"
        assert custom_data(col, reviewed.id) == {"fc": 1}
