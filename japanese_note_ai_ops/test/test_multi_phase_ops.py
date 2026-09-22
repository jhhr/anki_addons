"""Several bulk ops over one selection, run as a single operation.

A phase is a whole bulk op, not a step inside one, because a nested op plans every note's
task count before it starts anything - the judge cannot know how many words it will ask about
until the phase that generates them has run. What `run_op_phases` has to get right is what the
phases share: the notes list (so a later phase reads the earlier one's writes from memory),
the add/update dicts, and the undo entry the cleanup merges into, which is the first phase's.
"""

import asyncio
import time
import unittest
from typing import TYPE_CHECKING

from addon_modules import PausingClock, load_ops_module, mw

base_ops = load_ops_module("base_ops")
# The same module object base_ops imported, so pausing it pauses base_ops' run
api = load_ops_module("api_client")

if TYPE_CHECKING:
    # The real class, so `OpPhase` is a type and not just a name load_ops_module handed back
    from japanese_note_ai_ops.async_api_ops.base_ops import OpPhase
else:
    OpPhase = base_ops.OpPhase


class FakeNote:
    """A note a phase can write into, so the next phase can be asked what it sees."""

    def __init__(self, note_id: int):
        self.id = note_id
        self.fields: list[str] = []


class FakeCollection:
    def __init__(self):
        self.undo_entries: list[str] = []

    def add_custom_undo_entry(self, message: str) -> int:
        self.undo_entries.append(message)
        return 100 + len(self.undo_entries)


class FakeUpdater:
    """Records the phase resets instead of drawing them."""

    def __init__(self):
        self.phases: list[tuple[int, int, str]] = []

    def begin_phase(self, index: int, total: int, name: str = "") -> None:
        self.phases.append((index, total, name))


class RunOpPhasesTest(unittest.TestCase):
    def setUp(self):
        mw.progress.cancel = False
        self.col = FakeCollection()
        self.notes = [FakeNote(1), FakeNote(2)]
        self.updater = FakeUpdater()
        self.add_dict: dict = {}
        self.update_dict: dict = {}
        self.edited: list = []

    def tearDown(self):
        mw.progress.cancel = False

    def run_phases(self, phases):
        return asyncio.run(
            base_ops.run_op_phases(
                phases,
                self.col,
                notes=self.notes,
                edited_nids=self.edited,
                progress_updater=self.updater,
                notes_to_add_dict=self.add_dict,
                notes_to_update_dict=self.update_dict,
                label="Did the thing",
            )
        )

    def writing_phase(self, name: str, mark: str, seen: "list | None" = None) -> OpPhase:
        """A phase that writes `mark` into every note, reporting what it found there."""

        async def op(
            col,
            notes,
            edited_nids,
            progress_updater,
            notes_to_add_dict,
            notes_to_update_dict,
        ):
            if seen is not None:
                seen.extend(list(note.fields) for note in notes)
            for note in notes:
                note.fields.append(mark)
                notes_to_update_dict[note.id] = note
                edited_nids.append(note.id)
            return (
                col.add_custom_undo_entry(name),
                notes_to_add_dict,
                notes_to_update_dict,
                [],
            )

        return OpPhase(name, op)

    # --- what the phases share --------------------------------------------------------------

    def test_a_later_phase_sees_the_earlier_phase_s_writes(self):
        """The point of sharing one notes list: no reload, and one write per note at the end."""
        seen: list = []
        pos, add_dict, update_dict, to_remove = self.run_phases(
            [self.writing_phase("one", "a"), self.writing_phase("two", "b", seen)]
        )

        self.assertEqual(seen, [["a"], ["a"]])
        self.assertEqual([note.fields for note in self.notes], [["a", "b"], ["a", "b"]])
        self.assertIs(update_dict, self.update_dict)
        self.assertEqual(sorted(update_dict), [1, 2])
        self.assertEqual(to_remove, [])

    def test_the_first_phase_s_undo_entry_is_the_one_returned(self):
        """Cleanup merges everything into it, so the run is one undoable operation."""
        pos, _, _, _ = self.run_phases(
            [self.writing_phase("one", "a"), self.writing_phase("two", "b")]
        )

        self.assertEqual(self.col.undo_entries, ["one", "two"])
        self.assertEqual(pos, 101)

    def test_each_phase_is_announced_to_the_dialog(self):
        self.run_phases([self.writing_phase("one", "a"), self.writing_phase("two", "b")])

        self.assertEqual(self.updater.phases, [(1, 2, "one"), (2, 2, "two")])

    def test_a_single_phase_leaves_the_dialog_alone(self):
        """Every op goes through here now, and a plain one must look exactly as it did."""
        self.run_phases([self.writing_phase("one", "a")])

        self.assertEqual(self.updater.phases, [])

    # --- stopping ---------------------------------------------------------------------------

    def test_a_cancel_during_a_phase_stops_the_next_one_starting(self):
        """The cancelled phase still returns what it did; the run just does not go on."""
        started: list = []

        def cancelling_phase(name: str) -> OpPhase:
            async def op(col, notes, edited_nids, progress_updater, **dicts):
                started.append(name)
                mw.progress.cancel = True
                return (
                    col.add_custom_undo_entry(name),
                    dicts["notes_to_add_dict"],
                    dicts["notes_to_update_dict"],
                    [],
                )

            return OpPhase(name, op)

        second = self.writing_phase("two", "b")
        pos, _, _, _ = self.run_phases([cancelling_phase("one"), second])

        self.assertEqual(started, ["one"])
        self.assertEqual(self.notes[0].fields, [])
        self.assertEqual(pos, 101)

    # --- pausing ----------------------------------------------------------------------------

    def pausing_phase(self, name: str, started: list) -> OpPhase:
        """A phase that pauses the run as it finishes, the way a user's click lands mid-phase."""

        async def op(col, notes, edited_nids, progress_updater, **dicts):
            started.append(name)
            api.pause_run("paused by user")
            return (
                col.add_custom_undo_entry(name),
                dicts["notes_to_add_dict"],
                dicts["notes_to_update_dict"],
                [],
            )

        return OpPhase(name, op)

    def install_clock(self, on_sleep) -> PausingClock:
        """Drive the pause wait from a fake clock, inside a run on this thread."""
        api.begin_run()
        self.addCleanup(api.end_run)
        clock = PausingClock(on_sleep)
        saved = api.time
        setattr(api, "time", clock)
        self.addCleanup(setattr, api, "time", saved)
        return clock

    def test_a_paused_run_starts_the_next_phase_only_after_resume(self):
        started: list = []
        seen_while_paused: list = []

        def on_sleep(sleeps):
            seen_while_paused.append(list(started))
            if sleeps == 3:
                api.resume_run()

        clock = self.install_clock(on_sleep)
        self.run_phases([self.pausing_phase("one", started), self.writing_phase("two", "b")])

        self.assertEqual(seen_while_paused, [["one"]] * 3)
        self.assertEqual(len(clock.slept), 3)
        self.assertEqual(self.notes[0].fields, ["b"])

    def test_a_cancel_while_paused_between_phases_stops_the_next_one_starting(self):
        """Both kinds of cancel: the dialog's (Escape) and the run's own."""
        cancels = {
            "dialog": lambda: setattr(mw.progress, "cancel", True),
            "run": api.cancel_run,
        }
        for kind, cancel in cancels.items():
            with self.subTest(cancel=kind):
                mw.progress.cancel = False
                self.notes = [FakeNote(1), FakeNote(2)]
                started: list = []

                def on_sleep(sleeps, cancel=cancel):
                    if sleeps == 2:
                        cancel()

                self.install_clock(on_sleep)
                self.run_phases(
                    [self.pausing_phase("one", started), self.writing_phase("two", "b")]
                )

                self.assertEqual(started, ["one"])
                self.assertEqual(self.notes[0].fields, [])

    # --- phases that do not play along --------------------------------------------------------

    def test_a_phase_that_returns_nothing_is_skipped(self):
        """An op with no config bails out before it starts; the rest of the run is unaffected."""

        async def bails_out(col, notes, edited_nids, progress_updater, **dicts):
            return None

        pos, _, update_dict, _ = self.run_phases(
            [OpPhase("nothing", bails_out), self.writing_phase("two", "b")]
        )

        self.assertEqual(self.col.undo_entries, ["two"])
        self.assertEqual(pos, 101)
        self.assertEqual(sorted(update_dict), [1, 2])

    def test_an_undo_entry_is_made_when_every_phase_bails_out(self):
        """The cleanup writes its own notes into `pos` whatever the phases did."""

        async def bails_out(col, notes, edited_nids, progress_updater, **dicts):
            return None

        pos, _, _, _ = self.run_phases([OpPhase("nothing", bails_out)])

        self.assertEqual(self.col.undo_entries, ["Did the thing"])
        self.assertEqual(pos, 101)

    def test_a_phase_s_own_dicts_are_folded_into_the_shared_ones(self):
        """Ops are handed the shared dicts, but nothing stops one building its own."""
        new_note = FakeNote(0)
        other_note = FakeNote(7)
        self.add_dict["Word"] = [FakeNote(0)]

        async def op(col, notes, edited_nids, progress_updater, **dicts):
            return (
                col.add_custom_undo_entry("own dicts"),
                {"Word": [new_note], "Kanji": [new_note]},
                {other_note.id: other_note},
                [9],
            )

        _, add_dict, update_dict, to_remove = self.run_phases([OpPhase("own", op)])

        self.assertEqual(len(add_dict["Word"]), 2)
        self.assertIs(add_dict["Word"][1], new_note)
        self.assertEqual(add_dict["Kanji"], [new_note])
        self.assertEqual(list(update_dict), [7])
        self.assertEqual(to_remove, [9])


class PhaseProgressTest(unittest.TestCase):
    """The estimate has to start over at each phase or it carries the earlier one's times."""

    def test_begin_phase_resets_the_counters_and_titles_the_dialog(self):
        titles: list = []
        original_set_title = mw.progress.set_title
        mw.progress.set_title = titles.append
        try:
            updater = base_ops.AsyncTaskProgressUpdater(
                total_notes=5, total_tasks=10, title="Async AI op: Extract words"
            )
            updater.increment_counts(
                tasks_done=4, tasks_in_progress=2, notes_done=3, cumulative_task_time=12.0
            )
            updater.start_time -= 600

            updater.begin_phase(2, 2, "Judge words matchability")

            self.assertEqual(updater.total_tasks, 0)
            self.assertEqual(updater.tasks_done, 0)
            self.assertEqual(updater.tasks_in_progress, 0)
            self.assertEqual(updater.notes_done, 0)
            self.assertEqual(updater.cumulative_task_time, 0.0)
            self.assertEqual(updater.max_task_time, 0.0)
            self.assertLess(time.time() - updater.start_time, 5)
            self.assertEqual(
                titles[-1],
                "Async AI op: Extract words (Phase 2/2: Judge words matchability)",
            )
        finally:
            mw.progress.set_title = original_set_title


if __name__ == "__main__":
    unittest.main()
