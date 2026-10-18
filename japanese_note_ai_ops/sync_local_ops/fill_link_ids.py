"""The op "Fill link IDs" (link_ids.FILL_LINK_IDS): each selected note ends up holding a link ID
of its own in its type's link ID field (link_ids.py), and the legacy placeholders in the selected
notes' word arrays are resolved.

Three kinds of write, each to a selected note only, all decided before the first is made:

- a link ID field holding no ID (empty, "0", a legacy placeholder, other text) gets the note's
  note id: its link ID all along, which an export then carries;
- of the notes holding one ID (Anki's Create Copy and CopyAnywhere's duplicating copy the field),
  each but the one keeping it gets an ID of its own: its own note id when no note holds that,
  else a generated one (link_ids.plan_id_fills);
- in the word array of a note of the sentence role, a legacy negative placeholder becomes the
  link ID of the vocab note whose field holds it (that note's note id), `["match"]` when none
  does; one several notes hold is left (match_targets.resolve_placeholder_ids).

The ops that write or remove links refuse while two notes of a type hold one ID, naming this op
(link_ids.duplicates_error); run over every note of a type, it leaves none there. The notes of a
type whose settings name no link ID field are left out and counted. A run reads each type's link
IDs once, plans, and writes its report to user_files/fill_link_ids_<timestamp>.txt before it
changes anything, which the end message names; the notes are written in the cleanup, one undo
entry.
"""

from __future__ import annotations

import html
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    selected_notes_op,
    show_dialog_label,
)
from ..async_api_ops.chain_types import ChainStep, fail_step
from ..async_api_ops.link_lookup import link_index
from ..configuration import ADDON_USER_FILES_DIR
from ..link_ids import (
    FILL_LINK_IDS,
    DuplicateFill,
    IdFills,
    LinkIndex,
    NoteInHand,
    placeholder_holders,
    plan_id_fills,
)
from ..note_roles import LayoutError, is_sentence_type, layout_error, link_id_field, vocab_type_of
from ..word_array import match_flags, match_targets

logger = logging.getLogger(__name__)

LABEL = FILL_LINK_IDS
# The bulk op's message, which also names the run's undo entry
MESSAGE = "Filling link IDs"


class FillLinkIdsError(Exception):
    """The settings or the collection do not let the run start; the message, for the user, says
    why. Raised by the run before it writes anything."""


def _selected_type_counts(col: Collection, nids: Sequence[NoteId]) -> dict[str, int]:
    """How many of the notes are of each note type, by the type's name."""
    db = col.db
    assert db is not None
    counts: dict[str, int] = {}
    for mid, count in db.all(
        f"select mid, count() from notes where id in {ids2str(nids)} group by mid"
    ):
        notetype = col.models.get(mid)
        counts[notetype["name"] if notetype is not None else str(mid)] = count
    return counts


def _types_read(config: Mapping[str, Any], note_type_name: str) -> list[str]:
    """The types whose link IDs a run reads for selected notes of a type with a link ID field:
    the type's own, and for a type of the sentence role the vocab type its arrays link, whose
    fields hold the placeholders. Raises LayoutError as note_roles.vocab_type_of does."""
    names = [note_type_name]
    if is_sentence_type(config, note_type_name):
        vocab_type = vocab_type_of(config, note_type_name)
        if vocab_type != note_type_name:
            names.append(vocab_type)
    return names


def fill_preflight_error(
    col: Collection, config: Mapping[str, Any], nids: Sequence[NoteId]
) -> Optional[str]:
    """Why the run over these notes cannot start, for the user; None when it can. Checked before
    the run starts, and again by the run, which a script starts without the menu.

    It cannot when no selected note is of a type whose settings name a link ID field; when the
    settings give such a type no usable layout (note_roles.layout_error: a broken pair, or a
    `sentence_id_field` where it would be taken for the wrong field); or when they name a type
    or a field the collection lacks, or a field no search can name, which every op reading the
    links refuses (link_lookup.link_index)."""
    names = [name for name in _selected_type_counts(col, nids) if link_id_field(config, name)]
    if not names:
        return (
            f"{LABEL}: no selected note is of a note type whose settings name a link ID field"
            " (sentence_id_field on the sentence note type, new_note_id_field on the vocab note"
            " type), so no selected note has a field to fill."
        )
    for name in names:
        error = layout_error(config, name)
        if error:
            return error
        try:
            for read in _types_read(config, name):
                link_index(config, read, col=col)
        except LayoutError as e:
            return str(e)
    return None


@dataclass
class TypeFill:
    """What the run writes into the link ID fields of one type's selected notes."""

    name: str
    field: str
    selected: int
    ids: IdFills


@dataclass
class PlaceholderFill:
    """A legacy placeholder in a selected note's word array, and the notes whose link ID field
    holds it: one, and the word is linked to it; none, and it is matched again; several, and it
    is left."""

    note_id: int
    # Its word's dictionary form, to find it by in the report
    form: str
    placeholder: int
    holders: list[int]


@dataclass
class FillPlan:
    selected: int
    # The types with a link ID field, by name
    types: list[TypeFill]
    # Per note type whose settings name no link ID field, how many of its notes were selected
    skipped: dict[str, int]
    # Per selected note whose word array changes: its word list field, and the array's new text
    arrays: dict[int, tuple[str, str]]
    placeholders: list[PlaceholderFill]

    def writes(self) -> dict[int, dict[str, str]]:
        """Per note the run writes, each field it writes and the text written there."""
        out: dict[int, dict[str, str]] = {}
        for fill in self.types:
            for note_id, given in fill.ids.writes.items():
                out.setdefault(note_id, {})[fill.field] = str(given)
        for note_id, (field_name, text) in self.arrays.items():
            out.setdefault(note_id, {})[field_name] = text
        return out


def _resolve_placeholders(
    arr: list, holders: Mapping[str, list[int]]
) -> tuple[int, list[tuple[str, int, list[int]]]]:
    """Swap each legacy placeholder in `arr` for the link ID of the one note whose link ID
    field holds it (`holders`: link_ids.placeholder_holders), or `["match"]` when none does;
    one several notes hold stays (match_targets.resolve_placeholder_ids). Returns how many
    words changed, and each placeholder as (its word's dict form, the placeholder, its
    holders)."""
    found = []
    for _, elem in match_flags.iter_words(arr):
        held = match_flags.matched_note_id(elem)
        if held is not None and held < 0:
            found.append((elem[2], held, holders.get(str(held), [])))
    if not found:
        return 0, []
    changed = match_targets.resolve_placeholder_ids(arr, lambda held: holders.get(str(held), []))
    return changed, found


def plan_fill(
    config: Mapping[str, Any],
    notes_by_type: Mapping[str, Sequence[NoteInHand]],
    indexes: Mapping[str, LinkIndex],
) -> FillPlan:
    """What a run writes into the selected notes, `notes_by_type` (by their type's name).
    `indexes`: the link IDs of every type `_types_read` names for them, read with them, each
    used by this plan alone (link_ids.plan_id_fills)."""
    types: list[TypeFill] = []
    skipped: dict[str, int] = {}
    arrays: dict[int, tuple[str, str]] = {}
    placeholders: list[PlaceholderFill] = []
    for name in sorted(notes_by_type):
        notes = notes_by_type[name]
        id_field = link_id_field(config, name)
        if id_field is None:
            skipped[name] = len(notes)
            continue
        values = {note.id: note[id_field] for note in notes}
        types.append(TypeFill(name, id_field, len(notes), plan_id_fills(indexes[name], values)))
        if not is_sentence_type(config, name):
            continue
        # As the migration's planner reads them: by the whole text of the vocab type's fields,
        # before this run gives those notes their note ids, which are their link IDs already
        holders = placeholder_holders(indexes[vocab_type_of(config, name)])
        word_list_field = config[name]["word_list_field"]
        for note in notes:
            if word_list_field not in note:
                continue
            # A field that holds no array is never written
            arr = match_flags.decode_word_array(note[word_list_field])
            if arr is None:
                continue
            changed, found = _resolve_placeholders(arr, holders)
            placeholders += [PlaceholderFill(note.id, *placeholder) for placeholder in found]
            if changed:
                arrays[note.id] = (word_list_field, match_flags.format_word_array(arr))
    return FillPlan(
        selected=sum(len(notes) for notes in notes_by_type.values()),
        types=types,
        skipped=skipped,
        arrays=arrays,
        placeholders=placeholders,
    )


def _duplicate_line(duplicate: DuplicateFill) -> str:
    keeper = duplicate.keeper
    why = "its own note id" if keeper == duplicate.held else "the oldest holder"
    parts = [f"{keeper} keeps it ({why})"]
    for holder in duplicate.holders[1:]:
        given = duplicate.given.get(holder)
        if given is None:
            parts.append(f"{holder} not selected, still holds it")
        elif given == holder:
            parts.append(f"{holder} -> {given} (its own note id)")
        elif holder in duplicate.taken_by:
            parts.append(
                f"{holder} -> {given} (generated: its own note id is the ID of note"
                f" {duplicate.taken_by[holder]})"
            )
        else:
            parts.append(f"{holder} -> {given} (generated)")
    return f"  {duplicate.held}: " + "; ".join(parts)


def report_text(plan: FillPlan) -> str:
    """The run's report: counts per kind of write, then per type the IDs several notes held
    (each holder, the keeper and what each other one gets) and the fields holding text that
    is no ID, the placeholders in arrays, and the types left out."""
    writes = plan.writes()
    duplicates = [duplicate for fill in plan.types for duplicate in fill.ids.duplicates]
    given = [(holder, new) for duplicate in duplicates for holder, new in duplicate.given.items()]
    counts = [
        ("notes selected", plan.selected),
        ("notes written", len(writes)),
        ("empty ID fields given their note id", sum(len(f.ids.empty) for f in plan.types)),
        (
            "ID fields holding something but an ID given their note id",
            sum(len(f.ids.invalid) for f in plan.types),
        ),
        ("IDs more than one note held", len(duplicates)),
        ("holders given their own note id", sum(1 for h, new in given if new == h)),
        ("holders given a generated ID", sum(1 for h, new in given if new != h)),
        (
            "holders not selected, left holding an ID another note keeps",
            sum(len(duplicate.left) for duplicate in duplicates),
        ),
        (
            "legacy placeholders linked to the note holding them",
            sum(1 for p in plan.placeholders if len(p.holders) == 1),
        ),
        (
            "legacy placeholders no note holds, set to match",
            sum(1 for p in plan.placeholders if not p.holders),
        ),
        (
            "legacy placeholders several notes hold, left",
            sum(1 for p in plan.placeholders if len(p.holders) > 1),
        ),
        ("notes of a note type with no link ID field, left out", sum(plan.skipped.values())),
    ]
    lines = ["Counts", *(f"  {name}: {value}" for name, value in counts)]
    if not writes:
        lines += ["", "Nothing to write: no selected note was changed."]
    for fill in plan.types:
        lines += [
            "",
            f'Note type "{fill.name}", link ID field "{fill.field}", notes selected:'
            f" {fill.selected}",
        ]
        if fill.ids.duplicates:
            lines.append(
                "  IDs more than one note held: each holder, the one keeping it first, and the"
                " ID each other one gets"
            )
            lines += ["  " + _duplicate_line(duplicate) for duplicate in fill.ids.duplicates]
        if fill.ids.invalid:
            lines.append("  Fields holding something but an ID, given their note id")
            lines += [f"    {note_id}: {value!r}" for note_id, value in fill.ids.invalid.items()]
    if plan.placeholders:
        lines += ["", "Legacy placeholders in word arrays (note: word, placeholder -> link)"]
        for found in plan.placeholders:
            if len(found.holders) == 1:
                outcome = str(found.holders[0])
            elif not found.holders:
                outcome = "match, no note holds it"
            else:
                held_by = ", ".join(str(note_id) for note_id in found.holders)
                outcome = f"left, held by notes {held_by}"
            lines.append(f"  {found.note_id}: {found.form}, {found.placeholder} -> {outcome}")
    if plan.skipped:
        lines += ["", "Left out, their note type's settings naming no link ID field"]
        lines += [f'  "{name}", notes selected: {count}' for name, count in plan.skipped.items()]
    return "\n".join(lines) + "\n"


def _by_type(notes: Sequence[Note]) -> dict[str, list[Note]]:
    by_type: dict[str, list[Note]] = {}
    for note in notes:
        notetype = note.note_type()
        name = notetype["name"] if notetype is not None else str(note.mid)
        by_type.setdefault(name, []).append(note)
    return by_type


class LinkIdFill:
    """One run of the op: the plan and the writes the bulk op and the per-note op share.
    `fill_spec` makes one per run, so each run writes a report of its own (`report_path`,
    fixed when the run is made so that the end message can name it)."""

    def __init__(self, report_dir: Optional[str] = None) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.report_path = os.path.join(
            report_dir or ADDON_USER_FILES_DIR, f"fill_link_ids_{stamp}.txt"
        )
        self.plan: Optional[FillPlan] = None
        # Per note the run writes, each field and the text written there
        self._writes: dict[int, dict[str, str]] = {}

    def spec(self) -> NotesRunSpec:
        report = html.escape(self.report_path)
        return NotesRunSpec(
            done_text=f"Filled link IDs (report: {report})",
            title=f"Sync op: {MESSAGE}",
            bulk_op=self.bulk_op,
        )

    async def bulk_op(
        self,
        col: Collection,
        notes: Sequence[Note],
        edited_nids: list[NoteId],
        progress_updater: AsyncTaskProgressUpdater,
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
    ) -> BulkOpResult:
        """Read, plan and write the report, then make the writes note by note. Raises
        FillLinkIdsError, failing the run before it writes anything, when the settings or the
        collection do not allow it: a script starts the run without the menu's check."""
        config = mw.addonManager.getConfig(__name__) or {}
        error = fill_preflight_error(col, config, [note.id for note in notes])
        if error:
            raise FillLinkIdsError(error)
        self._show(f"Reading the link IDs of {len(notes)} notes", len(notes))
        notes_by_type = _by_type(notes)
        indexes: dict[str, LinkIndex] = {}
        for name in notes_by_type:
            if link_id_field(config, name) is None:
                continue
            for read in _types_read(config, name):
                if read not in indexes:
                    indexes[read] = link_index(config, read, col=col)
        plan = self.plan = plan_fill(config, notes_by_type, indexes)
        self._writes = plan.writes()
        self._write_report(plan)
        # The notes whose arrays change first: a cancel between notes saves those done, and a
        # field that held a legacy placeholder, written before an array still holding it, would
        # leave a rerun no note to resolve the placeholder to
        written = sorted(
            (note for note in notes if note.id in self._writes),
            key=lambda note: note.id not in plan.arrays,
        )
        # A cancel pressed while planning ends the run here, its report written
        if mw.progress.want_cancel():
            written = []
        return await bulk_notes_op(
            MESSAGE,
            config,
            self.note_op,
            col,
            written,
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

    def _write_report(self, plan: FillPlan) -> None:
        header = (
            f"{LABEL}, {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            "Notes are named by note id. Every write is to a selected note, made when the run"
            " ends, as one undo step.\n\n"
        )
        os.makedirs(os.path.dirname(self.report_path), exist_ok=True)
        with open(self.report_path, "w", encoding="utf-8") as file:
            file.write(header + report_text(plan))
        logger.info("%s: the report is %s", LABEL, self.report_path)

    def note_op(
        self,
        config: dict,
        note: Note,
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
    ) -> bool:
        """Make the plan's writes to one selected note; writes nothing to the collection."""
        for field_name, text in self._writes[note.id].items():
            note[field_name] = text
        notes_to_update_dict[note.id] = note
        return True


def fill_spec() -> NotesRunSpec:
    """The op's run, as the menu starts it and as a script does (`NotesRunSpec`): a new
    `LinkIdFill` each time, with its own plan and report."""
    return LinkIdFill().spec()


def fill_link_ids_from_selected(
    nids: Sequence[NoteId], parent: Any, chain: Optional[ChainStep] = None
):
    """Fill the selected notes' link IDs. The pre-flight runs here, where its message can be
    shown before any progress dialog, and fails the chain step when the run cannot start (the
    run checks again, for a script)."""
    config = mw.addonManager.getConfig(__name__) or {}
    error = fill_preflight_error(mw.col, config, nids)
    if error:
        logger.error("%s: %s", LABEL, error)
        showWarning(error, parent=parent, title=LABEL)
        fail_step(chain, error)
        return None
    spec = fill_spec()
    return selected_notes_op(
        spec.done_text,
        spec.bulk_op,
        nids,
        parent,
        AsyncTaskProgressUpdater(title=spec.title),
        chain=chain,
    )
