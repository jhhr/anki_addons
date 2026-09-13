# Staged copy definitions (format 2)

A copy definition used to be a mode (`Within note` / `Across notes`), a direction, and a
fixed set of slots: one variable list, one condition, one query, some field writes, some tag
writes, some file writes, some card actions. What each slot *meant* depended on the mode, so
"edit the trigger note and the notes a query found" had no shape at all -- it needed two
definitions and an ordering between them.

Format 2 replaces that with an ordered program of **stages**. A stage names the note it
reads and the note it writes. A stage that produces a value names it, and later stages in
scope can use it.

**Status.** The executor runs format 2 and nothing else: a stored format-1 definition is
migrated on the way in, every run. A new definition is created as a staged one, and the
stage editor writes format 2 into the config. An existing format-1 definition still opens
in its own editor and stays format 1 until you press **Save and convert to stages** there,
which rewrites it and reopens it as stages. A config holding both formats is normal and
works: the hooks, the picker and the bulk operation read a definition's trigger settings
through one set of accessors that understands either shape.

Still to come: the execution preview (a read-only run with a per-stage trace), and the
startup migration that converts every stored definition at once and retires the format-1
editor.

## Shape

```json
{
  "guid": "definition-guid",
  "format_version": 2,
  "definition_name": "Example",
  "triggers": {
    "note_types": ["Vocabulary"],
    "deck_names": [],
    "include_subdecks": false,
    "on_sync": false,
    "on_add": false,
    "on_review": false,
    "on_unfocus": { "edit_fields": [], "add_fields": [] }
  },
  "stages": [],
  "exports": [],
  "effects": { "add_note_compatible": true }
}
```

`triggers` decides which notes the definition considers; that filtering happens before any
stage runs. `exports` is authored. `effects` is derived -- the flow analyser computes it,
transitively through called definitions -- and is never edited by hand; a definition with no
readable `effects` is treated as not add-note compatible, which is the safe direction.

Every stage carries `guid`, `type`, an optional `name` for the editor, and `enabled`. An
unknown `type` makes the definition invalid rather than being skipped.

## Value expressions

Everything that computes text uses one shape:

```json
{ "mode": "text", "text": "{{trigger.Word}}", "code": "", "process_chain": [] }
```

Text mode interpolates; code mode runs the restricted executor and its result is validated by
whatever consumes it. The process chain runs after either, and is the same chain format 1
had.

References are qualified: `{{trigger.Word}}`, `{{note.Meaning}}`, `{{card.deck_name}}`,
`{{M}}` for a result. A reference that names no binding falls through to the note-value and
card-value keys format 1 used (`{{__Note_ID}}`, `{{Recognition__Card_Due}}`). Interpolating a
note, a card or a list is a validation error: how several values become one piece of text is
the definition's decision, so it has to say so with a reduce or with code.

Migrated expressions carry `syntax_version: 1` and keep format 1's unqualified names. New
expressions must not rely on that.

Code mode gets immutable facades -- `NoteFacade`, `CardFacade`, and iterable, indexable,
sliceable `NoteListFacade` / `CardListFacade` -- plus `find_notes`, `find_cards` (raw id
lists from the saved collection) and `get_note`, `get_card` (ids to facades). Assigning
through a facade raises: every change goes through a stage, which is what keeps it visible
to the preview and the commit.

## Stage types

| type | what it does |
| --- | --- |
| `variable` | computes one value and names it |
| `note_query` | `find_notes`, producing a `NoteList` |
| `card_query` | `find_cards`, producing a `CardList` |
| `edit_note` | writes fields, tags and note-level card actions to one named note |
| `edit_card` | applies card actions to one named card, with no card type selector |
| `read_file` | reads one media file into a `Text` result |
| `write_file` | replaces one media file |
| `list_variable` | declares an empty list |
| `store` | appends one value to a declared list |
| `for_each_note` | a child scope per note in a `NoteList` |
| `for_each_card` | a child scope per card in a `CardList`, binding the card's note too |
| `reduce` | folds a list into one result |
| `condition` | runs exactly one of its two branches |
| `call_definition` | runs another definition with a chosen note as its trigger |

Loops bind `index` (one-based) and `count` alongside their item. Body-local results are
discarded after each iteration and branch-local results do not escape their branch, so an
outer list plus `store` is how a loop reports anything back.

## Rules worth knowing before writing one

* **Queries read the saved collection.** A field a stage wrote but nobody has saved yet
  cannot change which notes a query matches. Its value is still available for building the
  query. Without that rule, what a definition did would depend on when a flush happened.
* **Reads see pending edits.** A later stage, and code, read the working note, so an edit an
  earlier stage made is visible. Within one `edit_note` stage every right-hand side reads the
  note as it was when the stage started, which is what lets one stage swap two fields.
* **Two references to one note converge.** However a note was reached, a run holds one
  working copy of it, so two stages editing it both land.
* **Nothing is written until the definition finishes.** Notes, cards and files are committed
  together once the whole definition has run; a failure anywhere commits none of it. File
  writes are applied after the collection changes and are outside Anki's undo.
* **Files are UTF-8, no BOM, no newline translation**, and a filename resolves inside the
  media folder -- a path separator or a `..` segment is refused. There is no append mode:
  `read_file`, build the new content, `write_file` with `overwrite: true`.
* **A called definition is isolated.** It gets its trigger note and nothing else of the
  caller's -- no variables, no lists, no loop bindings. It shares the working notes, cards
  and files, and returns only what it declares in `exports`. Call cycles are refused, and a
  chain deeper than 32 is refused whatever the guids say.
* **Add-note compatibility is a flag, not an inspection.** A definition that writes to any
  note but the trigger, or to any card, cannot run against a note that has not been added
  yet. The hooks read `effects.add_note_compatible`; the commit refuses anything else
  regardless, in case the JSON was edited by hand.

## What migration changes on purpose

* Across-note selection searches notes rather than cards, so a note with two matching cards
  is selected once. `select_card_by: None` becomes `first` and follows search order rather
  than the reverse of the card search.
* `Least_reps` is gone; it migrates to `random` and the migrator says so.
* File writes no longer translate newlines on Windows.
* A definition with no copy mode is reported rather than raising out of the whole operation.
* Trigger filtering runs before any stage, so a note the deck whitelist rejects no longer
  pays for the definition's variables first.

## The editor

The stage editor is one column: the trigger settings, then the stages in the order they
run, then the exports.

* A stage row shows what it does in one line and expands into its own editor. The controls
  inside are the ones format 1 used -- the same interpolated text edit, code editor,
  process chains, tag and card-action editors.
* **Add stage** offers the fourteen types. *Edit Card* appears only where a card binding is
  in scope, because it names one card and there would be nothing to name.
* The `⋮` menu on a row duplicates it, deletes it, or moves it into another block; `↑`/`↓`
  reorder it among its siblings. There is no drag-and-drop; the move menu does the same
  work, including moving a stage into or out of a loop or a branch.
* Every text edit's right-click menu is built from the analyser's record of what is in
  scope *at that stage*. A loop body offers the loop's note; the stage above the loop does
  not. Lists never appear, because no interpolation could turn one into text.
* A reference that stopped resolving -- to a result whose stage you deleted, say -- stays
  selected and is marked in red on both rows. Nothing is silently rewritten.
* **Save** is disabled while the analyser has a complaint, and the complaints are listed
  under the stage list with the path of the stage each one belongs to. Warnings (several
  trigger note types, file writes outside undo) do not block a save.
* The same panel says whether the definition can run while a note is being added, which is
  the flag stored in `effects` and the one the add hook checks.

## Where the code is

| file | what it holds |
| --- | --- |
| `logic/definition_schema.py` | format-2 types, discriminators, structural validation |
| `logic/definition_migration.py` | the pure format-1 -> format-2 migrator |
| `logic/flow_analysis.py` | scopes, result types, effects, exports, call cycles |
| `logic/execution/context.py` | the session, its overlays and caches, trace events |
| `logic/execution/facades.py` | the immutable note and card views code mode gets |
| `logic/execution/expressions.py` | both expression syntaxes and the process chain |
| `logic/execution/actions.py` | the leaf stage handlers |
| `logic/execution/evaluator.py` | block dispatch and the structural stages |
| `logic/execution/commit.py` | the collection and preview committers |
| `logic/execution/runner.py` | one definition against one trigger note |
| `logic/copy_primitives.py` | interpolation, process chains, card actions, progress |
| `logic/legacy_executor.py` | format-1 behaviour, kept as the migration's oracle |
| `ui/stage_document.py` | the editable stage tree: add, move, duplicate, delete, save readiness |
| `ui/stage_editor_context.py` | one scope per stage, turned into its menus and Add Stage entries |
| `ui/stage_edit_state.py` | the `EditState`-shaped shim that lets the format-1 widgets be reused |
| `ui/value_expression_editor.py` | the one editor every value expression uses |
| `ui/stage_editors.py` | one editor per stage type |
| `ui/stage_list.py` | the ordered, indented stage list |
| `ui/stage_triggers_editor.py` | the trigger settings at the top |
| `ui/stage_exports_editor.py` | the definition-level exports panel |
| `ui/edit_staged_definition_dialog.py` | the dialog, and what blocks a save |
