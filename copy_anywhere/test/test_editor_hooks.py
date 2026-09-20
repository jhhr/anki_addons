"""Characterization tests for the two editor hooks: `on_editor_did_load_note` and
`run_copy_fields_on_unfocus_field`.

They are one file because they are one mechanism. `editor_did_unfocus_field` hands the
handler `(changed, note, field_idx)` and no editor, so `run_copy_fields_on_unfocus_field`
cannot know which editor the user is typing in; `on_editor_did_load_note` exists purely to
fill a module-global `editor_for_note_id` that the unfocus handler then reads back to
decide whose `loadNote()` to call. The source calls this "a hack" in as many words.

**No running Anki.** Both handlers are plain functions. A real `aqt.editor.Editor` needs a
webview and a main window, but the handlers only ever read `editor.editorMode`,
`editor.note` (and `.note.id`), the Add dialog's deck chooser, and call
`editor.loadNoteKeepingFocus()`, so `FakeEditor` below supplies exactly those. `EditorMode` is a real enum -- it is the dict's key type and the
handler stores by it -- so it is imported for real.

**The global outlives everything but its editors.** `editor_for_note_id` is module state
that `on_editor_did_load_note` writes and only `on_editor_will_cleanup` -- run from a wrapped
`Editor.cleanup` -- erases from: an editor's entry goes when its window closes, and nothing
goes on profile switch. That is pinned here (`TestWhenEntriesAreDropped`), and it is also why
`_restore_editor_registry` below snapshots and restores the dict around every test: without
it these tests would leak editors into each other and into every later file in the suite.

**Three seams, all borrowed from the sibling hook files.** Copy definitions go in through
`mw.addonManager.configs["copy_anywhere"]["copy_definitions"]`, which the `col` fixture
refreshes per test; "the definition was filtered out" is only distinguishable from "the
definition ran and did nothing" by spying on `copy_for_single_trigger_note`; and, as in
`run_copy_fields_on_add`, the `Logger` the handler builds from `log_level` is caught by
replacing the name in the module (`hook_logger`). The modifies-other-notes branch does go through
`copy_fields()`, so its `CollectionOp` has to be driven inline, as in
`test_copy_fields_op.py`.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Optional

import pytest
from anki.notes import NoteId
from aqt import mw
from aqt.editor import EditorMode

import definitions as d
from anki_shared.testing import real_anki
from conftest import KANJI, VOCAB
from copy_anywhere.hooks import note_hooks
from copy_anywhere.hooks.note_hooks import (
    editor_for_note_id,
    on_editor_did_load_note,
    on_editor_will_cleanup,
    run_copy_fields_on_unfocus_field,
)
from copy_anywhere.logic import copy_fields as copy_fields_module

ADDON_TAG = "copy_anywhere"

# CA Vocab's field order, which is what `field_idx` indexes into.
WORD, READING, MEANING, FREQ, NOTE = 0, 1, 2, 3, 4


@pytest.fixture(autouse=True)
def _restore_editor_registry():
    """Snapshot and restore `note_hooks.editor_for_note_id` around every test.

    The dict is module-global and the addon never clears it, so an editor stored by one
    test would still be there for the next one -- and for every test in every file that
    runs after this one, since `run_copy_fields_on_unfocus_field` looks editors up by note
    id and note ids repeat across collections.
    """
    snapshot = dict(editor_for_note_id)
    try:
        yield
    finally:
        editor_for_note_id.clear()
        editor_for_note_id.update(snapshot)


class FakeEditor:
    """The whole surface the two handlers touch: `editorMode`, `note`,
    `loadNoteKeepingFocus()`.

    Plus, when `deck_id` is given, the Add dialog's deck chooser, which the unfocus handler
    reads through `parentWindow.deck_chooser.selected_deck_id` for a new note.
    """

    def __init__(self, mode: EditorMode, note=None, deck_id=None, current_field=None) -> None:
        self.editorMode = mode
        self.note = note
        self.currentField = current_field
        if deck_id is not None:
            self.parentWindow = SimpleNamespace(
                deck_chooser=SimpleNamespace(selected_deck_id=deck_id)
            )
        self.loads = 0
        self.load_args: list[tuple] = []

    def loadNote(self, *args, **kwargs) -> None:
        self.loads += 1
        self.load_args.append((args, kwargs))

    def loadNoteKeepingFocus(self) -> None:
        # What aqt's does, so a test sees the field index reach `loadNote()`
        self.loadNote(self.currentField)


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
    """Record what reached `copy_for_single_trigger_note`, then let it through."""
    calls: list[dict] = []
    original = note_hooks.copy_for_single_trigger_note

    def spy(**kwargs):
        into = kwargs.get("copied_into_notes")
        # Snapshotted, because the callee appends to the very list being recorded and the
        # question is what the handler handed over, not what came back in it.
        calls.append({**kwargs, "_into_at_call": None if into is None else list(into)})
        return original(**kwargs)

    monkeypatch.setattr(note_hooks, "copy_for_single_trigger_note", spy)

    class Calls:
        def names(self) -> list[str]:
            return [call["copy_definition"]["definition_name"] for call in calls]

        def field_onlys(self) -> list[Optional[str]]:
            return [call.get("field_only") for call in calls]

        def copied_into_notes_at_call(self) -> list:
            return [call["_into_at_call"] for call in calls]

        def collected_note_ids(self) -> list:
            return [
                None
                if call.get("copied_into_notes") is None
                else [note.id for note in call["copied_into_notes"]]
                for call in calls
            ]

    return Calls()


@pytest.fixture
def copies(monkeypatch):
    """Record `copy_fields()` calls and run the `CollectionOp` inline.

    `copy_fields` ends in `CollectionOp(...).run_in_background()`, which needs a taskman and
    `mw._increase_background_ops` that the stub `mw` does not have, so the op closure is
    captured and called directly against the collection -- the same stand-in
    `test_copy_fields_op.py` uses. The `success` / `failure` callbacks are recorded and
    never called: both build Qt widgets.
    """
    calls: list[dict] = []
    original = note_hooks.copy_fields
    captured: dict = {}

    class InlineCollectionOp:
        def __init__(self, parent, op):
            captured["parent"] = parent
            captured["op"] = op

        def success(self, callback):
            return self

        def failure(self, callback):
            return self

        def run_in_background(self, **kwargs):
            return captured["op"](mw.col)

    monkeypatch.setattr(copy_fields_module, "CollectionOp", InlineCollectionOp)

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(note_hooks, "copy_fields", spy)

    class Calls:
        def count(self) -> int:
            return len(calls)

        def names(self) -> list[list[str]]:
            return [
                [definition["definition_name"] for definition in call["copy_definitions"]]
                for call in calls
            ]

        def kwargs(self) -> dict:
            assert len(calls) == 1, f"expected exactly one copy_fields call, got {len(calls)}"
            return calls[0]

    return Calls()


# Definition builders ----------------------------------------------------------------------
#
# Every one of these carries the unfocus keys, since a definition without them is invisible
# to this handler however else it is configured.


def within(
    name="within",
    field="Note",
    value="{{Word}}",
    trigger="Word",
    on_edit=True,
    on_add=True,
    **extra,
):
    return d.within_note(
        definition_name=name,
        field_to_field_defs=[
            d.field_to_field(
                field,
                value,
                copy_on_unfocus_trigger_field=trigger,
                copy_on_unfocus_when_edit=on_edit,
                copy_on_unfocus_when_add=on_add,
            )
        ],
        **extra,
    )


def to_destinations(
    name="s2d",
    field="Note",
    value="copied",
    query="Word:inu",
    trigger="Word",
    on_edit=True,
    on_add=True,
    **extra,
):
    """Across notes, trigger note as source: the query picks the notes written into."""
    return d.source_to_destinations(
        definition_name=name,
        copy_from_cards_query=query,
        # All matching cards rather than one picked out of the result: CA Vocab has two
        # templates, so a count of 1 makes the destination a coin toss.
        select_card_count="0",
        field_to_field_defs=[
            d.field_to_field(
                field,
                value,
                copy_on_unfocus_trigger_field=trigger,
                copy_on_unfocus_when_edit=on_edit,
                copy_on_unfocus_when_add=on_add,
            )
        ],
        **extra,
    )


def to_sources(
    name="d2s",
    field="Note",
    value="{{Keyword}}",
    query="Kanji:neko",
    trigger="Word",
    on_edit=True,
    on_add=True,
    **extra,
):
    """Across notes, trigger note as destination: the query picks the notes read from."""
    return d.destination_to_sources(
        definition_name=name,
        copy_from_cards_query=query,
        field_to_field_defs=[
            d.field_to_field(
                field,
                value,
                copy_on_unfocus_trigger_field=trigger,
                copy_on_unfocus_when_edit=on_edit,
                copy_on_unfocus_when_add=on_add,
            )
        ],
        **extra,
    )


def new_note(col, note_type=VOCAB, **fields):
    """A note that has not been added: `id` 0 -- what the Add-cards editor holds."""
    model = col.models.by_name(note_type)
    assert model is not None
    note = col.new_note(model)
    for field_name, value in fields.items():
        note[field_name] = value
    return note


def existing_note(col, note_type=VOCAB, **fields):
    return real_anki.add_note(col, note_type, fields, deck_name="Other")


# 2.4 ---------------------------------------------------------------------------------------


class TestOnEditorDidLoadNote:
    def test_it_stores_the_editor_and_its_note_id_under_the_editor_mode(self, col):
        note = existing_note(col, Word="neko")
        editor = FakeEditor(EditorMode.BROWSER, note)
        on_editor_did_load_note(editor)
        assert editor_for_note_id[EditorMode.BROWSER] == (editor, note.id)

    def test_an_editor_with_no_note_is_stored_under_note_id_none(self, col):
        # Not 0: that is a real note's id while it sits unsaved in the Add dialog.
        editor = FakeEditor(EditorMode.BROWSER, None)
        on_editor_did_load_note(editor)
        assert editor_for_note_id[EditorMode.BROWSER] == (editor, None)

    def test_the_add_cards_editors_unsaved_note_is_note_id_zero(self, col):
        # Not a special case in the handler: a note that has not been added simply has
        # `id == 0`.
        editor = FakeEditor(EditorMode.ADD_CARDS, new_note(col, Word="neko"))
        on_editor_did_load_note(editor)
        assert editor_for_note_id[EditorMode.ADD_CARDS] == (editor, 0)

    def test_three_editors_at_once_are_three_distinct_entries(self, col):
        browser_note = existing_note(col, Word="neko")
        current_note = existing_note(col, Word="inu")
        editors = {
            EditorMode.ADD_CARDS: FakeEditor(EditorMode.ADD_CARDS, new_note(col, Word="tori")),
            EditorMode.BROWSER: FakeEditor(EditorMode.BROWSER, browser_note),
            EditorMode.EDIT_CURRENT: FakeEditor(EditorMode.EDIT_CURRENT, current_note),
        }
        for editor in editors.values():
            on_editor_did_load_note(editor)
        assert editor_for_note_id == {
            EditorMode.ADD_CARDS: (editors[EditorMode.ADD_CARDS], 0),
            EditorMode.BROWSER: (editors[EditorMode.BROWSER], browser_note.id),
            EditorMode.EDIT_CURRENT: (editors[EditorMode.EDIT_CURRENT], current_note.id),
        }

    def test_a_second_load_in_the_same_mode_replaces_the_first(self, col):
        first = FakeEditor(EditorMode.BROWSER, existing_note(col, Word="neko"))
        second = FakeEditor(EditorMode.BROWSER, existing_note(col, Word="inu"))
        on_editor_did_load_note(first)
        on_editor_did_load_note(second)
        assert editor_for_note_id[EditorMode.BROWSER][0] is second

    def test_the_same_editor_moving_to_another_note_updates_the_stored_id(self, col):
        # The usual case in the browser: one editor object, a different row selected. The
        # id is snapshotted at load time, so the hook firing again is the only thing that
        # keeps the registry honest.
        editor = FakeEditor(EditorMode.BROWSER, existing_note(col, Word="neko"))
        on_editor_did_load_note(editor)
        editor.note = existing_note(col, Word="inu")
        on_editor_did_load_note(editor)
        assert editor_for_note_id[EditorMode.BROWSER] == (editor, editor.note.id)

    def test_an_unknown_editor_mode_adds_a_fourth_key(self, col):
        # The dict is pre-seeded with the three real modes but the handler assigns rather
        # than looking up, so nothing constrains the key to those three. Harmless today --
        # a new `EditorMode` in a future Anki would simply appear here.
        editor = FakeEditor("PREVIEWER", existing_note(col, Word="neko"))  # type: ignore[arg-type]
        on_editor_did_load_note(editor)
        assert editor_for_note_id["PREVIEWER"] == (editor, editor.note.id)

    def test_it_returns_nothing(self, col):
        assert on_editor_did_load_note(FakeEditor(EditorMode.BROWSER, None)) is None


class TestWhenEntriesAreDropped:
    """`editor_for_note_id` loses an entry when its editor closes, and at no other time.

    aqt has no hook for an editor going away, so `init_note_hooks` wraps `Editor.cleanup`
    -- which the browser, the Add dialog and the reviewer's edit window all call on close --
    to run `on_editor_will_cleanup`. That the wrap reaches a real closing dialog is shown in
    the real-Anki suite; here the handler is called directly. Profile switch and collection
    close still leave entries behind, so a stale note id can match a wholly different note
    in a reopened collection.
    """

    def test_a_closed_editor_is_dropped_and_not_reloaded(self, col, set_definitions):
        # A browser closed while its note stays open in the reviewer: the unfocus handler
        # must not call `loadNote()` on it, since in a real Anki its webview is gone.
        note = existing_note(col, Word="neko")
        closed = FakeEditor(EditorMode.BROWSER, note)
        on_editor_did_load_note(closed)
        on_editor_will_cleanup(closed)
        set_definitions(within())
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert editor_for_note_id[EditorMode.BROWSER] is None
        assert closed.loads == 0

    def test_closing_one_editor_leaves_the_other_modes_alone(self, col, set_definitions):
        note = existing_note(col, Word="neko")
        closed = FakeEditor(EditorMode.BROWSER, note)
        current = FakeEditor(EditorMode.EDIT_CURRENT, note)
        on_editor_did_load_note(closed)
        on_editor_did_load_note(current)
        on_editor_will_cleanup(closed)
        set_definitions(within())
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert editor_for_note_id[EditorMode.EDIT_CURRENT] == (current, note.id)
        assert (closed.loads, current.loads) == (0, 1)

    def test_closing_an_editor_already_replaced_in_its_slot_keeps_the_newer_one(self, col):
        # The slot is matched by editor identity, not by mode, so an old browser closing
        # after a new one has loaded a note does not evict the new one.
        old = FakeEditor(EditorMode.BROWSER, existing_note(col, Word="neko"))
        new = FakeEditor(EditorMode.BROWSER, existing_note(col, Word="inu"))
        on_editor_did_load_note(old)
        on_editor_did_load_note(new)
        on_editor_will_cleanup(old)
        assert editor_for_note_id[EditorMode.BROWSER] == (new, new.note.id)

    def test_closing_an_editor_that_never_loaded_a_note_changes_nothing(self, col):
        registered = FakeEditor(EditorMode.BROWSER, existing_note(col, Word="neko"))
        on_editor_did_load_note(registered)
        snapshot = dict(editor_for_note_id)
        on_editor_will_cleanup(FakeEditor(EditorMode.BROWSER, None))
        assert editor_for_note_id == snapshot

    def test_the_entries_outlive_the_collection_they_refer_to(self, col):
        # Note ids are timestamps, so the id held here is not reserved against a different
        # collection -- which is exactly what a profile switch produces.
        note = existing_note(col, Word="neko")
        on_editor_did_load_note(FakeEditor(EditorMode.BROWSER, note))
        assert editor_for_note_id[EditorMode.BROWSER][1] == note.id


# 2.2 ---------------------------------------------------------------------------------------


class TestTheNewVersusExistingNoteGate:
    """`copy_on_unfocus_when_add` and `copy_on_unfocus_when_edit`, one test per cell.

    The two flags live on the *field-to-field def*, not on the copy definition, and which
    one is read is decided solely by `note.id == 0`.
    """

    def test_a_new_note_runs_when_copy_on_unfocus_when_add_is_set(
        self, col, set_definitions, ran
    ):
        set_definitions(within(on_add=True, on_edit=False))
        run_copy_fields_on_unfocus_field(False, new_note(col, Word="neko"), WORD)
        assert ran.names() == ["within"]

    def test_a_new_note_does_not_run_without_copy_on_unfocus_when_add(
        self, col, set_definitions, ran
    ):
        set_definitions(within(on_add=False, on_edit=True))
        run_copy_fields_on_unfocus_field(False, new_note(col, Word="neko"), WORD)
        assert ran.names() == []

    def test_an_existing_note_runs_when_copy_on_unfocus_when_edit_is_set(
        self, col, set_definitions, ran
    ):
        set_definitions(within(on_edit=True, on_add=False))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == ["within"]

    def test_an_existing_note_does_not_run_without_copy_on_unfocus_when_edit(
        self, col, set_definitions, ran
    ):
        set_definitions(within(on_edit=False, on_add=True))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == []

    def test_a_missing_flag_key_reads_as_off(self, col, set_definitions, ran):
        definition = within()
        del definition["field_to_field_defs"][0]["copy_on_unfocus_when_edit"]
        set_definitions(definition)
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == []

    def test_the_note_id_is_the_only_thing_that_picks_which_flag_is_read(
        self, col, set_definitions, ran
    ):
        # A note that exists in the database but is handed over with `id` 0 -- which is what
        # a duplicated note in the Add dialog looks like -- takes the add branch.
        set_definitions(within(on_add=True, on_edit=False))
        note = existing_note(col, Word="neko")
        note.id = NoteId(0)
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert ran.names() == ["within"]

    def test_the_copy_on_add_review_and_sync_flags_are_not_consulted(
        self, col, set_definitions, ran
    ):
        # The unfocus path has its own two flags and ignores the three that gate the other
        # triggers, so a definition switched off everywhere else still fires while typing.
        definition = within()
        for key in ["copy_on_add", "copy_on_review", "copy_on_sync"]:
            definition[key] = False
        set_definitions(definition)
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == ["within"]

    def test_a_definition_for_another_note_type_does_not_run(self, col, set_definitions, ran):
        set_definitions(within(note_types=[KANJI]))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == []

    def test_a_definition_with_no_field_to_field_defs_does_not_run(
        self, col, set_definitions, ran
    ):
        # The guard is on the list being non-empty, before any trigger-field matching, so a
        # tags-only or files-only definition can never be triggered from the editor.
        set_definitions(d.within_note(definition_name="tags-only", add_tags="tagged"))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == []


class TestANewNoteNeverRunsDefinitionsThatTouchOtherNotes:
    def test_source_to_destinations_is_skipped_on_a_new_note(
        self, col, set_definitions, ran, copies
    ):
        existing_note(col, Word="inu")
        set_definitions(to_destinations(on_add=True, on_edit=True))
        run_copy_fields_on_unfocus_field(False, new_note(col, Word="neko"), WORD)
        assert ran.names() == []
        assert copies.count() == 0

    def test_the_skip_happens_before_the_flags_are_even_read(self, col, set_definitions, copies):
        # The `continue` sits above the `copy_on_unfocus_when_add` check, so no combination
        # of flags brings this definition back. There is no note id to hand `copy_fields`
        # yet, which is the reason.
        existing_note(col, Word="inu")
        for on_add in [True, False]:
            set_definitions(to_destinations(on_add=on_add))
            run_copy_fields_on_unfocus_field(False, new_note(col, Word="neko"), WORD)
        assert copies.count() == 0
        assert col.get_note(col.find_notes("Word:inu")[0])["Note"] == ""

    def test_destination_to_sources_runs_on_a_new_note_since_it_writes_only_that_note(
        self, col, set_definitions, ran
    ):
        # The other notes are only read from, so a lookup into a note being typed in the Add
        # dialog needs no note id and runs like a Within-note definition.
        existing_note(col, KANJI, Kanji="neko", Keyword="cat")
        set_definitions(to_sources(on_add=True))
        note = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert ran.names() == ["d2s"]
        assert note["Note"] == "cat"

    def test_on_an_existing_note_it_also_skips_copy_fields(
        self, col, set_definitions, ran, copies
    ):
        # Same direct path as on a new note: the value lands in the editor's note object and
        # the editor's own save is what writes it.
        existing_note(col, KANJI, Kanji="neko", Keyword="cat")
        set_definitions(to_sources(on_edit=True))
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert ran.names() == ["d2s"]
        assert copies.count() == 0
        assert note["Note"] == "cat"
        assert col.get_note(note.id)["Note"] == ""

    def test_a_within_note_definition_alongside_still_runs_on_a_new_note(
        self, col, set_definitions, ran
    ):
        existing_note(col, Word="inu")
        set_definitions(to_destinations("skipped"), within("direct"))
        run_copy_fields_on_unfocus_field(False, new_note(col, Word="neko"), WORD)
        assert ran.names() == ["direct"]


class TestANewNoteEditsOnlyItself:
    """The Add dialog is the one place the add can still be cancelled (§8).

    So an unfocus on a note that has not been added yet may fill that note's fields and
    tags -- the add saves the object it mutated -- and nothing else. A card action on it is
    impossible rather than forbidden: it is skipped with a log line and does not disqualify
    the definition. Anything that would outlive a cancelled add -- another note, an existing
    card, a file -- is stopped, by the gate when the definition's effects admit it and by
    the backstop when they do not.
    """

    def flag(self):
        return d.card_action(VOCAB, "Recognition", set_flag=3)

    def card_named(self, note, template_name):
        return next(card for card in note.cards() if card.template()["name"] == template_name)

    def fill_and_flag(self, *extra_stages, **effects):
        definition = d.staged(
            "fill-and-flag",
            on_unfocus={"edit_fields": [], "add_fields": ["Word"]},
            stages=[
                d.edit_note(
                    "trigger",
                    [d.write("Meaning", d.text("{{trigger.Word}}"))],
                    card_actions=[self.flag()],
                ),
                *extra_stages,
            ],
        )
        definition["effects"].update(effects)
        return definition

    def test_the_field_is_filled_and_the_card_action_is_a_logged_skip(
        self, col, set_definitions, hook_logger, ran
    ):
        set_definitions(self.fill_and_flag())
        note = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)

        assert ran.names() == ["fill-and-flag"]
        assert note["Meaning"] == "neko"
        assert hook_logger.errors == []
        assert any("no cards yet" in message for message in hook_logger.warnings), (
            hook_logger.warnings
        )

    def test_a_definition_that_also_writes_a_file_is_skipped_by_the_gate(
        self, col, set_definitions, hook_logger, ran, media_dir
    ):
        # A file written while the note is being typed stays on disk even if the user
        # presses Escape, so the whole definition waits for the add.
        set_definitions(self.fill_and_flag(d.write_file("log.txt", d.text("{{trigger.Word}}"))))
        note = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)

        assert ran.names() == []
        assert note["Meaning"] == ""
        assert not (media_dir / "_log.txt").exists()
        assert hook_logger.errors == []

    def test_a_file_write_the_stored_effects_hid_is_refused_by_the_backstop(
        self, col, set_definitions, hook_logger, media_dir
    ):
        # The gate only reads what the definition claims. A hand-edited config claiming a
        # file write away is caught where the run is committed instead.
        set_definitions(
            self.fill_and_flag(
                d.write_file("log.txt", d.text("{{trigger.Word}}")),
                writes_files=False,
                add_note_compatible=True,
            )
        )
        note = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)

        assert not (media_dir / "_log.txt").exists()
        assert hook_logger.has_error("another note, card or file"), hook_logger.errors

    def test_an_edit_to_another_note_the_stored_effects_hid_is_refused_too(
        self, col, set_definitions, hook_logger
    ):
        # The same lie about the other half of the principle: the found note's card would
        # be flagged through the hook's own `update_cards` if the run were committed.
        other = existing_note(col, Word="inu")
        set_definitions(
            self.fill_and_flag(
                d.note_query("found", "Word:inu"),
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
                edits_other_notes=False,
                edits_other_cards=False,
                add_note_compatible=True,
            )
        )
        note = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)

        assert col.get_note(other.id)["Note"] == ""
        assert col.get_card(self.card_named(other, "Recognition").id).user_flag() == 0
        assert hook_logger.has_error("another note, card or file"), hook_logger.errors


class TestWhichFieldFiresADefinition:
    def test_a_field_that_is_no_definitions_trigger_runs_nothing(
        self, col, set_definitions, ran
    ):
        set_definitions(within(trigger="Word"))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), MEANING)
        assert ran.names() == []

    def test_the_field_index_is_an_index_into_the_note_types_field_order(
        self, col, set_definitions, ran
    ):
        # `note.keys()[field_idx]`, so the argument is positional: CA Vocab is
        # Word/Reading/Meaning/Freq/Note and index 2 is Meaning, whatever the field holds.
        set_definitions(within(trigger="Meaning", value="{{Meaning}}"))
        note = existing_note(col, Word="neko", Meaning="cat")
        run_copy_fields_on_unfocus_field(False, note, MEANING)
        assert ran.names() == ["within"]
        assert note["Note"] == "cat"

    def test_an_out_of_range_field_index_raises(self, col, set_definitions):
        # Nothing bounds-checks the index. Anki never sends one out of range, but the
        # exception would propagate out of the hook and unregister the handler for the
        # session (`_EditorDidUnfocusFieldFilter.__call__` removes a filter that raises).
        set_definitions(within())
        with pytest.raises(IndexError):
            run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), 99)

    def test_either_field_of_a_multi_field_trigger_fires_it(self, col, set_definitions, ran):
        trigger = d.quoted_list(["Word", "Meaning"])
        set_definitions(within(trigger=trigger))
        note = existing_note(col, Word="neko", Meaning="cat")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        run_copy_fields_on_unfocus_field(False, note, MEANING)
        assert ran.names() == ["within", "within"]

    def test_a_third_field_of_the_same_note_type_still_fires_nothing(
        self, col, set_definitions, ran
    ):
        set_definitions(within(trigger=d.quoted_list(["Word", "Meaning"])))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), READING)
        assert ran.names() == []

    def test_a_plainly_comma_separated_trigger_list_matches_nothing(
        self, col, set_definitions, ran
    ):
        # The split is on the exact three characters `", "`, as everywhere else in the
        # config, so a hand-written list stays one long field name.
        set_definitions(within(trigger="Word, Meaning"))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == []

    def test_an_empty_trigger_field_falls_back_to_the_destination_field(
        self, col, set_definitions, ran
    ):
        # In Within-note mode the destination field is in the edited note, so a definition
        # left with no trigger field fires when its own destination field is unfocused.
        set_definitions(within(field="Note", trigger=""))
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, NOTE)
        assert ran.names() == ["within"]
        assert note["Note"] == "neko"

    def test_an_empty_trigger_field_does_not_fire_on_other_fields(
        self, col, set_definitions, ran
    ):
        set_definitions(within(field="Note", trigger=""))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == []

    def test_the_trigger_field_need_not_be_a_field_of_the_note_type(
        self, col, set_definitions, ran
    ):
        # Names are compared against `note.keys()`, never validated against the note type,
        # so a trigger naming a field that was renamed away is silently inert.
        set_definitions(within(trigger="Renamed"))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == []


class TestAMigratedDefinitionsPerWriteUnfocusSettings:
    """The two settings format 1 kept on each field write, after the startup migration.

    A migrated definition is stored as stages, so the handler takes the format-2 branch and
    runs the whole definition. The settings survive on the writes themselves, and the
    executor is what honours them now -- which is the only reason a write can still be left
    out of an unfocus run while the rest of its definition runs.
    """

    def migrated(self, **defs):
        # Through `stage_definitions`, so the definition carries the derived `effects` the
        # startup migration would have given it and the handler branches on them as it will
        # in a real collection.
        from copy_anywhere.logic.flow_analysis import stage_definitions

        staged, problems = stage_definitions([
            d.within_note(
                definition_name="within",
                field_to_field_defs=[
                    d.field_to_field(
                        field,
                        "{{Word}}",
                        copy_on_unfocus_trigger_field="Word",
                        **settings,
                    )
                    for field, settings in defs.items()
                ],
            )
        ])
        assert problems == []
        return staged[0]

    def test_a_write_that_is_off_for_editing_is_left_out_of_an_edit_unfocus(
        self, col, set_definitions
    ):
        # The fast write fills straight away; the slow one was deliberately saved for the
        # bulk action, and used to run on every unfocus once the definition was migrated.
        set_definitions(
            self.migrated(
                Meaning={"copy_on_unfocus_when_edit": True},
                Note={"copy_on_unfocus_when_edit": False},
            )
        )
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Meaning"] == "neko"
        assert note["Note"] == ""

    def test_the_add_flag_is_what_counts_in_the_add_dialog(self, col, set_definitions):
        set_definitions(
            self.migrated(
                Meaning={"copy_on_unfocus_when_edit": True, "copy_on_unfocus_when_add": False},
                Note={"copy_on_unfocus_when_edit": False, "copy_on_unfocus_when_add": True},
            )
        )
        note = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Meaning"] == ""
        assert note["Note"] == "neko"

    def test_a_write_watching_another_field_is_left_out_too(self, col, set_definitions):
        definition = self.migrated(
            Meaning={"copy_on_unfocus_when_edit": True},
            Note={"copy_on_unfocus_when_edit": True},
        )
        writes = definition["stages"][0]["fields"]
        next(w for w in writes if w["field"] == "Note")["unfocus_trigger_fields"] = ["Reading"]
        set_definitions(definition)
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Meaning"] == "neko"
        assert note["Note"] == ""


class TestAFormatOneDefinitionStillInTheConfig:
    """One the startup migration could not convert, so the handler's format-1 branch runs it.

    That branch picks the field writes whose own add/edit flag is on and runs the definition
    with just those. The executor then checks the migrated copy of the same flag, so both
    have to be looking at the same one -- the migrated write is dropped otherwise, and a
    definition set to run only while adding writes nothing in the Add dialog.
    """

    def add_only(self):
        return within(trigger="Word", on_edit=False, on_add=True)

    def test_an_add_only_definition_writes_while_adding(self, col, set_definitions):
        set_definitions(self.add_only())
        note = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Note"] == "neko"

    def test_it_writes_nothing_while_editing(self, col, set_definitions):
        set_definitions(self.add_only())
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Note"] == ""

    def test_an_edit_only_definition_is_the_other_way_round(self, col, set_definitions):
        set_definitions(within(trigger="Word", on_edit=True, on_add=False))
        existing = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, existing, WORD)
        added = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, added, WORD)
        assert existing["Note"] == "neko"
        assert added["Note"] == ""


class TestTheDeckWhitelistOnThisPath:
    """An existing note is checked by its cards; a new one by the Add dialog's deck."""

    def test_a_deck_outside_the_whitelist_writes_nothing(self, col, set_definitions, ran):
        # The handler dispatches regardless -- the whitelist is enforced one layer down,
        # off the note's own cards -- so "did not copy" here is not "was filtered out".
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert ran.names() == ["within"]
        assert note["Note"] == ""

    def test_a_whitelisted_deck_lets_the_copy_through(self, col, set_definitions):
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="JP vocab")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Note"] == "neko"

    def test_a_new_note_is_skipped_when_the_add_dialog_is_on_another_deck(
        self, col, set_definitions
    ):
        # A note being added has no cards, so the whitelist step would have nothing to
        # check. The hook gives the handler no editor, but the registry holds the Add
        # dialog's, and its deck chooser says where the note is going.
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = new_note(col, Word="neko")
        on_editor_did_load_note(FakeEditor(EditorMode.ADD_CARDS, note, col.decks.id("Other")))
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Note"] == ""

    def test_a_new_note_copies_when_the_add_dialog_is_on_a_whitelisted_deck(
        self, col, set_definitions
    ):
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = new_note(col, Word="neko")
        on_editor_did_load_note(
            FakeEditor(EditorMode.ADD_CARDS, note, col.decks.id("JP vocab"))
        )
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Note"] == "neko"

    def test_a_new_note_with_no_add_dialog_still_passes_the_whitelist(
        self, col, set_definitions
    ):
        # With no Add editor registered there is no deck to check, and the whitelist step
        # lets a card-less note through, as it would without a `deck_id` anywhere else.
        set_definitions(within(only_copy_into_decks=d.quoted_list(["JP vocab"])))
        note = new_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Note"] == "neko"


class TestEveryDefWithThatTriggerIsGatedByItsOwnFlags:
    """Every def the field triggers passes or fails the add/edit gate on its own flags.

    The handler hands `copy_for_single_trigger_note` a copy of the definition holding only
    the defs that passed, still with `field_only=field_name`. `field_only` re-filters by
    trigger field alone, so on the whole definition it would bring the gated-out defs back.
    """

    def test_two_defs_sharing_a_trigger_field_both_write(self, col, set_definitions):
        definition = d.within_note(
            definition_name="two-defs",
            field_to_field_defs=[
                d.field_to_field(
                    "Note",
                    "{{Word}}-note",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=True,
                ),
                d.field_to_field(
                    "Meaning",
                    "{{Word}}-meaning",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=True,
                ),
            ],
        )
        set_definitions(definition)
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert (note["Note"], note["Meaning"]) == ("neko-note", "neko-meaning")

    def test_a_def_with_a_different_trigger_field_does_not_write(self, col, set_definitions):
        definition = d.within_note(
            definition_name="two-defs",
            field_to_field_defs=[
                d.field_to_field(
                    "Note",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=True,
                ),
                d.field_to_field(
                    "Meaning",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Reading",
                    copy_on_unfocus_when_edit=True,
                ),
            ],
        )
        set_definitions(definition)
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert (note["Note"], note["Meaning"]) == ("neko", "")

    def test_a_second_def_with_the_flag_off_does_not_run_after_a_first_that_has_it_on(
        self, col, set_definitions
    ):
        # The first def passing the gate must not carry the second through with it: the
        # user switched unfocus copying off for that def.
        definition = d.within_note(
            definition_name="two-defs",
            field_to_field_defs=[
                d.field_to_field(
                    "Note",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=True,
                ),
                d.field_to_field(
                    "Meaning",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=False,
                    copy_on_unfocus_when_add=False,
                ),
            ],
        )
        set_definitions(definition)
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert (note["Note"], note["Meaning"]) == ("neko", "")

    def test_a_first_def_with_the_flag_off_does_not_suppress_a_second_that_has_it_on(
        self, col, set_definitions, ran
    ):
        # The same gate the other way round. Config order is not something the user thinks
        # of as significant here, so an earlier def with the flag off must not stop a later
        # one on the same trigger field.
        definition = d.within_note(
            definition_name="two-defs",
            field_to_field_defs=[
                d.field_to_field(
                    "Note",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=False,
                ),
                d.field_to_field(
                    "Meaning",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=True,
                ),
            ],
        )
        set_definitions(definition)
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert ran.names() == ["two-defs"]
        assert (note["Note"], note["Meaning"]) == ("", "neko")

    def test_a_definition_touching_other_notes_reaches_copy_fields_with_only_the_gated_defs(
        self, col, set_definitions, copies
    ):
        # `copy_fields` gets the same `field_only`, so it would bring a gated-out def back
        # just as `copy_for_single_trigger_note` would.
        other = existing_note(col, Word="inu")
        definition = d.source_to_destinations(
            definition_name="s2d",
            copy_from_cards_query="Word:inu",
            select_card_count="0",
            field_to_field_defs=[
                d.field_to_field(
                    "Note",
                    "copied",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=True,
                ),
                d.field_to_field(
                    "Meaning",
                    "copied",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=False,
                ),
            ],
        )
        set_definitions(definition)
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        [passed] = copies.kwargs()["copy_definitions"]
        assert [f["copy_into_note_field"] for f in passed["field_to_field_defs"]] == ["Note"]
        assert (col.get_note(other.id)["Note"], col.get_note(other.id)["Meaning"]) == (
            "copied",
            "",
        )

    def test_the_configured_definition_is_left_with_all_its_defs(
        self, col, set_definitions
    ):
        # The gated defs go into a copy, so the config dict itself is not trimmed for the
        # next unfocus, where the other flag may be the one that is read.
        definition = d.within_note(
            definition_name="two-defs",
            field_to_field_defs=[
                d.field_to_field(
                    "Note",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_edit=True,
                ),
                d.field_to_field(
                    "Meaning",
                    "{{Word}}",
                    copy_on_unfocus_trigger_field="Word",
                    copy_on_unfocus_when_add=True,
                ),
            ],
        )
        set_definitions(definition)
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        [configured] = mw.addonManager.configs[ADDON_TAG]["copy_definitions"]
        assert len(configured["field_to_field_defs"]) == 2

    def test_the_field_name_is_what_is_passed_as_field_only(self, col, set_definitions, ran):
        set_definitions(within(trigger="Meaning"))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), MEANING)
        assert ran.field_onlys() == ["Meaning"]

    def test_several_definitions_on_one_trigger_field_all_run_in_config_order(
        self, col, set_definitions, ran
    ):
        set_definitions(within("first"), within("second", field="Meaning"))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == ["first", "second"]


class TestTheReturnValue:
    """`changed` is recomputed over the whole note and OR-ed with the incoming value."""

    def test_writing_the_unfocused_field_itself_returns_true(self, col, set_definitions):
        set_definitions(within(field="Word", value="{{Word}}!", trigger="Word"))
        assert run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)

    def test_writing_a_different_field_also_returns_true(self, col, set_definitions):
        # The comparison is over `note.values()`, not over the unfocused field, which is
        # what makes a Within-note definition writing elsewhere visible to the editor.
        set_definitions(within(field="Note", value="{{Word}}", trigger="Word"))
        assert run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)

    def test_a_definition_that_writes_the_same_value_back_returns_false(
        self, col, set_definitions, ran
    ):
        # It ran; the values simply did not move. "changed" means "differs", not "a copy
        # definition fired".
        set_definitions(within(field="Note", value="{{Word}}"))
        note = existing_note(col, Word="neko", Note="neko")
        assert run_copy_fields_on_unfocus_field(False, note, WORD) is False
        assert ran.names() == ["within"]

    def test_no_definitions_at_all_returns_false(self, col, set_definitions):
        set_definitions()
        assert run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD) is False

    def test_the_incoming_changed_argument_is_kept(self, col, set_definitions):
        # `editor_did_unfocus_field` is a *filter* hook: aqt feeds each handler the previous
        # one's answer and reloads the editor if the last answer is True. Anki itself always
        # seeds the chain with a literal False (`aqt/editor.py`, `onBridgeCmd`), so a True
        # coming in is another addon's, and dropping it would cost that addon its reload.
        set_definitions()
        assert run_copy_fields_on_unfocus_field(True, existing_note(col, Word="neko"), WORD) is True

    def test_a_note_with_no_note_type_passes_changed_through_before_anything_runs(
        self, col, set_definitions, ran
    ):
        set_definitions(within())
        note = existing_note(col, Word="neko")
        note.note_type = lambda: None  # type: ignore[method-assign]
        assert run_copy_fields_on_unfocus_field(False, note, WORD) is False
        assert run_copy_fields_on_unfocus_field(True, note, WORD) is True
        assert ran.names() == []


class TestWhichEditorsAreReloaded:
    def test_the_browser_and_the_reviewer_editor_on_one_note_both_reload(
        self, col, set_definitions
    ):
        # The source cannot tell which of the two the user typed in -- the hook passes no
        # editor -- so it reloads both. That is the entire reason the registry exists.
        note = existing_note(col, Word="neko")
        browser = FakeEditor(EditorMode.BROWSER, note)
        current = FakeEditor(EditorMode.EDIT_CURRENT, note)
        on_editor_did_load_note(browser)
        on_editor_did_load_note(current)
        set_definitions(within())
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert (browser.loads, current.loads) == (1, 1)

    def test_an_editor_on_another_note_is_not_reloaded(self, col, set_definitions):
        note = existing_note(col, Word="neko")
        other = FakeEditor(EditorMode.BROWSER, existing_note(col, Word="inu"))
        on_editor_did_load_note(other)
        set_definitions(within())
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert other.loads == 0

    def test_a_destination_to_sources_write_reloads_the_editor(self, col, set_definitions):
        # It writes the in-memory note rather than going through `copy_fields`, so `changed`
        # sees the new value and the editor shows it instead of saving the old one back.
        existing_note(col, KANJI, Kanji="neko", Keyword="cat")
        set_definitions(to_sources())
        note = existing_note(col, Word="neko")
        editor = FakeEditor(EditorMode.BROWSER, note)
        on_editor_did_load_note(editor)
        assert run_copy_fields_on_unfocus_field(False, note, WORD) is True
        assert editor.loads == 1
        assert note["Note"] == "cat"

    def test_an_incoming_true_alone_reloads_nothing(self, col, set_definitions):
        # The True is passed on, and aqt reloads the editor it belongs to; the extra reload
        # here is only for the second editor on a note this addon wrote to.
        note = existing_note(col, Word="neko")
        editor = FakeEditor(EditorMode.BROWSER, note)
        on_editor_did_load_note(editor)
        set_definitions()
        run_copy_fields_on_unfocus_field(True, note, WORD)
        assert editor.loads == 0

    def test_nothing_reloads_when_no_field_value_moved(self, col, set_definitions):
        note = existing_note(col, Word="neko", Note="neko")
        editor = FakeEditor(EditorMode.BROWSER, note)
        on_editor_did_load_note(editor)
        set_definitions(within(field="Note", value="{{Word}}"))
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert editor.loads == 0

    def test_a_new_note_matches_the_add_cards_editor(self, col, set_definitions):
        note = new_note(col, Word="neko")
        adder = FakeEditor(EditorMode.ADD_CARDS, note)
        on_editor_did_load_note(adder)
        set_definitions(within())
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert adder.loads == 1

    def test_a_new_note_does_not_match_an_editor_that_was_loaded_with_no_note(
        self, col, set_definitions
    ):
        # A browser sitting on an empty selection shares nothing with the note being typed
        # in the Add dialog, so it must not get a `loadNote()` it never asked for. The
        # no-note editor is stored under None for exactly this, since a new note's id is 0.
        note = new_note(col, Word="neko")
        adder = FakeEditor(EditorMode.ADD_CARDS, note)
        empty_browser = FakeEditor(EditorMode.BROWSER, None)
        on_editor_did_load_note(adder)
        on_editor_did_load_note(empty_browser)
        set_definitions(within())
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert (adder.loads, empty_browser.loads) == (1, 0)

    def test_an_unregistered_editor_mode_is_skipped(self, col, set_definitions):
        # The three keys start at None and the loop skips falsy entries, so a mode that has
        # never loaded a note costs nothing.
        note = existing_note(col, Word="neko")
        set_definitions(within())
        assert run_copy_fields_on_unfocus_field(False, note, WORD) is True

    def test_the_registry_is_read_before_the_copy_runs(self, col, set_definitions):
        # The matching editors are collected at the top of the handler, before any
        # definition executes, so a definition that somehow re-registered an editor would
        # not affect this run.
        note = existing_note(col, Word="neko")
        late = FakeEditor(EditorMode.BROWSER, note)
        set_definitions(within())

        original = note_hooks.copy_for_single_trigger_note

        def register_then_copy(**kwargs):
            on_editor_did_load_note(late)
            return original(**kwargs)

        note_hooks.copy_for_single_trigger_note = register_then_copy
        try:
            run_copy_fields_on_unfocus_field(False, note, WORD)
        finally:
            note_hooks.copy_for_single_trigger_note = original
        assert late.loads == 0

    def test_the_reload_keeps_the_caret_in_the_current_field(self, col, set_definitions):
        # aqt's own reaction to this handler returning True is `loadNoteKeepingFocus()` on a
        # 100ms timer, which passes `self.currentField` so the user does not lose their
        # place. The handler reloads the same editor first, so it has to keep it too, or the
        # caret is gone before aqt's reload runs.
        note = existing_note(col, Word="neko")
        editor = FakeEditor(EditorMode.BROWSER, note, current_field=2)
        on_editor_did_load_note(editor)
        set_definitions(within())
        assert run_copy_fields_on_unfocus_field(False, note, WORD) is True
        assert editor.load_args == [((2,), {})]

    def test_the_same_editor_registered_under_two_modes_reloads_twice(
        self, col, set_definitions
    ):
        # Not reachable from a real Anki, but it pins that the dedupe is by mode key and not
        # by editor identity.
        note = existing_note(col, Word="neko")
        editor = FakeEditor(EditorMode.BROWSER, note)
        on_editor_did_load_note(editor)
        editor.editorMode = EditorMode.EDIT_CURRENT
        on_editor_did_load_note(editor)
        set_definitions(within())
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert editor.loads == 2


class TestTheModifiesOtherNotesBranchGoesThroughCopyFields:
    def test_it_calls_copy_fields_with_this_note_and_this_field(
        self, col, set_definitions, copies
    ):
        existing_note(col, Word="inu")
        set_definitions(to_destinations())
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        kwargs = copies.kwargs()
        assert kwargs["note_ids"] == [note.id]
        assert kwargs["field_only"] == "Word"
        assert kwargs["undo_text_suffix"] == "triggered by unfocus field 'Word'"

    def test_the_undo_entry_carries_that_suffix(self, col, set_definitions, copies):
        existing_note(col, Word="inu")
        set_definitions(to_destinations())
        note = existing_note(col, Word="neko")
        before = col.undo_status().last_step
        run_copy_fields_on_unfocus_field(False, note, WORD)
        status = col.undo_status()
        assert status.undo == "Copy fields (s2d) for 1 notes triggered by unfocus field 'Word'"
        assert status.last_step == before + 1

    def test_the_other_note_is_written_to_the_database(self, col, set_definitions, copies):
        other = existing_note(col, Word="inu")
        set_definitions(to_destinations())
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert col.get_note(other.id)["Note"] == "copied"

    def test_all_such_definitions_share_one_copy_fields_call(
        self, col, set_definitions, copies
    ):
        existing_note(col, Word="inu")
        set_definitions(to_destinations("a"), to_destinations("b", field="Meaning"))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert copies.names() == [["a", "b"]]

    def test_they_run_after_every_non_modifying_definition(
        self, col, set_definitions, ran, copies
    ):
        # The modifying ones are collected during the loop and dispatched afterwards, so a
        # Within-note definition's write is already in the in-memory note that `copy_fields`
        # is handed. See the next test.
        existing_note(col, Word="inu")
        set_definitions(to_destinations("deferred"), within("direct"))
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert ran.names() == ["direct"]

    def test_copy_fields_reads_the_trigger_note_from_the_editor_not_the_database(
        self, col, set_definitions, copies
    ):
        # Anki does start a save before the hook, but `Editor._save_current_note` is an
        # `update_note(...).run_in_background()` -- a background CollectionOp with no
        # ordering against the hook body -- so the database can still hold the previous
        # value. The in-memory note is handed to `copy_fields` so the value copied into
        # other notes is the one on screen. Below, "typed" is what the editor holds and
        # "neko" is what is committed.
        other = existing_note(col, Word="inu")
        set_definitions(to_destinations(value="{{Word}}"))
        note = existing_note(col, Word="neko")
        note["Word"] = "typed"
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert copies.kwargs()["trigger_notes"] == [note]
        assert col.get_note(other.id)["Note"] == "typed"

    def test_no_copy_fields_call_and_no_undo_entry_when_nothing_matched(
        self, col, set_definitions, copies
    ):
        # The branch is guarded by `if editing_other_notes_definitions`, so a field no
        # definition triggers on never reaches `copy_fields` or its undo entry.
        set_definitions(to_destinations(trigger="Meaning"))
        note = existing_note(col, Word="neko")
        before = col.undo_status().last_step
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert copies.count() == 0
        assert col.undo_status().last_step == before


class TestNonModifyingDefinitionsDiscardTheirNoteList:
    def test_copied_into_notes_is_a_fresh_empty_list_that_nobody_keeps(
        self, col, set_definitions, ran
    ):
        # The callee does fill the list -- with the trigger note -- and the handler holds no
        # name for it, so `update_notes` is never called on anything it collected.
        set_definitions(within())
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert ran.copied_into_notes_at_call() == [[]]
        assert ran.collected_note_ids() == [[note.id]]

    def test_the_in_memory_note_carries_the_change_and_the_database_does_not(
        self, col, set_definitions
    ):
        # This is the design, not an oversight: the editor owns the note, and reloading it
        # from an object the handler mutated is how the value reaches the screen. Anki
        # writes it when the editor saves.
        set_definitions(within())
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note["Note"] == "neko"
        assert col.get_note(note.id)["Note"] == ""

    def test_no_undo_entry_is_created(self, col, set_definitions):
        set_definitions(within())
        note = existing_note(col, Word="neko")
        before = col.undo_status()
        run_copy_fields_on_unfocus_field(False, note, WORD)
        after = col.undo_status()
        assert (after.last_step, after.undo) == (before.last_step, before.undo)

    def test_tags_reach_the_in_memory_note_only(self, col, set_definitions):
        # Same mechanism as the fields: the object is the editor's, and the editor's own
        # save is what puts either of them in the database.
        set_definitions(within(add_tags="tagged"))
        note = existing_note(col, Word="neko")
        run_copy_fields_on_unfocus_field(False, note, WORD)
        assert note.tags == ["tagged"]
        assert col.get_note(note.id).tags == []

    def test_a_definition_that_only_moves_tags_reloads_the_editor_and_returns_true(
        self, col, set_definitions, ran
    ):
        # Tags are not field values, so they are compared on their own. Without that, a
        # definition that tagged the note but wrote no new field value would reload no
        # editor and -- since the return value is also what tells aqt to refresh -- the tag
        # bar would keep showing the old tags until something else reloaded the note.
        set_definitions(within(field="Note", value="{{Word}}", add_tags="tagged"))
        note = existing_note(col, Word="neko", Note="neko")
        editor = FakeEditor(EditorMode.BROWSER, note)
        on_editor_did_load_note(editor)
        assert run_copy_fields_on_unfocus_field(False, note, WORD) is True
        assert ran.names() == ["within"]
        assert note.tags == ["tagged"]
        assert editor.loads == 1

    def test_the_handler_opens_its_operation_log_at_the_configured_level(
        self, col, set_definitions, ran, hook_logger, capsys
    ):
        # The level comes from the config and reaches the file the operation writes, and
        # nothing is printed on the way: the addon's lines go to the loggers, and only a
        # handler decides where they land.
        set_definitions(within(field="Nonexistent"), log_level="debug")
        run_copy_fields_on_unfocus_field(False, existing_note(col, Word="neko"), WORD)
        assert hook_logger.levels == ["debug"]
        assert hook_logger.has_error("not found in note")
        assert "not found in note" not in capsys.readouterr().out
