"""The cleanup's note adding, which a cancelled run goes through as a finished one does.

A cancelled match run used to add none of the new notes it had prepared: the adding loop
stopped on the dialog's cancel flag, which stays set for the rest of a cancelled run. The
notes' meanings were paid for, and their placeholder ids were already in the saved word
arrays. The cleanup now re-arms that flag and gives the adding a cancel of its own, which
stops it and unlinks the notes it left out. These tests drive `add_new_notes` against a
stand-in collection.
"""

import contextlib
import json
import re
import types
import unittest
from unittest import mock

from addon_modules import FakeClock, load_ops_module, mw

base_ops = load_ops_module("base_ops")
mwtn = load_ops_module("match_words_to_notes")
controls_module = load_ops_module("progress_controls")

POS = 101


class FakeNote:
    def __init__(self, fields: dict, note_id: int = 0):
        self.id = note_id
        self.fields = fields
        self.tags: list = []

    def add_tag(self, tag):
        self.tags.append(tag)

    def set_tags_from_str(self, tags: str):
        self.tags = tags.split()

    def note_type(self):
        return {"name": "Word"}

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def __setitem__(self, field, value):
        self.fields[field] = value


class FakeDecks:
    def id_for_name(self, name: str):
        return 7 if name == "Vocab" else None


class FakeCollection:
    def __init__(self, failing: tuple = ()):
        self.decks = FakeDecks()
        self.failing = failing
        self.added: list = []
        self.merged: list = []
        self.updated: list = []
        self.next_id = 500

    def add_note(self, note, deck_id):
        if note in self.failing:
            raise RuntimeError("the backend refused the note")
        self.next_id += 1
        note.id = self.next_id
        self.added.append((note, deck_id))

    def merge_undo_entries(self, pos):
        self.merged.append(pos)
        return f"changes after {len(self.merged)} merges"

    def update_notes(self, notes):
        self.updated.extend(notes)


class FakeUpdater:
    def __init__(self):
        self.adding: list = []
        self.clearing: list = []
        self.tidying: list = []
        # The cleanup's own cancel, as armed by arm_cleanup_cancel; a test presses it
        self.cancel_pressed = False
        self.cancel_ended = False
        # The note count of each arm_cleanup_cancel
        self.armed: list = []

    def begin_cleanup_stage(self):
        pass

    def begin_cleanup(self):
        pass

    def arm_cleanup_cancel(self, total_notes):
        self.armed.append(total_notes)

    def cleanup_cancel_requested(self):
        return self.cancel_pressed

    def end_cleanup_cancel(self):
        self.cancel_ended = True

    def update_note_adding_progress(self, notes_added=0, total_notes=0, failed=0):
        self.adding.append((notes_added, total_notes, failed))

    def update_new_note_processing_progress(self, **_):
        pass

    def update_unadded_note_clearing_progress(self, notes_cleared=0, total_notes=0):
        self.clearing.append((notes_cleared, total_notes))

    def update_marker_tidying_progress(self, words_done=0, total_words=0):
        self.tidying.append((words_done, total_words))


CONFIG = {"Word": {"insert_deck": "Vocab", **{key: key for key in mwtn.MATCH_FIELD_KEYS}}}


def new_note(placeholder: str) -> FakeNote:
    return FakeNote({"word_list_field": "", "new_note_id_field": placeholder})


class AddNewNotesAfterCancelTests(unittest.TestCase):
    """After a cancel of the API work, which the adding does not take for its own.

    The dialog's flag still holds that cancel here, as it does until the adding re-arms it
    (arm_cleanup_cancel; the real re-arm: ReArmedCancelTests); the adding reads only the
    cleanup's cancel, which nobody presses in these tests.
    """

    def setUp(self):
        # The flag a cancelled run leaves set for the rest of it
        mw.progress.cancel = True
        self.addCleanup(setattr, mw.progress, "cancel", False)
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def test_every_prepared_note_is_added_and_handed_on_with_its_id(self):
        col = FakeCollection()
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        handed: list = []

        def new_notes_op(new_notes, config, progress_updater):
            handed.extend((note, note.id) for note in new_notes)
            return {}

        result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, new_notes_op)

        self.assertEqual([note for note, _ in col.added], notes)
        self.assertEqual({deck for _, deck in col.added}, {7})
        self.assertEqual(handed, [(notes[0], 501), (notes[1], 502), (notes[2], 503)])
        # every add merged into the run's one undo entry
        self.assertEqual(col.merged, [POS, POS, POS])
        self.assertEqual(result.added, 3)
        self.assertEqual(result.op_changes, "changes after 3 merges")
        self.assertEqual(self.updater.adding[-1], (3, 3, 0))

    def test_what_new_notes_op_rewrote_is_saved_into_the_same_undo_entry(self):
        col = FakeCollection()
        notes = [new_note("-1111111")]
        referencing = FakeNote({"word_list_field": "[]"}, note_id=2)

        def new_notes_op(new_notes, config, progress_updater):
            return {2: referencing, new_notes[0].id: new_notes[0]}

        result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, new_notes_op)

        self.assertEqual(col.updated, [referencing, notes[0]])
        self.assertEqual(col.merged, [POS, POS])
        # The added note is saved but counted as added, not as a note edited
        self.assertEqual(result.updated_nids, [2])

    def test_a_note_that_fails_to_add_keeps_its_placeholder_where_it_is_referenced(self):
        failing = new_note("-1111111")
        added = new_note("-2222222")
        arr = [
            ["様", "noun", "様", "さま", [-1111111, 3], []],
            ["本", "noun", "本", "ほん", [-2222222], []],
        ]
        referencing = FakeNote({"word_list_field": json.dumps(arr)}, note_id=2)
        referencing["new_note_id_field"] = ""
        col = FakeCollection(failing=(failing,))

        with (
            mock.patch.object(mwtn, "col_find_notes", lambda _: [2]),
            mock.patch.object(mwtn, "col_get_notes", lambda _: [referencing]),
        ):
            result = base_ops.add_new_notes(
                col, [failing, added], CONFIG, POS, self.updater, mwtn.update_fake_note_ids
            )

        self.assertEqual((failing.id, added.id), (0, 501))
        saved = json.loads(referencing["word_list_field"])
        self.assertEqual([saved[0][4], saved[1][4]], [[-1111111, 3], [501]])
        self.assertEqual(failing["new_note_id_field"], "-1111111")
        self.assertEqual(added["new_note_id_field"], "501")
        self.assertEqual(result.added, 1)
        self.assertEqual(self.updater.adding[-1], (1, 2, 1))
        self.assertEqual(col.updated, [referencing, added])

    def test_without_new_notes_op_the_notes_are_still_added(self):
        col = FakeCollection()

        result = base_ops.add_new_notes(col, [new_note("-1111111")], CONFIG, POS, self.updater)

        self.assertEqual((result.added, result.updated_nids), (1, []))
        self.assertEqual(col.updated, [])


class ReArmedCancelTests(unittest.TestCase):
    """The real updater over a dialog whose flag a cancel of the API work has set."""

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        # Anki's dialog, reduced to the flag Escape, the close box and Cancel set
        self.win = types.SimpleNamespace(wantCancel=True)
        self.run_was_cancelled = True
        patches = (
            mock.patch.object(base_ops, "run_cancelled", lambda: self.run_was_cancelled),
            mock.patch.object(mw.progress, "_win", self.win, create=True),
            mock.patch.object(mw.progress, "want_cancel", lambda: self.win.wantCancel),
            mock.patch.object(mw.progress, "update", lambda **_: None),
            mock.patch.object(
                controls_module, "sip", types.SimpleNamespace(isdeleted=lambda _win: False)
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.updater = base_ops.AsyncTaskProgressUpdater(title="Matching")

    def test_the_first_cancel_does_not_stop_the_adding_once_re_armed(self):
        """What 'every prepared note is added with the flag set' means now."""
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        col = FakeCollection()

        self.updater.begin_cleanup()
        result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater)

        self.assertEqual([note for note, _ in col.added], notes)
        self.assertEqual(result.counts, base_ops.NewNotesCounts(3, 0, 0))

    def test_a_second_cancel_stops_it(self):
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        col = FakeCollection()
        col_add = col.add_note

        def add_and_press(note, deck_id):
            col_add(note, deck_id)
            if note is notes[0]:
                self.win.wantCancel = True

        col.add_note = add_and_press  # type: ignore[method-assign]
        unlinking: list = []

        self.updater.begin_cleanup()
        result = base_ops.add_new_notes(
            col,
            notes,
            CONFIG,
            POS,
            self.updater,
            unadded_notes_op=lambda notes, config, updater: unlinking.extend(notes) or {},
        )

        self.assertEqual([note for note, _ in col.added], [notes[0]])
        self.assertEqual(unlinking, notes[1:])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 0, 2))

    def test_in_a_run_not_cancelled_a_press_during_the_write_stops_the_adding(self):
        """The buttons are greyed while the edited notes are written, but Escape and the close
        box still set the flag: with no cancel of the API work for it to hold, that is a press
        to stop the adding, which the re-arm used to erase."""
        self.run_was_cancelled = False
        notes = [new_note("-1111111"), new_note("-2222222")]
        col = FakeCollection()
        unlinking: list = []

        self.updater.begin_cleanup()
        result = base_ops.add_new_notes(
            col,
            notes,
            CONFIG,
            POS,
            self.updater,
            unadded_notes_op=lambda notes, config, updater: unlinking.extend(notes) or {},
        )

        self.assertEqual(col.added, [])
        self.assertEqual(unlinking, notes)
        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 0, 2))

    def test_in_a_run_not_cancelled_a_clear_flag_is_left_clear(self):
        self.run_was_cancelled = False
        self.win.wantCancel = False
        notes = [new_note("-1111111"), new_note("-2222222")]
        col = FakeCollection()

        self.updater.begin_cleanup()
        result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater)

        self.assertEqual([note for note, _ in col.added], notes)
        self.assertEqual(result.counts, base_ops.NewNotesCounts(2, 0, 0))

    def test_a_flag_nothing_could_reset_does_not_stop_the_adding(self):
        """Without the dialog the first cancel set, or with one the reset fails on, the flag
        is left as it was; the adding cannot be cancelled then, but it is not stopped by the
        cancel of the API work either."""
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        col = FakeCollection()
        unlinking: list = []

        self.updater.begin_cleanup()
        with mock.patch.object(controls_module, "_dialog", lambda: None):
            result = base_ops.add_new_notes(
                col,
                notes,
                CONFIG,
                POS,
                self.updater,
                unadded_notes_op=lambda notes, config, updater: unlinking.extend(notes) or {},
            )

        self.assertTrue(self.win.wantCancel)
        self.assertEqual([note for note, _ in col.added], notes)
        self.assertEqual(unlinking, [])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(3, 0, 0))

    def test_the_dedupe_runs_with_the_first_cancel_already_reset(self):
        """It is the first stage the cancel stops, so the arming comes before it."""
        notes = [new_note("-1111111"), new_note("-2222222")]
        seen: list = []

        def dedupe(notes, config, progress_updater):
            seen.append((self.win.wantCancel, progress_updater.cleanup_cancel_requested()))
            return notes, {}

        self.updater.begin_cleanup()
        result = base_ops.add_new_notes(
            FakeCollection(), notes, CONFIG, POS, self.updater, filter_new_notes_op=dedupe
        )

        self.assertEqual(seen, [(False, False)])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(2, 0, 0))

    def test_an_adding_that_raises_leaves_the_cancel_disarmed(self):
        """A Cancel left live would say the rest of the cleanup can be stopped."""
        greyed: list = []
        patch = mock.patch.object(base_ops, "disable_run_controls", lambda: greyed.append(True))
        patch.start()
        self.addCleanup(patch.stop)

        self.updater.begin_cleanup()
        with self.assertRaisesRegex(Exception, "has not been configured"):
            base_ops.add_new_notes(
                FakeCollection(), [unconfigured_note("-1111111")], CONFIG, POS, self.updater
            )
        self.win.wantCancel = True

        self.assertFalse(self.updater.cleanup_cancel_requested())
        # begin_cleanup's and the adding's end
        self.assertEqual(greyed, [True, True])


class SlowCollection(FakeCollection):
    def __init__(self, clock: FakeClock):
        super().__init__()
        self.clock = clock

    def add_note(self, note, deck_id):
        # copy_anywhere's on-add definitions run in here, which is what makes adding slow
        self.clock.advance(2)
        super().add_note(note, deck_id)


class CleanupStageClockTests(unittest.TestCase):
    """The adding and the new-note processing each time themselves, not the run before them.

    With the real progress updater under a fake clock, and an hour of API work behind it.
    """

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.drawn: list = []
        saved_update = mw.progress.update
        mw.progress.update = lambda **kwargs: self.drawn.append(kwargs["label"])
        self.addCleanup(setattr, mw.progress, "update", saved_update)
        self.clock = FakeClock()
        saved_time = base_ops.time
        base_ops.time = self.clock
        self.addCleanup(setattr, base_ops, "time", saved_time)
        self.updater = base_ops.AsyncTaskProgressUpdater(total_tasks=200, title="Matching")
        self.updater.increment_counts(tasks_done=200, notes_done=40)
        self.clock.advance(3600)
        self.updater.begin_cleanup()

    def test_each_stage_starts_its_own_clock_and_its_progress_calls_do_not_restart_it(self):
        col = SlowCollection(self.clock)
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]

        def new_notes_op(new_notes, config, progress_updater):
            for processed in (1, 2):
                self.clock.advance(5)
                progress_updater.update_new_note_processing_progress(
                    new_notes_processed=processed, total_notes=3
                )
            return {}

        base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, new_notes_op)

        adding = [label for label in self.drawn if "Adding notes" in label]
        self.assertIn("3/3", adding[-1])
        self.assertIn("Time: 00:00:06", adding[-1])
        self.assertIn("Avg time per note: 2.00s", adding[-1])
        processing = [label for label in self.drawn if "Processing new notes" in label]
        self.assertIn("2/3", processing[-1])
        self.assertIn("Time: 00:00:10", processing[-1])
        self.assertIn("ETA: 00:00:05", processing[-1])


def w(form: str, match_data: list, subs: tuple = ()) -> list:
    return [form, "noun", form, "よみ", match_data, list(subs)]


def sentence(note_id: int, *words: list) -> FakeNote:
    fields = {"word_list_field": mwtn.format_word_array(list(words)), "new_note_id_field": ""}
    return FakeNote(fields, note_id)


def match_data(note: FakeNote) -> list:
    return [elem[4] for _, elem in mwtn.match_flags.iter_words(words_of(note))]


def words_of(note: FakeNote) -> list:
    arr = mwtn.match_flags.decode_word_array(note["word_list_field"])
    assert arr is not None
    return arr


class FakeSearch:
    """The collection behind `col_find_notes` / `col_get_notes`, holding the notes as saved:
    nothing the op changes reaches it, as nothing is saved until cleanup gets the notes back,
    and each read is a fresh note. A `"field:*text*"` term is a substring search, as in Anki,
    so a longer placeholder holding a shorter one's digits is found too."""

    def __init__(self, *notes: FakeNote):
        self.saved = {note.id: dict(note.fields) for note in notes}
        self.queries: list = []
        self.read: list = []
        # What sort_base_note_ids was asked for
        self.sort_bases: list = []
        # The notes of another note type than "Word", by id
        self.note_types: dict = {}

    def find_notes(self, query: str) -> list:
        self.queries.append(query)
        terms = re.findall(r'"(\w+):\*(-\d+)\*"', query)
        return [
            nid
            for nid, fields in self.saved.items()
            if any(field in fields and text in fields[field] for field, text in terms)
        ]

    def get_notes(self, nids) -> list:
        self.read.extend(nids)
        notes = [FakeNote(dict(self.saved[nid]), nid) for nid in nids]
        for note in notes:
            if note.id in self.note_types:
                note_type = {"name": self.note_types[note.id]}
                note.note_type = lambda note_type=note_type: note_type  # type: ignore
        return notes

    def get_note(self, nid) -> FakeNote:
        return self.get_notes([nid])[0]

    def sort_base_note_ids(self, sort_field: str, bases) -> dict:
        """word_index.sort_base_note_ids: a word's notes are those whose sort field is the word
        and then anything in brackets, found whatever the brackets hold."""
        self.sort_bases.append((sort_field, sorted(bases)))
        found: dict = {}
        for base in bases:
            key = mwtn.word_key(base)
            for nid, fields in self.saved.items():
                value = fields.get(sort_field, "")
                if value and mwtn.word_key(value.split("(")[0].strip()) == key:
                    found.setdefault(key, []).append(nid)
        return found

    def patched(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(mwtn, "col_find_notes", self.find_notes))
        stack.enter_context(mock.patch.object(mwtn, "col_get_notes", self.get_notes))
        stack.enter_context(mock.patch.object(mwtn, "col_get_note", self.get_note))
        stack.enter_context(
            mock.patch.object(mwtn, "sort_base_note_ids", self.sort_base_note_ids)
        )
        return stack


class ClearUnaddedNoteIdsTests(unittest.TestCase):
    """The new notes a cancel of the adding left out: the words linked to them are matched
    again, since no note will ever hold their placeholders. A failed add keeps its own."""

    def setUp(self):
        self.updater = FakeUpdater()

    def test_the_words_of_a_note_not_added_are_cleared_where_they_are_saved(self):
        not_added = new_note("-1111111")
        # its add was attempted and raised; not among the notes handed over
        failed = new_note("-2222222")
        both = sentence(2, w("様", [-1111111, 3]), w("本", [-2222222]))
        nested = sentence(3, w("本棚", ["dontmatch"], (w("本", [-1111111]), w("棚", [444]))))
        longer = sentence(4, w("机", [-11111112]))
        search = FakeSearch(both, nested, longer)

        with search.patched():
            updated = mwtn.clear_unadded_note_ids([not_added], CONFIG, self.updater)

        # found by the substring search, but its placeholder is another note's: not handed
        # back to be saved
        self.assertEqual(sorted(updated), [2, 3])
        self.assertEqual(match_data(updated[2]), [["match"], [-2222222]])
        self.assertEqual(match_data(updated[3]), [["dontmatch"], ["match"], [444]])
        self.assertEqual(search.read, [2, 3, 4])
        self.assertEqual(search.queries, ['("word_list_field:*-1111111*")'])
        self.assertEqual((not_added.id, not_added["new_note_id_field"]), (0, "-1111111"))
        self.assertEqual(failed["new_note_id_field"], "-2222222")
        self.assertEqual(self.updater.clearing, [(0, 1), (1, 1)])

    def test_many_placeholders_are_searched_for_together_and_a_note_is_read_once(self):
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        first_and_last = sentence(2, w("様", [-1111111]), w("本", [-3333333, 2]))
        second = sentence(3, w("棚", [-2222222]))
        search = FakeSearch(first_and_last, second)

        with search.patched(), mock.patch.object(mwtn, "PLACEHOLDERS_PER_SEARCH", 2):
            updated = mwtn.clear_unadded_note_ids(notes, CONFIG, self.updater)

        self.assertEqual(
            search.queries,
            [
                '("word_list_field:*-1111111*" OR "word_list_field:*-2222222*")',
                '("word_list_field:*-3333333*")',
            ],
        )
        # the second search finds the first note again, in the collection as it was saved, and
        # takes it as the first search left it rather than reading it again
        self.assertEqual(search.read, [2, 3])
        self.assertEqual(sorted(updated), [2, 3])
        self.assertEqual(match_data(updated[2]), [["match"], ["match"]])
        self.assertEqual(match_data(updated[3]), [["match"]])
        self.assertEqual(self.updater.clearing, [(0, 3), (2, 3), (3, 3)])

    def test_a_field_that_is_no_array_is_left_as_it_is_and_tagged(self):
        broken_text = '[["様", "noun", "様", "さま", [-1111111], ['
        broken = FakeNote({"word_list_field": broken_text, "new_note_id_field": ""}, 5)
        # a note of a kind the match op writes no arrays into, whatever its field holds
        other_kind = FakeNote({"word_list_field": "-1111111"}, 6)
        search = FakeSearch(broken, other_kind)

        with search.patched():
            updated = mwtn.clear_unadded_note_ids([new_note("-1111111")], CONFIG, self.updater)

        self.assertEqual(search.read, [5, 6])
        self.assertEqual(list(updated), [5])
        self.assertEqual(updated[5]["word_list_field"], broken_text)
        self.assertEqual(updated[5].tags, [mwtn.INVALID_WORD_ARRAY_TAG])

    def test_a_note_holding_no_placeholder_is_not_searched_for(self):
        search = FakeSearch(sentence(2, w("様", [42])))

        with search.patched():
            updated = mwtn.clear_unadded_note_ids(
                [new_note(""), new_note("42")], CONFIG, self.updater
            )

        self.assertEqual((search.queries, updated), ([], {}))


class SearchableCollection(FakeCollection):
    """A collection whose saves reach the notes `FakeSearch` finds, as the cleanup's stages
    read what the stage before saved. The cancel is pressed during one note's add_note."""

    def __init__(self, search: FakeSearch, updater: FakeUpdater, failing: tuple = ()):
        super().__init__(failing)
        self.search = search
        self.updater = updater
        self.press_cancel_during = None

    def add_note(self, note, deck_id):
        super().add_note(note, deck_id)
        # From the note_will_be_added hooks, which run to the end whatever is pressed
        if note is self.press_cancel_during:
            self.updater.cancel_pressed = True

    def update_notes(self, notes):
        super().update_notes(notes)
        for note in notes:
            self.search.saved[note.id] = dict(note.fields)


class Recorder:
    """A new_notes_op / unadded_notes_op that records the notes it is handed."""

    def __init__(self, returns: dict | None = None, raises: bool = False):
        self.handed: list = []
        self.returns = returns or {}
        self.raises = raises

    def __call__(self, notes, config, progress_updater):
        self.handed.append(list(notes))
        if self.raises:
            raise Exception('Note type "Word" has not been configured in the settings.')
        return self.returns


def meaning_note(sort: str, meaning: str, placeholder: str) -> FakeNote:
    return FakeNote({
        "word_list_field": "",
        "new_note_id_field": placeholder,
        "word_sort_field": sort,
        "english_meaning_field": meaning,
        "meaning_field": "",
    })


class CancelledAddingTests(unittest.TestCase):
    """The cleanup's own cancel, pressed during the adding or the dedupe before it (SPEC D11).

    Added notes are resolved, failed ones keep their placeholders, and the ones never tried
    are unlinked: their words go back to be matched again.
    """

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def add(self, col, notes, **ops):
        new_notes_op = ops.pop("new_notes_op", mwtn.update_fake_note_ids)
        return base_ops.add_new_notes(
            col, notes, CONFIG, POS, self.updater, new_notes_op, **ops
        )

    def test_a_cancel_between_adds_leaves_the_rest_to_be_unlinked(self):
        notes = [new_note(p) for p in ("-1111111", "-2222222", "-3333333", "-4444444")]
        added_first, failing, added_last, left_out = notes
        referencing = sentence(
            2,
            w("様", [-1111111]),
            w("本", [-2222222, 3]),
            w("棚", [-3333333]),
            w("机", [-4444444, 2]),
        )
        search = FakeSearch(referencing)
        col = SearchableCollection(search, self.updater, failing=(failing,))
        col.press_cancel_during = added_last
        resolved: list = []

        def new_notes_op(new_notes, config, progress_updater):
            resolved.extend(new_notes)
            return mwtn.update_fake_note_ids(new_notes, config, progress_updater)

        with search.patched():
            result = self.add(
                col,
                notes,
                new_notes_op=new_notes_op,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )

        # The note being added when Cancel was pressed is added: its hooks run to the end
        self.assertEqual([note for note, _ in col.added], [added_first, added_last])
        self.assertEqual(resolved, [added_first, added_last])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(2, 1, 1))
        self.assertEqual(left_out.id, 0)
        saved = FakeNote(dict(search.saved[2]), 2)
        # resolved, kept as the trace of a failed add, resolved, matched again
        self.assertEqual(match_data(saved), [[501], [-2222222, 3], [502], ["match"]])
        self.assertTrue(self.updater.cancel_ended)
        self.assertEqual(self.updater.clearing[-1], (1, 1))
        # Every write in the run's one undo entry, the unlinking's last
        self.assertEqual(set(col.merged), {POS})
        self.assertEqual(result.op_changes, f"changes after {len(col.merged)} merges")
        self.assertIn(2, result.updated_nids)

    def test_the_unlinking_gets_only_the_notes_never_tried(self):
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        col = SearchableCollection(FakeSearch(), self.updater, failing=(notes[0],))
        col.press_cancel_during = notes[1]
        resolving, unlinking = Recorder(), Recorder()

        result = self.add(col, notes, new_notes_op=resolving, unadded_notes_op=unlinking)

        self.assertEqual(resolving.handed, [[notes[1]]])
        self.assertEqual(unlinking.handed, [[notes[2]]])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 1, 1))
        self.assertEqual(self.updater.adding[-1], (1, 3, 1))

    def test_a_cancel_before_the_first_add_adds_nothing_and_unlinks_everything(self):
        notes = [new_note("-1111111"), new_note("-2222222")]
        col = FakeCollection()
        self.updater.cancel_pressed = True
        resolving, unlinking = Recorder(), Recorder()

        result = self.add(col, notes, new_notes_op=resolving, unadded_notes_op=unlinking)

        self.assertEqual(col.added, [])
        self.assertEqual(resolving.handed, [])
        self.assertEqual(unlinking.handed, [notes])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 0, 2))
        self.assertTrue(self.updater.cancel_ended)

    def test_without_a_cancel_nothing_is_unlinked(self):
        notes = [new_note("-1111111"), new_note("-2222222")]
        col = FakeCollection(failing=(notes[1],))
        resolving, unlinking = Recorder(), Recorder()

        result = self.add(col, notes, new_notes_op=resolving, unadded_notes_op=unlinking)

        self.assertEqual(resolving.handed, [[notes[0]]])
        self.assertEqual(unlinking.handed, [])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 1, 0))
        # Nothing is left for the Cancel to stop once the adding is over
        self.assertTrue(self.updater.cancel_ended)

    def test_a_note_with_no_deck_to_go_to_is_a_failed_add(self):
        no_deck = new_note("-1111111")
        no_deck.note_type = lambda: {"name": "Other"}  # type: ignore[method-assign]
        config = {**CONFIG, "Other": {**CONFIG["Word"], "insert_deck": "Missing"}}
        col = FakeCollection()
        unlinking = Recorder()

        result = base_ops.add_new_notes(
            col, [no_deck], config, POS, self.updater, unadded_notes_op=unlinking
        )

        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 1, 0))
        self.assertEqual(self.updater.adding[-1], (0, 1, 1))
        self.assertEqual(unlinking.handed, [])

    def test_a_cancel_during_the_dedupe_unlinks_every_note_as_prepared(self):
        """The duplicate the dedupe dropped included: an interrupted dedupe leaves some of
        its references pointing at it."""
        kept, duplicate, other = new_note("-1111111"), new_note("-2222222"), new_note("-3333333")
        remapped = FakeNote({"word_list_field": "[[-1111111]]"}, note_id=2)
        removed = FakeNote({"word_list_field": "[[-1111111]]"}, note_id=3)
        col = FakeCollection()
        unlinking = Recorder(returns={4: FakeNote({}, 4), 0: FakeNote({}, 0)})

        def dedupe(notes, config, progress_updater):
            self.updater.cancel_pressed = True
            return [kept, other], {2: remapped, 3: removed}

        resolving = Recorder()
        result = self.add(
            col,
            [kept, duplicate, other],
            new_notes_op=resolving,
            filter_new_notes_op=dedupe,
            unadded_notes_op=unlinking,
            notes_to_remove={3},
        )

        self.assertEqual(col.added, [])
        self.assertEqual(resolving.handed, [])
        self.assertEqual(unlinking.handed, [[kept, duplicate, other]])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 0, 3))
        # What the dedupe remapped is saved before the unlinking searches the collection;
        # a note the run removed and a note never added are not
        self.assertEqual([note.id for note in col.updated], [2, 4])
        self.assertEqual(result.filtered_nids, [2])
        self.assertEqual(result.updated_nids, [4])
        self.assertEqual(col.merged, [POS, POS])

    def test_a_cancel_in_the_adding_after_a_finished_dedupe_leaves_its_duplicates_out(self):
        """The dedupe remapped every reference to a duplicate it dropped, so only the rest of
        the deduped list is left to unlink."""
        kept, duplicate, other = new_note("-1111111"), new_note("-2222222"), new_note("-3333333")
        col = SearchableCollection(FakeSearch(), self.updater)
        col.press_cancel_during = kept
        unlinking = Recorder()

        result = self.add(
            col,
            [kept, duplicate, other],
            new_notes_op=Recorder(),
            filter_new_notes_op=lambda notes, config, updater: ([kept, other], {}),
            unadded_notes_op=unlinking,
        )

        self.assertEqual([note for note, _ in col.added], [kept])
        self.assertEqual(unlinking.handed, [[other]])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 0, 1))

    def test_a_cancel_before_the_dedupe_skips_it(self):
        dedupe = Recorder()
        unlinking = Recorder()
        self.updater.cancel_pressed = True
        notes = [new_note("-1111111")]

        self.add(FakeCollection(), notes, filter_new_notes_op=dedupe, unadded_notes_op=unlinking)

        self.assertEqual((dedupe.handed, unlinking.handed), ([], [notes]))

    def test_the_cancel_is_armed_before_the_dedupe_with_every_note_as_prepared(self):
        notes = [new_note("-1111111"), new_note("-2222222"), new_note("-3333333")]
        armed_at_dedupe: list = []

        def dedupe(notes, config, progress_updater):
            armed_at_dedupe.extend(self.updater.armed)
            return notes[:1], {}

        self.add(FakeCollection(), notes, filter_new_notes_op=dedupe, new_notes_op=None)

        self.assertEqual(armed_at_dedupe, [3])
        self.assertEqual(self.updater.armed, [3])

    def test_with_nothing_to_add_the_cancel_is_never_armed(self):
        """Arming waits on the main thread; nothing would be left for the cancel to stop."""
        dedupe = Recorder()

        result = self.add(FakeCollection(), [], filter_new_notes_op=dedupe)

        self.assertEqual((self.updater.armed, dedupe.handed), ([], []))
        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 0, 0))

    def test_an_unlinking_that_raises_loses_nothing_else(self):
        notes = [new_note("-1111111"), new_note("-2222222")]
        col = FakeCollection()
        col_add = col.add_note

        def add_and_press(note, deck_id):
            col_add(note, deck_id)
            self.updater.cancel_pressed = True

        col.add_note = add_and_press  # type: ignore[method-assign]
        referencing = FakeNote({"word_list_field": "[]"}, note_id=2)

        with self.assertLogs(base_ops.logger, "ERROR") as logs:
            result = self.add(
                col,
                notes,
                new_notes_op=Recorder(returns={2: referencing}),
                unadded_notes_op=Recorder(raises=True),
            )

        self.assertIn("Error unlinking the new notes not added", "\n".join(logs.output))
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 0, 1))
        self.assertEqual(col.updated, [referencing])
        self.assertEqual(result.updated_nids, [2])


def unconfigured_note(placeholder: str) -> FakeNote:
    """A new note whose note type the config does not know: get_field_config raises for it."""
    note = new_note(placeholder)
    note.note_type = lambda: {"name": "Unconfigured"}  # type: ignore[method-assign]
    return note


class CleanupTailTests(unittest.TestCase):
    """What the adding leaves for after it happens whatever raised before (review finding C5).

    The adding's cancel is ended however the adding stops, and a resolving that raises does
    not cost the unlinking: that is what puts back the words of notes that will never exist.
    """

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def test_a_resolving_that_raises_after_a_partial_cancel_still_unlinks_then_fails(self):
        notes = [new_note("-1111111"), new_note("-2222222")]
        col = SearchableCollection(FakeSearch(), self.updater)
        col.press_cancel_during = notes[0]
        unlinked = FakeNote({"word_list_field": "[]"}, note_id=9)
        unlinking = Recorder(returns={9: unlinked})

        with (
            self.assertRaisesRegex(Exception, "has not been configured"),
            self.assertLogs(base_ops.logger, "ERROR") as logs,
        ):
            base_ops.add_new_notes(
                col, notes, CONFIG, POS, self.updater, Recorder(raises=True),
                unadded_notes_op=unlinking,
            )

        self.assertEqual(unlinking.handed, [[notes[1]]])
        self.assertEqual(col.updated, [unlinked])
        # The add, then the unlinking's save, both in the run's undo entry
        self.assertEqual(col.merged, [POS, POS])
        self.assertIn("Error resolving the new notes' ids", "\n".join(logs.output))
        self.assertTrue(self.updater.cancel_ended)

    def test_a_resolving_that_raises_with_nothing_to_unlink_still_fails(self):
        with self.assertRaisesRegex(Exception, "has not been configured"), self.assertLogs(
            base_ops.logger, "ERROR"
        ):
            base_ops.add_new_notes(
                FakeCollection(), [new_note("-1111111")], CONFIG, POS, self.updater,
                Recorder(raises=True), unadded_notes_op=Recorder(),
            )

    def test_a_raise_in_the_add_loop_still_ends_the_cancel(self):
        notes = [new_note("-1111111"), unconfigured_note("-2222222")]

        with self.assertRaisesRegex(Exception, "has not been configured"):
            base_ops.add_new_notes(FakeCollection(), notes, CONFIG, POS, self.updater)

        self.assertEqual(self.updater.armed, [2])
        self.assertTrue(self.updater.cancel_ended)

    def test_a_dedupe_that_raises_still_ends_the_cancel(self):
        def dedupe(notes, config, progress_updater):
            raise RuntimeError("the dedupe broke")

        with self.assertRaisesRegex(RuntimeError, "the dedupe broke"):
            base_ops.add_new_notes(
                FakeCollection(), [new_note("-1111111")], CONFIG, POS, self.updater,
                filter_new_notes_op=dedupe,
            )

        self.assertTrue(self.updater.cancel_ended)


class EditedNotesCountTests(unittest.TestCase):
    """How the final message counts the notes the note adding saved (review finding C6): a
    selected note as one of the selection edited, any other as an other note, each once."""

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def test_notes_resolved_or_unlinked_outside_the_selection_are_other_notes(self):
        added = base_ops.NewNotesAdded(
            base_ops.NewNotesCounts(1, 0, 1), None, updated_nids=[2, 9, 3], filtered_nids=[3, 8]
        )
        # What the run's own writes already counted
        edited, other = [2], [8]

        base_ops.count_new_notes_edits(added, {1, 2, 3}, edited, other)

        self.assertEqual((edited, other), ([2, 3], [8, 9]))

    def test_through_a_cancelled_adding_the_added_notes_are_not_counted_as_edited(self):
        """They are counted as added; the notes rewritten for them are what was edited."""
        added_note, left_out = new_note("-1111111"), new_note("-2222222")
        selected = sentence(2, w("様", [-1111111]))
        not_selected = sentence(9, w("本", [-2222222, 3]))
        search = FakeSearch(selected, not_selected)
        col = SearchableCollection(search, self.updater)
        col.press_cancel_during = added_note

        with search.patched():
            result = base_ops.add_new_notes(
                col, [added_note, left_out], CONFIG, POS, self.updater,
                mwtn.update_fake_note_ids, unadded_notes_op=mwtn.clear_unadded_note_ids,
            )
        edited: list = []
        other: list = []
        base_ops.count_new_notes_edits(result, {2}, edited, other)

        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 0, 1))
        # The added note was saved too, with its own id written in
        self.assertIn(added_note, col.updated)
        self.assertEqual((edited, other), ([2], [9]))


class DedupeCancelTests(unittest.TestCase):
    """deduplicate_notes_list stops between its merges when the adding is cancelled."""

    def setUp(self):
        self.updater = FakeUpdater()
        self.notes = [
            meaning_note("hon(m1)", "book", "-1000001"),
            meaning_note("hon(m2)", "book", "-1000002"),
            meaning_note("yama(m1)", "mountain", "-1000003"),
            meaning_note("yama(m2)", "mountain", "-1000004"),
        ]
        self.search = FakeSearch(sentence(2, w("本", [-1000002]), w("山", [-1000004, 2])))
        find_notes = self.search.find_notes

        def find_and_press(query):
            # Pressed while the first merge searches the collection
            self.updater.cancel_pressed = True
            return find_notes(query)

        self.search.find_notes = find_and_press  # type: ignore[method-assign]

    def test_a_cancel_between_merges_returns_every_note_and_what_was_remapped(self):
        with self.search.patched():
            notes, updated = mwtn.deduplicate_notes_list(self.notes, CONFIG, self.updater)

        self.assertEqual(notes, self.notes)
        # The first merge remapped its duplicate to the note kept; the second never ran
        self.assertEqual(match_data(updated[2]), [[-1000001], [-1000004, 2]])
        self.assertEqual(len(self.search.queries), 1)

    def test_without_an_updater_nothing_stops_it(self):
        """The dedupe of existing notes calls it with none, outside any cleanup."""
        with self.search.patched():
            notes, updated = mwtn.deduplicate_notes_list(self.notes, CONFIG)

        self.assertEqual(notes, [self.notes[0], self.notes[2]])
        self.assertEqual(match_data(updated[2]), [[-1000001], [-1000003, 2]])

    def test_through_the_cleanup_every_word_linked_to_a_prepared_note_is_matched_again(self):
        col = SearchableCollection(self.search, self.updater)
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)

        with self.search.patched():
            result = base_ops.add_new_notes(
                col,
                self.notes,
                CONFIG,
                POS,
                self.updater,
                mwtn.update_fake_note_ids,
                filter_new_notes_op=mwtn.deduplicate_notes_list,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )

        self.assertEqual(col.added, [])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 0, 4))
        # The remapped word and the one the dedupe never reached, both unlinked
        saved = FakeNote(dict(self.search.saved[2]), 2)
        self.assertEqual(match_data(saved), [["match"], ["match"]])


def vocab_note(sort: str, note_id: int = 0, **fields: str) -> FakeNote:
    """A vocab note with every field the match op reads, each named after its config key."""
    values = {key: "" for key in mwtn.MATCH_FIELD_KEYS}
    values.update(word_sort_field=sort, meaning_field="意味", **fields)
    return FakeNote(values, note_id)


def meaning_number(note: FakeNote) -> int:
    found = re.search(r"\(m(\d+)\)", note["word_sort_field"])
    return int(found.group(1)) if found else 0


def reading_type(processed_furigana: str) -> str:
    """check_word_reading_type, read off the tags the stand-in kana_highlight puts in."""
    for kind in ("kun", "on"):
        if f"<{kind}>" in processed_furigana:
            return kind
    return ""


# A note clean_up's Cancel is pressed during that is never added: nothing presses it
NO_CANCEL = object()


class FakeMarkerIndex:
    """The run's word index, asked only for the notes of a word that may carry markers."""

    def __init__(self, *nids: int):
        self.nids = list(nids)

    def marker_note_ids(self, word, regex):
        return list(self.nids)


class NewNoteHarness(unittest.TestCase):
    """Makes new notes of one word through the match op's real creation paths, over a stand-in
    collection, and runs the cleanup's adding over them."""

    WORD = "言葉"

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()
        self.to_add: dict = {}
        self.to_update: dict = {}
        self.current = vocab_note("", 1)
        # What the stand-in kana_highlight makes of the next new reading's furigana
        self.processed_furigana = ""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
            ("clean_meaning_in_note", lambda **_: True),
            ("copy_into_new_note", lambda note: FakeNote(dict(note.fields))),
            ("Note", lambda col, model: vocab_note("")),
            ("make_furigana_from_reading", lambda word, reading: f"{word}[{reading}]"),
            ("kana_highlight", lambda **_: self.processed_furigana),
            ("check_word_reading_type", reading_type),
        ):
            stack.enter_context(mock.patch.object(mwtn, name, value))
        stack.enter_context(mock.patch.object(mw, "col", None, create=True))

    def args(self, reading: str = "ことば", index=None) -> dict:
        return {
            **{key: key for key in mwtn.MATCH_FIELD_KEYS},
            "word": self.WORD,
            "reading": reading,
            "sentence": "",
            "word_index": 0,
            "part_of_speech": "noun",
            "current_note": self.current,
            "note_type": {"name": "Word"},
            "notes_to_add_dict": self.to_add,
            "notes_to_update_dict": self.to_update,
            "all_generated_meanings_dict": {},
            "processed_word_tuples": {},
            "word_note_index": index,
            "sentence_cache": None,
            "note_cache": None,
        }

    def new_meaning(self, en_meaning: str, *matching: FakeNote) -> FakeNote:
        """A CREATE NEW as match_single_word_in_word_tuple makes one: copied from the first of
        the notes matched with the largest meaning number, given the number after it."""
        matching_notes = [
            self.to_update.get(note.id, note) if note.id else note for note in matching
        ]
        largest = max(meaning_number(note) for note in matching_notes)
        note_to_copy = next(n for n in matching_notes if meaning_number(n) == largest)
        mwtn.create_new_note_from_matched_note(
            CONFIG, note_to_copy, matching_notes, largest + 1, "意味", en_meaning, "", self.args()
        )
        return self.to_add[self.WORD][-1]

    def new_reading(self, reading: str, *marker_nids: int) -> FakeNote:
        created = mwtn.create_new_note_without_matching(
            CONFIG, "", self.args(reading, FakeMarkerIndex(*marker_nids))
        )
        self.assertTrue(created)
        return self.to_add[self.WORD][-1]

    def clean_up(self, search: FakeSearch, notes=None, cancel_during=None, failing: tuple = ()):
        """The cleanup from its first save on: the notes the run edited, then the adding, with
        Cancel pressed before the first note is added, or while `cancel_during` is (NO_CANCEL:
        never). The notes in `failing` fail to add."""
        col = SearchableCollection(search, self.updater, failing)
        col.update_notes([note for note in self.to_update.values() if note.id])
        if notes is None:
            notes = [note for word_notes in self.to_add.values() for note in word_notes]
        self.updater.cancel_pressed = cancel_during is None
        col.press_cancel_during = cancel_during
        with search.patched():
            base_ops.add_new_notes(
                col,
                notes,
                CONFIG,
                POS,
                self.updater,
                mwtn.update_fake_note_ids,
                filter_new_notes_op=mwtn.deduplicate_notes_list,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )
        return col

    def tidy(self, col: "SearchableCollection") -> list:
        """The cleanup's last stage, over every note clean_up saved and added: the ids of the
        notes it renamed."""
        saved = [*col.updated, *(note for note, _ in col.added)]
        with col.search.patched():
            _, renamed = base_ops.tidy_markers(
                col, saved, CONFIG, POS, self.updater, mwtn.tidy_sort_field_markers
            )
        return renamed

    def sort_field(self, search: FakeSearch, note) -> str:
        return search.saved[note if isinstance(note, int) else note.id]["word_sort_field"]


class SiblingMarkersTests(NewNoteHarness):
    """The markers a new note puts on the other notes of its word, undone when it is not added.

    A second meaning copied from a note renames that note (m1); a new reading of a word gives
    the word's other notes (r1), or (kun)/(on). They are saved with the run's other edits,
    before the adding starts, so a cancel that leaves the new note out has to write them back.
    """

    def test_a_second_meaning_not_added_leaves_the_first_note_as_it_was(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            second = self.new_meaning("words", search.get_note(2))
        self.assertEqual(second["word_sort_field"], f"{self.WORD} (m2)")

        col = self.clean_up(search)

        self.assertEqual(col.added, [])
        # Saved renamed with the run's edits, then written back, into the run's undo entry
        self.assertEqual(col.updated[0]["word_sort_field"], f"{self.WORD} (m1)")
        self.assertEqual(search.saved[2]["word_sort_field"], self.WORD)
        self.assertEqual(set(col.merged), {POS})

    def test_a_cancel_during_the_dedupe_takes_the_rename_back_too(self):
        """The dedupe stops before renumbering its notes then, but it renumbers only new
        notes, and none of them is added."""
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            first = search.get_note(2)
            second = self.new_meaning("words", first)
            self.new_meaning("words", first, second)
        find_notes = search.find_notes

        def find_and_press(query):
            # Pressed while the dedupe's first merge searches the collection
            self.updater.cancel_pressed = True
            return find_notes(query)

        search.find_notes = find_and_press  # type: ignore[method-assign]

        col = self.clean_up(search, cancel_during=second)

        self.assertEqual(col.added, [])
        self.assertEqual(search.saved[2]["word_sort_field"], self.WORD)

    def test_the_rename_stays_when_the_second_meaning_is_added(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            first = search.get_note(2)
            second = self.new_meaning("words", first)
            third = self.new_meaning("speech", first, second)
        self.assertEqual(third["word_sort_field"], f"{self.WORD} (m3)")

        col = self.clean_up(search, cancel_during=second)

        self.assertEqual([note for note, _ in col.added], [second])
        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (m1)")

    def test_notes_numbered_before_the_run_are_not_touched(self):
        search = FakeSearch(vocab_note(f"{self.WORD} (m1)", 2), vocab_note(f"{self.WORD} (m2)", 3))
        with search.patched():
            third = self.new_meaning("speech", search.get_note(2), search.get_note(3))
        self.assertEqual(third["word_sort_field"], f"{self.WORD} (m3)")

        col = self.clean_up(search)

        self.assertEqual(col.updated, [])
        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (m1)")
        self.assertEqual(search.saved[3]["word_sort_field"], f"{self.WORD} (m2)")

    def test_a_reading_number_is_taken_back(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            other_reading = self.new_reading("げんご", 2)
        self.assertEqual(other_reading["word_sort_field"], f"{self.WORD} (r2)")
        self.assertEqual(self.to_update[2]["word_sort_field"], f"{self.WORD} (r1)")

        self.clean_up(search)

        self.assertEqual(search.saved[2]["word_sort_field"], self.WORD)

    def test_a_kun_marker_is_taken_back(self):
        search = FakeSearch(
            vocab_note(self.WORD, 2, word_processed_furigana_field="<kun>こと</kun>ば")
        )
        self.processed_furigana = "<on>げん</on>ご"
        with search.patched():
            other_reading = self.new_reading("げんご", 2)
        self.assertEqual(other_reading["word_sort_field"], f"{self.WORD} (on)")
        self.assertEqual(self.to_update[2]["word_sort_field"], f"{self.WORD} (kun)")

        self.clean_up(search)

        self.assertEqual(search.saved[2]["word_sort_field"], self.WORD)

    def test_the_run_s_other_edits_of_the_renamed_note_are_kept(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            second = self.new_meaning("words", search.get_note(2))
        placeholder = int(second["new_note_id_field"])
        # Edited elsewhere in the run: its meaning, and its sentence linked to the new note
        renamed = self.to_update[2]
        renamed["meaning_field"] = "新しい意味"
        renamed["word_list_field"] = mwtn.format_word_array([w(self.WORD, [placeholder, 3])])

        self.clean_up(search)

        saved = FakeNote(dict(search.saved[2]), 2)
        self.assertEqual(saved["word_sort_field"], self.WORD)
        self.assertEqual(saved["meaning_field"], "新しい意味")
        self.assertEqual(match_data(saved), [["match"]])

    def test_a_note_made_seeing_the_rename_keeps_it_when_added(self):
        """The notes of one word are added in the order they were made, so one made seeing a
        rename is left out whenever the note that made it is. Not so across words: a ずる
        word's new meaning is copied from its じる note, and is added among the じる word's."""
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            first = search.get_note(2)
            second = self.new_meaning("words", first)
            third = self.new_meaning("speech", first, second)

        col = self.clean_up(search, notes=[third, second], cancel_during=third)

        self.assertEqual([note for note, _ in col.added], [third])
        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (m1)")

    def test_a_reading_made_seeing_the_numbering_keeps_it_when_added(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            second = self.new_reading("げんご", 2)
            third = self.new_reading("ことのは", 2)
        self.assertEqual(third["word_sort_field"], f"{self.WORD} (r3)")

        self.clean_up(search, notes=[third, second], cancel_during=third)

        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (r1)")

    def test_a_note_renamed_since_is_left_as_it_is(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            self.new_meaning("words", search.get_note(2))
        # Renamed again after the new note, by something the run does not record
        self.to_update[2]["word_sort_field"] = f"{self.WORD} (m5)"

        self.clean_up(search)

        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (m5)")

    def test_renames_by_two_notes_left_out_are_undone_newest_first(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            other_reading = self.new_reading("げんご", 2)
            self.new_meaning("words", search.get_note(2))
        self.assertEqual(self.to_update[2]["word_sort_field"], f"{self.WORD} (r1)(m1)")

        self.clean_up(search)

        self.assertEqual(search.saved[2]["word_sort_field"], self.WORD)
        self.assertEqual(other_reading.id, 0)

    def test_only_the_rename_by_the_note_left_out_is_undone(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            other_reading = self.new_reading("げんご", 2)
            self.new_meaning("words", search.get_note(2))

        self.clean_up(search, cancel_during=other_reading)

        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (r1)")

    def test_a_new_note_renamed_by_one_left_out_is_written_back_once_added(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            other_reading = self.new_reading("げんご", 2)
            second = self.new_meaning("words", other_reading)
        self.assertEqual(other_reading["word_sort_field"], f"{self.WORD} (r2)(m1)")
        self.assertEqual(second["word_sort_field"], f"{self.WORD} (r2)(m2)")

        self.clean_up(search, cancel_during=other_reading)

        self.assertEqual(search.saved[other_reading.id]["word_sort_field"], f"{self.WORD} (r2)")
        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (r1)")


class FinalMessageTests(unittest.TestCase):
    def setUp(self):
        self.shown: list = []
        saved = base_ops.tooltip
        base_ops.tooltip = lambda message, **_: self.shown.append(message)
        self.addCleanup(setattr, base_ops, "tooltip", saved)

    def succeed(self, added: int, failed: int = 0, not_added: int = 0) -> str:
        counts = base_ops.NewNotesCounts(added, failed, not_added)
        base_ops.on_bulk_success(None, "Matched words", [1], [], [1, 2], None, new_notes=counts)
        return self.shown[-1]

    def test_the_new_notes_added_are_counted(self):
        self.assertIn("Added 3 new notes.", self.succeed(3))
        self.assertIn("Added 1 new note.", self.succeed(1))

    def test_no_line_when_none_were_added(self):
        self.assertNotIn("Added", self.succeed(0))

    def test_a_cancelled_adding_says_what_it_left_out_and_what_failed(self):
        message = self.succeed(2, failed=1, not_added=4)

        self.assertIn("Added 2 of 7 new notes.", message)
        self.assertIn("4 not added, as the adding was cancelled", message)
        self.assertIn("left to be matched again", message)
        self.assertIn("1 could not be added", message)
        self.assertIn("keep their placeholder ids", message)

    def test_a_cancel_before_the_first_add_is_said_too(self):
        message = self.succeed(0, not_added=5)

        self.assertIn("Added 0 of 5 new notes.", message)
        self.assertIn("5 not added", message)
        self.assertNotIn("could not be added", message)


KUN_FURIGANA = "<kun>こと</kun>ば"
ON_FURIGANA = "<on>げん</on>ご"


class ReadingMarkerOrderTests(NewNoteHarness):
    """A new reading's markers, and the ones it puts on the word's other notes, come out the
    same whatever order the word's notes are found in.

    They were once decided inside the loop over those notes, once per note from what had been
    seen so far, and whether any kun/on note carried its marker was overwritten by each note
    instead of added up. A rename made from half the facts stayed: the same notes gave every
    note (r1) in one order and left them all alone in the other.
    """

    def new_reading_both_orders(self, notes: list, processed_furigana: str) -> tuple:
        """The new note's sort field and the others' as saved, the same in both orders."""
        results = []
        for order in (list(notes), list(reversed(notes))):
            self.to_add.clear()
            self.to_update.clear()
            search = FakeSearch(*order)
            self.processed_furigana = processed_furigana
            with search.patched():
                new = self.new_reading("ことば", *(note.id for note in order))
            others = {
                note.id: self.to_update.get(note.id, note)["word_sort_field"] for note in notes
            }
            results.append((new["word_sort_field"], others))
        self.assertEqual(results[0], results[1], "the notes' order changed the markers")
        return results[0]

    def test_an_on_note_found_last_does_not_leave_numbers_on_every_note(self):
        W = self.WORD
        kun = vocab_note(W, 2, word_processed_furigana_field=KUN_FURIGANA)
        on = vocab_note(f"{W} (on)", 3, word_processed_furigana_field=ON_FURIGANA)

        new, others = self.new_reading_both_orders([kun, on], KUN_FURIGANA)

        self.assertEqual(new, f"{W} (kun)")
        self.assertEqual(others, {2: W, 3: f"{W} (on)"})

    def test_a_numbered_note_found_last_does_not_number_the_others(self):
        W = self.WORD
        plain = vocab_note(W, 2)
        numbered = vocab_note(f"{W} (r2)", 3)

        new, others = self.new_reading_both_orders([plain, numbered], "")

        self.assertEqual(new, f"{W} (r3)")
        self.assertEqual(others, {2: W, 3: f"{W} (r2)"})

    def test_one_kun_note_with_its_marker_is_enough_whichever_comes_last(self):
        W = self.WORD
        marked = vocab_note(f"{W} (kun)", 2, word_processed_furigana_field=KUN_FURIGANA)
        unmarked = vocab_note(W, 3, word_processed_furigana_field=KUN_FURIGANA)
        on = vocab_note(f"{W} (on)", 4, word_processed_furigana_field=ON_FURIGANA)

        new, others = self.new_reading_both_orders([marked, unmarked, on], KUN_FURIGANA)

        # Another kun reading among marked kun notes: numbered, and so are they
        self.assertEqual(new, f"{W} (kun)(r2)")
        self.assertEqual(others[2], f"{W} (kun)(r1)")
        self.assertEqual(others[4], f"{W} (on)")

    def test_a_second_reading_numbers_both(self):
        W = self.WORD
        first = vocab_note(W, 2, word_processed_furigana_field=KUN_FURIGANA)

        new, others = self.new_reading_both_orders([first], KUN_FURIGANA)

        self.assertEqual(new, f"{W} (r2)")
        self.assertEqual(others, {2: f"{W} (r1)"})

    def test_the_first_note_of_a_word_gets_no_marker(self):
        search = FakeSearch()
        self.processed_furigana = KUN_FURIGANA
        with search.patched():
            new = self.new_reading("ことば")

        self.assertEqual(new["word_sort_field"], self.WORD)
        self.assertEqual(self.to_update, {})


if __name__ == "__main__":
    unittest.main()


class TidySortFieldMarkersTests(unittest.TestCase):
    """The marker tidying, given the notes the cleanup saved: which words it reads, and that it
    tidies each word whole (sort_field_markers.tidy_word_markers says how)."""

    WORD = "言葉"

    def setUp(self):
        self.updater = FakeUpdater()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(mwtn, "check_word_reading_type", reading_type))

    def tidy(self, search: FakeSearch, *notes) -> dict:
        with search.patched():
            renamed = mwtn.tidy_sort_field_markers(list(notes), CONFIG, self.updater)
        return {nid: note["word_sort_field"] for nid, note in renamed.items()}

    def test_the_word_is_tidied_whole_with_notes_the_run_never_touched(self):
        search = FakeSearch(
            vocab_note(f"{self.WORD} (m1)", 2),
            vocab_note(f"{self.WORD} (m3)", 3),
            vocab_note(f"{self.WORD} (m4)", 4),
        )
        added = FakeNote(dict(search.saved[4]), 4)

        self.assertEqual(
            self.tidy(search, added), {3: f"{self.WORD} (m2)", 4: f"{self.WORD} (m3)"}
        )

    def test_a_tidy_word_renames_nothing(self):
        search = FakeSearch(vocab_note(f"{self.WORD} (m1)", 2), vocab_note(f"{self.WORD} (m2)", 3))

        self.assertEqual(self.tidy(search, search.get_note(3)), {})

    def test_only_the_words_a_saved_note_carries_markers_for_are_read(self):
        search = FakeSearch(
            vocab_note(self.WORD, 2),
            vocab_note("言語 (r2)", 3),
            vocab_note("単語 (m1)", 4),
            vocab_note("単語", 5),
        )

        renamed = self.tidy(search, *search.get_notes([2, 3, 4]))

        self.assertEqual(search.sort_bases, [("word_sort_field", ["単語", "言語"])])
        self.assertEqual(renamed, {3: "言語", 4: "単語 (m2)", 5: "単語 (m1)"})

    def test_with_no_markers_nothing_is_read(self):
        search = FakeSearch(vocab_note(self.WORD, 2), vocab_note(f"{self.WORD} (x1)", 3))

        self.assertEqual(self.tidy(search, *search.get_notes([2, 3])), {})
        self.assertEqual(search.sort_bases, [])
        self.assertEqual(search.read, [2, 3])

    def test_a_note_not_added_or_of_a_note_type_not_configured_is_not_looked_at(self):
        search = FakeSearch(vocab_note(f"{self.WORD} (m1)", 2))
        not_added = vocab_note(f"{self.WORD} (m1)")
        unconfigured = vocab_note(f"{self.WORD} (m1)", 2)
        unconfigured.note_type = lambda: {"name": "Sentence"}  # type: ignore[method-assign]

        self.assertEqual(self.tidy(search, not_added, unconfigured), {})
        self.assertEqual(search.sort_bases, [])

    def test_an_unmarked_reading_is_of_the_kind_its_furigana_says(self):
        kun, on = "<kun>こと</kun>ば", "<on>げん</on>ご"
        search = FakeSearch(
            vocab_note(f"{self.WORD} (kun)", 2, word_processed_furigana_field=kun),
            vocab_note(self.WORD, 3, word_processed_furigana_field=on),
        )
        self.assertEqual(self.tidy(search, search.get_note(2)), {})

        search.saved[3]["word_processed_furigana_field"] = kun
        self.assertEqual(
            self.tidy(search, search.get_note(2)),
            {2: f"{self.WORD} (r1)", 3: f"{self.WORD} (r2)"},
        )

    def test_each_note_s_kind_is_read_from_its_own_note_type_s_field(self):
        kun, on = "<kun>こと</kun>ば", "<on>げん</on>ご"
        config = {**CONFIG, "Kana": {**CONFIG["Word"], "word_processed_furigana_field": "kana"}}
        search = FakeSearch(
            vocab_note(f"{self.WORD} (kun)", 2, word_processed_furigana_field=kun),
            vocab_note(self.WORD, 3, word_processed_furigana_field=kun, kana=on),
        )
        search.note_types[3] = "Kana"

        with search.patched():
            renamed = mwtn.tidy_sort_field_markers(search.get_notes([2]), config, self.updater)

        # Read as an on reading, so the (kun) still tells the two apart
        self.assertEqual(renamed, {})

    def test_a_note_of_a_type_with_another_sort_field_is_not_the_word_s(self):
        config = {**CONFIG, "Other": {**CONFIG["Word"], "word_sort_field": "other_sort"}}
        search = FakeSearch(
            vocab_note(f"{self.WORD} (m1)", 2),
            vocab_note(f"{self.WORD} (m2)", 3, other_sort=""),
        )
        search.note_types[3] = "Other"

        with search.patched():
            renamed = mwtn.tidy_sort_field_markers(search.get_notes([2]), config, self.updater)

        self.assertEqual({nid: n["word_sort_field"] for nid, n in renamed.items()}, {2: self.WORD})

    def test_the_progress_counts_words(self):
        search = FakeSearch(vocab_note("言語 (r2)", 3), vocab_note("単語 (m1)", 4))

        self.tidy(search, *search.get_notes([3, 4]))

        self.assertEqual(self.updater.tidying, [(0, 2), (1, 2), (2, 2)])


class TidyMarkersStageTests(unittest.TestCase):
    """base_ops.tidy_markers, the cleanup stage that saves what the tidying renamed."""

    def setUp(self):
        self.updater = FakeUpdater()
        self.col = FakeCollection()

    def run_stage(self, op, notes=(), notes_to_remove=None):
        return base_ops.tidy_markers(
            self.col, list(notes), CONFIG, POS, self.updater, op, notes_to_remove
        )

    def test_the_notes_renamed_are_saved_into_the_run_s_undo_entry(self):
        renamed = vocab_note("言葉", 2)
        saved = [vocab_note("言葉 (m1)", 2)]
        op = Recorder(returns={2: renamed})

        changes, nids = self.run_stage(op, saved)

        self.assertEqual(op.handed, [saved])
        self.assertEqual(self.col.updated, [renamed])
        self.assertEqual(self.col.merged, [POS])
        self.assertEqual((changes, nids), ("changes after 1 merges", [2]))

    def test_a_note_removed_by_the_run_or_without_an_id_is_not_saved(self):
        op = Recorder(returns={2: vocab_note("言葉", 2), 0: vocab_note("言葉")})

        self.assertEqual(self.run_stage(op, notes_to_remove={2}), (None, []))
        self.assertEqual((self.col.updated, self.col.merged), ([], []))

    def test_a_tidying_that_raises_saves_nothing_and_does_not_fail_the_op(self):
        with self.assertLogs(base_ops.logger, "ERROR"):
            result = self.run_stage(Recorder(raises=True), [vocab_note("言葉 (m1)", 2)])

        self.assertEqual(result, (None, []))
        self.assertEqual((self.col.updated, self.col.merged), ([], []))


class TidyAfterAddingTests(NewNoteHarness):
    """What the match op's new notes leave of their word's markers, tidied by the cleanup's last
    stage: what restore_renamed_sort_fields leaves (it takes back only what a cancel left
    out), and what it cannot, a failed add or a meaning that could not be made."""

    def test_a_gap_a_failed_add_leaves_is_closed(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            first = search.get_note(2)
            second = self.new_meaning("words", first)
            third = self.new_meaning("speech", first, second)

        col = self.clean_up(search, cancel_during=NO_CANCEL, failing=(second,))
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (m1)")
        self.assertEqual(self.sort_field(search, third), f"{self.WORD} (m3)")

        self.assertEqual(self.tidy(col), [third.id])
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (m1)")
        self.assertEqual(self.sort_field(search, third), f"{self.WORD} (m2)")
        self.assertEqual(set(col.merged), {POS})

    def test_a_meaning_a_failed_add_leaves_alone_loses_its_number(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            second = self.new_meaning("words", search.get_note(2))

        col = self.clean_up(search, cancel_during=NO_CANCEL, failing=(second,))
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (m1)")

        self.assertEqual(self.tidy(col), [2])
        self.assertEqual(self.sort_field(search, 2), self.WORD)

    def test_a_gap_a_cancel_leaves_across_words_is_closed(self):
        """A note made seeing a rename keeps it when added though the note that made it is
        left out, which a ずる word's meaning copied from its じる note does (SiblingMarkers)."""
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            first = search.get_note(2)
            second = self.new_meaning("words", first)
            third = self.new_meaning("speech", first, second)

        col = self.clean_up(search, notes=[third, second], cancel_during=third)
        self.assertEqual(self.sort_field(search, third), f"{self.WORD} (m3)")

        self.tidy(col)
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (m1)")
        self.assertEqual(self.sort_field(search, third), f"{self.WORD} (m2)")

    def failed_reading(self, processed_furigana: str, *marker_nids: int) -> None:
        """A new reading whose meaning could not be made: its markers stay on the word's
        other notes, and nothing is added."""
        self.processed_furigana = processed_furigana
        with mock.patch.object(mwtn, "clean_meaning_in_note", lambda **_: False):
            created = mwtn.create_new_note_without_matching(
                CONFIG, "", self.args("げんご", FakeMarkerIndex(*marker_nids))
            )
        self.assertFalse(created)
        self.assertEqual(self.to_add, {})

    def test_the_kun_marker_a_failed_reading_leaves_is_taken_off(self):
        search = FakeSearch(vocab_note(self.WORD, 2, word_processed_furigana_field=KUN_FURIGANA))
        with search.patched():
            self.failed_reading(ON_FURIGANA, 2)
        self.assertEqual(self.to_update[2]["word_sort_field"], f"{self.WORD} (kun)")

        col = self.clean_up(search, notes=[])
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (kun)")

        self.assertEqual(self.tidy(col), [2])
        self.assertEqual(self.sort_field(search, 2), self.WORD)

    def test_the_reading_number_a_failed_reading_leaves_is_taken_off(self):
        search = FakeSearch(vocab_note(self.WORD, 2), vocab_note(f"{self.WORD} (m1)", 3))
        search.saved[2]["word_sort_field"] = f"{self.WORD} (m2)"
        with search.patched():
            self.failed_reading("", 2, 3)
        self.assertEqual(self.to_update[2]["word_sort_field"], f"{self.WORD} (r1)(m2)")
        self.assertEqual(self.to_update[3]["word_sort_field"], f"{self.WORD} (r1)(m1)")

        col = self.clean_up(search, notes=[])

        self.assertEqual(sorted(self.tidy(col)), [2, 3])
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (m2)")
        self.assertEqual(self.sort_field(search, 3), f"{self.WORD} (m1)")

    def test_what_a_cancel_left_consistent_is_left_as_it_is(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            second = self.new_reading("げんご", 2)
            third = self.new_reading("ことのは", 2)
            self.new_meaning("words", second)

        col = self.clean_up(search, cancel_during=third)
        fields = {nid: dict(values) for nid, values in search.saved.items()}

        self.assertEqual(self.tidy(col), [])
        self.assertEqual(search.saved, fields)
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (r1)")
        self.assertEqual(self.sort_field(search, second), f"{self.WORD} (r2)")
        self.assertEqual(self.sort_field(search, third), f"{self.WORD} (r3)")


class FakeCollectionOp:
    """aqt's CollectionOp, running the op at once on this thread and handing its result on."""

    def __init__(self, parent, op):
        self.op = op
        self.on_success = None

    def success(self, on_success):
        self.on_success = on_success
        return self

    def run_in_background(self):
        result = self.op(mw.col)
        if self.on_success is not None:
            self.on_success(result)


class RunCollection(SearchableCollection):
    """The collection a whole selected_notes_op runs against."""

    def get_note(self, nid):
        return self.search.get_note(nid)

    def remove_notes(self, nids):
        pass


class SelectedNotesOpTidyTests(unittest.TestCase):
    """The marker tidying runs last in selected_notes_op's cleanup, over every note it saved and
    added, whether or not the run had notes to add."""

    def setUp(self):
        self.updater = FakeUpdater()
        self.finished: list = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(base_ops, "CollectionOp", FakeCollectionOp))
        stack.enter_context(mock.patch.object(base_ops, "install_run_controls", lambda: None))
        stack.enter_context(
            mock.patch.object(
                base_ops,
                "on_bulk_success",
                lambda out, done, edited, other, *_, new_notes, **__: self.finished.append(
                    (list(edited), list(other), new_notes)
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(base_ops, "phase_log", lambda _: contextlib.nullcontext())
        )
        addon_manager = types.SimpleNamespace(getConfig=lambda _: CONFIG)
        stack.enter_context(mock.patch.object(mw, "addonManager", addon_manager))

    def run_op(self, search, selected, edited: dict, to_add: list, tidy_op):
        col = RunCollection(search, self.updater)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(mw, "col", col, create=True))

        async def bulk_op(col, notes, notes_to_add_dict, notes_to_update_dict, **_):
            notes_to_update_dict.update(edited)
            if to_add:
                notes_to_add_dict["言葉"] = list(to_add)
            return POS, notes_to_add_dict, notes_to_update_dict, []

        with stack:
            base_ops.selected_notes_op(
                "Done",
                bulk_op,
                selected,
                None,
                self.updater,
                new_notes_op=Recorder(),
                tidy_markers_op=tidy_op,
            )
        return col

    def test_with_nothing_to_add_the_notes_the_run_edited_are_tidied(self):
        search = FakeSearch(vocab_note("言葉 (kun)", 2), vocab_note("単語", 3))
        edited = {2: vocab_note("言葉 (kun)", 2)}
        tidied = vocab_note("言葉", 2)
        tidy_op = Recorder(returns={2: tidied})

        col = self.run_op(search, [3], edited, [], tidy_op)

        self.assertEqual(tidy_op.handed, [[edited[2]]])
        self.assertEqual(col.updated[-1], tidied)
        self.assertEqual(col.merged[-1], POS)
        # Not selected, so an other note, counted once
        self.assertEqual(self.finished, [([], [2], base_ops.NewNotesCounts())])

    def test_after_the_adding_the_notes_added_are_tidied_too(self):
        search = FakeSearch(vocab_note("言葉 (m1)", 2))
        edited = {2: vocab_note("言葉 (m1)", 2)}
        new = vocab_note("言葉 (m3)")
        tidy_op = Recorder()

        def rename_the_added_note(notes, config, updater):
            tidy_op(notes, config, updater)
            new["word_sort_field"] = "言葉 (m2)"
            return {new.id: new}

        col = self.run_op(search, [2], edited, [new], rename_the_added_note)

        self.assertEqual([note for note, _ in col.added], [new])
        self.assertEqual(tidy_op.handed, [[edited[2], new]])
        self.assertIs(col.updated[-1], new)
        # The added note renamed is counted as added, not as an edited note
        self.assertEqual(self.finished, [([2], [], base_ops.NewNotesCounts(added=1))])

    def test_without_a_tidying_op_nothing_is_tidied(self):
        search = FakeSearch(vocab_note("言葉 (kun)", 2))
        edited = {2: vocab_note("言葉 (kun)", 2)}

        col = self.run_op(search, [2], edited, [], None)

        self.assertEqual(col.updated, [edited[2]])
