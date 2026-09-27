# copy_anywhere (CopyAnywhere)

Read the root [AGENTS.md](../AGENTS.md) first.

Batch field editing driven by saved **copy definitions**. A definition is a list of
**stages** that run in order for one trigger note: variables, note and card queries, Select
Note / Select Card, loops, reduce, conditions, edits to notes and cards (field writes with a
process chain, tags, card actions), file reads and writes, and calls into other
definitions. Values come from `{{Field}}` templates or sandboxed Python ("code mode"). This
is **format 2**; the older flat shape (format 1: copy modes, field-to-field defs) is
converted at startup and only survives as migration input.

The specification is [docs/staged-definitions.md](docs/staged-definitions.md): the shape,
every rule, the editor, the preview, and which file holds what. The user guide is
[ADDON_README.md](ADDON_README.md) (it ships in the package; `README.md` is for
developers and does not). Decisions and deferred work are in
[docs/follow-ups.md](docs/follow-ups.md).

Triggers: the browser Edit menu dialog and right-click submenu, note add, card review,
editor field unfocus, and sync (for cards whose custom data `fc` flag the scheduler JS
reset; see [docs/anki-patterns.md](../docs/anki-patterns.md)).

## Map

| path | role |
| --- | --- |
| `__init__.py` | at import: `migrate_config()`, `init_browser_hooks()`, `init_sync_hook()`, `init_note_hooks()`, `init_rename_hooks()` |
| `configuration.py` | `Config` (saves on every mutation), format-1 TypedDicts, the trigger accessors both formats go through, `migrate_config` |
| `logging_setup.py` | one log file per triggered operation under `user_files/logs` (keeps 50), reference-counted; a ContextVar supplies the `[definition][NID:n]` prefix; also captures the `jp_text_processing` logger |
| `hooks/` | browser menus, add / review / unfocus handlers (`note_hooks.py` wraps `Editor.cleanup` and `V3Scheduler.answer_card`, guarded by a `copy_anywhere_wrapped` attribute), the sync sweep, and when the rename pass runs (`rename_hooks.py`) |
| `logic/definition_schema.py` | format-2 types, stage-type constants, structural validation |
| `logic/definition_migration.py` | the pure format-1 -> format-2 migrator, and stage-guid repair |
| `logic/flow_analysis.py` | scopes, result types, effects, exports, call cycles: what the editor blocks a save on and what `effects` records |
| `logic/copy_fields.py` | the operation: which notes each definition runs for, the undo entry, the sync tail |
| `logic/execution/` | the evaluator: `runner.py` one definition for one note, `evaluator.py` the structural stages, `actions.py` the leaf stages, `expressions.py` both value syntaxes, `context.py` the session, `commit.py` what happens to a run's changes |
| `logic/copy_primitives.py` | interpolation, process chains, card actions, progress; older names are re-exported from `copy_fields` |
| `logic/preview.py`, `logic/unsaved_note_search.py` | a run that writes nothing; judging a search against a note not yet in the collection |
| `hooks/rename_hooks.py` | when the reconcile pass runs: every collection load (a rename synced in arrives with no other hook), every operation that changed a note type, and a deck-only change only when the decks' ids or names differ from what the last completed pass saw (an answer reports a deck change); after a note type change, a dialog listing only the marks that pass added |
| `logic/object_refs.py` | note type, deck and card type references (`{id, name}`, `{note_type_id, template_id, name}`) and the one rule every reader resolves them by: the id while it exists, the name only when it does not. Fields are names, not references |
| `logic/rename_reconcile.py` | the reconcile pass: binds null ids, refreshes cached names, diffs `name_snapshot` by id, follows a trigger field or card type rename into a definition with one trigger note type, and marks (`broken_by_rename`) the rest; the snapshot and its collection stamp (the path); the mark readers every run path uses; `unresolved_references` and `trigger_names_not_on_every_note_type` for the editor and the picker |
| `logic/query_terms.py` | the `deck:`, `note:`, `card:` and field terms a search spells that the collection does not have, exact names only; `CollectionNames` is the name list a caller shares across many scans |
| `logic/*_process.py`, `FatalProcessError.py` | the five process-chain steps; `FatalProcessError` aborts a whole run |
| `ui/` | the picker (`pick_copy_definition_dialog`), `edit_staged_definition_dialog` and its parts: `stage_document`, `stage_list`, `stage_editors` (one editor per stage type), `stage_preview`, the triggers and exports panels, `rename_marks_banner` (a definition's rename marks, one Dismiss each: the only way a mark goes besides undoing the rename) |
| `utils/` | `duplicate_note`, `merge_cards`, `move_card_to_deck`, media-folder helpers, `replace_custom_field_values` |

Call chain for a bulk run:

    copy_fields()                         main thread; opens the operation log; CollectionOp
      op: add_custom_undo_entry
        copy_fields_in_background()       per definition: select note ids by SQL
          copy_for_single_trigger_note()  format 1 -> 2 if needed, deck whitelist, session
            run_definition_for_trigger_note()   evaluate the stages, then commit
        update_notes / update_cards / merge_undo_entries   after EACH definition

## Invariants

- **Evaluation never writes to the database.** Stages edit the session's working note and
  card objects and queue file writes; `commit.py` hands the notes and cards to the caller's
  `copied_into_notes` / `copied_into_cards_dict` and puts files on disk. A failure anywhere
  leaves the collection alone, and preview runs the real evaluator and simply does not
  commit. A caller that passes no lists gets no note write, on purpose: on add, Anki saves
  the note; on unfocus, the editor does. Tests therefore assert on returned objects, not on
  a re-fetched note, except at the `copy_fields` level.
- `update_notes`, `update_cards` and `merge_undo_entries` run after **every** definition.
  Later definitions re-fetch from the database, and a skipped merge ends in "target undo op
  not found".
- `card.edited` is an ad-hoc marker attribute; `take_edited_cards` collects the marked
  cards and removes it before `update_cards`.
- One `ExecutionSession` per trigger note. Its overlays keep one working object per note
  and card, so a later stage sees an earlier stage's edit and two references converge.
  Queries do **not** read the overlays: they search the saved collection, so what a query
  matches never depends on unsaved edits. The query cache returns copies, because selection
  consumes the list. `file_cache` lives for one definition run.
- An Edit Note stage reads its right-hand sides from a `duplicate_note` snapshot taken when
  the stage starts, which is what lets one stage swap two fields.
- `copy_fields` must be called on the main thread. The add hook therefore calls
  `copy_for_single_trigger_note` directly and handles undo itself; notes with id 0 are
  filtered out before `update_notes`. Its undo entry lands before Anki's own "Add note"
  entry, a documented limitation.
- On a note that is not in the collection yet (add, or unfocus in the Add dialog), a
  definition whose `effects` reach past that note is not run while typing, and
  `add_note_compatible_only=True` fails any definition that queues a change to anything
  else anyway. The hooks read `effects` and never inspect stages.
- The review handler merges into the recorded Answer Card undo step, folds card-action edits
  into the reviewed card with `merge_cards` before the single `update_card`, then sets
  `fc=1`, or `fc=-1` when sync-only definitions remain. When it refused a marked on-review
  definition for this note, it leaves `fc` as the scheduler set it, so the card stays
  queued for the run after the fix. The sync sweep ends by setting every `fc` of 0 or -1
  to 1, except on the note types a refused definition triggers on
  (`note_type_ids_held_for_rename`).
- **A definition carrying a `broken_by_rename` mark is not run on any path.** Every run
  path asks `copy_fields.refused_for_rename`: the add, review and unfocus hooks before they
  pick a way to run it (`once_per_session=True`, so a hook firing per note cannot fill the
  50-file log cap with one refusal), and `copy_for_single_trigger_note` as the backstop for
  the bulk and sync runs. `call_definition` refuses a marked callee (`evaluator.py`); the
  picker and the browser menu disable it. A new run path must ask too. Read a mark only
  through `broken_by_rename_entries` / `_messages`: stored entries come in more than one
  shape, and anything with a message counts.
- The pass never re-derives a mark, and nothing else does either (`_save_definitions`
  saves marks as they stand). An entry goes only when its object is called `old` again
  (the rename undone) or the user dismisses it in the editor.
- The unfocus handler is a filter hook: return `changed or we_changed`. It runs definitions
  that reach other notes through `copy_fields(trigger_notes=[note])` because the editor's
  note can be ahead of the database, and reloads editors with `loadNoteKeepingFocus`.
- Return contract of a run: `True` is success **or a benign skip** (deck whitelist, unmet
  condition, `skip_block`); `False` aborts the bulk loop.
- Multi-value format-1 strings (note types, decks, tags, trigger fields) are stored as
  `A", "B`, the format `MultiComboBox` emits; format 2 stores JSON arrays under `triggers`.
  Parse the former with a helper that drops `""` (`split_tags` does); a bare split of an
  empty string yields `[""]` and has caused bugs.
- The editor blocks a save while `flow_analysis` reports a problem. The evaluator re-checks
  what hand-edited JSON could break (call depth, a skip the analyser did not expect) and
  fails the run with a message rather than raising.
- The picker keeps `checkboxes` and `definition_note_ids` index-parallel with the config
  list.

## Config

Defaults in `config.json` (`log_level`, `copy_fields_shortcut`, `copy_definitions`); the
user's definitions live in `meta.json`, are large, and are edited only through the dialogs.
`migrate_config()` is gated on the `version` key (`CONFIG_VERSION`, now `0.5.0`): below
0.2.0 it fills in definition guids; below 0.3.0 it converts every definition to stages, all
or nothing, keeping the originals under `pre_stage_migration_copy_definitions`; below 0.4.0
it rewrites references into the current syntax; below 0.5.0 it stores trigger note types,
decks and card-action card types as references with a null id, which the reconcile pass
binds. Stage guids are repaired on every start. The config also holds `name_snapshot`, the
names the referenced ids last had (`logic/rename_reconcile.py`).
The spec's "The startup migration" section is the user-facing account.

A change to the format-2 shape needs: the schema and its validation, `flow_analysis`, the
evaluator, the stage editor and `stage_document`'s defaults and summary, a builder in
`test/definitions.py`, and the spec. Old stored data needs a migration step. Definitions in
the wild contain user code that calls names exposed by the shared `execute_code` and by
the stage environment; renaming one of those names breaks stored definitions silently.

## Tests

`copy_anywhere/test` (a real `Collection` behind a stubbed `mw`, plus Qt tests of the
editor and preview) and `copy_anywhere/test_anki` (running Anki via pytest-anki2) are both
in the root `testpaths`. Reuse, do not reinvent:

- `test/conftest.py`: `col` (fresh collection with note types `CA Vocab`, `CA Sentence`,
  `CA Kanji`, `CA Cloze`, `CA Odd` and a three-level deck tree), `stub_mw`, `media_dir`,
  `logger` (`RecordingLogger` with `has_error` / `has_debug`), autouse log redirection.
- `test/note_types.py`: the names those note types are built from (`VOCAB`, `KANJI`, ...,
  their fields and templates) and `DEFAULT_CONFIG`. Tests import them from here, not from
  `conftest`, which mypy.ini excludes; an import from it resolves to the root conftest.
- `test/definitions.py`: format-1 builders (`within_note`, `destination_to_sources`,
  `source_to_destinations`, `field_to_field`, `card_action`, ...) and one builder per stage
  type plus `staged` for format 2.
- `test/test_readme_screenshots.py` loads every `docs/examples/` definition and checks the
  guide's pictures exist; `COPY_ANYWHERE_SCREENSHOTS=1` regenerates `docs/images/`.
- `test_anki/conftest.py`: `real_mw`, `addon_config`, `restore_stub_mw`.
- Renames: `test/test_rename_reconcile.py` (the pass and `rename_hooks`; helpers `store`,
  `rename_field`, `rename_template`, `saves`, `FakeChanges`, `answer_a_card`) and
  `test/test_rename_editor.py` (the picker's marks, the editor's banner, warnings and
  blockers).

The tests from before format 2 are characterization tests: they pin behaviour, including
behaviour that looks odd, and now run through the migrator. A test that fails after your
change is a behaviour change to justify, not a test to update. Not covered: the `Config`
CRUD methods, `hooks/browser_hooks.py` beyond the disabled entry of a marked definition
(`test/test_rename_editor.py`), `utils/replace_custom_field_values.py`. None of the
UI has been exercised in a real Anki window by the suite; the Qt tests use offscreen
widgets.

## Known rough edges

- Dead code: `ProgressUpdateDef`, and `get_field_values_from_notes` /
  `get_variable_values_for_note` in `copy_primitives` (only the tests call them now);
  `build_action` / `add_action_to_gear` / `add_separator_to_gear` in
  `hooks/browser_hooks.py`; the inline `test()`/`main()` in `logic/regex_process.py` that
  `.vscode/` runs through `test/run_with_setup.py`.
- Within one definition nothing is saved until every trigger note has run, so two trigger
  notes writing the same note or card leave only the later copy (spec: "Only edited cards
  are handed over").
- `config.md` is empty.

## Shared code

Declares `interpolate`, `ui`, `utils`, `anki`, `jp_text_processing`, `word_array`.
`word_array` and `jp_text_processing` are partly there for the shared `execute_code`, which
exposes `kana_highlight`, `decode_word_array` and `format_word_array` to code mode when they
import. The interpolation engine and most widgets this addon uses are shared with
`related_card_disperse`; change them in `anki_shared/`, never under `shared/`, and run that
addon's tests too. The picker's "Use selected notes" / "Use all notes from current search"
pair is `ui.note_source_buttons`, shared with `japanese_note_ai_ops`' multi-op dialog. It
always starts on the selection, and with nothing selected that is no notes: the search has
to be clicked, so a stray Enter never applies to a whole search. The search is the one the
browser last ran, not the box text, and `browser_query()` groups it in parentheses so that an
`or` in it cannot escape the definition's note type and deck terms. Several
helpers here are generic with one owner (`utils/merge_cards`, `move_card_to_deck`,
`duplicate_note`, the media-folder helpers): when another addon needs one, move it per [docs/shared-code.md](../docs/shared-code.md) instead of copying.
