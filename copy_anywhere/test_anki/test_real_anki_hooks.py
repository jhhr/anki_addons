"""Characterization tests for the parts of CopyAnywhere that need Anki actually running.

Everything the other fourteen files pin, they pin by calling a handler directly against a
real collection behind a stubbed `mw`. That leaves four things unproven, and they are the
four things here:

* **Nothing showed that the handlers are attached at all.** `init_note_hooks()` is the only
  place the addon meets Anki's hook objects, and no stub-mode test calls it. Here the hooks
  are counted before and after, and then fired the way Anki fires them -- `col.add_note()`
  really running `note_will_be_added`, `reviewer_did_answer_card` really reaching the review
  handler -- so a definition that stopped being registered would fail a test.

* **`copy_fields()` above the `op`.** The stub suite drives the `op` closure inline and
  deliberately never calls `on_success` / `on_failure`, because both build Qt widgets. Here
  the real `CollectionOp` runs on a real background thread and both callbacks fire, so the
  tooltip, the `ScrollMessageBox` and the progress title are observable.

* **The sync hooks**, which are `gui_hooks` and so do not exist outside a running Anki.

* **The config**, which comes from the real `AddonManager` reading config.json and meta.json
  off disk rather than from a dict handed to a stub.

Two facts worth knowing before reading the assertions:

1. **A sync run only looks at flagged cards.** `copy_fields_in_background` selects notes with
   `json_extract(c.data, '$.cd', '$.fc') = 0` when `is_sync`, so a freshly added note is
   invisible to a sync definition until something writes `{"fc":0}` into its card. That is
   what `flagged_note()` does, and without it every sync assertion here would pass
   vacuously with an empty result.

2. **`tooltip` and `ScrollMessageBox` are replaced with recorders.** Both are real Qt
   windows with real timers; what is under test is whether they are built, with what text
   and for which parent, not whether Qt can draw them.
"""

import sys
from typing import Any, Optional

import pytest
from anki.hooks import note_will_be_added
from aqt.editor import EditorMode
from aqt.gui_hooks import (
    editor_did_load_note,
    editor_did_unfocus_field,
    reviewer_did_answer_card,
    sync_did_finish,
    sync_will_start,
)

from anki_shared.testing import real_anki
from .conftest import VOCAB

WAIT = 15000


# Test data ------------------------------------------------------------------------------


def field_to_field(into: str = "Meaning", text: str = "{{Word}}!", **extra: Any) -> dict:
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
        **extra,
    }


def within_note(
    name: str = "d1",
    field_to_field_defs: Optional[list[dict]] = None,
    note_types: str = f'"{VOCAB}"',
    **extra: Any,
) -> dict:
    """A within-note definition: the smallest one that writes a field.

    The keys are spelled out rather than imported from the backend suite's `definitions.py`,
    because that module does `from conftest import VOCAB` -- a top-level import that only
    resolves while that suite's directory is the one pytest put on `sys.path`.
    """
    definition = {
        "guid": f"def-{name}",
        "definition_name": name,
        "copy_on_sync": False,
        "copy_on_add": False,
        "copy_on_review": False,
        "copy_into_note_types": note_types,
        "field_to_field_defs": field_to_field_defs or [field_to_field()],
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
    definition.update(extra)
    return definition


def flagged_note(mw, word: str = "kitsune"):
    """A note whose cards carry `{"fc":0}` -- the only kind a sync run can see."""
    note = real_anki.add_note(mw.col, VOCAB, {"Word": word})
    for card in note.cards():
        real_anki.set_custom_data(mw.col, card.id, '{"fc":0}')
    return note


# Recorders ------------------------------------------------------------------------------


@pytest.fixture
def dialogs(monkeypatch):
    """Records the two windows `on_success` / `on_failure` build, instead of showing them."""
    from copy_anywhere.logic import copy_fields as copy_fields_module

    recorded: dict[str, list] = {"tooltips": [], "boxes": []}
    monkeypatch.setattr(
        copy_fields_module,
        "tooltip",
        lambda text, **kwargs: recorded["tooltips"].append((text, kwargs)),
    )
    monkeypatch.setattr(
        copy_fields_module,
        "ScrollMessageBox",
        lambda messages, title, parent=None, **kwargs: recorded["boxes"].append(
            (list(messages), title, parent)
        ),
    )
    return recorded


@pytest.fixture
def sync_tooltips(monkeypatch):
    """`sync_hook`'s own tooltip -- a different import than the one `copy_fields` uses."""
    from copy_anywhere.hooks import sync_hook

    recorded: list = []
    monkeypatch.setattr(
        sync_hook, "tooltip", lambda text, **kwargs: recorded.append((text, kwargs))
    )
    return recorded


class TestTheHooksAreActuallyRegistered:
    """`init_note_hooks()` and `init_sync_hook()`, and Anki firing what they attached.

    Every other file in this suite calls the handlers itself. If `init_note_hooks()` stopped
    attaching one of them -- or attached it to the wrong hook -- nothing else in the suite
    would notice.
    """

    def test_init_note_hooks_attaches_exactly_one_handler_to_each_of_the_four_hooks(
        self, real_mw
    ):
        from copy_anywhere.hooks import note_hooks

        before = [
            hook.count()
            for hook in (
                note_will_be_added,
                editor_did_load_note,
                editor_did_unfocus_field,
                reviewer_did_answer_card,
            )
        ]

        note_hooks.init_note_hooks()

        after = [
            hook.count()
            for hook in (
                note_will_be_added,
                editor_did_load_note,
                editor_did_unfocus_field,
                reviewer_did_answer_card,
            )
        ]
        # A delta rather than an absolute count: Anki itself has already registered two
        # handlers on editor_did_load_note by the time a profile is open.
        assert [now - then for now, then in zip(after, before)] == [1, 1, 1, 1]

    def test_init_note_hooks_wraps_editor_cleanup_only_once(self, real_mw):
        # `Editor.cleanup` is a class attribute, so a second call must not stack another
        # wrapper on it; the handler would still be correct, just run once per wrapper.
        from aqt.editor import Editor

        from copy_anywhere.hooks import note_hooks

        note_hooks.init_note_hooks()
        wrapped = Editor.cleanup
        note_hooks.init_note_hooks()
        assert Editor.cleanup is wrapped
        assert wrapped.copy_anywhere_wrapped is True

    def test_adding_a_note_through_the_collection_reaches_the_add_handler(
        self, real_mw, addon_config
    ):
        from copy_anywhere.hooks import note_hooks

        note_hooks.init_note_hooks()
        addon_config(copy_definitions=[within_note(copy_on_add=True)])

        note = real_anki.add_note(real_mw.col, VOCAB, {"Word": "neko"})

        # The handler mutates the note before the insert, so the value is in the database
        # without any later update: this is `col.add_note` firing `note_will_be_added`.
        assert real_mw.col.get_note(note.id)["Meaning"] == "neko!"

    def test_the_reviewer_hook_reaches_the_review_handler_and_flags_the_card(
        self, real_mw, addon_config
    ):
        from copy_anywhere.hooks import note_hooks

        note_hooks.init_note_hooks()
        addon_config(copy_definitions=[within_note(copy_on_review=True)])
        note = real_anki.add_note(real_mw.col, VOCAB, {"Word": "neko"})
        card = note.cards()[0]

        reviewer_did_answer_card(real_mw.reviewer, card, 3)

        assert real_mw.col.get_note(note.id)["Meaning"] == "neko!"
        # `fc` is 1 rather than -1 because no definition in this config asks for
        # copy_on_sync, so nothing is left for the sync run to redo.
        assert real_mw.col.get_card(card.id).custom_data == '{"fc":1}'

    def test_init_sync_hook_attaches_to_both_sync_hooks_and_firing_one_runs_the_definitions(
        self, anki_session, real_mw, addon_config, sync_tooltips
    ):
        from copy_anywhere.hooks import sync_hook

        addon_config(copy_definitions=[within_note(copy_on_sync=True)])
        note = flagged_note(real_mw)
        before = (sync_will_start.count(), sync_did_finish.count())

        sync_hook.init_sync_hook()

        assert (sync_will_start.count(), sync_did_finish.count()) == (
            before[0] + 1,
            before[1] + 1,
        )
        sync_will_start()
        # The local half is a CollectionOp, so firing the hook only starts it; the write
        # lands from a background thread later. Waiting for the op to be *finished* rather
        # than only for the value matters: the write happens mid-op, and the rest of the op
        # still needs the collection this fixture is about to close.
        anki_session.qtbot.waitUntil(
            lambda: real_mw._background_op_count == 0, timeout=WAIT
        )
        assert real_mw.col.get_note(note.id)["Meaning"] == "kitsune!"


class TestTheConfigComesFromTheRealAddonManager:
    """`Config.load()` against `AddonManager.getConfig`, not a dict on a stub."""

    def test_a_definition_in_meta_json_is_picked_up_and_beats_the_shipped_default(
        self, real_mw, addon_config
    ):
        from copy_anywhere.configuration import Config

        addon_config(copy_definitions=[within_note("from-meta")], log_level="debug")

        config = Config()
        config.load()

        # `getConfig` re-reads both files on every call and merges meta.json over
        # config.json, so the user's value wins and no restart or cache flush is involved.
        assert [d["definition_name"] for d in config.copy_definitions] == ["from-meta"]
        assert config.log_level == "debug"


class TestTheCollectionOpPath:
    """`copy_fields()` above its `op`: the real `CollectionOp`, and both its callbacks."""

    def test_the_progress_title_reaches_the_real_progress_manager(
        self, anki_session, real_mw, addon_config, dialogs, monkeypatch
    ):
        from copy_anywhere.logic import copy_fields as copy_fields_module

        addon_config()
        note = real_anki.add_note(real_mw.col, VOCAB, {"Word": "inu"})
        titles: list[str] = []
        finishes: list[int] = []
        monkeypatch.setattr(
            type(real_mw.progress), "set_title", lambda self, title: titles.append(title)
        )
        original_finish = type(real_mw.progress).finish
        monkeypatch.setattr(
            type(real_mw.progress),
            "finish",
            lambda self: (finishes.append(1), original_finish(self))[1],
        )
        done: list[int] = []

        returned = copy_fields_module.copy_fields(
            copy_definitions=[within_note()],
            note_ids=[note.id],
            progress_title="Copying fields for local changes",
            on_done=lambda: done.append(1),
        )

        # With a real `mw`, `CollectionOp.run_in_background()` returns None, so
        # `copy_fields` hands back nothing at all -- the result is only reachable through
        # the callbacks.
        assert returned is None
        anki_session.qtbot.waitUntil(lambda: bool(done), timeout=WAIT)
        assert titles == ["Copying fields for local changes"]
        # Two finishes, not one: `taskman.with_progress` already finished the progress
        # before handing the future to `on_success`, which finishes it a second time. The
        # count is clamped at zero, so the extra call is harmless here -- but it is a real
        # extra call, and a nested op would see its own dialog closed early.
        assert finishes == [1, 1]
        # The dialog itself closes on a later turn of the event loop, not inside `finish()`.
        anki_session.qtbot.waitUntil(lambda: real_mw.progress.busy() == 0, timeout=WAIT)

    def test_a_run_that_copied_something_reports_it_as_a_tooltip(
        self, anki_session, real_mw, addon_config, dialogs
    ):
        from copy_anywhere.logic.copy_fields import copy_fields

        addon_config()
        note = real_anki.add_note(real_mw.col, VOCAB, {"Word": "inu"})
        done: list[int] = []

        copy_fields(
            copy_definitions=[within_note()],
            note_ids=[note.id],
            on_done=lambda: done.append(1),
        )
        anki_session.qtbot.waitUntil(lambda: bool(done), timeout=WAIT)

        assert real_mw.col.get_note(note.id)["Meaning"] == "inu!"
        (text, kwargs) = dialogs["tooltips"][0]
        # One definition gets "Finished in " rather than a total time -- the branch reads
        # `len(copy_definitions) > 1`, so the single-definition text ends up with a dangling
        # "Finished in " and no duration at all.
        assert text.startswith("Finished in <br>")
        assert "1 destinations" in text
        # `parent` is whatever the caller passed, and every hook-side caller passes nothing,
        # so the tooltip is parented on `aqt.mw.app.activeWindow()` by `aqt.utils.tooltip`.
        assert kwargs == {"parent": None, "period": 6000, "y_offset": 100}
        assert dialogs["boxes"] == []

    def test_a_run_with_a_logged_message_opens_the_debug_dialog_and_no_tooltip(
        self, anki_session, real_mw, addon_config, dialogs
    ):
        from copy_anywhere.logic.copy_fields import copy_fields

        addon_config()
        note = real_anki.add_note(real_mw.col, VOCAB, {"Word": "inu"})
        done: list[int] = []

        copy_fields(
            copy_definitions=[within_note(field_to_field_defs=[field_to_field(into="Nope")])],
            note_ids=[note.id],
            on_done=lambda: done.append(1),
        )
        anki_session.qtbot.waitUntil(lambda: bool(done), timeout=WAIT)

        (messages, title, parent) = dialogs["boxes"][0]
        assert title == "Copy fields debug Messages"
        assert parent is None
        assert "Field 'Nope' not found in note" in messages[0]
        # The box is driven by "was anything logged", not by "did anything fail", and the
        # default log level is `error` -- so a run that logs one error opens a window even
        # though the copy itself was skipped quietly.
        assert dialogs["tooltips"] == []

    def test_a_sync_run_reports_through_update_sync_result_and_opens_no_dialog(
        self, anki_session, real_mw, addon_config, dialogs
    ):
        from copy_anywhere.logic.copy_fields import copy_fields

        addon_config()
        flagged_note(real_mw)
        results: list[tuple] = []
        done: list[int] = []

        copy_fields(
            copy_definitions=[within_note(copy_on_sync=True)],
            update_sync_result=lambda text, count: results.append((text, count)),
            on_done=lambda: done.append(1),
        )
        anki_session.qtbot.waitUntil(lambda: bool(done), timeout=WAIT)

        # `is_sync` is "was an update_sync_result given", and it redirects the result away
        # from the tooltip and suppresses the debug window even when there is something to
        # show -- a sync is not a moment to open windows in front of the user.
        assert [count for _, count in results] == [1]
        assert dialogs["tooltips"] == []
        assert dialogs["boxes"] == []

    def test_on_failure_finishes_the_progress_and_re_raises_into_ankis_error_handler(
        self, anki_session, real_mw, addon_config, dialogs, monkeypatch
    ):
        from copy_anywhere.logic.copy_fields import copy_fields

        addon_config()
        real_anki.add_note(real_mw.col, VOCAB, {"Word": "inu"})
        raised: list[BaseException] = []
        done: list[int] = []
        # The re-raise happens inside a taskman closure on the main thread, so it leaves
        # `copy_fields` through the Qt event loop rather than through the caller. Standing
        # in for `sys.excepthook` is the only way to see it without pytest-qt failing the
        # test on Anki's behalf.
        monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: raised.append(value))

        # An empty `note_ids_per_definition` makes the op raise IndexError on its first
        # definition, which is the shortest route to a genuine failure inside the op.
        copy_fields(
            copy_definitions=[within_note()],
            note_ids_per_definition=[],
            on_done=lambda: done.append(1),
        )
        anki_session.qtbot.waitUntil(lambda: bool(done), timeout=WAIT)
        anki_session.qtbot.waitUntil(lambda: bool(raised), timeout=WAIT)

        assert isinstance(raised[0], IndexError)
        # `on_failure` finishes the progress before it re-raises, so the dialog does not
        # outlive the failed op -- though the window itself closes a turn of the event loop
        # later, which is why this waits rather than reading `busy()` straight away.
        anki_session.qtbot.waitUntil(lambda: real_mw.progress.busy() == 0, timeout=WAIT)
        # `on_done` runs before the re-raise, so a caller's cleanup is not skipped by the
        # failure -- but note the window title here is "Copy Fields", capitalised
        # differently from the identical window `on_success` opens.
        (messages, title, _) = dialogs["boxes"][0]
        assert title == "Copy Fields debug Messages"
        assert "Copying failed: list index out of range" in messages[0]


class TestTheSyncHooks:
    """`local_changes_copy_definitions` / `remote_changes_copy_definitions` and `SyncResult`.

    Both are called through the `SyncResult` that `init_sync_hook()` closes over, so the one
    object carries state from the start of a sync to the end of it. The functions are called
    directly here; that they are attached to `sync_will_start` / `sync_did_finish` is pinned
    in `TestTheHooksAreActuallyRegistered`.
    """

    def test_the_local_and_remote_runs_accumulate_into_one_result_and_clear_it(
        self, anki_session, real_mw, addon_config, sync_tooltips
    ):
        from copy_anywhere.hooks import sync_hook

        addon_config(copy_definitions=[within_note("onsync", copy_on_sync=True)])
        local_note = flagged_note(real_mw, "kitsune")
        sync_result = sync_hook.SyncResult()

        sync_hook.local_changes_copy_definitions(sync_result)
        anki_session.qtbot.waitUntil(
            lambda: bool(sync_result.local_changes_text), timeout=WAIT
        )
        assert sync_result.definitions_count == 1
        assert real_mw.col.get_note(local_note.id)["Meaning"] == "kitsune!"

        # A note that arrived from the server: flagged, and not yet copied into.
        remote_note = flagged_note(real_mw, "tanuki")
        sync_hook.remote_changes_copy_definitions(sync_result)
        anki_session.qtbot.waitUntil(lambda: bool(sync_tooltips), timeout=WAIT)

        assert real_mw.col.get_note(remote_note.id)["Meaning"] == "tanuki!"
        (text, kwargs) = sync_tooltips[0]
        assert "<b>Local changes:</b>" in text and "<b>Remote changes:</b>" in text
        # The period grows with the *accumulated* count across both halves, one second per
        # definition run, and the tooltip is pushed up so it does not cover Anki's own
        # sync tooltip.
        assert kwargs["period"] == 5000 + 2 * 1000
        assert kwargs["y_offset"] == 200
        assert kwargs["parent"] is real_mw
        # Showing the tooltip is also what resets the result, so the next sync starts empty.
        assert sync_result.has_changes() is False
        assert sync_result.definitions_count == 0

    def test_without_copy_on_sync_definitions_the_local_side_starts_no_op(
        self, real_mw, addon_config, sync_tooltips, monkeypatch
    ):
        from copy_anywhere.hooks import sync_hook

        started: list[dict] = []
        monkeypatch.setattr(sync_hook, "copy_fields", lambda **kwargs: started.append(kwargs))
        addon_config(copy_definitions=[within_note("not-on-sync")])
        sync_result = sync_hook.SyncResult()
        sync_result.local_changes_text = "left over from an earlier sync"
        sync_result.incr_count(2)

        sync_hook.local_changes_copy_definitions(sync_result)

        assert started == []
        # The local side returns without touching the result at all, so a leftover survives
        # into the remote half rather than being cleared here.
        assert sync_result.local_changes_text == "left over from an earlier sync"
        assert sync_tooltips == []

    def test_without_copy_on_sync_definitions_the_remote_side_still_shows_the_tooltip(
        self, real_mw, addon_config, sync_tooltips, monkeypatch
    ):
        from copy_anywhere.hooks import sync_hook

        started: list[dict] = []
        monkeypatch.setattr(sync_hook, "copy_fields", lambda **kwargs: started.append(kwargs))
        addon_config(copy_definitions=[within_note("not-on-sync")])
        sync_result = sync_hook.SyncResult()
        sync_result.local_changes_text = "left over from an earlier sync"
        sync_result.incr_count(2)

        sync_hook.remote_changes_copy_definitions(sync_result)

        assert started == []
        # The remote side is the only one that reports, so its early return still has to go
        # through `show_result_tooltip` -- otherwise a local-only sync would report nothing.
        # It is shown synchronously here, without the 100ms delay the op path uses.
        (text, _) = sync_tooltips[0]
        assert text == "<b>Local changes:</b><br>left over from an earlier sync"
        assert sync_result.has_changes() is False


class TestARealEditor:
    """The editor half of the hooks, against an `Editor` Anki really built.

    `run_copy_fields_on_unfocus_field` reloads editors out of the module-level
    `editor_for_note_id` dict, which only `on_editor_did_load_note` fills -- and that fires
    from inside a JS callback after the editor's webview has loaded, so nothing short of a
    real editor proves the two halves meet.

    Only the Add-cards editor is exercised. The Browser one works the same way but crashed
    the interpreter on exit often enough to be not worth having.
    """

    def test_a_real_add_cards_editor_registers_itself_and_is_reloaded_after_a_copy(
        self, anki_session, real_mw, addon_config, monkeypatch
    ):
        import aqt

        from copy_anywhere.hooks import note_hooks

        note_hooks.init_note_hooks()
        addon_config(
            copy_definitions=[
                within_note(
                    field_to_field_defs=[
                        field_to_field(
                            copy_on_unfocus_when_add=True,
                            copy_on_unfocus_trigger_field="Word",
                        )
                    ]
                )
            ]
        )
        dialog = aqt.dialogs.open("AddCards", real_mw)
        anki_session.qtbot.waitUntil(
            lambda: note_hooks.editor_for_note_id[EditorMode.ADD_CARDS] is not None,
            timeout=WAIT,
        )

        (editor, note_id) = note_hooks.editor_for_note_id[EditorMode.ADD_CARDS]
        assert editor is dialog.editor
        # An unsaved note has id 0, which is what the unfocus handler matches on to find
        # the Add-cards editor, and what tells it to skip definitions that touch other notes.
        assert note_id == 0

        loaded: list = []
        monkeypatch.setattr(type(editor), "loadNote", lambda self, **kwargs: loaded.append(self))
        note = editor.note
        note["Word"] = "tori"

        changed = editor_did_unfocus_field(False, note, 0)

        assert note["Meaning"] == "tori!"
        # The handler's return value is recomputed from "did any field value change",
        # ignoring the `changed` it was passed, and the editor is reloaded exactly once
        # even though the dict holds three slots.
        assert changed is True
        assert loaded == [editor]

        # Closing an Add-cards dialog whose editor still holds a modified note raises a
        # "discard your input?" message box, and that box crashes the process on the way
        # out. Dropping the note first closes it silently.
        dialog.editor.note = None
        aqt.dialogs.markClosed("AddCards")
        dialog.close()

    def test_closing_a_real_add_cards_dialog_drops_its_editor_from_the_registry(
        self, anki_session, real_mw, monkeypatch
    ):
        # aqt has no hook for an editor closing; `init_note_hooks` wraps `Editor.cleanup`,
        # which `AddCards._close` calls. Without it the dialog's editor would stay in the dict
        # and be reloaded, webview gone, the next time a note with id 0 was unfocused.
        import aqt

        from copy_anywhere.hooks import note_hooks

        note_hooks.init_note_hooks()
        # An earlier test's dialog may have left its editor here, which would satisfy the
        # wait below before this dialog's editor has registered.
        monkeypatch.setitem(note_hooks.editor_for_note_id, EditorMode.ADD_CARDS, None)
        dialog = aqt.dialogs.open("AddCards", real_mw)
        anki_session.qtbot.waitUntil(
            lambda: note_hooks.editor_for_note_id[EditorMode.ADD_CARDS] is not None,
            timeout=WAIT,
        )
        assert note_hooks.editor_for_note_id[EditorMode.ADD_CARDS][0] is dialog.editor

        dialog.editor.note = None
        aqt.dialogs.markClosed("AddCards")
        dialog.close()

        anki_session.qtbot.waitUntil(
            lambda: note_hooks.editor_for_note_id[EditorMode.ADD_CARDS] is None,
            timeout=WAIT,
        )
