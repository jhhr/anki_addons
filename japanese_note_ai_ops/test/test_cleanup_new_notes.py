"""The cleanup's note adding, which a cancelled run goes through as a finished one does.

A cancelled match run used to add none of the new notes it had prepared: the adding loop
stopped on the dialog's cancel flag, which stays set for the rest of a cancelled run. The
notes' meanings were paid for, and their IDs were already in the saved word arrays. The
cleanup now re-arms that flag and gives the adding a cancel of its own, which stops it and
unlinks the notes it left out. These tests drive `add_new_notes` against a stand-in
collection.

A new note holds its link ID (link_ids.py) from the moment the match op makes it, in its
new_note_id_field, and the arrays link it by that before and after the add: nothing is
rewritten once it is added, and a note not added, cancelled or failed, has its ID taken out of
the arrays again.
"""

import contextlib
import json
import re
import types
import unittest
from unittest import mock

from addon_modules import FakeClock, load_addon_module, load_ops_module, mw

link_ids = load_addon_module("link_ids", subdir="")
base_ops = load_ops_module("base_ops")
mwtn = load_ops_module("match_words_to_notes")
cm = load_ops_module("clean_meaning")
controls_module = load_ops_module("progress_controls")

POS = 101
# Link IDs as the match op generates them for its new notes (link_ids.new_link_id: 16 digits,
# never a 13-digit note id)
NEW_1, NEW_2, NEW_3, NEW_4 = (int(str(n) * 16) for n in range(1, 5))


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
        return {"Vocab": 7, "Default": 1}.get(name)


class FakeCollection:
    def __init__(self, failing: tuple = ()):
        self.decks = FakeDecks()
        self.failing = failing
        self.added: list = []
        self.merged: list = []
        self.updated: list = []
        self.next_id = 500
        # A note_will_be_added hook, which col.add_note runs on the note first: before it has
        # an id, and before the backend can refuse it
        self.hook = None

    def add_note(self, note, deck_id):
        if self.hook is not None:
            self.hook(note)
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

    def show_title(self):
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


def new_note(held) -> FakeNote:
    """A new note as the match op prepares it: holding its link ID `held`, not added yet."""
    return FakeNote({"word_list_field": "", "new_note_id_field": str(held)})


def writes_its_id(note: FakeNote) -> None:
    """An add hook that fills the id field with the note's id, as a CopyAnywhere definition
    writing {{__Note_ID}} into it does: 0, the id of a note not in the collection yet."""
    note["new_note_id_field"] = str(note.id)


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
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
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
        notes = [new_note(NEW_1)]
        referencing = FakeNote({"word_list_field": "[]"}, note_id=2)

        def new_notes_op(new_notes, config, progress_updater):
            return {2: referencing, new_notes[0].id: new_notes[0]}

        result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, new_notes_op)

        self.assertEqual(col.updated, [referencing, notes[0]])
        self.assertEqual(col.merged, [POS, POS])
        # The added note is saved but counted as added, not as a note edited
        self.assertEqual(result.updated_nids, [2])

    def test_a_note_that_fails_to_add_is_unlinked_and_one_added_keeps_its_link_id(self):
        """A failed add's ID was kept in the arrays as a debugging hint, back when the next
        match run reset any placeholder no note held. A link ID no note holds is a link to no
        note, which no match run resets: it is taken out as a cancel's notes are."""
        failing = new_note(NEW_1)
        added = new_note(NEW_2)
        arr = [
            ["様", "noun", "様", "さま", [NEW_1, 3], []],
            ["本", "noun", "本", "ほん", [NEW_2], []],
        ]
        referencing = FakeNote({"word_list_field": json.dumps(arr)}, note_id=2)
        referencing["new_note_id_field"] = ""
        col = FakeCollection(failing=(failing,))

        with (
            mock.patch.object(mwtn, "col_find_notes", lambda _: [2]),
            mock.patch.object(mwtn, "col_get_notes", lambda _: [referencing]),
        ):
            result = base_ops.add_new_notes(
                col, [failing, added], CONFIG, POS, self.updater,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )

        self.assertEqual((failing.id, added.id), (0, 501))
        saved = json.loads(referencing["word_list_field"])
        self.assertEqual([saved[0][4], saved[1][4]], [["match"], [NEW_2]])
        self.assertEqual(added["new_note_id_field"], str(NEW_2))
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 1, 0))
        self.assertEqual(self.updater.adding[-1], (1, 2, 1))
        # Only the unlinking's write: the added note is saved by its add alone
        self.assertEqual(col.updated, [referencing])
        self.assertEqual(result.updated_nids, [2])

    def test_without_new_notes_op_the_notes_are_still_added(self):
        col = FakeCollection()

        result = base_ops.add_new_notes(col, [new_note(NEW_1)], CONFIG, POS, self.updater)

        self.assertEqual((result.added, result.updated_nids), (1, []))
        self.assertEqual(col.updated, [])


class UndoCountingCollection(FakeCollection):
    """Keeps an undo step counter the adds move and the merges fold back, and can refuse a
    merge, as an undo entry some hook made in between can make Anki do."""

    def __init__(self, failing: tuple = (), refuse_merge_after: int = 0):
        super().__init__(failing)
        self.step = 3
        self.refuse_merge_after = refuse_merge_after

    def add_note(self, note, deck_id):
        super().add_note(note, deck_id)
        self.step += 1

    def merge_undo_entries(self, pos):
        if self.refuse_merge_after and len(self.merged) >= self.refuse_merge_after:
            raise RuntimeError("target undo op not found")
        self.step = POS
        return super().merge_undo_entries(pos)

    def undo_status(self):
        return types.SimpleNamespace(undo="Matching words", redo="", last_step=self.step)


class NoteAddRecordTests(unittest.TestCase):
    """A run that records its notes records each note's add apart from its undo merge, with the
    undo queue's head around them, so a failed add and a failed merge are told apart."""

    def setUp(self):
        import os
        import tempfile

        self.capture = load_ops_module("capture")
        self.capture._quiet_until.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "capture.sqlite3")
        self.assertTrue(self.capture.install(self.path))
        self.addCleanup(self.capture.shutdown)
        run_id = self.capture.begin_run("run", notes=True)
        scope = self.capture.run_scope(run_id)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def events(self):
        import sqlite3
        from contextlib import closing

        self.assertTrue(self.capture.current_store().flush())
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT note_id, payload_json FROM events WHERE kind = 'note.add'"
                " ORDER BY event_id"
            ).fetchall()
        return [(note_id, json.loads(payload)) for note_id, payload in rows]

    def test_each_add_is_timed_apart_from_its_merge_and_a_failed_add_is_named(self):
        failing = new_note(NEW_2)
        col = UndoCountingCollection(failing=(failing,))

        base_ops.add_new_notes(
            col, [new_note(NEW_1), failing], CONFIG, POS, self.updater
        )

        [(added_id, added), (failed_id, failed)] = self.events()
        # Named by the link ID each was made with, under the payload's old key
        self.assertEqual((added_id, added["placeholder"]), (501, NEW_1))
        self.assertGreaterEqual(added["seconds"], 0)
        self.assertGreaterEqual(added["merge_seconds"], 0)
        self.assertEqual(added["undo_before"]["last_step"], 3)
        self.assertEqual(added["undo_after"]["last_step"], POS)
        self.assertIsNone(added["add_error"])
        self.assertEqual((failed_id, failed["placeholder"]), (None, NEW_2))
        self.assertIsNone(failed["merge_seconds"])
        self.assertIn("refused", failed["add_error"])

    def test_an_add_hook_that_writes_the_id_field_does_not_change_the_link_id_recorded(self):
        # Read off the note after the add, both were 0: an export then knew neither which ID
        # the added note was linked by nor which one the failed add left in the arrays
        failing = new_note(NEW_2)
        col = UndoCountingCollection(failing=(failing,))
        col.hook = writes_its_id

        base_ops.add_new_notes(
            col, [new_note(NEW_1), failing], CONFIG, POS, self.updater
        )

        [(added_id, added), (failed_id, failed)] = self.events()
        self.assertEqual((added_id, added["placeholder"]), (501, NEW_1))
        self.assertEqual((failed_id, failed["placeholder"]), (None, NEW_2))

    def test_a_note_with_no_deck_to_go_to_is_recorded_as_a_failed_add(self):
        # Counted failed and left out of the capture, its placeholder was one an export could
        # not tell from a word's, and it stayed in the expected word arrays, random every run
        no_deck = new_note(NEW_3)
        no_deck.note_type = lambda: {"name": "Other"}  # type: ignore[method-assign]
        config = {**CONFIG, "Other": {**CONFIG["Word"], "insert_deck": "Missing"}}

        base_ops.add_new_notes(UndoCountingCollection(), [no_deck], config, POS, self.updater)

        [(note_id, failed)] = self.events()
        self.assertEqual((note_id, failed["placeholder"]), (None, NEW_3))
        self.assertEqual(failed["add_error"], "no deck to add it to")
        self.assertIsNone(failed["merge_seconds"])

    def test_a_refused_merge_is_recorded_as_one_and_raises_as_it_always_did(self):
        col = UndoCountingCollection(refuse_merge_after=1)

        with self.assertRaises(RuntimeError):
            base_ops.add_new_notes(
                col, [new_note(NEW_1), new_note(NEW_2)], CONFIG, POS, self.updater
            )

        [_, (_, refused)] = self.events()
        self.assertIsNone(refused["add_error"])
        self.assertIn("target undo op not found", refused["merge_error"])
        # The add went through: it is the merge that failed
        self.assertEqual(refused["undo_after"]["last_step"], POS + 1)


class ReArmedCancelTests(unittest.TestCase):
    """The real updater over a dialog whose flag a cancel of the API work has set."""

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        # Anki's dialog, reduced to the flag Escape, the close box and Cancel set
        self.win = types.SimpleNamespace(wantCancel=True)
        patches = (
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
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
        col = FakeCollection()

        self.updater.begin_cleanup()
        result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater)

        self.assertEqual([note for note, _ in col.added], notes)
        self.assertEqual(result.counts, base_ops.NewNotesCounts(3, 0, 0))

    def test_a_second_cancel_stops_it(self):
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
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

    def test_in_a_run_not_cancelled_a_press_after_the_api_work_is_the_run_s_cancel(self):
        """Pressed while the results were flushed or the edited notes written, with Cancel
        saying it cancels the run: as a press during the API work, it keeps the notes prepared,
        where stopping the adding would drop every one the run paid for."""
        notes = [new_note(NEW_1), new_note(NEW_2)]
        col = FakeCollection()

        with mock.patch.object(base_ops, "run_cancelled", lambda: False):
            self.updater.begin_cleanup()
            result = base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater)

        self.assertEqual([note for note, _ in col.added], notes)
        self.assertEqual(result.counts, base_ops.NewNotesCounts(2, 0, 0))

    def test_a_flag_nothing_could_reset_does_not_stop_the_adding(self):
        """Without the dialog the first cancel set, or with one the reset fails on, the flag
        is left as it was; the adding cannot be cancelled then, but it is not stopped by the
        cancel of the API work either."""
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
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
        notes = [new_note(NEW_1), new_note(NEW_2)]
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
                FakeCollection(), [unconfigured_note(NEW_1)], CONFIG, POS, self.updater
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
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]

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
    so a longer number holding an ID's digits is found too."""

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
        terms = re.findall(r'"(\w+):\*(-?\d+)\*"', query)
        # The note type a two-type layout's search starts with; a note not in note_types is
        # a "Word" note
        of_type = re.match(r'"note:(\w+)" ', query)
        return [
            nid
            for nid, fields in self.saved.items()
            if (of_type is None or self.note_types.get(nid, "Word") == of_type.group(1))
            and any(field in fields and text in fields[field] for field, text in terms)
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
    """The new notes the cleanup did not add, a cancel's or a failed add's: the words linked to
    them are matched again, since no note will ever hold their link IDs."""

    def setUp(self):
        self.updater = FakeUpdater()

    def test_the_words_of_a_note_not_added_are_cleared_where_they_are_saved(self):
        not_added = new_note(NEW_1)
        # another new note's, added: not among the notes handed over
        added = new_note(NEW_2)
        both = sentence(2, w("様", [NEW_1, 3]), w("本", [NEW_2]))
        nested = sentence(3, w("本棚", ["dontmatch"], (w("本", [NEW_1]), w("棚", [444]))))
        # A longer number holding the ID's digits, which the substring search finds too
        longer_id = int(f"9{NEW_1}")
        longer = sentence(4, w("机", [longer_id]))
        search = FakeSearch(both, nested, longer)

        with search.patched():
            updated = mwtn.clear_unadded_note_ids([not_added], CONFIG, self.updater)

        # found by the substring search, but its ID is another one: not handed back to be
        # saved
        self.assertEqual(sorted(updated), [2, 3])
        self.assertEqual(match_data(updated[2]), [["match"], [NEW_2]])
        self.assertEqual(match_data(updated[3]), [["dontmatch"], ["match"], [444]])
        self.assertEqual(search.read, [2, 3, 4])
        self.assertEqual(search.queries, [f'("word_list_field:*{NEW_1}*")'])
        self.assertEqual((not_added.id, not_added["new_note_id_field"]), (0, str(NEW_1)))
        self.assertEqual(added["new_note_id_field"], str(NEW_2))
        self.assertEqual(self.updater.clearing, [(0, 1), (1, 1)])

    def test_many_link_ids_are_searched_for_together_and_a_note_is_read_once(self):
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
        first_and_last = sentence(2, w("様", [NEW_1]), w("本", [NEW_3, 2]))
        second = sentence(3, w("棚", [NEW_2]))
        search = FakeSearch(first_and_last, second)

        with search.patched(), mock.patch.object(mwtn, "LINK_IDS_PER_SEARCH", 2):
            updated = mwtn.clear_unadded_note_ids(notes, CONFIG, self.updater)

        self.assertEqual(
            search.queries,
            [
                f'("word_list_field:*{NEW_1}*" OR "word_list_field:*{NEW_2}*")',
                f'("word_list_field:*{NEW_3}*")',
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
        broken_text = f'[["様", "noun", "様", "さま", [{NEW_1}], ['
        broken = FakeNote({"word_list_field": broken_text, "new_note_id_field": ""}, 5)
        # a note of a kind the match op writes no arrays into, whatever its field holds
        other_kind = FakeNote({"word_list_field": str(NEW_1)}, 6)
        search = FakeSearch(broken, other_kind)

        with search.patched():
            updated = mwtn.clear_unadded_note_ids([new_note(NEW_1)], CONFIG, self.updater)

        self.assertEqual(search.read, [5, 6])
        self.assertEqual(list(updated), [5])
        self.assertEqual(updated[5]["word_list_field"], broken_text)
        self.assertEqual(updated[5].tags, [mwtn.INVALID_WORD_ARRAY_TAG])

    def test_a_note_holding_no_link_id_is_not_searched_for(self):
        """Nor by the note id a link ID falls back to: a note not added has none, and the
        words linked to any other note are not this one's to clear. A legacy placeholder is
        no link ID either (link_ids.parse_link_id)."""
        search = FakeSearch(sentence(2, w("様", [-1111111]), w("本", [0])))

        with search.patched():
            updated = mwtn.clear_unadded_note_ids(
                [new_note(""), new_note("0"), new_note("-1111111")], CONFIG, self.updater
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
        if note.id:
            # Written as the note_will_be_added hooks left it, as Anki's add does
            self.search.saved[note.id] = dict(note.fields)
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


def meaning_note(sort: str, meaning: str, held: int) -> FakeNote:
    return FakeNote({
        "word_list_field": "",
        "new_note_id_field": str(held),
        "word_sort_field": sort,
        "english_meaning_field": meaning,
        "meaning_field": "",
    })


class CancelledAddingTests(unittest.TestCase):
    """The cleanup's own cancel, pressed during the adding or the dedupe before it.

    Added notes keep the link IDs the arrays link them by; the failed ones and the ones never
    tried are unlinked: their words go back to be matched again.
    """

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def add(self, col, notes, **ops):
        # The match op's: no new_notes_op, since nothing is rewritten after the add
        return base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, **ops)

    def test_a_cancel_between_adds_leaves_the_rest_to_be_unlinked(self):
        notes = [new_note(held) for held in (NEW_1, NEW_2, NEW_3, NEW_4)]
        added_first, failing, added_last, left_out = notes
        referencing = sentence(
            2,
            w("様", [NEW_1]),
            w("本", [NEW_2, 3]),
            w("棚", [NEW_3]),
            w("机", [NEW_4, 2]),
        )
        search = FakeSearch(referencing)
        col = SearchableCollection(search, self.updater, failing=(failing,))
        col.press_cancel_during = added_last

        with search.patched():
            result = self.add(col, notes, unadded_notes_op=mwtn.clear_unadded_note_ids)

        # The note being added when Cancel was pressed is added: its hooks run to the end
        self.assertEqual([note for note, _ in col.added], [added_first, added_last])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(2, 1, 1))
        self.assertEqual(left_out.id, 0)
        saved = FakeNote(dict(search.saved[2]), 2)
        # linked as it was, matched again (a failed add), linked as it was, matched again
        self.assertEqual(match_data(saved), [[NEW_1], ["match"], [NEW_3], ["match"]])
        self.assertTrue(self.updater.cancel_ended)
        # The failed add and the note never tried, in one search
        self.assertEqual(self.updater.clearing[-1], (2, 2))
        # Every write in the run's one undo entry, the unlinking's last
        self.assertEqual(set(col.merged), {POS})
        self.assertEqual(result.op_changes, f"changes after {len(col.merged)} merges")
        self.assertIn(2, result.updated_nids)

    def test_the_unlinking_gets_the_failed_adds_and_the_notes_never_tried(self):
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
        col = SearchableCollection(FakeSearch(), self.updater, failing=(notes[0],))
        col.press_cancel_during = notes[1]
        new_notes_op, unlinking = Recorder(), Recorder()

        result = self.add(col, notes, new_notes_op=new_notes_op, unadded_notes_op=unlinking)

        self.assertEqual(new_notes_op.handed, [[notes[1]]])
        self.assertEqual(unlinking.handed, [[notes[0], notes[2]]])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 1, 1))
        self.assertEqual(self.updater.adding[-1], (1, 3, 1))

    def test_a_cancel_before_the_first_add_adds_nothing_and_unlinks_everything(self):
        notes = [new_note(NEW_1), new_note(NEW_2)]
        col = FakeCollection()
        self.updater.cancel_pressed = True
        new_notes_op, unlinking = Recorder(), Recorder()

        result = self.add(col, notes, new_notes_op=new_notes_op, unadded_notes_op=unlinking)

        self.assertEqual(col.added, [])
        self.assertEqual(new_notes_op.handed, [])
        self.assertEqual(unlinking.handed, [notes])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 0, 2))
        self.assertTrue(self.updater.cancel_ended)

    def test_without_a_cancel_only_the_failed_adds_are_unlinked(self):
        notes = [new_note(NEW_1), new_note(NEW_2)]
        col = FakeCollection(failing=(notes[1],))
        new_notes_op, unlinking = Recorder(), Recorder()

        result = self.add(col, notes, new_notes_op=new_notes_op, unadded_notes_op=unlinking)

        self.assertEqual(new_notes_op.handed, [[notes[0]]])
        self.assertEqual(unlinking.handed, [[notes[1]]])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 1, 0))
        # Nothing is left for the Cancel to stop once the adding is over
        self.assertTrue(self.updater.cancel_ended)

    def test_a_note_with_no_deck_to_go_to_is_a_failed_add(self):
        no_deck = new_note(NEW_1)
        no_deck.note_type = lambda: {"name": "Other"}  # type: ignore[method-assign]
        config = {**CONFIG, "Other": {**CONFIG["Word"], "insert_deck": "Missing"}}
        col = FakeCollection()
        unlinking = Recorder()

        result = base_ops.add_new_notes(
            col, [no_deck], config, POS, self.updater, unadded_notes_op=unlinking
        )

        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 1, 0))
        self.assertEqual(self.updater.adding[-1], (0, 1, 1))
        self.assertEqual(unlinking.handed, [[no_deck]])

    def test_a_note_type_with_no_insert_deck_adds_to_the_default_deck(self):
        """The key is optional, as config.md says; left out, it stopped every add."""
        note = new_note(NEW_1)
        config = {"Word": {k: v for k, v in CONFIG["Word"].items() if k != "insert_deck"}}
        col = FakeCollection()

        result = base_ops.add_new_notes(col, [note], config, POS, self.updater)

        self.assertEqual(col.added, [(note, col.decks.id_for_name("Default"))])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 0, 0))

    def test_a_cancel_during_the_dedupe_unlinks_every_note_as_prepared(self):
        """The duplicate the dedupe dropped included: an interrupted dedupe leaves some of
        its references pointing at it."""
        kept, duplicate, other = new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)
        remapped = FakeNote({"word_list_field": f"[[{NEW_1}]]"}, note_id=2)
        removed = FakeNote({"word_list_field": f"[[{NEW_1}]]"}, note_id=3)
        col = FakeCollection()
        unlinking = Recorder(returns={4: FakeNote({}, 4), 0: FakeNote({}, 0)})

        def dedupe(notes, config, progress_updater):
            self.updater.cancel_pressed = True
            return [kept, other], {2: remapped, 3: removed}

        new_notes_op = Recorder()
        result = self.add(
            col,
            [kept, duplicate, other],
            new_notes_op=new_notes_op,
            filter_new_notes_op=dedupe,
            unadded_notes_op=unlinking,
            notes_to_remove={3},
        )

        self.assertEqual(col.added, [])
        self.assertEqual(new_notes_op.handed, [])
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
        kept, duplicate, other = new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)
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
        notes = [new_note(NEW_1)]

        self.add(FakeCollection(), notes, filter_new_notes_op=dedupe, unadded_notes_op=unlinking)

        self.assertEqual((dedupe.handed, unlinking.handed), ([], [notes]))

    def test_the_cancel_is_armed_before_the_dedupe_with_every_note_as_prepared(self):
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
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
        notes = [new_note(NEW_1), new_note(NEW_2)]
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


def unconfigured_note(held: int) -> FakeNote:
    """A new note whose note type the config does not know: get_field_config raises for it."""
    note = new_note(held)
    note.note_type = lambda: {"name": "Unconfigured"}  # type: ignore[method-assign]
    return note


class AddHookWritesIdFieldTests(unittest.TestCase):
    """An add hook that writes `new_note_id_field`, which holds the link ID the arrays link a
    new note by (base_ops.link_id_kept).

    `col.add_note` runs every addon's note_will_be_added hook on the note before adding it. A
    CopyAnywhere definition filling that field with the note's id writes "0" there: the note's
    link ID would then be its note id, which no array links, and the arrays saved before the
    adding would link nothing.
    """

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def add(self, col, notes, **ops):
        return base_ops.add_new_notes(col, notes, CONFIG, POS, self.updater, **ops)

    def test_the_note_keeps_its_link_id_and_is_saved_again_holding_it(self):
        added = new_note(NEW_1)
        linking = sentence(2, w("様", [NEW_1, 4]))
        search = FakeSearch(linking, sentence(3, w("本", [77])))
        col = SearchableCollection(search, self.updater)
        col.hook = writes_its_id

        with search.patched():
            result = self.add(col, [added])

        self.assertEqual(added["new_note_id_field"], str(NEW_1))
        # The add wrote the hook's "0"; saved again holding the ID, in the run's undo entry
        self.assertEqual(col.updated, [added])
        self.assertEqual(search.saved[501]["new_note_id_field"], str(NEW_1))
        self.assertEqual(col.merged, [POS, POS])
        # Nothing looked for and nothing rewritten: the array links the note already
        self.assertEqual(search.queries, [])
        self.assertEqual(search.saved[2], linking.fields)
        # The added note is counted as added, not as an edited note
        self.assertEqual(result.updated_nids, [])

    def test_a_note_no_hook_touched_is_not_saved_again(self):
        added = new_note(NEW_1)
        col = SearchableCollection(FakeSearch(), self.updater)

        self.add(col, [added])

        self.assertEqual((col.updated, col.merged), ([], [POS]))
        self.assertEqual(col.search.saved[501]["new_note_id_field"], str(NEW_1))

    def test_a_note_whose_add_then_fails_is_unlinked_by_the_id_it_was_made_with(self):
        failing = new_note(NEW_1)
        search = FakeSearch(sentence(2, w("様", [NEW_1])))
        col = SearchableCollection(search, self.updater, failing=(failing,))
        col.hook = writes_its_id

        with search.patched():
            result = self.add(col, [failing], unadded_notes_op=mwtn.clear_unadded_note_ids)

        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 1, 0))
        self.assertEqual(failing["new_note_id_field"], str(NEW_1))
        self.assertEqual(match_data(FakeNote(dict(search.saved[2]), 2)), [["match"]])

    def test_a_cancel_during_the_hook_still_unlinks_the_notes_left_out(self):
        added, left_out = new_note(NEW_1), new_note(NEW_2)
        search = FakeSearch(sentence(2, w("様", [NEW_1]), w("本", [NEW_2, 3])))
        col = SearchableCollection(search, self.updater)
        col.hook = writes_its_id
        col.press_cancel_during = added

        with search.patched():
            self.add(col, [added, left_out], unadded_notes_op=mwtn.clear_unadded_note_ids)

        self.assertEqual(match_data(FakeNote(dict(search.saved[2]), 2)), [[NEW_1], ["match"]])
        self.assertEqual(search.saved[501]["new_note_id_field"], str(NEW_1))

    def test_a_field_holding_no_link_id_is_the_hooks_to_write(self):
        # A note some other op prepared: there is nothing of the match op's in the field
        note = new_note("")
        col = FakeCollection()
        col.hook = lambda added: added.__setitem__("new_note_id_field", "from the hook")

        base_ops.add_new_notes(col, [note], CONFIG, POS, self.updater)

        self.assertEqual(note["new_note_id_field"], "from the hook")


class CleanupTailTests(unittest.TestCase):
    """What the adding leaves for after it happens whatever raised before (review finding C5).

    The adding's cancel is ended however the adding stops, and a new_notes_op that raises does
    not cost the unlinking: that is what puts back the words of notes that will never exist.
    """

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def test_a_new_notes_op_that_raises_after_a_partial_cancel_still_unlinks_then_fails(self):
        notes = [new_note(NEW_1), new_note(NEW_2)]
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
        self.assertIn("Error in new_notes_op, after the adding", "\n".join(logs.output))
        self.assertTrue(self.updater.cancel_ended)

    def test_a_new_notes_op_that_raises_with_nothing_to_unlink_still_fails(self):
        with self.assertRaisesRegex(Exception, "has not been configured"), self.assertLogs(
            base_ops.logger, "ERROR"
        ):
            base_ops.add_new_notes(
                FakeCollection(), [new_note(NEW_1)], CONFIG, POS, self.updater,
                Recorder(raises=True), unadded_notes_op=Recorder(),
            )

    def test_a_raise_in_the_add_loop_still_ends_the_cancel(self):
        notes = [new_note(NEW_1), unconfigured_note(NEW_2)]

        with self.assertRaisesRegex(Exception, "has not been configured"):
            base_ops.add_new_notes(FakeCollection(), notes, CONFIG, POS, self.updater)

        self.assertEqual(self.updater.armed, [2])
        self.assertTrue(self.updater.cancel_ended)

    def test_a_dedupe_that_raises_still_ends_the_cancel(self):
        def dedupe(notes, config, progress_updater):
            raise RuntimeError("the dedupe broke")

        with self.assertRaisesRegex(RuntimeError, "the dedupe broke"):
            base_ops.add_new_notes(
                FakeCollection(), [new_note(NEW_1)], CONFIG, POS, self.updater,
                filter_new_notes_op=dedupe,
            )

        self.assertTrue(self.updater.cancel_ended)


class RefusingSearchableCollection(SearchableCollection):
    """A SearchableCollection that refuses every merge once `refuse_after` have gone through, as
    Anki does after a hook wrote the collection outside its undo (a raw SQL statement): that
    throws the undo queue away, and the run's entry with it."""

    def __init__(self, search: FakeSearch, updater: FakeUpdater, refuse_after: int):
        super().__init__(search, updater)
        self.refuse_after = refuse_after

    def merge_undo_entries(self, pos):
        if len(self.merged) >= self.refuse_after:
            raise RuntimeError("target undo op not found")
        return super().merge_undo_entries(pos)


class AddingRaisesTests(unittest.TestCase):
    """A dedupe or an adding that raises still fails the op, but only after the tail has run.

    The arrays saved before the adding link every prepared note by its ID, and no later run
    repairs an ID no note holds: the notes never added are unlinked, the added ones an add hook
    wrote over are saved again holding their IDs, and new_notes_op gets the added notes.
    """

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def test_a_refused_merge_still_saves_what_a_hook_wrote_over_and_unlinks_the_rest(self):
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
        search = FakeSearch(sentence(2, w("様", [NEW_1]), w("本", [NEW_2]), w("机", [NEW_3])))
        # The first add's merge goes through, the second's is refused
        col = RefusingSearchableCollection(search, self.updater, refuse_after=1)
        col.hook = writes_its_id

        with search.patched(), self.assertRaisesRegex(RuntimeError, "target undo op"):
            base_ops.add_new_notes(
                col, notes, CONFIG, POS, self.updater,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )

        # Both added notes hold their link IDs again, the one never tried is unlinked
        self.assertEqual(search.saved[501]["new_note_id_field"], str(NEW_1))
        self.assertEqual(search.saved[502]["new_note_id_field"], str(NEW_2))
        saved = FakeNote(dict(search.saved[2]), 2)
        self.assertEqual(match_data(saved), [[NEW_1], [NEW_2], ["match"]])
        self.assertTrue(self.updater.cancel_ended)

    def test_a_note_type_the_config_lacks_still_unlinks_the_rest(self):
        notes = [new_note(NEW_1), unconfigured_note(NEW_2), new_note(NEW_3)]
        search = FakeSearch(sentence(2, w("様", [NEW_1]), w("机", [NEW_3])))
        col = SearchableCollection(search, self.updater)
        col.hook = writes_its_id

        with search.patched(), self.assertRaisesRegex(Exception, "has not been configured"):
            base_ops.add_new_notes(
                col, notes, CONFIG, POS, self.updater,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )

        self.assertEqual(search.saved[501]["new_note_id_field"], str(NEW_1))
        # The unlinking cannot place the note it raised on and skips it, not the one after
        self.assertEqual(match_data(FakeNote(dict(search.saved[2]), 2)), [[NEW_1], ["match"]])

    def test_new_notes_op_still_gets_the_added_notes_and_the_adding_s_error_is_raised(self):
        """The migration's new_notes_op makes each added sentence note its vocab notes' example.
        What it returns is saved although its merge is refused too, the run's undo entry being
        gone: the write stands as an undo step of its own."""
        notes = [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)]
        col = UndoCountingCollection(refuse_merge_after=1)
        vocab = FakeNote({"word_list_field": "[]"}, note_id=2)
        new_notes_op, unlinking = Recorder(returns={2: vocab}), Recorder()

        with (
            self.assertRaisesRegex(RuntimeError, "target undo op not found"),
            self.assertLogs(base_ops.logger, "ERROR") as logs,
        ):
            base_ops.add_new_notes(
                col, notes, CONFIG, POS, self.updater, new_notes_op, unadded_notes_op=unlinking
            )

        # The second note was added before its merge was refused, the third never tried
        self.assertEqual([note.id for note in notes], [501, 502, 0])
        self.assertEqual(new_notes_op.handed, [notes[:2]])
        self.assertEqual(col.updated, [vocab])
        self.assertEqual(unlinking.handed, [notes[2:]])
        self.assertEqual(col.merged, [POS])
        self.assertIn("after the adding raised", "\n".join(logs.output))

    def test_the_adding_s_error_is_raised_rather_than_a_new_notes_op_s_after_it(self):
        unlinking = Recorder()

        with self.assertRaisesRegex(RuntimeError, "target undo op not found"), self.assertLogs(
            base_ops.logger, "ERROR"
        ):
            base_ops.add_new_notes(
                UndoCountingCollection(refuse_merge_after=1),
                [new_note(NEW_1), new_note(NEW_2), new_note(NEW_3)], CONFIG, POS, self.updater,
                Recorder(raises=True), unadded_notes_op=unlinking,
            )

        # Both raised, and the unlinking ran all the same
        self.assertEqual(len(unlinking.handed), 1)


class EditedNotesCountTests(unittest.TestCase):
    """How the final message counts the notes the note adding saved (review finding C6): a
    selected note as one of the selection edited, any other as an other note, each once."""

    def setUp(self):
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)
        self.updater = FakeUpdater()

    def test_each_note_is_counted_once_whatever_was_counted_before(self):
        edited, other = [1], [7]

        base_ops.count_edits([1, 2, 2, 7, 8, 8, 3], {1, 2, 3}, edited, other)

        self.assertEqual((edited, other), ([1, 2, 3], [7, 8]))

    def test_notes_resolved_or_unlinked_outside_the_selection_are_other_notes(self):
        added = base_ops.NewNotesAdded(
            base_ops.NewNotesCounts(1, 0, 1), None, updated_nids=[2, 9, 3], filtered_nids=[3, 8]
        )
        # What the run's own writes already counted
        edited, other = [2], [8]

        base_ops.count_new_notes_edits(added, {1, 2, 3}, edited, other)

        self.assertEqual((edited, other), ([2, 3], [8, 9]))

    def test_through_a_cancelled_adding_the_added_notes_are_not_counted_as_edited(self):
        """They are counted as added; the notes rewritten for them are what was edited: here
        only the unlinked one, as the sentence linking the added note needs no rewriting."""
        added_note, left_out = new_note(NEW_1), new_note(NEW_2)
        selected = sentence(2, w("様", [NEW_1]))
        not_selected = sentence(9, w("本", [NEW_2, 3]))
        search = FakeSearch(selected, not_selected)
        col = SearchableCollection(search, self.updater)
        col.press_cancel_during = added_note

        with search.patched():
            result = base_ops.add_new_notes(
                col, [added_note, left_out], CONFIG, POS, self.updater,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )
        edited: list = []
        other: list = []
        base_ops.count_new_notes_edits(result, {2}, edited, other)

        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 0, 1))
        self.assertNotIn(added_note, col.updated)
        self.assertEqual((edited, other), ([], [9]))


class DedupeCancelTests(unittest.TestCase):
    """deduplicate_notes_list stops between its merges when the adding is cancelled."""

    def setUp(self):
        self.updater = FakeUpdater()
        self.notes = [
            meaning_note("hon(m1)", "book", NEW_1),
            meaning_note("hon(m2)", "book", NEW_2),
            meaning_note("yama(m1)", "mountain", NEW_3),
            meaning_note("yama(m2)", "mountain", NEW_4),
        ]
        self.search = FakeSearch(sentence(2, w("本", [NEW_2]), w("山", [NEW_4, 2])))
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
        self.assertEqual(match_data(updated[2]), [[NEW_1], [NEW_4, 2]])
        self.assertEqual(len(self.search.queries), 1)

    def test_without_an_updater_nothing_stops_it(self):
        """The dedupe of existing notes calls it with none, outside any cleanup."""
        with self.search.patched():
            notes, updated = mwtn.deduplicate_notes_list(self.notes, CONFIG)

        self.assertEqual(notes, [self.notes[0], self.notes[2]])
        # Repointed by link ID, the match_quality kept
        self.assertEqual(match_data(updated[2]), [[NEW_1], [NEW_3, 2]])

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
                filter_new_notes_op=mwtn.deduplicate_notes_list,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )

        self.assertEqual(col.added, [])
        self.assertEqual(result.counts, base_ops.NewNotesCounts(0, 0, 4))
        # The remapped word and the one the dedupe never reached, both unlinked
        saved = FakeNote(dict(self.search.saved[2]), 2)
        self.assertEqual(match_data(saved), [["match"], ["match"]])


class DedupeRepointTests(unittest.TestCase):
    """The words linked to a duplicate are linked to the note kept, by link ID, in the decoded
    array: the field's text holding the ID's digits only finds the note."""

    def test_a_number_holding_the_duplicate_s_digits_is_another_note_s(self):
        """The dedupe of existing notes repoints a note linked by its note id, 13 digits, which
        a generated ID can hold: replaced in the field's text, that ID was cut into another."""
        kept = FakeNote(
            {"new_note_id_field": "", "word_sort_field": "hon(m1)",
             "english_meaning_field": "book", "meaning_field": "本"},
            1674931277301,
        )
        duplicate = FakeNote(
            {"new_note_id_field": "", "word_sort_field": "hon(m2)",
             "english_meaning_field": "book, volume", "meaning_field": "書物"},
            1674931277302,
        )
        holding_its_digits = int(f"3{duplicate.id}12")
        linking = sentence(2, w("本", [duplicate.id, 4]), w("冊", [holding_its_digits]))
        search = FakeSearch(linking)

        with search.patched():
            notes, updated = mwtn.deduplicate_notes_list([kept, duplicate], CONFIG)

        self.assertEqual(notes, [kept])
        self.assertEqual(match_data(updated[2]), [[kept.id, 4], [holding_its_digits]])

    def test_two_pending_duplicates_are_merged_by_their_link_ids(self):
        kept = meaning_note("hon(m1)", "book", NEW_1)
        duplicate = meaning_note("hon(m2)", "book, volume", NEW_2)
        search = FakeSearch(sentence(2, w("本", [NEW_2, 3]), w("本", [NEW_1])))

        with search.patched():
            notes, updated = mwtn.deduplicate_notes_list([kept, duplicate], CONFIG)

        self.assertEqual(notes, [kept])
        self.assertEqual(search.queries, [f'"word_list_field:*{NEW_2}*"'])
        self.assertEqual(match_data(updated[2]), [[NEW_1, 3], [NEW_1]])
        # The kept note holds its own ID still, and the longer meaning
        self.assertEqual(kept["new_note_id_field"], str(NEW_1))
        self.assertEqual(kept["english_meaning_field"], "book, volume")


# The two-type layout: the arrays are in "Sentence" notes, under a field name the "Word" block
# does not have, and the new and the duplicate notes are "Word" notes. The vocab notes keep
# their old array field, under the same name as the sentence type's, until the user deletes it
TWO_TYPE_CONFIG = {
    "Sentence": {
        "vocab_note_type": "Word",
        "word_list_field": "s_array",
        "sentence_field": "s_sentence",
        "sentence_audio_field": "s_audio",
        "furigana_sentence_field": "s_furigana",
        "kanjified_sentence_field": "s_kanjified",
        "insert_deck": "Vocab",
    },
    "Word": {
        "sentence_note_type": "Sentence",
        "insert_deck": "Vocab",
        **{key: key for key in mwtn.MATCH_FIELD_KEYS if key not in mwtn.SENTENCE_MATCH_FIELD_KEYS},
    },
}


def sentence_note(note_id: int, *words: list) -> FakeNote:
    """A sentence note: its array, and no new_note_id_field."""
    note = FakeNote({"s_array": mwtn.format_word_array(list(words))}, note_id)
    note.note_type = lambda: {"name": "Sentence"}  # type: ignore[method-assign]
    return note


def old_array_note(note_id: int, *words: list) -> FakeNote:
    """A vocab note with its own id and the array it held before the split, never read now."""
    fields = {"s_array": mwtn.format_word_array(list(words)), "new_note_id_field": str(note_id)}
    return FakeNote(fields, note_id)


def two_type_search(*notes: FakeNote) -> FakeSearch:
    search = FakeSearch(*notes)
    for note in notes:
        search.note_types[note.id] = note.note_type()["name"]
    return search


def array_match_data(note: FakeNote) -> list:
    arr = mwtn.match_flags.decode_word_array(note["s_array"])
    assert arr is not None
    return [elem[4] for _, elem in mwtn.match_flags.iter_words(arr)]


class TwoTypeLayoutCleanupTests(unittest.TestCase):
    """The new notes are vocab notes and the arrays linking them are the sentence notes': every
    search names the sentence type and its field, so a vocab note's old array is never
    rewritten, and a sentence note, which has no new_note_id_field, is."""

    def setUp(self):
        self.updater = FakeUpdater()
        self.old_array = old_array_note(7, w("様", [NEW_1]), w("本", [1674931277302]))

    def test_the_words_of_a_vocab_note_not_added_are_cleared_in_the_sentence_notes(self):
        linking = sentence_note(2, w("様", [NEW_1, 3]), w("本", [444]))
        search = two_type_search(linking, self.old_array)

        with search.patched():
            updated = mwtn.clear_unadded_note_ids(
                [FakeNote({"new_note_id_field": str(NEW_1)})], TWO_TYPE_CONFIG, self.updater
            )

        self.assertEqual(search.queries, [f'"note:Sentence" ("s_array:*{NEW_1}*")'])
        self.assertEqual(list(updated), [2])
        self.assertEqual(array_match_data(updated[2]), [["match"], [444]])

    def test_a_duplicate_vocab_note_is_repointed_in_the_sentence_notes_only(self):
        """As the dedupe of existing notes runs it: the group's notes are saved vocab notes,
        and the one kept still has its old array, linking the duplicate."""
        kept = old_array_note(1674931277301, w("本", [1674931277302]))
        kept.fields.update(word_sort_field="hon(m1)", english_meaning_field="book")
        kept.fields["meaning_field"] = "本"
        duplicate = FakeNote(
            {
                "new_note_id_field": "1674931277302",
                "word_sort_field": "hon(m2)",
                "english_meaning_field": "book, volume",
                "meaning_field": "書物",
            },
            1674931277302,
        )
        linking = sentence_note(2, w("本", [1674931277302, 4]))
        search = two_type_search(linking, self.old_array)

        with search.patched():
            notes, updated = mwtn.deduplicate_notes_list([kept, duplicate], TWO_TYPE_CONFIG)

        self.assertEqual(notes, [kept])
        self.assertEqual(search.queries, ['"note:Sentence" "s_array:*1674931277302*"'])
        self.assertEqual(sorted(updated), [2, 1674931277301])
        self.assertEqual(array_match_data(updated[2]), [[1674931277301, 4]])
        # the kept note's meaning is the longer one, its old array as it was
        self.assertEqual(kept["english_meaning_field"], "book, volume")
        self.assertEqual(array_match_data(kept), [[1674931277302]])

    def test_saved_duplicates_are_referred_to_by_their_link_ids(self):
        """A saved note is linked by its link ID (link_ids.py): the generated ID its field
        holds, else its note id, whatever else the field holds. A "0" there (CopyAnywhere's
        `{{__Note_ID}}` written while the note had no id yet) was taken for the kept note's
        reference, and every array linking the duplicate was pointed at 0."""
        generated = 1160697660039337
        kept = FakeNote(
            {
                "new_note_id_field": "0",
                "word_sort_field": "hon(m1)",
                "english_meaning_field": "book",
                "meaning_field": "本",
            },
            1674931277301,
        )
        duplicate = FakeNote(
            {
                "new_note_id_field": str(generated),
                "word_sort_field": "hon(m2)",
                "english_meaning_field": "book, volume",
                "meaning_field": "書物",
            },
            1674931277302,
        )
        # Linked by the duplicate's ID, and by its note id, which is no link to it
        linking = sentence_note(2, w("本", [generated, 4]), w("本", [1674931277302]))
        search = two_type_search(linking)

        with search.patched():
            notes, updated = mwtn.deduplicate_notes_list([kept, duplicate], TWO_TYPE_CONFIG)

        self.assertEqual(notes, [kept])
        self.assertEqual(search.queries, [f'"note:Sentence" "s_array:*{generated}*"'])
        self.assertEqual(sorted(updated), [2, 1674931277301])
        self.assertEqual(array_match_data(updated[2]), [[1674931277301, 4], [1674931277302]])

    def test_through_a_cancelled_adding_the_sentence_notes_keep_the_added_note_s_id(self):
        """And lose the one left out's; nothing is rewritten for the note added."""
        added, left_out = (FakeNote({"new_note_id_field": str(h)}) for h in (NEW_1, NEW_2))
        linking = sentence_note(2, w("様", [NEW_1]), w("本", [NEW_2, 3]))
        search = two_type_search(linking, self.old_array)
        col = SearchableCollection(search, self.updater)
        col.press_cancel_during = added
        saved_phase_log = base_ops.phase_log
        base_ops.phase_log = lambda _name: contextlib.nullcontext()
        self.addCleanup(setattr, base_ops, "phase_log", saved_phase_log)

        with search.patched():
            result = base_ops.add_new_notes(
                col, [added, left_out], TWO_TYPE_CONFIG, POS, self.updater,
                filter_new_notes_op=mwtn.deduplicate_notes_list,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )

        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 0, 1))
        self.assertEqual(array_match_data(FakeNote(search.saved[2])), [[NEW_1], ["match"]])
        self.assertEqual(search.saved[501]["new_note_id_field"], str(NEW_1))
        self.assertNotIn(7, [note.id for note in col.updated])


class NewNotesOfTwoTypesTests(unittest.TestCase):
    """New notes of two note types, each with its own name for its link ID field
    (note_roles.link_id_field): each note's ID is kept through the add, and unlinked, by its
    own type's field."""

    def test_each_note_s_link_id_is_kept_and_unlinked_by_its_own_type_s_field(self):
        config = {
            **CONFIG,
            "Other": {**CONFIG["Word"], "new_note_id_field": "other_id_field"},
        }
        added = new_note(NEW_1)
        failing = FakeNote({"word_list_field": "", "other_id_field": str(NEW_2)})
        failing.note_type = lambda: {"name": "Other"}  # type: ignore[method-assign]
        # In the one-type layout a note holding arrays is told apart by having the new note's
        # type's ID field (_ArrayLinks.holds_array): one sentence of each kind
        other_sentence = FakeNote(
            {"word_list_field": mwtn.format_word_array([w("本", [NEW_2, 3])]),
             "other_id_field": ""},
            3,
        )
        search = FakeSearch(sentence(2, w("様", [NEW_1])), other_sentence)
        updater = FakeUpdater()
        col = SearchableCollection(search, updater, failing=(failing,))

        def writes_both(note):
            for field in ("new_note_id_field", "other_id_field"):
                if field in note:
                    note[field] = "0"

        col.hook = writes_both
        with mock.patch.object(base_ops, "phase_log", lambda _: contextlib.nullcontext()):
            with search.patched():
                base_ops.add_new_notes(
                    col, [added, failing], config, POS, updater,
                    unadded_notes_op=mwtn.clear_unadded_note_ids,
                )

        self.assertEqual(search.saved[501]["new_note_id_field"], str(NEW_1))
        self.assertEqual(failing["other_id_field"], str(NEW_2))
        self.assertEqual(match_data(FakeNote(dict(search.saved[2]), 2)), [[NEW_1]])
        self.assertEqual(match_data(FakeNote(dict(search.saved[3]), 3)), [["match"]])


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
        # The run's index of the vocab type, which hands out the new notes' link IDs
        self.link_index = mwtn.LinkIndex.from_rows([])
        # What the stand-in kana_highlight makes of the next new reading's furigana
        self.processed_furigana = ""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
            ("clean_meaning_in_note", lambda **_: cm.CleanResult(True)),
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
            "vocab_link_index": self.link_index,
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

    def clean_up(
        self,
        search: FakeSearch,
        notes=None,
        cancel_during=None,
        failing: tuple = (),
        tidy: bool = True,
    ):
        """The cleanup from its first save on: the notes the run edited, then the adding, with
        Cancel pressed before the first note is added, or while `cancel_during` is (NO_CANCEL:
        never), then the marker tidying unless not `tidy`. The notes in `failing` fail to
        add."""
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
                filter_new_notes_op=mwtn.deduplicate_notes_list,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )
        if tidy:
            self.tidy(col)
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
    """The markers a new note puts on the other notes of its word, gone when it is not added.

    A second meaning copied from a note renames that note (m1); a new reading of a word gives
    the word's other notes (r1), or (kun)/(on). They are saved with the run's other edits,
    before the adding starts, so a cancel that leaves the new note out has to write them back:
    the cleanup's marker tidying does, as it tidies every word a saved note has markers for.
    """

    def test_a_new_meaning_is_cleaned_with_every_note_matched_and_the_run_s_caches(self):
        # All the word's meanings, the first one without an (mN) marker included, so that the
        # new one is cleaned knowing what it must not repeat; the caches because the sentences
        # are gathered for each of those notes
        seen: dict = {}

        def clean_meaning_in_note(**kwargs):
            seen.update(kwargs)
            return cm.CleanResult(True)

        sentence_cache, note_cache = object(), object()
        first = vocab_note(f"{self.WORD}", 1)
        second = vocab_note(f"{self.WORD} (m2)", 2)
        args = {**self.args(), "sentence_cache": sentence_cache, "note_cache": note_cache}
        with mock.patch.object(mwtn, "clean_meaning_in_note", clean_meaning_in_note):
            mwtn.create_new_note_from_matched_note(
                CONFIG, second, [first, second], 3, "意味", "sense", "", args
            )

        self.assertEqual(seen["other_meaning_notes"], [first, second])
        self.assertIs(seen["note"], self.to_add[self.WORD][-1])
        self.assertIs(seen["sentence_cache"], sentence_cache)
        self.assertIs(seen["note_cache"], note_cache)

    def test_a_new_meaning_a_matched_note_holds_links_the_word_to_that_note(self):
        # The match op's CREATE NEWs made most of the collection's notes that repeat another
        # note's meaning. When the cleaning finds the note whose sense the new one repeats, the
        # word is linked to it and nothing is added or renamed; the quality rated the meaning
        # that is not added, so the word is left to be rated
        first = vocab_note(self.WORD, 1)
        pending = vocab_note(f"{self.WORD} (m2)", 0, new_note_id_field=str(NEW_4))
        # Each by its link ID: the added note's its note id, its field holding none
        for holder, linked_id in ((first, 1), (pending, NEW_4)):
            with self.subTest(linked_id=linked_id):
                tuples: dict = {}
                qualities = {0: 5}
                args = {**self.args(), "processed_word_tuples": tuples, "match_qualities": qualities}
                result = cm.CleanResult(False, holder)
                with mock.patch.object(mwtn, "clean_meaning_in_note", lambda **_: result):
                    created = mwtn.create_new_note_from_matched_note(
                        CONFIG, pending, [first, pending], 3, "意味", "sense", "", args
                    )

                self.assertTrue(created)
                self.assertEqual(
                    tuples, {0: (self.WORD, "ことば", holder["word_sort_field"], linked_id)}
                )
                self.assertEqual(qualities, {})
                self.assertEqual((self.to_add, self.to_update), ({}, {}))
                self.assertEqual(first["word_sort_field"], self.WORD)

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
        held = int(second["new_note_id_field"])
        # Edited elsewhere in the run: its meaning, and its sentence linked to the new note
        renamed = self.to_update[2]
        renamed["meaning_field"] = "新しい意味"
        renamed["word_list_field"] = mwtn.format_word_array([w(self.WORD, [held, 3])])

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

    def test_the_markers_of_two_notes_left_out_both_go(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            other_reading = self.new_reading("げんご", 2)
            self.new_meaning("words", search.get_note(2))
        self.assertEqual(self.to_update[2]["word_sort_field"], f"{self.WORD} (r1)(m1)")

        self.clean_up(search)

        self.assertEqual(search.saved[2]["word_sort_field"], self.WORD)
        self.assertEqual(other_reading.id, 0)

    def test_only_the_marker_the_note_left_out_needed_goes(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            other_reading = self.new_reading("げんご", 2)
            self.new_meaning("words", search.get_note(2))

        self.clean_up(search, cancel_during=other_reading)

        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (r1)")

    def test_a_new_note_marked_by_one_left_out_loses_the_marker_once_added(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            other_reading = self.new_reading("げんご", 2)
            second = self.new_meaning("words", other_reading)
        self.assertEqual(other_reading["word_sort_field"], f"{self.WORD} (r2)(m1)")
        self.assertEqual(second["word_sort_field"], f"{self.WORD} (r2)(m2)")

        self.clean_up(search, cancel_during=other_reading)

        self.assertEqual(search.saved[other_reading.id]["word_sort_field"], f"{self.WORD} (r2)")
        self.assertEqual(search.saved[2]["word_sort_field"], f"{self.WORD} (r1)")


class OneTypeNewNoteTests(NewNoteHarness):
    """The one-type layout makes its new notes as it always has: the four sentence fields
    copied from the processed note, its translation not though the block names one, and no own
    sentence handed to the cleaning, whose prompts would change with it."""

    CONFIG = {"Word": {**CONFIG["Word"], "translated_sentence_field": "translation"}}

    def setUp(self):
        super().setUp()
        self.current.fields.update(
            sentence_field="言葉を選ぶ。",
            sentence_audio_field="[sound:sentence.mp3]",
            furigana_sentence_field=" 言葉[ことば]を 選[えら]ぶ。",
            kanjified_sentence_field="言葉を選ぶ。",
            translation="Choosing words.",
        )
        self.cleaned: list = []

        def clean_meaning_in_note(**kwargs):
            self.cleaned.append(kwargs)
            return cm.CleanResult(True)

        patch = mock.patch.object(mwtn, "clean_meaning_in_note", clean_meaning_in_note)
        patch.start()
        self.addCleanup(patch.stop)

    def assert_sentence_copied(self, new) -> None:
        for field in ("sentence_field", "sentence_audio_field", "kanjified_sentence_field"):
            self.assertEqual(new[field], self.current[field])
        self.assertNotIn("translation", new)
        [kwargs] = self.cleaned
        self.assertIs(kwargs["note"], new)
        self.assertIsNone(kwargs.get("own_sentence"))

    def test_a_note_made_without_a_match(self):
        args = {
            **self.args(index=FakeMarkerIndex()),
            "sentence": self.current["furigana_sentence_field"],
        }
        with FakeSearch().patched():
            self.assertTrue(mwtn.create_new_note_without_matching(self.CONFIG, "", args))
        new = self.to_add[self.WORD][-1]
        self.assert_sentence_copied(new)
        self.assertEqual(new["furigana_sentence_field"], self.current["furigana_sentence_field"])

    def test_a_copied_note(self):
        old = vocab_note(self.WORD, 2, sentence_field="古い文。", word_list_field="[]")
        mwtn.create_new_note_from_matched_note(
            self.CONFIG, old, [old], 2, "意味", "sense", "", self.args()
        )
        new = self.to_add[self.WORD][-1]
        self.assert_sentence_copied(new)
        self.assertEqual(new["word_list_field"], "")

    def test_the_processed_note_names_no_example_and_its_id_field_is_left_empty(self):
        """Nothing names an example in the one-type layout, so nothing is filled (the two-type
        layout fills it, so that the ID travels with the note): the note is as before but for
        the new note's link ID."""
        args = {**self.args(index=FakeMarkerIndex()), "word_path": [1]}
        with FakeSearch().patched():
            self.assertTrue(mwtn.create_new_note_without_matching(self.CONFIG, "", args))

        new = self.to_add[self.WORD][-1]
        self.assertEqual(self.current["new_note_id_field"], "")
        # Keyed by the processed note's link ID, its note id while its field holds none
        key = f"vocab|{self.current.id}|1|{self.WORD}|ことば"
        self.assertEqual(new["new_note_id_field"], str(link_ids.new_link_id(key, ())))


class NewNoteLinkIdTests(NewNoteHarness):
    """A new note's link ID: generated when the note is made, from the processed note's link
    ID, the word's place in its array, the word and its reading; in the note's
    new_note_id_field and in its word's result from then on, and kept once added."""

    def made(self, reading: str = "げんご", word_path=(2,), run=None) -> FakeNote:
        if run is not None:
            self.link_index = run
        args = {**self.args(reading, FakeMarkerIndex()), "word_path": list(word_path)}
        with FakeSearch().patched():
            self.assertTrue(mwtn.create_new_note_without_matching(CONFIG, "", args))
        self.tuples = args["processed_word_tuples"]
        return self.to_add[self.WORD][-1]

    def test_the_word_is_linked_by_the_id_the_note_holds(self):
        new = self.made()

        held = int(new["new_note_id_field"])
        self.assertTrue(link_ids.GENERATED_FLOOR <= held < link_ids.LINK_ID_LIMIT)
        key = f"vocab|{self.current.id}|2|{self.WORD}|げんご"
        self.assertEqual(held, link_ids.new_link_id(key, ()))
        self.assertEqual(self.tuples, {0: (self.WORD, "げんご", self.WORD, held)})

    def test_the_same_input_gets_the_same_id_in_another_run(self):
        first = int(self.made(run=mwtn.LinkIndex.from_rows([]))["new_note_id_field"])
        again = int(self.made(run=mwtn.LinkIndex.from_rows([]))["new_note_id_field"])
        elsewhere = int(self.made(word_path=(3,), run=mwtn.LinkIndex.from_rows([]))[
            "new_note_id_field"
        ])

        self.assertEqual(first, again)
        self.assertNotEqual(first, elsewhere)

    def test_two_notes_of_one_run_never_share_an_id(self):
        """Not even made from one key: two sentence notes holding one link ID, at the same
        word, and not an ID a vocab note held when the run read its index."""
        key = f"vocab|{self.current.id}|2|{self.WORD}|げんご"
        first_choice = link_ids.new_link_id(key, ())
        run = mwtn.LinkIndex.from_rows([(5, str(first_choice))])

        made = [int(self.made(run=run)["new_note_id_field"]) for _ in range(3)]

        self.assertEqual(len(set(made)), 3)
        self.assertNotIn(first_choice, made)
        self.assertEqual(made[0], link_ids.new_link_id(key, {first_choice}))

    def test_a_copied_note_gets_an_id_of_its_own(self):
        """The copy brings the copied note's ID along, which would make it a second holder."""
        old = vocab_note(self.WORD, 2, new_note_id_field=str(NEW_3))
        mwtn.create_new_note_from_matched_note(CONFIG, old, [old], 2, "意味", "sense", "", {
            **self.args(), "word_path": [2],
        })

        new = self.to_add[self.WORD][-1]
        key = f"vocab|{self.current.id}|2|{self.WORD}|ことば"
        self.assertEqual(new["new_note_id_field"], str(link_ids.new_link_id(key, ())))
        self.assertEqual(old["new_note_id_field"], str(NEW_3))

    def test_the_id_is_in_the_array_before_the_add_and_unchanged_after_it(self):
        new = self.made()
        held = int(new["new_note_id_field"])
        # The processed note as the match op saves it before the adding: its word linked by
        # the new note's ID (match_targets.save_results)
        linking = sentence(9, w(self.WORD, [held, 4]))
        search = FakeSearch(linking)

        col = self.clean_up(search, cancel_during=NO_CANCEL, tidy=False)

        self.assertEqual([note for note, _ in col.added], [new])
        self.assertEqual(search.saved[new.id]["new_note_id_field"], str(held))
        self.assertEqual(new["new_note_id_field"], str(held))
        # Nothing rewritten after the add, and nothing looked for
        self.assertEqual(search.saved[9], linking.fields)
        self.assertEqual(search.queries, [])


# The two-type layout as the match op makes new notes in it, each sentence field named apart per
# block: the sentence type's names, which the vocab type has too (a vocab note's sentence and
# array from before the split, kept until the user deletes the fields), and the vocab type's own
# copies of its example's translation and audio, and the example's id
NEW_NOTE_CONFIG = {
    "Sentence": {
        "vocab_note_type": "Word",
        "insert_deck": "Sentences",
        "word_list_field": "s_array",
        "sentence_field": "s_sentence",
        "sentence_audio_field": "s_audio",
        "furigana_sentence_field": "s_furigana",
        "kanjified_sentence_field": "s_kanjified",
        "word_extraction_sentence_field": "s_extraction",
        "translated_sentence_field": "s_translation",
    },
    "Word": {
        "sentence_note_type": "Sentence",
        "insert_deck": "Vocab",
        "example_sentence_id_field": "v_example_id",
        "translated_sentence_field": "v_translation",
        "sentence_audio_field": "v_audio",
        **{key: key for key in mwtn.MATCH_FIELD_KEYS if key not in mwtn.SENTENCE_MATCH_FIELD_KEYS},
    },
}
# The vocab type's fields its notes held their sentence and array in before the split
OLD_SENTENCE_FIELDS = (
    "s_array",
    "s_sentence",
    "s_audio",
    "s_furigana",
    "s_kanjified",
    "s_extraction",
    "s_translation",
)
# The target's sentence as the main prompt showed it, its occurrence of the word in <b>
TARGET_SENTENCE = "その<b>言葉</b>を選ぶ。"


class TypedNote(FakeNote):
    def __init__(self, type_name: str, fields: dict, note_id: int = 0):
        super().__init__(fields, note_id)
        self.type_name = type_name

    def note_type(self):
        return {"name": self.type_name}


def word_type_note(note_id: int = 0, **fields: str) -> TypedNote:
    """A note of the two-type layout's vocab type: every field its block names, and the old
    sentence fields."""
    names = [name for key, name in NEW_NOTE_CONFIG["Word"].items() if key.endswith("_field")]
    values = dict.fromkeys((*names, *OLD_SENTENCE_FIELDS), "")
    values.update(fields)
    return TypedNote("Word", values, note_id)


def processed_sentence_note() -> TypedNote:
    array = [w("その", ["dontmatch"]), w("言葉", ["match"]), w("を選ぶ", ["dontmatch"]), ["。"]]
    fields = {
        "s_array": mwtn.format_word_array(array),
        "s_sentence": "その言葉を選ぶ。",
        "s_audio": "[sound:sentence.mp3]",
        "s_furigana": "その 言葉[ことば]を 選[えら]ぶ。",
        "s_kanjified": "其の言葉を選ぶ。",
        "s_extraction": "その言葉を選ぶ。",
        "s_translation": "Choosing <b>the</b> words.",
    }
    return TypedNote("Sentence", fields, 20)


class TwoTypeNewNoteTests(unittest.TestCase):
    """New notes in the two-type layout: of the vocab type, holding no sentence but their
    example's translation, audio and id, the example being the sentence note processed; and
    cleaned with the target's own sentence, which clean_meaning cannot read off a note that is
    not added yet."""

    WORD = "言葉"

    def setUp(self):
        self.to_add: dict = {}
        self.to_update: dict = {}
        self.sentence = processed_sentence_note()
        self.cleaned: list = []
        self.looked_up: list = []
        self.config = NEW_NOTE_CONFIG
        # The run's index of the vocab type, which hands out the new notes' link IDs
        self.link_index = mwtn.LinkIndex.from_rows([])
        collection = types.SimpleNamespace(models=types.SimpleNamespace(by_name=self.by_name))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
            ("clean_meaning_in_note", self.clean_meaning_in_note),
            ("copy_into_new_note", lambda note: TypedNote(note.type_name, dict(note.fields))),
            ("Note", self.empty_note),
            ("run_on_collection", lambda what, fn: fn()),
            ("make_furigana_from_reading", lambda word, reading: f"{word}[{reading}]"),
            ("kana_highlight", lambda **_: ""),
            ("check_word_reading_type", reading_type),
        ):
            stack.enter_context(mock.patch.object(mwtn, name, value))
        stack.enter_context(mock.patch.object(mw, "col", collection, create=True))

    def by_name(self, name: str):
        self.looked_up.append(name)
        return {"name": name} if name in NEW_NOTE_CONFIG else None

    def empty_note(self, col, model) -> TypedNote:
        """Note(col, model): an empty note of the type it is made in."""
        if model["name"] == "Word":
            return word_type_note()
        return TypedNote(model["name"], dict.fromkeys(OLD_SENTENCE_FIELDS, ""))

    def clean_meaning_in_note(self, **kwargs):
        self.cleaned.append(kwargs)
        return cm.CleanResult(True)

    def args(self, index=None) -> dict:
        return {
            **mwtn.get_match_fields(self.config, {"name": "Sentence"}),
            "word": self.WORD,
            "reading": "ことば",
            "sentence": self.sentence["s_furigana"],
            "prompt_sentence": TARGET_SENTENCE,
            "word_index": 0,
            "part_of_speech": "noun",
            "current_note": self.sentence,
            "note_type": {"name": "Sentence"},
            "vocab_note_type": "Word",
            "notes_to_add_dict": self.to_add,
            "notes_to_update_dict": self.to_update,
            "all_generated_meanings_dict": {},
            "processed_word_tuples": {},
            "word_note_index": index or FakeMarkerIndex(),
            "sentence_cache": None,
            "note_cache": None,
            "word_path": [1],
            "vocab_link_index": self.link_index,
        }

    def made_without_a_match(self) -> TypedNote:
        with FakeSearch().patched():
            created = mwtn.create_new_note_without_matching(self.config, "", self.args())
        self.assertTrue(created)
        return self.to_add[self.WORD][-1]

    def copied_from(self, note_to_copy: TypedNote) -> TypedNote:
        created = mwtn.create_new_note_from_matched_note(
            self.config, note_to_copy, [note_to_copy], 2, "意味", "sense", "", self.args()
        )
        self.assertTrue(created)
        return self.to_add[self.WORD][-1]

    def with_sentence_id_field(self, value: str) -> None:
        """The sentence type's block names its link ID field, which the processed sentence
        note holds `value` in."""
        self.config = {
            **NEW_NOTE_CONFIG,
            "Sentence": {**NEW_NOTE_CONFIG["Sentence"], "sentence_id_field": "s_id"},
        }
        self.sentence.fields["s_id"] = value

    def old_note(self) -> TypedNote:
        """A vocab note of the word holding its own example, and its sentence and array from
        before the split."""
        note = word_type_note(
            2,
            word_sort_field=self.WORD,
            meaning_field="言語。",
            meaning_audio_field="[sound:meaning.mp3]",
            v_translation="The old example.",
            v_audio="[sound:old.mp3]",
            v_example_id="30",
            **{field: f"old {field}" for field in OLD_SENTENCE_FIELDS},
        )
        note.tags = ["studied"]
        return note

    def assert_example_and_no_sentence(self, new: TypedNote) -> None:
        self.assertEqual(new.note_type(), {"name": "Word"})
        self.assertEqual(
            (new["v_translation"], new["v_audio"], new["v_example_id"]),
            ("Choosing <b>the</b> words.", "[sound:sentence.mp3]", "20"),
        )
        self.assertEqual(
            {field: new[field] for field in OLD_SENTENCE_FIELDS},
            dict.fromkeys(OLD_SENTENCE_FIELDS, ""),
        )
        # Added to the vocab type's deck: the cleanup reads it off the note's own type
        self.assertEqual(base_ops._insert_deck_id(FakeCollection(), NEW_NOTE_CONFIG, new), 7)

    def assert_cleaned_with_the_target_s_sentence(self, new: TypedNote) -> None:
        [kwargs] = self.cleaned
        self.assertIs(kwargs["note"], new)
        self.assertEqual(
            kwargs["own_sentence"],
            {"jp_sentence": TARGET_SENTENCE, "en_sentence": "Choosing the words."},
        )

    def test_a_note_made_without_a_match_is_a_vocab_note_with_the_example_only(self):
        new = self.made_without_a_match()

        self.assert_example_and_no_sentence(new)
        self.assertEqual(self.looked_up, ["Word"])
        self.assertEqual(
            (new["word_kanjified_field"], new["word_reading_field"]), (self.WORD, "ことば")
        )
        self.assertEqual(new.tags, ["new_matched_jp_word"])

    def test_a_note_made_without_a_match_is_cleaned_with_the_target_s_sentence(self):
        self.assert_cleaned_with_the_target_s_sentence(self.made_without_a_match())

    def test_a_copied_note_has_the_example_and_not_the_copied_note_s_sentence_or_array(self):
        old = self.old_note()

        new = self.copied_from(old)

        self.assert_example_and_no_sentence(new)
        self.assertEqual(new["meaning_audio_field"], "")
        self.assertEqual(new.tags, ["new_matched_jp_word"])
        self.assertEqual((new["meaning_field"], new["english_meaning_field"]), ("意味", "sense"))
        # The note copied keeps its own example and old fields
        self.assertEqual((old["v_example_id"], old["s_array"]), ("30", "old s_array"))

    def test_a_copied_note_is_cleaned_with_the_target_s_sentence(self):
        self.assert_cleaned_with_the_target_s_sentence(self.copied_from(self.old_note()))

    def test_a_new_vocab_note_holds_its_link_id_keyed_by_the_sentence_note_s(self):
        for make in (self.made_without_a_match, lambda: self.copied_from(self.old_note())):
            with self.subTest(make=make):
                self.link_index = mwtn.LinkIndex.from_rows([])
                new = make()

                key = f"vocab|{self.sentence.id}|1|{self.WORD}|ことば"
                self.assertEqual(new["new_note_id_field"], str(link_ids.new_link_id(key, ())))

    def test_the_sentence_note_s_empty_id_field_is_filled_with_its_note_id(self):
        """The new note names it by that, the ID it has anyway, now written so that it travels
        with the note: the note is saved by the run in any case, its array linking the new
        note."""
        self.with_sentence_id_field("")

        new = self.made_without_a_match()

        self.assertEqual(self.sentence["s_id"], "20")
        self.assertEqual(new["v_example_id"], "20")

    def test_a_sentence_note_holding_a_link_id_is_named_by_it_and_left_as_it_is(self):
        held = 4000000000000001
        self.with_sentence_id_field(str(held))

        new = self.made_without_a_match()

        self.assertEqual((self.sentence["s_id"], new["v_example_id"]), (str(held), str(held)))
        key = f"vocab|{held}|1|{self.WORD}|ことば"
        self.assertEqual(new["new_note_id_field"], str(link_ids.new_link_id(key, ())))

    def test_the_new_note_s_id_is_in_the_sentence_array_before_the_add_and_after_it(self):
        new = self.made_without_a_match()
        held = int(new["new_note_id_field"])
        linking = TypedNote("Sentence", {"s_array": mwtn.format_word_array(
            [w("その", ["dontmatch"]), w(self.WORD, [held, 4])]
        )}, 20)
        search = FakeSearch(linking)
        search.note_types[20] = "Sentence"
        updater = FakeUpdater()
        col = SearchableCollection(search, updater)

        with search.patched(), mock.patch.object(
            base_ops, "phase_log", lambda _: contextlib.nullcontext()
        ):
            result = base_ops.add_new_notes(
                col, [new], self.config, POS, updater,
                filter_new_notes_op=mwtn.deduplicate_notes_list,
                unadded_notes_op=mwtn.clear_unadded_note_ids,
            )

        self.assertEqual(result.counts, base_ops.NewNotesCounts(1, 0, 0))
        self.assertEqual(search.saved[new.id]["new_note_id_field"], str(held))
        self.assertEqual(search.saved[20], linking.fields)
        self.assertEqual(search.queries, [])

    def test_a_field_the_sentence_note_lacks_stops_the_note_before_any_rename(self):
        # The copy is checked before the word's other notes are renamed for the new one, which
        # nothing would take back but the tidying
        del self.sentence.fields["s_translation"]
        search = FakeSearch(word_type_note(2, word_sort_field=self.WORD))

        with search.patched(), self.assertRaises(mwtn.LayoutError):
            mwtn.create_new_note_without_matching(
                NEW_NOTE_CONFIG, "", self.args(FakeMarkerIndex(2))
            )

        self.assertEqual((self.to_add, self.to_update, self.cleaned), ({}, {}, []))


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
        self.assertIn(
            "1 could not be added: the words linked to them are left to be matched again",
            message,
        )
        self.assertNotIn("placeholder", message)

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
        # 単語 was added by hand after 単語 (m1): it comes after, rather than moving it up
        self.assertEqual(renamed, {3: "言語", 5: "単語 (m2)"})

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
    stage: a cancel's, a failed add's and a meaning's that could not be made. Each shows the
    markers as the adding leaves them, then tidied."""

    def test_a_gap_a_failed_add_leaves_is_closed(self):
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            first = search.get_note(2)
            second = self.new_meaning("words", first)
            third = self.new_meaning("speech", first, second)

        col = self.clean_up(search, cancel_during=NO_CANCEL, failing=(second,), tidy=False)
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

        col = self.clean_up(search, cancel_during=NO_CANCEL, failing=(second,), tidy=False)
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

        col = self.clean_up(search, notes=[third, second], cancel_during=third, tidy=False)
        self.assertEqual(self.sort_field(search, third), f"{self.WORD} (m3)")

        self.tidy(col)
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (m1)")
        self.assertEqual(self.sort_field(search, third), f"{self.WORD} (m2)")

    def failed_reading(self, processed_furigana: str, *marker_nids: int) -> None:
        """A new reading whose meaning could not be made: its markers stay on the word's
        other notes, and nothing is added."""
        self.processed_furigana = processed_furigana
        with mock.patch.object(mwtn, "clean_meaning_in_note", lambda **_: cm.CleanResult(False)):
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

        col = self.clean_up(search, notes=[], tidy=False)
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (kun)")

        self.assertEqual(self.tidy(col), [2])
        self.assertEqual(self.sort_field(search, 2), self.WORD)

    def test_the_reading_number_a_failed_reading_leaves_is_taken_off(self):
        search = FakeSearch(
            vocab_note(f"{self.WORD} (m1)", 2), vocab_note(f"{self.WORD} (m2)", 3)
        )
        with search.patched():
            self.failed_reading("", 2, 3)
        self.assertEqual(self.to_update[2]["word_sort_field"], f"{self.WORD} (r1)(m1)")
        self.assertEqual(self.to_update[3]["word_sort_field"], f"{self.WORD} (r1)(m2)")

        col = self.clean_up(search, notes=[], tidy=False)

        self.assertEqual(sorted(self.tidy(col)), [2, 3])
        self.assertEqual(self.sort_field(search, 2), f"{self.WORD} (m1)")
        self.assertEqual(self.sort_field(search, 3), f"{self.WORD} (m2)")

    def test_the_markers_of_a_note_a_cancel_left_out_go(self):
        """Two readings added, and a meaning of the second left out, which had numbered it."""
        search = FakeSearch(vocab_note(self.WORD, 2))
        with search.patched():
            second = self.new_reading("げんご", 2)
            third = self.new_reading("ことのは", 2)
            self.new_meaning("words", second)

        col = self.clean_up(search, cancel_during=third, tidy=False)
        self.assertEqual(self.sort_field(search, second), f"{self.WORD} (r2)(m1)")

        self.assertEqual(self.tidy(col), [second.id])
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
        stack.enter_context(mock.patch.object(base_ops, "start_run_controls", lambda: None))
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

    def test_a_note_registered_after_the_op_answered_is_not_added(self):
        """By a thread a cancel abandoned, during the edited notes' write: its link ID is in
        no result saved, so added it would be a note no sentence links to."""
        search = FakeSearch(vocab_note("言葉", 2))
        answered, late = vocab_note("言葉 (m2)"), vocab_note("言葉 (m3)")
        col = RunCollection(search, self.updater)
        shared: list = []
        write = col.update_notes

        def update_notes(notes):
            write(notes)
            shared[0].setdefault("言葉", []).append(late)

        col.update_notes = update_notes  # type: ignore[method-assign]

        async def bulk_op(col, notes, notes_to_add_dict, notes_to_update_dict, **_):
            shared.append(notes_to_add_dict)
            notes_to_add_dict["言葉"] = [answered]
            notes_to_update_dict[2] = vocab_note("言葉 (m1)", 2)
            return POS, {"言葉": [answered]}, notes_to_update_dict, []

        with mock.patch.object(mw, "col", col, create=True):
            base_ops.selected_notes_op(
                "Done", bulk_op, [2], None, self.updater, new_notes_op=Recorder()
            )

        self.assertEqual([note for note, _ in col.added], [answered])
        self.assertIn(late, shared[0]["言葉"])

    def test_without_a_tidying_op_nothing_is_tidied(self):
        search = FakeSearch(vocab_note("言葉 (kun)", 2))
        edited = {2: vocab_note("言葉 (kun)", 2)}

        col = self.run_op(search, [2], edited, [], None)

        self.assertEqual(col.updated, [edited[2]])
