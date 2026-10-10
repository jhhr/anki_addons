"""The op "Move sentences to sentence notes": every vocab note's sentence moves into a note of the
sentence type, which the vocab note keeps as its example. Run once over every note of the vocab
type, and again after a cancel or over notes a first run left out: a rerun joins the sentence
notes there are rather than making them again.

What to do is decided before any note is processed. The bulk op reads every vocab note of the
type and every sentence note (one SQL pass each, with the cards' review counts), and
`sentence_migration.plan_migration` makes the plan; this module only applies it. The plan's
report goes to user_files/sentence_migration_<timestamp>.txt before the first note is processed,
and the run's end message names the file.

The per-note op applies the plan note by note, so progress, pause and cancel work as in any sync
op, and nothing is written before the cleanup:

- A new sentence note is registered for adding when the first of its vocab notes is processed.
  The notes are processed in the plan's order, so the sentence notes are added oldest origin
  first and their ids, which are creation times, follow their origins'. Its vocab notes are
  changed only in `new_notes_op`, once it has been added and has an id: a sentence note never
  added (a cancel before its first vocab note, or of the adding; a failed add) leaves its vocab
  notes as they were, and a rerun picks them up.
- A vocab note whose example is an existing sentence note (a rerun's join) is changed by the
  per-note op, and that sentence note with it when its array or tags change.

The added sentence notes' cards are suspended (a sentence note is never studied) in
`new_notes_op`: inside the cleanup, before base_ops saves what it returns and merges into the
run's undo entry, so one undo reverts the adds, the updates and the suspension together.
"""

from __future__ import annotations

import html
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from functools import partial
from typing import Any, Optional

from anki.collection import Collection
from anki.notes import Note, NoteId
from anki.utils import ids2str
from aqt import mw
from aqt.utils import showWarning

from ..async_api_ops.base_ops import (
    AsyncTaskProgressUpdater,
    BulkOpResult,
    NotesRunSpec,
    bulk_notes_op,
    note_context,
    selected_notes_op,
    show_dialog_label,
)
from ..async_api_ops.chain_types import ChainStep, fail_step
from ..async_api_ops.progress_errors import report_exception
from ..configuration import ADDON_USER_FILES_DIR
from ..note_roles import SENTENCE_KEYS, copy_example, sentence_type_of
from .sentence_migration import (
    COPY_TAGS_KEY,
    EXAMPLE_ID_KEY,
    MOVE_TAGS_KEY,
    SEEN_COUNT_KEY,
    WORD_KEYS,
    WORD_LIST_KEY,
    MigrationPlan,
    NewSentence,
    SentenceRecord,
    SentenceUpdate,
    VocabRecord,
    plan_migration,
    preflight_error,
)

logger = logging.getLogger(__name__)

LABEL = "Move sentences to sentence notes"
# The bulk op's message, which also names the run's undo entry
MESSAGE = "Moving sentences to sentence notes"


class PreflightError(Exception):
    """The settings or the collection do not allow the migration; the message, for the user,
    says why. Raised by the run before it writes anything."""


def _named(block: Mapping[str, Any], key: str) -> str:
    value = block.get(key)
    return value if isinstance(value, str) else ""


def _selected_types(col: Collection, nids: Sequence[NoteId]) -> list[str]:
    db = col.db
    assert db is not None
    names = []
    for mid in db.list(f"select distinct mid from notes where id in {ids2str(nids)}"):
        notetype = col.models.get(mid)
        names.append(notetype["name"] if notetype is not None else str(mid))
    return names


def collection_preflight_error(
    col: Collection, config: Mapping[str, Any], nids: Sequence[NoteId]
) -> Optional[str]:
    """`sentence_migration.preflight_error` for these notes of the collection: why the run
    cannot start, None when it can."""
    note_type_fields = {
        notetype["name"]: [field["name"] for field in notetype["flds"]]
        for notetype in col.models.all()
    }
    return preflight_error(
        config,
        _selected_types(col, nids),
        note_type_fields,
        lambda name: col.decks.id_for_name(name) is not None,
    )


def _read_notes(
    col: Collection, notetype: Mapping[str, Any], names: Mapping[str, str]
) -> list[tuple[int, list[str], dict[str, str]]]:
    """Every note of the type as (id, tags, the fields `names` gives by key), in one SQL pass as
    word_index._read_notes reads: loading a Note each is a backend call per note, 25k of them.
    The pre-flight has checked that each field is on the type."""
    db = col.db
    assert db is not None
    ords = {field["name"]: field["ord"] for field in notetype["flds"]}
    wanted = {key: ords[name] for key, name in names.items()}
    rows = []
    for nid, tags, flds in db.all(
        "select id, tags, flds from notes where mid = ?", notetype["id"]
    ):
        values = flds.split("\x1f")
        rows.append(
            (
                nid,
                tags.split(),
                {key: values[ord_] if ord_ < len(values) else "" for key, ord_ in wanted.items()},
            )
        )
    return rows


def read_records(
    col: Collection, config: Mapping[str, Any], vocab_type: str, sentence_type: str
) -> tuple[list[VocabRecord], list[SentenceRecord]]:
    """Every vocab note of the type and every sentence note, as the planner takes them. The
    vocab notes' old sentence fields and array are read by the names the sentence block gives
    them, which the vocab notes keep until the user deletes the fields."""
    db = col.db
    assert db is not None
    vocab_block, sentence_block = config[vocab_type], config[sentence_type]
    sentence_names = {
        key: _named(sentence_block, key)
        for key in (*SENTENCE_KEYS, WORD_LIST_KEY)
        if _named(sentence_block, key)
    }
    vocab_names = {
        **sentence_names,
        **{key: _named(vocab_block, key) for key in (EXAMPLE_ID_KEY, *WORD_KEYS)},
    }
    vocab_notetype = col.models.by_name(vocab_type)
    sentence_notetype = col.models.by_name(sentence_type)
    assert vocab_notetype is not None and sentence_notetype is not None
    # A vocab note has one card; the count shows the one that has more
    reviews = {
        nid: (reps or 0, cards)
        for nid, reps, cards in db.all(
            "select nid, sum(reps), count() from cards where nid in"
            " (select id from notes where mid = ?) group by nid",
            vocab_notetype["id"],
        )
    }

    def sentence_fields(values: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in values.items() if key in SENTENCE_KEYS}

    vocab = [
        VocabRecord(
            note_id=nid,
            tags=tags,
            fields=sentence_fields(values),
            word_list=values[WORD_LIST_KEY],
            example_id=values[EXAMPLE_ID_KEY],
            kanjified=values[WORD_KEYS[0]],
            normal=values[WORD_KEYS[1]],
            reading=values[WORD_KEYS[2]],
            reps=reviews.get(nid, (0, 0))[0],
            card_count=reviews.get(nid, (0, 0))[1],
        )
        for nid, tags, values in _read_notes(col, vocab_notetype, vocab_names)
    ]
    sentences = [
        SentenceRecord(
            note_id=nid,
            tags=tags,
            fields=sentence_fields(values),
            word_list=values[WORD_LIST_KEY],
        )
        for nid, tags, values in _read_notes(col, sentence_notetype, sentence_names)
    ]
    return vocab, sentences


class SentenceMigration:
    """One run of the op: the plan, and what the bulk op, the per-note op and `new_notes_op`
    share while applying it. `migrate_spec` makes one per run, so nothing outlives its run, and
    each run writes a report of its own (`report_path`, fixed when the run is made so that the
    end message can name it)."""

    def __init__(self, report_dir: Optional[str] = None) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.report_path = os.path.join(
            report_dir or ADDON_USER_FILES_DIR, f"sentence_migration_{stamp}.txt"
        )
        self.plan: Optional[MigrationPlan] = None
        self._col: Optional[Collection] = None
        self._config: Mapping[str, Any] = {}
        self._vocab_type = ""
        self._sentence_type = ""
        self._sentence_notetype: Any = None
        self._updates: dict[int, SentenceUpdate] = {}
        # The selected notes as the run loaded them, which new_notes_op changes: the per-note
        # op leaves a vocab note of a new sentence note alone, so they are as the collection is
        self._loaded: dict[int, Note] = {}
        # Per new sentence note (its index in the plan), its note once registered for adding
        self._registered: dict[int, Note] = {}
        # The existing sentence notes joined, each read once and updated in memory
        self._joined: dict[int, Note] = {}

    def spec(self) -> NotesRunSpec:
        report = html.escape(self.report_path)
        return NotesRunSpec(
            done_text=f"Moved sentences to sentence notes (report: {report})",
            title=f"Sync op: {MESSAGE}",
            bulk_op=self.bulk_op,
            new_notes_op=self.add_examples,
        )

    @property
    def col(self) -> Collection:
        assert self._col is not None, "the run has not started"
        return self._col

    @property
    def _plan(self) -> MigrationPlan:
        assert self.plan is not None, "the run has not planned yet"
        return self.plan

    async def bulk_op(
        self,
        col: Collection,
        notes: Sequence[Note],
        edited_nids: list[NoteId],
        progress_updater: AsyncTaskProgressUpdater,
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
    ) -> BulkOpResult:
        """Read, plan and write the report, then apply the plan note by note, in its order.
        Raises PreflightError, failing the run before it writes anything, when the settings or
        the collection do not allow it: a script starts the run without the menu's check."""
        config = mw.addonManager.getConfig(__name__) or {}
        nids = [note.id for note in notes]
        error = collection_preflight_error(col, config, nids)
        if error:
            raise PreflightError(error)
        self._col, self._config = col, config
        [self._vocab_type] = _selected_types(col, nids)
        self._sentence_type = sentence_type_of(config, self._vocab_type)
        self._sentence_notetype = col.models.by_name(self._sentence_type)
        sentence_block = config[self._sentence_type]

        show = partial(self._show, total=len(notes))
        show(f"Reading the notes of {self._vocab_type} and {self._sentence_type}")
        vocab, sentences = read_records(col, config, self._vocab_type, self._sentence_type)
        show(f"Planning: {len(vocab)} vocab notes, {len(sentences)} sentence notes")
        plan = self.plan = plan_migration(
            vocab,
            sentences,
            nids,
            count_seen=bool(_named(sentence_block, SEEN_COUNT_KEY)),
            move_tags=sentence_block.get(MOVE_TAGS_KEY, []),
            copy_tags=sentence_block.get(COPY_TAGS_KEY, []),
        )
        self._updates = {update.note_id: update for update in plan.updates}
        self._write_report(plan)
        self._loaded = {note.id: note for note in notes}
        # A cancel pressed while planning: the run then ends here, its report written
        ordered = [] if mw.progress.want_cancel() else self._in_plan_order(notes)
        return await bulk_notes_op(
            MESSAGE,
            config,
            self.note_op,
            col,
            ordered,
            edited_nids,
            progress_updater,
            notes_to_add_dict,
            notes_to_update_dict,
            is_sync_op=True,
        )

    @staticmethod
    def _show(text: str, total: int) -> None:
        # Nothing else draws the dialog while the op thread reads and plans
        mw.taskman.run_on_main(
            partial(show_dialog_label, f"<b>{html.escape(text)}</b>", 0, max(total, 1))
        )

    def _write_report(self, plan: MigrationPlan) -> None:
        header = (
            f"{LABEL}, {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f'Vocab note type "{self._vocab_type}", sentence note type "{self._sentence_type}"\n'
            "Notes are named by id; a sentence note not added yet by its origin's.\n\n"
        )
        os.makedirs(os.path.dirname(self.report_path), exist_ok=True)
        with open(self.report_path, "w", encoding="utf-8") as file:
            file.write(header + plan.report.text())
        logger.info("%s: the report is %s", LABEL, self.report_path)

    def _in_plan_order(self, notes: Sequence[Note]) -> list[Note]:
        """The notes of new sentence notes first, by their sentence note's place in the plan,
        then those of existing ones, then the rest as selected: each new sentence note is
        registered by the first of its notes processed, so they are added in the plan's order."""
        plan = self._plan
        rank: dict[int, tuple[int, int]] = {}
        for index, new in enumerate(plan.new_sentences):
            for vocab_id in new.vocab_ids:
                rank[vocab_id] = (0, index)
        for index, update in enumerate(plan.updates):
            for vocab_id in update.vocab_ids:
                rank[vocab_id] = (1, index)
        return sorted(notes, key=lambda note: rank.get(note.id, (2, 0)))

    def note_op(
        self,
        config: dict,
        note: Note,
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
    ) -> bool:
        """Apply the plan to one selected vocab note; writes nothing to the collection."""
        plan = self._plan
        example = plan.examples.get(note.id)
        if example is None:
            # Given no example (already migrated, no sentence text, none found): the report
            # says which and why
            return False
        if example.new_index is not None:
            if example.new_index not in self._registered:
                new = plan.new_sentences[example.new_index]
                sentence_note = self._new_sentence_note(new)
                self._registered[example.new_index] = sentence_note
                notes_to_add_dict.setdefault(f"sentence of {new.origin_id}", []).append(
                    sentence_note
                )
            # The note itself waits for its sentence note to exist: add_examples
            return True
        assert example.note_id is not None
        sentence_note = self._join(example.note_id, notes_to_update_dict)
        self._make_example(sentence_note, example.note_id, note)
        notes_to_update_dict[note.id] = note
        return True

    def _new_sentence_note(self, new: NewSentence) -> Note:
        note = self.col.new_note(self._sentence_notetype)
        block = self._config[self._sentence_type]
        for key, value in new.fields.items():
            note[block[key]] = value
        note.tags = list(new.tags)
        return note

    def _join(self, sentence_id: int, notes_to_update_dict: dict[NoteId, Note]) -> Note:
        """The existing sentence note, read and changed as the plan says the first time a vocab
        note joins it; saved only when that changes it."""
        sentence_note = self._joined.get(sentence_id)
        if sentence_note is not None:
            return sentence_note
        sentence_note = self.col.get_note(NoteId(sentence_id))
        update = self._updates[sentence_id]
        if update.word_list is not None:
            sentence_note[self._config[self._sentence_type][WORD_LIST_KEY]] = update.word_list
        if update.tags_to_add:
            sentence_note.tags = [*sentence_note.tags, *update.tags_to_add]
        if update.word_list is not None or update.tags_to_add:
            notes_to_update_dict[sentence_note.id] = sentence_note
        self._joined[sentence_id] = sentence_note
        return sentence_note

    def _make_example(self, sentence_note: Note, sentence_id: int, vocab_note: Note) -> None:
        copy_example(
            self._config,
            self._vocab_type,
            sentence_note=sentence_note,
            sentence_id=sentence_id,
            vocab_note=vocab_note,
        )
        self._remove_tags(vocab_note)

    def _remove_tags(self, vocab_note: Note) -> None:
        lost = self._plan.tags_to_remove.get(vocab_note.id)
        if lost:
            gone = {tag.casefold() for tag in lost}
            vocab_note.tags = [tag for tag in vocab_note.tags if tag.casefold() not in gone]

    def add_examples(
        self,
        added_notes: list[Note],
        config: dict,
        progress_updater: AsyncTaskProgressUpdater,
    ) -> dict[NoteId, Note]:
        """`new_notes_op`: make each added sentence note the example of its vocab notes, take
        off them the tags the plan moves, and suspend the added notes' cards.

        Returns the notes to save: the vocab notes, and the added sentence notes as they are,
        which Anki does not write again (an unchanged note is skipped). Those are there so that
        base_ops' save, and its merge into the run's undo entry after it, which takes the
        suspension made here, happens even if no vocab note could be changed: it merges only
        when there is something to save."""
        plan = self._plan
        changed_with = plan.changed_with_new()
        index_of = {id(note): index for index, note in self._registered.items()}
        to_save: dict[NoteId, Note] = {}
        ours: list[Note] = []
        for done, sentence_note in enumerate(added_notes, start=1):
            index = index_of.get(id(sentence_note))
            if index is not None:
                ours.append(sentence_note)
                with note_context(sentence_note):
                    try:
                        to_save.update(self._examples_of(index, changed_with[index], sentence_note))
                    except Exception as e:
                        # Its vocab notes stay as they were, joined to it by the next run
                        logger.error("Sentence note %s: %s", sentence_note.id, e)
                        report_exception(e, "Making the sentence note its vocab notes' example")
            progress_updater.update_new_note_processing_progress(
                new_notes_processed=done, total_notes=len(added_notes)
            )
        self._suspend(ours)
        for sentence_note in ours:
            to_save.setdefault(sentence_note.id, sentence_note)
        return to_save

    def _examples_of(
        self, index: int, vocab_ids: Sequence[int], sentence_note: Note
    ) -> dict[NoteId, Note]:
        """The vocab notes added sentence note `index` changes: each it is the example of gets
        the copy, and each loses the tags the plan moves off it (an unselected origin only
        those). In memory; returned only if every one of them could be changed."""
        examples = set(self._plan.new_sentences[index].vocab_ids)
        changed: dict[NoteId, Note] = {}
        for vocab_id in vocab_ids:
            vocab_note = self._loaded.get(vocab_id)
            if vocab_note is None:
                # An unselected origin, which the run did not load
                vocab_note = self.col.get_note(NoteId(vocab_id))
            if vocab_id in examples:
                self._make_example(sentence_note, sentence_note.id, vocab_note)
            else:
                self._remove_tags(vocab_note)
            changed[vocab_note.id] = vocab_note
        return changed

    def _suspend(self, sentence_notes: Sequence[Note]) -> None:
        if not sentence_notes:
            return
        db = self.col.db
        assert db is not None
        nids = ids2str(note.id for note in sentence_notes)
        card_ids = db.list(f"select id from cards where nid in {nids}")
        if card_ids:
            self.col.sched.suspend_cards(card_ids)


def migrate_spec() -> NotesRunSpec:
    """The op's run, as the menu starts it and as a script does (`NotesRunSpec`): a new
    `SentenceMigration` each time, with its own plan and report."""
    return SentenceMigration().spec()


def migrate_to_sentence_notes_from_selected(
    nids: Sequence[NoteId], parent: Any, chain: Optional[ChainStep] = None
):
    """Move the selected vocab notes' sentences to sentence notes. The pre-flight runs here,
    where its message can be shown before any progress dialog, and fails the chain step when
    the run cannot start (the run checks again, for a script)."""
    config = mw.addonManager.getConfig(__name__) or {}
    error = collection_preflight_error(mw.col, config, nids)
    if error:
        logger.error("%s: %s", LABEL, error)
        showWarning(error, parent=parent, title=LABEL)
        fail_step(chain, error)
        return None
    spec = migrate_spec()
    return selected_notes_op(
        spec.done_text,
        spec.bulk_op,
        nids,
        parent,
        AsyncTaskProgressUpdater(title=spec.title),
        spec.new_notes_op,
        chain=chain,
    )
