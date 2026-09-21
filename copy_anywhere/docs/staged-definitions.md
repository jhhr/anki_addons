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
    "note_types": [{ "id": 1699999999999, "name": "Vocabulary" }],
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
stage runs. A note type, a deck and a card type are each stored as `{ "id", "name" }` -- the
objects Anki gives a stable id. **The id wins where it still exists**, so renaming one in
Anki does not stop the definition; the name is what is looked up when the id is gone, which
is what makes a definition written for a note type you have not created yet, or one shipped
as an example with `"id": null`, still bind. A card type takes both halves,
`{ "note_type_id", "template_id", "name" }`, because a template id is only unique within its
note type, and its `name` keeps the display form `"<NoteType><::><CardType>"`. A bare string
is still read wherever a reference is expected. **Field names are not references**: a field
is what you type in expressions, queries and code, so it is stored as the name you spelled.

`exports` is authored: each one is `{ "name", "stage_guid", "result" }`, naming
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
`{{M}}` for a result. The note-value and card-value keys format 1 used are read through the
note that holds them -- `{{trigger.__Note_ID}}`, `{{note.Recognition__Card_Due}}`. A bare
name is a result, or one of the two values the run itself supplies
(`{{__Target_Notes_Count}}`, `{{__Query_Note_Index}}`), and anything else fails the stage
saying which name it was: a reference that resolves to nothing is a mistake, not an empty
string. The analyser says the same from the same text, so the save is refused rather than
the definition failing once per note later. Interpolating a note, a card or a list is a
validation error: how several values become one piece of text is the definition's decision,
so it has to say so with a reduce or with code.

Migration rewrites format 1's unqualified names into this syntax, so nothing stored still
speaks format 1: `{{Word}}` becomes a reference to whichever note that stage read it from,
`{{__Dest__Word}}` to the one it wrote. A migrated definition does keep format 1's variable
names, which did not have to be identifiers; a missing name, a reserved binding name and the
`__` prefix are refused on a migrated definition too, since the runtime owns those.

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
  writes are applied after the collection changes and are outside Anki's undo. A media
  write that fails (a full disk, a read-only folder) fails the run and names the file: the
  note and card changes are kept, the files queued before it are on disk, and it and the
  files queued after it are not written.
* **Files are UTF-8, no BOM, no newline translation**, and a filename resolves inside the
  media folder -- a path separator or a `..` segment is refused. There is no append mode:
  `read_file`, build the new content, `write_file` with `overwrite: true`.
* **A file this run has queued counts as already there.** Both `skip_if_exists` and
  `overwrite: false` ask about the pending write as well as the media folder, so a second
  stage naming a file an earlier one wrote skips or refuses rather than replacing it. Asking
  only about the folder made the answer depend on whether a previous run had committed: the
  same two stages overwrote silently the first time and refused the second.
* **A called definition is isolated.** It gets its trigger note and nothing else of the
  caller's -- no variables, no lists, no loop bindings. It shares the working notes, cards
  and files, and returns only what it declares in `exports`. Call cycles are refused, and a
  chain deeper than 32 is refused whatever the guids say. A disabled call stage, or one under
  a disabled stage, does not close a cycle: disabling the call is how a user breaks one.
* **`skip_block` names the block the stage is in.** In a loop body it ends that iteration
  and the loop carries on; in a `then` or `else` branch it ends the branch and the stage
  after the condition runs; at the root it ends the definition, and what ran before it still
  counts. Only the last of those leaves root results maybe-unset, which is why only that one
  can invalidate an export -- and only from the skipping stage on. Its own result counts as
  maybe-unset too, since a stage that skips never declared one; a result produced before it
  is set whichever way the skip goes and stays exportable.
* **Only a migrated copy condition skips the trigger note.** Format 1 evaluated its copy
  condition before anything else and skipped the whole note when it did not match, so the
  migrator marks that stage `unmatched_skips_trigger` and the executor honours it: nothing
  is committed and the note is counted as skipped rather than copied into. A condition
  authored here is an ordinary branch however its predicate is matched -- a non-match takes
  the `else`, empty or not, and the definition carries on. The marker rather than the
  predicate kind, because the two are different questions: inferring it from "it is a search
  and has no `else`" made a condition anywhere but the outermost position reach out of its
  block and discard what earlier stages had already written, while reporting success. The
  marked stage is a third way the rest of the root block may not run, so it invalidates
  exports from itself on, exactly as the two `skip_block` policies do -- and unlike them it
  is not scoped to its block: marked inside a loop body or a branch it still ends the whole
  definition, so the root stage that holds it is the one exports are refused from.
* **A stage that only feeds one migrated field write shares its gates.** Migrating
  Destination-to-sources moves each write's per-source read in front of it, into a list, a
  loop and a reduce, and that is where the work is -- the code or the process chain runs once
  per source note there. So the migrator copies the write's `unfocus_trigger_fields` and its
  two `unfocus_when_*` flags onto those stages, and a `write_if: "empty"` along with the
  field it asks about as `write_if_field`, and the executor skips them the same way it
  skips the write. Without it an unfocus of one field evaluated every other write's
  right-hand side once per source note, and so did a write whose field was already filled,
  and a raising one failed the definition, discarding the writes that would have been
  applied.
* **Add-note compatibility is a flag, not an inspection.** While a note is being added the
  add can still be cancelled, so a definition may edit only that note: its fields and its
  tags. Writing to any other note, acting on a card that already exists, or writing a file
  would outlive a cancelled add, so any of the three disqualifies the definition; the add
  hook writes those changes itself, once the add is past cancelling. The hooks read
  `effects.add_note_compatible` (a format-1 definition answers from its mode, its card
  actions and its file writes: its card actions reach the trigger's own cards except in
  Source-to-destinations); the commit refuses anything but trigger-note changes from a
  definition that claims compatibility, in case the JSON was edited by hand. Such a
  definition is not dropped: the add hook runs it after the trigger-only ones, under its
  own undo entry, and the unfocus hook skips it while a note is being added. The Add dialog
  is the one place the add can still be cancelled, so both hooks arm that backstop on a new
  note; it refuses by failing the run and logging, which at the default `log_level` is what
  the operation log holds. The editor warns and saves the definition anyway, since all
  three of those read the stored flag and none of them the editor.
* **A card action on the note being added does not run, and does not disqualify it.** The
  add hook fires before the note is in the collection: it has id 0 and no cards, so an Edit
  Note stage targeting the trigger applies its field writes and skips its card actions with
  a log line, and nothing runs them later. A skipped action leaves nothing behind for a
  cancelled add to strand, so it is not one of the three things above. Card actions on
  other notes' cards, and edits to other notes, are: they really would run, and that is why
  they disqualify the definition. The editor names the stage whose action will not run.
* **Reading the trigger's cards while it is being added is allowed.** Reading leaves
  nothing behind for a cancelled add to strand, so none of it is refused. A card-value key
  on a note with no cards -- `{{trigger.Recognition__Card_Due}}` -- answers from a new
  card's defaults rather than being reported as an unknown reference, exactly as it did in
  format 1; on a cloze note with no cards yet it answers empty. A query for the cards of a
  note that is not in the collection finds none, so a loop over its results runs zero times.
  The editor says nothing about any of this.

## Following a rename in Anki

Anki has one rename hook -- for a field -- and it fires while the Fields dialog is still
open, so a cancelled dialog leaves it having lied; a note type, a card type and a deck
rename fire nothing at all, and a rename made on another device arrives as "something
changed". So no rename hook is used. Instead, the objects with a stable id are stored as
references and resolved by id (above), and one **reconcile pass** keeps the rest honest.

The pass runs when the collection is opened and after any operation that reports changing a
note type or a deck -- which covers every dialog that can rename one, its undo and its redo,
and the everything-changed operation a sync ends with. It:

1. **binds** a reference whose `id` is null but whose name resolves, which is how an example
   definition, a hand-written one and a definition kept for a note type you had not made
   yet pick up their ids;
2. **refreshes** the cached `name` of every reference whose id still resolves, so a picker
   never shows a name the collection has stopped using;
3. **follows a renamed field or card type** of a trigger note type into that definition's
   field slots -- a field write on the trigger, the unfocus lists, each write's trigger
   fields -- and into every `{{trigger.Word}}` and `{{trigger.Recognition__Card_Due}}` token
   in an expression's **text**. The whole rename map is applied in one step, so two fields
   that swap names swap correctly;
4. **reports** everything else: a reference that resolves to nothing, an object that has been
   deleted, a field or template with no id (note types saved before Anki 23.10 can have
   them, and nothing can follow a name with no id behind it), and code that still mentions
   an old name.

What it never rewrites is a search term, `selection.sort_field`, or code. A query is free
text read by Anki's own grammar -- `col.replace_in_search_node` swaps every term of a kind
and so cannot rename one deck inside a query naming two -- and `note['Word']` is a spelling
of a field name that no `{{...}}` rewrite can see. Those are reported and fixed by hand.

**Where the report reaches you.** The pass writes all of it into one operation log, which
you only see with the log level turned up, so the three things you can act on are also put
where you already look:

- a reference that resolves to nothing is shown in the editor under the name it was
  written with, marked `(not found)`, and **blocks the save** until you pick another or
  remove it. Picking a live entry from the same box clears it;
- a query stage shows an amber note under the search naming what it spells that this
  collection does not have -- `deck:`, `note:` and `card:` terms and field searches, exact
  names only: a term with a wildcard, a regex or a `{{...}}` reference in it is left alone.
  It is a note, not a blocker: a query may name something you have not made yet;
- the definition list marks a definition the last pass could not resolve, with the names in
  its tooltip.

A sort field is still not rewritten and still sorts a note that lacks it as empty -- a
query legitimately mixes note types -- but a run where *no* selected note had the field
logs one warning, because then the sort did nothing at all.

Both names of a rename come from `name_snapshot` in the addon config: per referenced note
type id, its name and the names of its fields and templates by their ids, plus a name per
referenced deck id. It is written by the same pass and refreshed whenever definitions are
saved, so it is never older than the last save, and a changed name under an unchanged id is
what a rename *is*. The pass writes the config only when something changed.

## The startup migration

`migrate_config()` runs before anything can read a definition. It converts every stored
definition to stages, rewrites format 1's references into the current syntax, fills in each
one's derived `effects`, and bumps the config version.

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
* A definition that both fills a field and acts on a card says out loud that the card
  action does nothing while the note is being added. Format 1 ran it on every unfocus in
  the Add dialog and let the card action quietly do nothing, because a note with id 0 has
  no cards. Format 2 has one rule for that -- while a note is being added a definition may
  edit only that note -- so the field write still runs as you type, and the card action on
  the new note is a logged skip; nothing runs it later. The editor says so, naming the
  stage, rather than refusing the definition: the rule is enforced by the hooks and again
  by the commit, both of which read the stored `effects`, so what the editor allows changes
  nothing about what runs. A definition triggered only by unfocus-while-adding runs there
  just as it did, as long as the only thing it edits is the note being added; one that also
  writes to another note, to a card that already exists, or to a file is skipped, and
  turning on "Run when adding a new note" is what gives it a moment to run in.

**What migration does to a format-1 expression.** The stages record which note format 1
read an unqualified name from and which one `__Dest__` meant, and the migrator spends that
record on the references themselves: `{{Word}}` comes out as `{{trigger.Word}}` or
`{{note.Word}}`, `{{__Dest__Word}}` as the note the stage writes, and a card value keeps its
card type name (`{{trigger.Recognition__Card_Due}}`). A name the migrator itself bound --
a variable's result, a synthesized join -- stays bare, because it is a result; so do
`{{__Target_Notes_Count}}` and `{{__Query_Note_Index}}`, which the run supplies. So a
migrated expression is shown, edited and judged exactly as an authored one: the editor's
menu offers the stage's scope, and what the menu offers is what is already in the box.
Code is rewritten the same way, but only its `{{...}}` references: the code itself then
runs as format-2 code does, with every binding in scope under its own name, `note` meaning
the stage's note or the loop's, and read-only facades where format 1 handed it the note
object. Code that reached for a note by some other means, or wrote through one, is yours to
check by hand. A config an earlier version already staged is rewritten in place by the
`0.4.0` config migration.

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
* A reference nothing answers to is a complaint against the stage that holds it, not a
  surprise at run time: a bare name that is neither a result in scope nor one of the two
  values the run supplies, and a `{{trigger.X}}` naming something none of the definition's
  trigger note types has a field for, nor a note value or card value by name. Only the
  trigger can be checked that far -- a note from a query holds whatever the query matched,
  so a name read off one of those is still the run's to report.
* A reference that stopped resolving -- to a result whose stage you deleted, say -- stays
  selected and is marked in red on both rows. Nothing is silently rewritten. Moving a stage
  keeps its export for the same reason: a stage moved into a loop cannot be exported, so its
  row is marked rather than dropped, and moving it back out is all it takes to restore it.
  Renaming a top-level result carries its export to the new name -- an export named after
  the result is renamed with it, one you named yourself keeps that name.
* A condition says which of its two forms it is. *Match it as an Anki search against a note*
  is what a migrated copy condition is -- a search, run against the note the row names -- and
  it has no code form; turning it off leaves the ordinary expression a condition authored
  here holds, and drops the copy-condition marker with it, since a stage that is not a search
  is not format 1's condition either. Choosing the search form hides the code toggle rather
  than turning it off: a code predicate comes back when the form is turned off again, and a
  save made while the form is on stores the text in the box, since that is what the search
  runs. A condition whose text predicate is empty is reported, on either form -- as an
  expression it would never hold, as a search it is refused at run time. *Only check it
  during a sync* is format 1's `condition_only_on_sync`: outside a sync the condition is not
  checked and the branch runs.
* A reduce says which of its two forms it is, and shows only the controls that form reads.
  *Join them into one text* is what every migrated reduce is, and its separator is format 1's
  `select_card_separator`; *fold them with an expression* is the one that runs the starting
  value and the per-item expression. Showing both at once meant a migrated join offered two
  expression editors the executor ignores and no way to see the separator at all.
* A migrated field write says which editor fields trigger it, beside its *write if* combo.
  Format 1 asked that per write and the executor still honours the answer, so leaving it off
  the row made it the one thing about a write that could not be changed -- and since tags and
  card actions in the same stage are not gated, a write skipped by it left the note tagged
  and saved with the field still empty. A write authored here has no such list: format 2
  watches fields for the definition as a whole.
* Deleting a stage takes its export with it, and a call stage whose callee is missing keeps
  the results it binds. Both are the same rule the marked rows above follow: the panel is
  rebuilt from the definition, so anything it cannot currently offer has to be preserved
  rather than written back as "not wanted" -- otherwise the choice disappears on the way past
  instead of when the user makes it.
* **Save** is disabled while the analyser has a complaint, and the complaints are listed
  under the stage list with the path of the stage each one belongs to. Warnings (several
  trigger note types, file writes outside undo) do not block a save.
* The same panel says whether the definition can run while a note is being added, which is
  the flag stored in `effects` and the one the add hook checks, and it has up to two amber
  notes to go with it. One is about what is *impossible*: a card action on the note being
  added has no card to reach, so it will not run for that note and nothing runs it later --
  the stage is named. The other is about what is *forbidden*: an edit to another note, to a
  card that already exists, or to a file would outlive a cancelled add, so that work
  happens after the add rather than as part of it (or, for an unfocus-only trigger, not at
  all), and the stages responsible are listed after "Because of". They are independent: a
  definition can earn both, one, or neither, and neither of them blocks the save.

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
