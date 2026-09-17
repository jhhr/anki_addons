"""Characterization tests for `copy_fields_in_background`: the bulk loop.

This is the layer above `copy_for_single_trigger_note`: it turns a definition's
`copy_into_note_types` into a set of note-type ids, runs one SQL query to decide which notes
the definition applies to, walks them, and assembles the result text the tooltip shows.

Two things here are load-bearing and stated nowhere:

* the loop asks `mw.progress.want_cancel()` *after* processing a note and *after* looking at
  whether that note succeeded, so a failure stops the run unreported even when cancelled;
* `ProgressUpdater` keeps the notes that got past the deck whitelist and condition query
  (`note_cnt`, what sync reporting reads) apart from the ones skipped by them, and needs both
  to recognise the last note -- so a run whose last note is skipped still renders it.

Nothing below `copy_fields()` writes to the database, so every assertion here is on the
returned `CacheResults`, on the `copied_into_notes` / `copied_into_cards_dict` the loop
appends to, or on what the stub progress recorded.
"""

import html
import json
import time

import pytest
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import KANJI, VOCAB
from copy_anywhere.logic.copy_fields import (
    CacheResults,
    ProgressUpdater,
    copy_fields_in_background,
)


def run_bulk(definition, notes=None, cards=None, results=None, **kwargs):
    """Drive the loop the way `copy_fields`'s `op` does, and hand back what it filled."""
    return copy_fields_in_background(
        copy_definition=definition,
        copied_into_cards_dict=cards if cards is not None else {},
        copied_into_notes=notes if notes is not None else [],
        results=results if results is not None else CacheResults(result_text="", changes=None),
        **kwargs,
    )


def summary(results: CacheResults) -> str:
    """The result text with its indentation collapsed, so a fragment can be matched."""
    return " ".join(results.get_result_text().split())


def copy_word_into_note(**extra):
    """A Within-note definition that writes Word into Note, so a run leaves a trace."""
    return d.within_note(field_to_field_defs=[d.field_to_field("Note", "{{Word}}")], **extra)


def flag_cards(col, note, fc):
    """Set the custom-data field-changed flag the sync selection reads, on every card."""
    for card in note.cards():
        real_anki.set_custom_data(col, card.id, json.dumps({"fc": fc}))


def make_updater(total, is_across=False, title=None, start_time=None, name="a definition"):
    return ProgressUpdater(
        start_time=start_time if start_time is not None else time.time(),
        definition_name=name,
        total_notes_count=total,
        is_across=is_across,
        title=title,
    )


@pytest.fixture
def progress(stub_mw):
    """The stub progress with its recordings cleared: it is session-scoped and accumulates."""
    stub_mw.progress.updates.clear()
    stub_mw.progress.titles.clear()
    return stub_mw.progress


@pytest.fixture
def cancel_after(monkeypatch):
    """Replace `want_cancel` with one that answers True from the nth question onward."""

    def install(nth):
        calls = {"n": 0}

        def want_cancel():
            calls["n"] += 1
            return calls["n"] >= nth

        monkeypatch.setattr(mw.progress, "want_cancel", want_cancel)
        return lambda: calls["n"]

    return install


class TestNoteTypeSelection:
    def test_a_missing_copy_into_note_types_is_an_error_before_anything_runs(self, col, logger):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = copy_word_into_note()
        definition["copy_into_note_types"] = None
        copied: list = []

        results = run_bulk(definition, notes=copied)

        # The message interpolates the value itself, so it reads "... 'None' not found",
        # which is what a user sees when the key is absent rather than misspelt.
        assert logger.has_error("copy_into_note_types 'None'")
        assert copied == []
        assert summary(results) == ""

    def test_the_stored_quoted_list_splits_into_one_id_per_name(self, col, progress):
        # Two names, two ids in the `mid IN (...)` clause, so the loop walks one note of each
        # type. The progress bar's max is the only place the fetched count is observable --
        # a two-note-type definition has no field the two share to copy into.
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"})
        definition = d.within_note(note_types=[VOCAB, KANJI], field_to_field_defs=[])

        run_bulk(definition)

        assert progress.updates[-1]["max"] == 2
        assert progress.updates[-1]["value"] == 2

    def test_one_name_selects_only_that_note_type(self, col, progress):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"})
        definition = d.within_note(note_types=[KANJI], field_to_field_defs=[])

        run_bulk(definition)

        assert progress.updates[-1]["max"] == 1

    def test_an_unknown_name_is_dropped_and_the_rest_still_run(self, col, logger):
        # `id_for_name` answers None for a name that no longer exists, and `filter(None, ...)`
        # quietly removes it -- a renamed note type degrades to "fewer notes", never an error.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = copy_word_into_note()
        definition["copy_into_note_types"] = d.quoted_list(["No Such Type", VOCAB])
        copied: list = []

        run_bulk(definition, notes=copied)

        assert [note_.id for note_ in copied] == [note.id]
        assert logger.errors == []

    def test_every_name_unknown_leaves_an_empty_in_clause_and_finds_nothing(self, col, logger):
        # `ids2str([])` is "()", and SQLite accepts `mid IN ()` as "matches nothing" rather
        # than raising, so this lands on the ordinary "did not find any notes" path.
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = copy_word_into_note()
        definition["copy_into_note_types"] = d.quoted_list(["Nope", "Nada"])

        results = run_bulk(definition)

        assert logger.has_error("Did not find any notes of note type(s)")
        assert summary(results) == ""

    def test_the_error_names_the_raw_stored_string_quotes_and_all(self, col, logger):
        definition = copy_word_into_note()
        definition["copy_into_note_types"] = d.quoted_list(["Nope", "Nada"])

        run_bulk(definition)

        # The message echoes the stored form rather than the split names, so the quoting the
        # editor wrote leaks into the user-facing text.
        assert logger.has_error('Did not find any notes of note type(s) Nope", "Nada')

    def test_a_missing_copy_mode_raises_rather_than_erroring(self, col):
        # `copy_into_note_types` is read with `.get`, but `copy_mode` is read with `[]`, so a
        # definition dict missing that key takes the whole op down instead of logging.
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = copy_word_into_note()
        del definition["copy_mode"]

        with pytest.raises(KeyError, match="copy_mode"):
            run_bulk(definition)


class TestNoteIdFilter:
    def test_none_means_every_note_of_the_type(self, col):
        first = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        second = real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        copied: list = []

        run_bulk(copy_word_into_note(), notes=copied, note_ids=None)

        assert {note.id for note in copied} == {first.id, second.id}

    def test_a_list_narrows_the_query_to_those_ids(self, col):
        first = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        copied: list = []

        run_bulk(copy_word_into_note(), notes=copied, note_ids=[first.id])

        assert [note.id for note in copied] == [first.id]

    def test_an_id_of_another_note_type_intersects_to_nothing(self, col, logger):
        kanji = real_anki.add_note(col, KANJI, {"Kanji": "neko", "Keyword": "cat"})
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        results = run_bulk(copy_word_into_note(), note_ids=[kanji.id])

        # The two filters are ANDed, so an id list only ever narrows the note-type set and is
        # never a way to reach a note the definition does not name.
        assert logger.has_error("Did not find any notes")
        assert summary(results) == ""

    def test_an_empty_list_is_not_the_same_as_none(self, col, logger):
        # `note_ids=[]` is not None, so it becomes `AND n.id IN ()` and matches nothing -- a
        # caller that passes an empty selection gets an error, not a full-collection run.
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        results = run_bulk(copy_word_into_note(), note_ids=[])

        assert logger.has_error("Did not find any notes")
        assert summary(results) == ""


class TestZeroNotes:
    def test_a_manual_run_with_no_notes_is_an_error_and_returns_results_untouched(
        self, col, logger
    ):
        definition = copy_word_into_note()
        definition["copy_into_note_types"] = d.quoted_list(["Nope"])
        results = CacheResults(result_text="earlier definition", changes=None)

        returned = run_bulk(definition, results=results)

        assert returned is results
        assert returned.get_result_text() == "earlier definition"
        assert returned.get_count() == 0
        assert logger.has_error("Did not find any notes")

    def test_a_sync_run_with_no_flagged_cards_says_nothing_at_all(self, col, logger):
        # Zero results is the normal case on sync -- nothing was reviewed -- so neither the
        # error nor the result text fires.
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        results = run_bulk(copy_word_into_note(), is_sync=True)

        assert logger.errors == []
        assert summary(results) == ""
        assert results.get_count() == 0

    def test_a_sync_run_whose_notes_are_all_condition_skipped_also_says_nothing(self, col):
        # `should_report_result` is keyed on the ProgressUpdater's `note_cnt`, which counts
        # the notes that got past the condition query -- not the notes the query returned. So
        # a sync that fetched notes but did nothing with them is silent too.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        flag_cards(col, note, 0)
        definition = copy_word_into_note(copy_condition_query="tag:never")

        results = run_bulk(definition, is_sync=True)

        assert summary(results) == ""
        assert results.get_count() == 0

    def test_a_manual_run_whose_notes_are_all_condition_skipped_still_reports(self, col):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = copy_word_into_note(copy_condition_query="tag:never")

        results = run_bulk(definition)

        assert "processed" in summary(results)
        assert results.get_count() == 1


class TestSyncSelection:
    def test_a_note_with_two_flagged_cards_is_processed_once(self, col):
        # The sync query joins `notes` to `cards`, so without its DISTINCT a two-card note
        # would come back once per flagged card and be handed to `update_notes` twice.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        assert len(note.cards()) == 2
        flag_cards(col, note, 0)
        copied: list = []

        results = run_bulk(copy_word_into_note(), notes=copied, is_sync=True)

        assert [note_.id for note_ in copied] == [note.id]
        assert "1 destinations" in summary(results)

    def test_one_flagged_card_out_of_two_means_one_run(self, col):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.set_custom_data(col, note.cards()[0].id, json.dumps({"fc": 0}))
        copied: list = []

        run_bulk(copy_word_into_note(), notes=copied, is_sync=True)

        assert [note_.id for note_ in copied] == [note.id]

    def test_a_manual_run_ignores_the_flag_entirely_and_never_duplicates(self, col):
        # The non-sync query is `FROM notes n` alone -- no card join, so no duplication and
        # no flag to satisfy.
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        flag_cards(col, note, 0)
        copied: list = []

        run_bulk(copy_word_into_note(), notes=copied, is_sync=False)

        assert [note_.id for note_ in copied] == [note.id]


class TestSyncFlagBranch:
    """`copy_on_sync_after_review` is `not copy_on_review and copy_on_sync`, and picks the IN."""

    @pytest.fixture
    def flagged(self, col):
        """One single-card note per flag value, so each branch has all three to choose from."""
        notes = {}
        for name, fc in [("zero", 0), ("minus", -1), ("one", 1)]:
            note = real_anki.add_note(col, KANJI, {"Kanji": name, "Keyword": name})
            flag_cards(col, note, fc)
            notes[name] = note
        return notes

    @staticmethod
    def kanji_definition(**extra):
        return d.within_note(
            note_types=[KANJI],
            field_to_field_defs=[d.field_to_field("Keyword", "{{Kanji}}")],
            **extra,
        )

    def test_sync_only_takes_both_zero_and_minus_one(self, col, flagged):
        definition = self.kanji_definition(copy_on_sync=True, copy_on_review=False)
        copied: list = []

        run_bulk(definition, notes=copied, is_sync=True)

        assert {note.id for note in copied} == {flagged["zero"].id, flagged["minus"].id}

    def test_review_plus_sync_narrows_to_zero_only(self, col, flagged):
        # -1 is the "changed during review" marker; a definition that already ran on review
        # has handled those, so its sync pass takes only the plain 0 rows.
        definition = self.kanji_definition(copy_on_sync=True, copy_on_review=True)
        copied: list = []

        run_bulk(definition, notes=copied, is_sync=True)

        assert {note.id for note in copied} == {flagged["zero"].id}

    def test_neither_flag_set_also_narrows_to_zero_only(self, col, flagged):
        # A definition with copy_on_sync off can still be driven with is_sync=True by the
        # caller, and when it is, it gets the strict `= 0` branch.
        definition = self.kanji_definition(copy_on_sync=False, copy_on_review=False)
        copied: list = []

        run_bulk(definition, notes=copied, is_sync=True)

        assert {note.id for note in copied} == {flagged["zero"].id}

    def test_a_card_with_no_custom_data_is_never_selected(self, col, flagged):
        real_anki.add_note(col, KANJI, {"Kanji": "unflagged", "Keyword": "unflagged"})
        definition = self.kanji_definition(copy_on_sync=True)
        copied: list = []

        run_bulk(definition, notes=copied, is_sync=True)

        # `json_extract` of a missing key is NULL, and NULL matches neither `IN` nor `=`.
        assert {note.id for note in copied} == {flagged["zero"].id, flagged["minus"].id}

    def test_note_ids_narrows_the_sync_selection_too(self, col, flagged):
        definition = self.kanji_definition(copy_on_sync=True)
        copied: list = []

        run_bulk(definition, notes=copied, is_sync=True, note_ids=[flagged["minus"].id])

        assert [note.id for note in copied] == [flagged["minus"].id]


class TestCancellation:
    def test_cancelling_mid_loop_stops_the_walk_and_still_reports(self, col, cancel_after):
        for i in range(4):
            real_anki.add_note(col, VOCAB, {"Word": f"w{i}", "Meaning": f"m{i}"})
        cancel_after(2)
        copied: list = []

        results = run_bulk(copy_word_into_note(), notes=copied)

        assert len(copied) == 2
        assert "2 destinations" in summary(results)
        assert results.get_count() == 1

    def test_the_check_runs_after_the_note_so_cancelling_up_front_still_does_one(
        self, col, cancel_after
    ):
        # The order in the loop body is: process, render, ask. A user who cancels before the
        # op starts still gets exactly one note copied.
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        cancel_after(1)
        copied: list = []

        run_bulk(copy_word_into_note(), notes=copied)

        assert len(copied) == 1

    def test_the_question_is_asked_once_per_note(self, col, cancel_after):
        for i in range(3):
            real_anki.add_note(col, VOCAB, {"Word": f"w{i}", "Meaning": f"m{i}"})
        call_count = cancel_after(99)

        run_bulk(copy_word_into_note())

        assert call_count() == 3

    def test_a_failing_note_wins_over_a_cancel(self, col, logger, cancel_after):
        # `if not success` is checked before `want_cancel()`, so a definition that failed on
        # its very first note returns without reporting even when the user also cancelled --
        # the failure is the reason the run has to be debugged.
        real_anki.add_note(col, VOCAB, {"Word": "", "Meaning": "cat"})
        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        cancel_after(1)
        definition = d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Meaning}}")],
            copy_condition_query="{{Word}}",
        )

        results = run_bulk(definition)

        assert logger.has_error("could not be interpolated")
        assert summary(results) == ""
        assert results.get_count() == 0


class TestAFailingNote:
    """A note whose condition query interpolates to nothing returns False from the inner call."""

    @pytest.fixture
    def three_notes(self, col):
        """The middle note has an empty Word, so `{{Word}}` interpolates to "" for it."""
        return [
            real_anki.add_note(col, VOCAB, {"Word": "aaa", "Meaning": "first"}),
            real_anki.add_note(col, VOCAB, {"Word": "", "Meaning": "second"}),
            real_anki.add_note(col, VOCAB, {"Word": "ccc", "Meaning": "third"}),
        ]

    @staticmethod
    def definition():
        return d.within_note(
            field_to_field_defs=[d.field_to_field("Note", "{{Meaning}}")],
            copy_condition_query="{{Word}}",
        )

    def test_the_loop_returns_at_the_failing_note_leaving_the_rest_untouched(
        self, col, logger, three_notes
    ):
        # The query has no ORDER BY, but a single note type comes back in rowid order, which
        # is the order these three were added in.
        first, second, third = three_notes
        copied: list = []

        run_bulk(self.definition(), notes=copied)

        assert [note.id for note in copied] == [first.id]
        assert logger.has_error(f"note id {second.id}")
        assert not logger.has_error(f"note id {third.id}")

    def test_the_partial_run_reports_nothing_at_all(self, col, three_notes):
        # The early `return results` skips the whole reporting block, so a run that copied
        # into one note and then died is indistinguishable from one that did nothing -- the
        # only trace is the logged error.
        results = run_bulk(self.definition())

        assert summary(results) == ""
        assert results.get_count() == 0

    def test_the_results_object_handed_in_is_the_one_handed_back(self, col, three_notes):
        results = CacheResults(result_text="earlier", changes=None)

        returned = run_bulk(self.definition(), results=results)

        assert returned is results
        assert returned.get_result_text() == "earlier"


class TestProgressUpdaterRendering:
    def test_the_default_title_is_set_from_the_constructor(self, col, progress):
        make_updater(total=3)
        assert progress.titles == ["Copying fields"]

    def test_a_given_title_replaces_the_default(self, col, progress):
        make_updater(total=3, title="Syncing")
        assert progress.titles == ["Syncing"]

    def test_a_sub_half_second_call_on_a_middle_note_renders_nothing(self, col, progress):
        updater = make_updater(total=5)
        updater.update_counts(note_cnt_inc=1)

        updater.maybe_render_update()

        assert progress.updates == []

    def test_force_overrides_the_throttle(self, col, progress):
        updater = make_updater(total=5)
        updater.update_counts(note_cnt_inc=1)

        updater.maybe_render_update(force=True)

        assert len(progress.updates) == 1

    def test_the_last_note_overrides_the_throttle_without_force(self, col, progress):
        updater = make_updater(total=2)
        updater.update_counts(note_cnt_inc=2)

        updater.maybe_render_update()

        assert len(progress.updates) == 1

    def test_enough_elapsed_time_lets_a_middle_note_through(self, col, progress):
        updater = make_updater(total=5, start_time=time.time() - 10)
        updater.update_counts(note_cnt_inc=1)

        updater.maybe_render_update()

        assert len(progress.updates) == 1

    def test_a_render_resets_the_throttle_clock(self, col, progress):
        # The throttle is measured against the last render, not against the start, so a run
        # that has been going for ten seconds still only draws twice a second.
        updater = make_updater(total=5, start_time=time.time() - 10)
        updater.update_counts(note_cnt_inc=1)

        updater.maybe_render_update()
        updater.maybe_render_update()

        assert len(progress.updates) == 1

    def test_skipped_notes_count_toward_the_last_note_and_the_bar(self, col, progress):
        updater = make_updater(total=3)
        updater.update_counts(note_cnt_inc=1, skipped_note_cnt_inc=2)

        updater.maybe_render_update()

        update = progress.updates[-1]
        assert update["value"] == 3
        assert "Copied 1/3 notes, skipped 2" in update["label"]
        # ...but not toward `get_counts`, whose note count is "processed" to its callers.
        assert updater.get_counts()[0] == 1

    def test_zero_total_notes_renders_nothing_even_when_forced(self, col, progress):
        # The `no_notes` guard sits after the throttle test and before the `note_cnt /
        # total_notes_count` division, so it is also what keeps a zero total from raising
        # ZeroDivisionError.
        updater = make_updater(total=0)

        updater.maybe_render_update()
        updater.maybe_render_update(force=True)
        updater.update_counts(note_cnt_inc=1)
        updater.maybe_render_update(force=True)

        assert progress.updates == []

    def test_the_label_carries_the_note_counter_and_the_bar_bounds(self, col, progress):
        updater = make_updater(total=4)
        updater.update_counts(note_cnt_inc=4)

        updater.maybe_render_update()

        update = progress.updates[-1]
        assert "Copied 4/4 notes" in update["label"]
        assert update["value"] == 4
        assert update["max"] == 4

    def test_the_definition_name_is_escaped_in_the_label(self, col, progress):
        updater = make_updater(total=1, name="a <b> & c")
        updater.update_counts(note_cnt_inc=1)

        updater.maybe_render_update()

        assert html.escape("a <b> & c") in progress.updates[-1]["label"]

    def test_an_eta_appears_once_past_a_tenth_of_the_way(self, col, progress):
        updater = make_updater(total=4)
        updater.update_counts(note_cnt_inc=4)

        updater.maybe_render_update()

        assert "ETA:" in progress.updates[-1]["label"]

    def test_no_eta_while_still_under_a_tenth_and_under_a_second(self, col, progress):
        updater = make_updater(total=100)
        updater.update_counts(note_cnt_inc=1)

        updater.maybe_render_update(force=True)

        assert "ETA:" not in progress.updates[-1]["label"]

    def test_no_eta_before_the_first_note_even_after_a_second(self, col, progress):
        # `elapsed_s > 1` opens the ETA branch, and the inner `note_cnt > 0` guard is the only
        # thing stopping a division by zero inside it.
        updater = make_updater(total=100, start_time=time.time() - 10)

        updater.maybe_render_update()

        assert "ETA:" not in progress.updates[-1]["label"]

    def test_the_sources_line_appears_only_for_an_across_definition(self, col, progress):
        across = make_updater(total=1, is_across=True)
        across.update_counts(note_cnt_inc=1, processed_sources_inc=7)
        across.maybe_render_update()
        assert ", sources: 7" in progress.updates[-1]["label"]

        within = make_updater(total=1, is_across=False)
        within.update_counts(note_cnt_inc=1, processed_sources_inc=7)
        within.maybe_render_update()
        assert "sources" not in progress.updates[-1]["label"]

    def test_the_optional_lines_are_omitted_while_their_counters_are_zero(self, col, progress):
        updater = make_updater(total=1)
        updater.update_counts(note_cnt_inc=1)

        updater.maybe_render_update()

        label = progress.updates[-1]["label"]
        assert "destination notes" not in label
        assert "files" not in label
        assert "cards" not in label


class TestProgressUpdaterCounters:
    def test_get_counts_returns_notes_sources_destinations_files_cards(self, col):
        updater = make_updater(total=3)

        updater.update_counts(
            note_cnt_inc=1,
            processed_sources_inc=2,
            processed_destinations_inc=3,
            processed_files_inc=4,
            processed_cards_inc=5,
        )

        assert updater.get_counts() == (1, 2, 3, 4, 5)

    def test_the_counters_accumulate_across_calls(self, col):
        updater = make_updater(total=3)

        updater.update_counts(note_cnt_inc=1, processed_sources_inc=2)
        updater.update_counts(note_cnt_inc=1, processed_sources_inc=3)

        assert updater.get_counts() == (2, 5, 0, 0, 0)

    def test_an_omitted_increment_leaves_its_counter_alone(self, col):
        updater = make_updater(total=3)
        updater.update_counts(processed_files_inc=1)

        updater.update_counts()

        assert updater.get_counts() == (0, 0, 0, 1, 0)

    def test_a_zero_increment_is_applied_rather_than_treated_as_absent(self, col):
        # The guard is `is not None`, not truthiness, so 0 is a real increment. Nothing in the
        # addon passes 0 today, but it is the difference between the two spellings.
        updater = make_updater(total=3)

        updater.update_counts(note_cnt_inc=0)

        assert updater.get_counts()[0] == 0


class TestCounterArithmeticThroughTheLoop:
    def test_one_destination_is_counted_per_note_copied_into(self, col):
        for i in range(3):
            real_anki.add_note(col, VOCAB, {"Word": f"w{i}", "Meaning": f"m{i}"})

        results = run_bulk(copy_word_into_note())

        assert "3 destinations" in summary(results)

    def test_sources_are_counted_once_per_trigger_note_not_once_per_run(self, col):
        for word in ["aaa", "bbb", "ccc"]:
            real_anki.add_note(col, VOCAB, {"Word": word, "Meaning": word}, tags=["src"])
        definition = d.destination_to_sources(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            copy_from_cards_query="tag:src",
            select_card_by="Random",
            select_card_count="0",
        )

        results = run_bulk(definition)

        # Three trigger notes, each finding the same three sources: the counter is a running
        # total of work done, not a count of distinct notes touched.
        assert "processed with 9 sources" in summary(results)
        assert "3 destinations" in summary(results)

    def test_a_written_file_is_counted_once_per_note(self, col, media_dir):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})
        definition = d.within_note(field_to_field_defs=[])
        definition["field_to_file_defs"] = [d.field_to_file("{{Word}}.txt", "{{Meaning}}")]

        results = run_bulk(definition)

        assert "2 files" in summary(results)
        assert "destinations" not in summary(results)

    def test_only_a_card_an_action_actually_edited_is_counted(self, col):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.within_note(field_to_field_defs=[])
        definition["card_actions"] = [d.card_action(VOCAB, "Recognition", set_flag=2)]
        cards: dict = {}

        results = run_bulk(definition, cards=cards)

        # Both of the note's cards are collected for the later `update_cards`, but the counter
        # only follows the `edited` marker, so it reports the one that changed.
        assert "1 cards" in summary(results)
        assert len(cards) == 2

    def test_a_note_with_no_sources_does_not_count_its_destination(self, col):
        # The "no sources found" early return copies nothing, so it must not count the
        # destination either -- a destination is counted only when it was written to.
        real_anki.add_note(col, VOCAB, {"Word": "trig", "Meaning": "t"})
        definition = d.destination_to_sources(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            copy_from_cards_query="tag:nothing",
            select_card_by="Random",
            select_card_count="0",
        )
        copied: list = []

        results = run_bulk(definition, notes=copied)

        assert "destinations" not in summary(results)
        assert copied == []


class TestResultText:
    def test_a_plain_run_names_the_definition_and_says_processed(self, col):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        results = run_bulk(copy_word_into_note(definition_name="my copy"))

        text = summary(results)
        assert "<i>my copy:</i>" in text
        assert text.endswith("processed </span>")

    def test_the_across_branch_appends_the_source_count(self, col):
        real_anki.add_note(col, VOCAB, {"Word": "aaa", "Meaning": "a"}, tags=["src"])
        real_anki.add_note(col, VOCAB, {"Word": "trig", "Meaning": "t"}, tags=["trig"])
        definition = d.destination_to_sources(
            field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
            copy_from_cards_query="tag:src",
            select_card_by="Random",
            select_card_count="0",
        )

        results = run_bulk(definition)

        # `is_across` is decided from `copy_mode` alone, so the phrasing switches even when the
        # source count would be zero.
        assert "processed with 2 sources" in summary(results)

    def test_the_definition_name_is_escaped(self, col):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        results = run_bulk(copy_word_into_note(definition_name="a <b> & c"))

        assert "<i>a &lt;b&gt; &amp; c:</i>" in summary(results)

    def test_a_run_that_did_nothing_measurable_still_reports_and_counts(self, col):
        # Every counter is zero, so the text is just the timing and the name -- and the count
        # still goes up, which is what makes `copy_fields` show a tooltip for it.
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.within_note(field_to_field_defs=[])

        results = run_bulk(definition)

        text = summary(results)
        assert "destinations" not in text
        assert "files" not in text
        assert "cards" not in text
        assert results.get_count() == 1

    def test_each_definition_appends_to_the_shared_results(self, col):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        results = CacheResults(result_text="", changes=None)

        run_bulk(copy_word_into_note(definition_name="first"), results=results)
        run_bulk(copy_word_into_note(definition_name="second"), results=results)

        # `copy_fields` threads one CacheResults through every definition, and this is where
        # the per-definition lines and the count come from.
        assert "<i>first:</i>" in summary(results)
        assert "<i>second:</i>" in summary(results)
        assert results.get_count() == 2


class TestTheFinalRender:
    def test_a_full_run_renders_an_update_for_its_last_note(self, col, progress):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        real_anki.add_note(col, VOCAB, {"Word": "inu", "Meaning": "dog"})

        run_bulk(copy_word_into_note())

        assert progress.updates
        assert progress.updates[-1]["value"] == 2
        assert progress.updates[-1]["max"] == 2

    def test_a_run_whose_last_note_is_condition_skipped_still_renders_a_full_bar(
        self, col, progress
    ):
        # `total_notes_count` is the number the SQL returned, so the "last note" check has to
        # count the condition-skipped notes as well as the copied ones, or it never matches
        # and the sub-0.5s throttle suppresses every update of the run.
        real_anki.add_note(col, VOCAB, {"Word": "aaa", "Meaning": "a"})
        real_anki.add_note(col, VOCAB, {"Word": "bbb", "Meaning": "b"})
        definition = copy_word_into_note(copy_condition_query="Word:aaa")

        results = run_bulk(definition)

        assert progress.updates
        assert progress.updates[-1]["value"] == 2
        assert progress.updates[-1]["max"] == 2
        assert "Copied 1/2 notes, skipped 1" in progress.updates[-1]["label"]
        assert "1 destinations" in summary(results)

    def test_zero_notes_means_the_loop_never_asks_for_a_render(self, col, progress):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        run_bulk(copy_word_into_note(), is_sync=True)

        assert progress.updates == []
        # The title is still set, because the ProgressUpdater is built before the loop.
        assert progress.titles == ["Copying fields"]

    def test_the_progress_title_argument_reaches_the_updater(self, col, progress):
        real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})

        run_bulk(copy_word_into_note(), progress_title="Syncing fields")

        assert progress.titles == ["Syncing fields"]
