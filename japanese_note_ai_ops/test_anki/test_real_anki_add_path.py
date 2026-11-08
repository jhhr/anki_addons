"""A run's note adding in a running Anki, with CopyAnywhere's add hook really attached.

The cleanup adds each new note with `col.add_note`, which fires `note_will_be_added`, so every
other addon's add hook runs inside the run's add phase, and then merges the add into the run's
one undo entry. Here a stand-in op prepares one new note as the match op does, link ID and
all, and `selected_notes_op` runs it through a real `CollectionOp`. The run records its
notes (config `capture_notes`) into a store in the test's directory, so what the add cost, what
the merge cost and what the undo queue did are read back from it.

A CopyAnywhere definition that reaches past the note being added writes under an undo entry of
its own, in the middle of the run's; whether the run's merge then still finds its entry is
exactly what the add and merge being recorded apart is for.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

import aqt.operations
import pytest

from anki_shared.testing import real_anki
from anki_shared.word_array.field_text import decode_word_array, format_word_array
from japanese_note_ai_ops.async_api_ops import base_ops, capture

from .conftest import COPIED, FIELDS, NOTETYPE

WAIT = 30000
# The link ID the match op generates for a new note, which it holds from before the add
LINK_ID = "4321098765432109"
UNDO_LABEL = "Adding one note for 1 notes."


def definition(name: str, **extra: Any) -> dict:
    """A format-1 CopyAnywhere definition over NOTETYPE that runs when a note is added."""
    base = {
        "guid": f"def-{name}",
        "definition_name": name,
        "copy_on_sync": False,
        "copy_on_add": True,
        "copy_on_review": False,
        "copy_into_note_types": f'"{NOTETYPE}"',
        "field_to_file_defs": [],
        "field_to_variable_defs": [],
        "card_actions": [],
        "add_tags": "",
        "remove_tags": "",
        "only_copy_into_decks": None,
        "include_subdecks": False,
        "copy_condition_query": None,
        "condition_only_on_sync": False,
        "copy_from_cards_query": None,
        "sort_by_field": None,
        "select_card_by": "None",
        "select_card_count": "1",
        "select_card_separator": ", ",
        "show_error_if_none_found": False,
        "run_also_if_no_sources_found": False,
        "copy_mode": "Within note",
        "across_mode_direction": None,
    }
    base.update(extra)
    return base


def field_to_field(into: str, text: str) -> dict:
    return {
        "guid": f"ftf-{into}",
        "copy_into_note_field": into,
        "copy_from_text": text,
        "copy_as_code": "",
        "use_code": False,
        "copy_if_empty": False,
        "copy_on_unfocus_when_edit": False,
        "copy_on_unfocus_when_add": False,
        "copy_on_unfocus_trigger_field": "",
        "process_chain": None,
    }


def within_the_new_note() -> dict:
    word = FIELDS["word_kanjified_field"]
    return definition("within", field_to_field_defs=[field_to_field(COPIED, "{{%s}}!" % word)])


def into_another_note(query: str) -> dict:
    """Source to destinations: the new note is the source, the query's notes are written."""
    word = FIELDS["word_kanjified_field"]
    return definition(
        "into-other",
        copy_mode="Across notes",
        across_mode_direction="Source to destinations",
        copy_from_cards_query=query,
        select_card_count="0",
        field_to_field_defs=[field_to_field(COPIED, "from {{%s}}" % word)],
    )


def own_id_into_the_id_field() -> dict:
    """What a definition keeping a note's own id in that field for its card template does. On
    add the note has no id yet, and `{{__Note_ID}}` is 0."""
    into = FIELDS["new_note_id_field"]
    return definition("own-id", field_to_field_defs=[field_to_field(into, "{{__Note_ID}}")])


def one_new_note(word: str):
    """A bulk op that prepares one new note for the cleanup to add, as the match op does."""

    async def bulk_adds_one(
        col, notes, edited_nids, progress_updater, notes_to_add_dict, notes_to_update_dict
    ):
        pos = col.add_custom_undo_entry(UNDO_LABEL)
        model = col.models.by_name(NOTETYPE)
        note = col.new_note(model)
        note[FIELDS["word_kanjified_field"]] = word
        note[FIELDS["word_sort_field"]] = word
        note[FIELDS["new_note_id_field"]] = LINK_ID
        return pos, {word: [note]}, notes_to_update_dict, []

    return bulk_adds_one


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "capture.sqlite3"
    assert capture.install(str(path), keep_days=None)
    yield path
    capture.shutdown(timeout=10.0)


@pytest.fixture
def shown(monkeypatch):
    """What the run's end would show: its tooltip, and an exception aqt would put in a box."""
    seen: dict[str, list] = {"tooltips": [], "exceptions": []}
    monkeypatch.setattr(base_ops, "tooltip", lambda *a, **k: seen["tooltips"].append(a))
    monkeypatch.setattr(base_ops, "showWarning", lambda *a, **k: seen["tooltips"].append(a))
    monkeypatch.setattr(
        aqt.operations, "show_exception", lambda **k: seen["exceptions"].append(k["exception"])
    )
    return seen


def run(anki_session, mw, bulk_op, nids, new_notes_op=None) -> None:
    updater = base_ops.AsyncTaskProgressUpdater(title="Async AI op: Adding")
    base_ops.selected_notes_op("Added one note", bulk_op, nids, mw, updater, new_notes_op)
    anki_session.qtbot.waitUntil(lambda: mw._background_op_count == 0, timeout=WAIT)


def events(store, kind: str) -> list[dict]:
    capture_store = capture.current_store()
    assert capture_store is not None and capture_store.flush(10.0)
    with closing(sqlite3.connect(str(store))) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE kind = ? ORDER BY event_id", (kind,)
        ).fetchall()
    return [json.loads(payload) for (payload,) in rows]


def test_the_add_hook_runs_in_the_add_phase_and_the_run_stays_one_undo_step(
    anki_session, real_mw, config, copy_anywhere_config, store, shown
):
    from copy_anywhere.hooks import note_hooks

    config()
    copy_anywhere_config(copy_definitions=[within_the_new_note()])
    note_hooks.init_note_hooks()
    trigger = real_anki.add_note(real_mw.col, NOTETYPE, {FIELDS["word_sort_field"]: "店"})

    run(anki_session, real_mw, one_new_note("猫"), [trigger.id])

    assert shown["exceptions"] == []
    [added_id] = [nid for nid in real_mw.col.find_notes(f'"note:{NOTETYPE}"') if nid != trigger.id]
    # The hook filled the field before the insert: this is col.add_note firing it
    assert real_mw.col.get_note(added_id)[COPIED] == "猫!"
    # The add, the hook's field and the cleanup are one undo step, the run's own
    assert real_mw.col.undo_status().undo == UNDO_LABEL
    [add] = events(store, "note.add")
    assert add["placeholder"] == int(LINK_ID)
    assert add["add_error"] is None and add["merge_error"] is None
    assert add["seconds"] > 0 and add["merge_seconds"] is not None
    [loop] = [p for p in events(store, "phase") if p["label"] == "cleanup: add_note loop"]
    assert loop["added"] == 1 and loop["add_seconds"] >= add["seconds"] - 1e-3


def test_a_hook_writing_the_id_field_leaves_the_new_note_its_link_id(
    anki_session, real_mw, config, copy_anywhere_config, store, shown
):
    """The field holds the link ID the arrays link the new note by, from before the add, and
    nothing rewrites them after it. The hook's 0 there, which the add writes to the collection,
    made the note's link ID its note id, which no array links: the ID is put back and the note
    saved again holding it (base_ops.link_id_kept), within the run's one undo step."""
    from copy_anywhere.hooks import note_hooks

    config()
    # A sentence linking the word to the note about to be added, as the cleanup saves it
    # before the adding
    array = format_word_array([["猫", "noun", "猫", "ねこ", [int(LINK_ID), 4], []]])
    sentence_fields = {FIELDS["word_sort_field"]: "店", FIELDS["word_list_field"]: array}
    trigger = real_anki.add_note(real_mw.col, NOTETYPE, sentence_fields)
    copy_anywhere_config(copy_definitions=[own_id_into_the_id_field()])
    note_hooks.init_note_hooks()

    run(anki_session, real_mw, one_new_note("猫"), [trigger.id])

    assert shown["exceptions"] == []
    [added_id] = [nid for nid in real_mw.col.find_notes(f'"note:{NOTETYPE}"') if nid != trigger.id]
    linked = decode_word_array(real_mw.col.get_note(trigger.id)[FIELDS["word_list_field"]])
    assert linked is not None and linked[0][4] == [int(LINK_ID), 4]
    assert real_mw.col.get_note(added_id)[FIELDS["new_note_id_field"]] == LINK_ID
    # The hook did write over it: the note was saved again
    [kept] = [p for p in events(store, "phase") if p["label"] == "cleanup: link IDs kept"]
    assert kept["notes"] == 1
    # The capture names the link ID the note went in with, not what the hook wrote
    [add] = events(store, "note.add")
    assert add["placeholder"] == int(LINK_ID)
    assert events(store, "note.added") == [{"placeholder": int(LINK_ID), "note_id": added_id}]
    assert real_mw.col.undo_status().undo == UNDO_LABEL


def test_a_hook_writing_another_note_under_its_own_undo_entry_is_seen_in_the_merge(
    anki_session, real_mw, config, copy_anywhere_config, store, shown
):
    from copy_anywhere.hooks import note_hooks

    config()
    other = real_anki.add_note(real_mw.col, NOTETYPE, {FIELDS["word_sort_field"]: "犬"})
    copy_anywhere_config(
        copy_definitions=[into_another_note(f'"{FIELDS["word_sort_field"]}:犬"')]
    )
    note_hooks.init_note_hooks()
    trigger = real_anki.add_note(real_mw.col, NOTETYPE, {FIELDS["word_sort_field"]: "店"})

    run(anki_session, real_mw, one_new_note("猫"), [trigger.id])

    # The hook reached the other note
    assert real_mw.col.get_note(other.id)[COPIED] == "from 猫"
    [add] = events(store, "note.add")
    # One add under a hook's own undo entry does not break the merge: the hook's entry folds
    # into the run's, which is still the one step to undo. Whatever does break it (issue #11's
    # round 13) is not one add; the recorded undo queue around each add is how to find it
    assert (add["add_error"], add["merge_error"]) == (None, None)
    assert add["undo_after"] == {"undo": UNDO_LABEL, "last_step": add["undo_before"]["last_step"]}
    assert shown["exceptions"] == []
    assert real_mw.col.undo_status().undo == UNDO_LABEL
