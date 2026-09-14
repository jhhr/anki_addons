# Staged copy definitions (format 2)

A copy definition used to be a mode (`Within note` / `Across notes`), a direction, and a
fixed set of slots: one variable list, one condition, one query, some field writes, some tag
writes, some file writes, some card actions. What each slot *meant* depended on the mode, so
"edit the trigger note and the notes a query found" had no shape at all -- it needed two
definitions and an ordering between them.

Format 2 replaces that with an ordered program of **stages**. A stage names the note it
reads and the note it writes. A stage that produces a value names it, and later stages in
scope can use it.

**Status.** Format 2 is the only format. Anki converts every stored definition on the first
start after this release, and there is one executor and one editor from there on.

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
stage runs. `exports` is authored: each one is `{ "name", "stage_guid", "result" }`, naming
a root stage and which of its results to take. Only a `call_definition` binds more than one
result -- one per declared output -- and an export that leaves `result` out takes the stage's
single result, which is what every export written before call outputs could be exported does. `effects` is derived -- the flow analyser computes it,
transitively through called definitions -- and is never edited by hand; a definition with no
readable `effects` is treated as not add-note compatible, which is the safe direction.

Because it is transitive, a definition's `effects` can go stale when a definition it calls
changes. So saving, adding or removing any definition recomputes `effects` for all of them,
not just the one that changed.

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
* **`skip_block` names the block the stage is in.** In a loop body it ends that iteration
  and the loop carries on; in a `then` or `else` branch it ends the branch and the stage
  after the condition runs; at the root it ends the definition, and what ran before it still
  counts. Only the last of those leaves root results maybe-unset, which is why only that one
  can invalidate an export -- and only from the skipping stage on. Its own result counts as
  maybe-unset too, since a stage that skips never declared one; a result produced before it
  is set whichever way the skip goes and stays exportable.
* **Add-note compatibility is a flag, not an inspection.** A definition that writes to any
  note but the trigger, or to any card, cannot run against a note that has not been added
  yet. The hooks read `effects.add_note_compatible`; the commit refuses anything else
  regardless, in case the JSON was edited by hand. It defers such a definition rather than
  dropping it: the add hook runs it once the note exists, under its own undo entry. The
  editor warns about the combination and saves it anyway, since all three of those read
  the stored flag and none of them the editor.

## The startup migration

`migrate_config()` runs before anything can read a definition. It converts every stored
definition to stages, fills in each one's derived `effects`, and bumps the config version.

It is all or nothing. If any definition cannot be converted -- one naming no copy mode, say,
which format 1 could not have run either -- nothing is converted, the version stays where it
was, and the reason is printed. The next start tries again. That is deliberate: saving the
definitions that did convert would drop the one that did not from a config you can still
open and fix by hand.

**The originals.** The run that converts them writes the format-1 definitions to
`pre_stage_migration_copy_definitions` in the addon config first, and never touches that key
again -- so it holds what you had before the upgrade, not the result of any later run. It is
kept for one release.

It is there to read, and to convert again: copying that list back over `copy_definitions` and
setting `version` to `0.2.0` makes the next start migrate them afresh, which is what you want
once a migration bug has been fixed. It is not a way to keep running format 1 -- there is no
executor for it any more, so anything you put in `copy_definitions` is migrated before it
runs. If a definition migrates to something you did not want, fix the stages it produced, or
read the original here and build it again.

A definition that somehow reaches the editor still in format 1 is converted on the way in
by the same pure migrator, and one that cannot be converted is reported rather than opened.

## What migration changes on purpose

* Across-note selection searches notes rather than cards, so a note with two matching cards
  is selected once. `select_card_by: None` becomes `first` and follows search order rather
  than the reverse of the card search.
* `Least_reps` is gone; it migrates to `random` and the migrator says so.
* File writes no longer translate newlines on Windows.
* A definition with no copy mode is reported rather than raising out of the whole operation.
* Trigger filtering runs before any stage, so a note the deck whitelist rejects no longer
  pays for the definition's variables first.
* A file's name and its `__Dest__` spelling of the same field now agree. Format 1
  interpolated the name over the live note but `__Dest__` over a copy taken before anything
  ran; the file is written by a stage after the one that writes the field, and a stage reads
  what the stages before it did.
* A definition that both fills a field and acts on a card fills the field a moment later
  when a note is being added. Format 1 ran it on every unfocus in the Add dialog and let the
  card action quietly do nothing, because a note with id 0 has no cards. Format 2 has one
  rule for that -- a definition touching another note or any card waits until the note
  exists -- so the whole definition is deferred to the moment the note is saved rather than
  running as you type. The editor says so rather than refusing the definition: the rule is
  enforced by the hooks and again by the commit, both of which read the stored `effects`, so
  what the editor allows changes nothing about what runs. A definition triggered only by
  unfocus-while-adding has nothing to defer to and is simply not run there; turning on
  "Run when adding a new note" is what gives it a moment to run in.

**What a migrated field write keeps.** Format 1 asked three questions per field write that
format 2 asks once per definition: which editor fields trigger it, whether it runs on unfocus
while editing, and whether it runs on unfocus while adding. The migrator records the answers
on the write itself, as `unfocus_trigger_fields`, `unfocus_when_edit` and `unfocus_when_add`,
and the executor narrows an unfocus run by them. A write that has none of those keys -- which
is every write the stage editor produces -- is not narrowed: `triggers.on_unfocus` has already
decided, for the definition as a whole, that this unfocus should run it.

What migration deliberately does *not* change is a definition that format 1 refused to run.
A `select_card_by` that is missing or unreadable selected nothing and said why, and it still
does: mapping it to "take the first note" would make a definition that has never written a
note start writing one. A refusal also stops the block, exactly as an empty result does.
Format 1 had one early return for both -- it was what kept a destination-to-sources
definition from interpolating an empty source list and wiping the fields it was meant to
fill -- so a query that cannot run goes through the stage's `if_empty` policy rather than
handing back an empty list and letting the writes below it run.

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
  selected and is marked in red on both rows. Nothing is silently rewritten. Moving a stage
  keeps its export for the same reason: a stage moved into a loop cannot be exported, so its
  row is marked rather than dropped, and moving it back out is all it takes to restore it.
* A condition says which of its two forms it is. *Match it as an Anki search against a note*
  is what a migrated copy condition is -- a search, run against the note the row names -- and
  it has no code form; turning it off leaves the ordinary expression a condition authored
  here holds. *Only check it during a sync* is format 1's `condition_only_on_sync`: outside
  a sync the condition is not checked and the branch runs.
* **Save** is disabled while the analyser has a complaint, and the complaints are listed
  under the stage list with the path of the stage each one belongs to. Warnings (several
  trigger note types, file writes outside undo) do not block a save.
* The same panel says whether the definition can run while a note is being added, which is
  the flag stored in `effects` and the one the add hook checks.

## The preview

The right half of the editor runs the definition against one note and reports what it would
do, without doing any of it. Pick a note from the list -- it offers notes the definition's
triggers would consider, narrowed by whatever you add to the search box -- and press **Run
preview**.

It is the same evaluator, not a simulation of it: the searches are real, code and process
chains run under the same restrictions, called definitions run, and the depth and cycle
checks still apply. The only differences are that the mutations are recorded rather than
committed, and that nothing reaches the media folder. A file read after a previewed write
sees the previewed content, so a read-modify-write chain previews as it would run.

The trace lists every stage in the order it ran, with a mark for what happened to it.
Selecting one shows what that stage could see when it started, what it produced, how long
it took and what it would have changed. A stage inside a loop ran once per pass, so its
events appear under an **Iteration** heading each; opening that stage in the list selects
its last pass. Values longer than about 500 characters are cut in the trace, and the full
value is not kept: a previewed run holds one summary per value, not a second copy of the
collection.

Nothing is rerun as you type -- a query or a nested call is not cheap enough for that -- so
an edit, or choosing a different note, marks the trace as stale until you run it again.

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
| `logic/preview.py` | a read-only run, its trace, and the trigger-note search |
| `configuration.py` | the config, the trigger accessors, and the startup migration |
| `ui/stage_document.py` | the editable stage tree: add, move, duplicate, delete, save readiness |
| `ui/stage_editor_context.py` | one scope per stage, turned into its menus and Add Stage entries |
| `ui/stage_edit_state.py` | the state object that lets the older shared widgets be reused |
| `ui/value_expression_editor.py` | the one editor every value expression uses |
| `ui/stage_editors.py` | one editor per stage type |
| `ui/stage_list.py` | the ordered, indented stage list |
| `ui/stage_triggers_editor.py` | the trigger settings at the top |
| `ui/stage_exports_editor.py` | the definition-level exports panel |
| `ui/stage_preview.py` | the preview pane: note picker, run control, trace |
| `ui/edit_staged_definition_dialog.py` | the dialog, and what blocks a save |
