# copy_anywhere (CopyAnywhere)

Read the root [AGENTS.md](../AGENTS.md) first.

Batch field editing driven by saved **copy definitions**. A definition fills destination
fields from `{{Field}}` templates, special note/card values, variables or sandboxed Python
("code mode"), then optionally runs a process chain (regex replace, Kana Highlight, Word
Highlight, Kanjium to Javdejong, Fonts check). Modes: within one note, or across notes via a
search query interpolated from the trigger note, in either direction ("Destination to
sources" writes the trigger note; "Source to destinations" writes the found notes). A
definition can also add/remove tags, write files into `collection.media`, and act on cards
(move deck, flag, suspend, bury, desired retention).

Triggers: the browser Edit menu dialog and right-click submenu, note add, card review,
editor field unfocus, and sync (for cards whose custom data `fc` flag the scheduler JS
reset; see [docs/anki-patterns.md](../docs/anki-patterns.md)).

## Map

| path | role |
| --- | --- |
| `__init__.py` | at import: `migrate_config()`, `init_browser_hooks()`, `init_sync_hook()`, `init_note_hooks()` |
| `configuration.py` | TypedDicts for the whole config shape (`CopyDefinition` and its parts), `Config` (saves on every mutation), `migrate_config` |
| `logging_setup.py` | one log file per triggered operation under `user_files/logs` (keeps 50), reference-counted; a ContextVar supplies the `[definition][NID:n]` prefix; also captures the `jp_text_processing` logger |
| `hooks/browser_hooks.py` | Edit-menu action, context submenu with one action per definition, a "CustomData" reset submenu |
| `hooks/note_hooks.py` | add / review / unfocus handlers; wraps `Editor.cleanup` and `V3Scheduler.answer_card` (guarded by a `copy_anywhere_wrapped` attribute) |
| `hooks/sync_hook.py` | runs every `copy_on_sync` definition at sync start and finish; one combined tooltip |
| `logic/copy_fields.py` | the engine (about 1600 lines) |
| `logic/execute_code_wrappers.py` | validates code-mode return shapes for files and card actions over the shared `execute_code_core` |
| `logic/*_process.py`, `FatalProcessError.py` | the five chain steps; `FatalProcessError` aborts a whole run |
| `utils/` | `duplicate_note`, `merge_cards`, `move_card_to_deck`, media-folder helpers, `replace_custom_field_values` |
| `ui/` | `pick_copy_definition_dialog` (run, edit, duplicate, reorder), `edit_copy_definition_dialog` and one editor module per tab, `edit_state.EditState` shared between tabs |

Engine call chain for a bulk run:

    copy_fields()                       main thread; opens the operation log; CollectionOp
      op: add_custom_undo_entry
        copy_fields_in_background()     per definition: select note ids by SQL
          copy_for_single_trigger_note()  variables, deck whitelist, condition, target notes
            copy_into_single_note()       field values, process chain, tags, files, card actions
        update_notes / update_cards / merge_undo_entries   after EACH definition

## Invariants

- **Only the top layer writes to the database.** Everything from
  `copy_for_single_trigger_note` down mutates the `Note` and `Card` objects it is given and
  appends them to the caller's `copied_into_notes` / `copied_into_cards_dict`. A caller that
  passes no lists gets no write, on purpose: on add, Anki saves the note; on unfocus, the
  editor does. Tests therefore assert on returned objects, not on a re-fetched note, except
  at the `copy_fields` level.
- `update_notes`, `update_cards` and `merge_undo_entries` run after **every** definition.
  Later definitions re-fetch from the database, and a skipped merge ends in "target undo op
  not found".
- `card.edited` is an ad-hoc marker attribute; it must be deleted before `update_cards`.
- `copy_fields` must be called on the main thread. The add hook therefore calls
  `copy_for_single_trigger_note` directly and handles undo itself; notes with id 0 are
  filtered out before `update_notes`. Its undo entry lands before Anki's own "Add note"
  entry, a documented limitation.
- The review handler merges into the recorded Answer Card undo step, folds card-action edits
  into the reviewed card with `merge_cards` before the single `update_card`, then sets
  `fc=1`, or `fc=-1` when sync-only definitions remain. The sync sweep ends by setting every
  `fc` of 0 or -1 to 1.
- The unfocus handler is a filter hook: return `changed or we_changed`. It never runs
  other-note definitions on a new note, runs "Source to destinations" through
  `copy_fields(trigger_notes=[note])` because the editor's note can be ahead of the database,
  and reloads editors with `loadNoteKeepingFocus`.
- Return contract inside the engine: `True` is success **or a benign skip** (deck whitelist,
  unmet condition, no sources); `False` aborts the bulk loop. Zero sources without
  `run_also_if_no_sources_found` returns early so that destination fields are not wiped.
- Source and destination notes are `duplicate_note` copies, so every field definition reads
  pre-edit values and a field swap works.
- `extra_state` (query cache) is rebuilt per note and never hits across notes;
  `test/test_across_target_notes.py` pins this. Cached lists must be copied before use,
  because selection pops from them. `file_cache` lives for one definition run.
- Every `start_operation_log` needs exactly one `finish_operation_log`. `copy_fields`
  releases in both `on_success` and `on_failure`; `on_failure` re-raises on purpose.
- Multi-value config strings (note types, decks, tags, trigger fields) are stored as
  `A", "B`, the format `MultiComboBox` emits. `"-"` means none for decks and the sort field.
  Parse with a helper that drops `""` (`split_tags` does); a bare split of an empty string
  yields `[""]` and has caused bugs.
- UI tabs build lazily and rows load incrementally. A getter must call the tab's
  `create_*_tab()` and `finish_loading_*()` before reading widgets. The picker dialog keeps
  `checkboxes` and `definition_note_ids` index-parallel with the config list.
- Validation exists only in the UI (`EditCopyDefinitionDialog.check_fields`). The engine
  re-checks defensively and logs instead of raising.

## Config

Defaults in `config.json` (`log_level`, `copy_fields_shortcut`, `copy_definitions`); the
user's definitions live in `meta.json`, are large, and are edited only through the dialogs.
`migrate_config()` is gated on a `version` key and currently has one step (GUIDs, below
0.2.0). A change to the `CopyDefinition` shape needs: the TypedDict, a migration step, the
editor UI, the engine, and a builder in `test/definitions.py`. Definitions in the wild
contain user code that calls names exposed by the shared `execute_code`; renaming one of
those names breaks stored definitions silently.

## Tests

`copy_anywhere/test` (about 650 characterization tests on a real `Collection` behind a
stubbed `mw`) and `copy_anywhere/test_anki` (running Anki via pytest-anki2) are both in the
root `testpaths`. Reuse, do not reinvent:

- `test/conftest.py`: `col` (fresh collection with note types `CA Vocab`, `CA Sentence`,
  `CA Kanji`, `CA Cloze`, `CA Odd` and a three-level deck tree), `stub_mw`, `media_dir`,
  `logger` (`RecordingLogger` with `has_error` / `has_debug`), autouse log redirection.
- `test/definitions.py`: builders `within_note`, `destination_to_sources`,
  `source_to_destinations`, `field_to_field`, `field_to_file`, `field_to_variable`,
  `card_action`, `regex_process`, `fonts_check_process`, `quoted_list`.
- `test_anki/conftest.py`: `real_mw`, `addon_config`, `restore_stub_mw`.

These are characterization tests: they pin current behaviour, including behaviour that
looks odd. A test that fails after your change is a behaviour change to justify, not a test
to update. Not covered at all: everything in `ui/` except the picker's note counts
(`test/test_pick_dialog_note_source.py`), `migrate_config`, the `Config` CRUD
methods, `hooks/browser_hooks.py`, `utils/replace_custom_field_values.py`.

## Known rough edges

- `get_variable_values_for_note` can raise `CopyFailedException` outside the `try` in
  `copy_for_single_trigger_note`. `variable_values_dict` stays `None` when
  `field_to_variable_defs` is explicitly `None`, and across mode then indexes it.
- The `AnyProcess` union omits `WordHighlightProcess`.
- `get_field_to_field_defs()` output carries no `guid`; rows mint a new one on load.
- Callbacks that raise inside `EditState.call_callbacks` are dropped silently.
- Dead code: `ProgressUpdateDef`; `build_action` / `add_action_to_gear` /
  `add_separator_to_gear` in `hooks/browser_hooks.py`; the inline `test()`/`main()` in
  `logic/regex_process.py` that `.vscode/` runs through `test/run_with_setup.py`.
- The README says the hotkey is Alt+Shift+C; the `config.json` default is `Ctrl+Shift+C`.
  `config.md` is empty.

## Shared code

Declares `interpolate`, `ui`, `utils`, `anki`, `jp_text_processing`, `word_array`.
`word_array` and `jp_text_processing` are partly there for the shared `execute_code`, which
exposes `kana_highlight`, `decode_word_array` and `format_word_array` to code mode when they
import. The interpolation engine and most widgets this addon uses are shared with
`related_card_disperse`; change them in `anki_shared/`, never under `shared/`, and run that
addon's tests too. Several helpers here are generic with one owner (`utils/merge_cards`,
`move_card_to_deck`, `duplicate_note`, the media-folder helpers): when another addon needs
one, move it per [docs/shared-code.md](../docs/shared-code.md) instead of copying.
