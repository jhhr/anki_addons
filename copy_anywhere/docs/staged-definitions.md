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
note that holds them -- `{{trigger.__Note_ID}}`, `{{note.Recognition__Card_Due}}`. A card
value names its card type in front (`Cloze 2__Card_Due` for a cloze card), or none to read
the note's only card (`{{note.__Card_Due}}`), which fails the stage on a note with several;
the spelling says which is meant, whatever note types the definition triggers on. Through a
card binding there is no card type to name: `{{card.__Card_Due}}`, or one of the card
properties code sees. The references inside a cloze (`{{c1::{{trigger.Word}}}}`) are
references like any other; the cloze marker is kept around what they resolve to, and one
that is never closed is refused. A bare
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

Every binding in scope is a name in the code. Besides them, `note` is the stage's own note
-- the note an `edit_note` writes, as the stage started; the trigger for every other stage
-- unless a binding is called `note`, as a loop's item is, which then wins. `cards` is the
cards of whichever note `note` turned out to be, so inside a loop over notes it is the loop
note's; a binding called `note` that holds no note leaves it the stage's note's, and a
binding called `cards` wins. They are fetched only if the code reads them, once per
evaluation. `destination` is the note an `edit_note` writes, as the stage started, and the
trigger everywhere else, a `write_file` included.

A card action's code is older and runs apart from all this, exactly as format 1 ran it:
`note` is a view of the note the stage writes offering only `note[field]` and `keys()`,
`cards` that note's cards read from the collection, and no bindings, no `destination`, no
`get_note`, and no `{{...}}` substitution.

## Stage types

| type | what it does |
| --- | --- |
| `variable` | computes one value and names it |
| `note_query` | `find_notes`, producing a `NoteList` |
| `card_query` | `find_cards`, producing a `CardList` |
| `select_note` | picks one note of a `NoteList` by its position, producing a `NoteRef` |
| `select_card` | picks one card of a `CardList` by its position, producing a `CardRef` |
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

`select_note` is how one note of a list becomes a note the rest of the definition can edit
and read, with no loop, and `select_card` does the same for one card of a card list. The
`index` is a value expression: text that reads as a whole number (0 is the first, 1 the
second, and a negative number counts from the end), or code that returns one, or `None` for
nothing. Text that comes out blank -- an empty field it read -- is nothing too, the way
`None` is; an index box left empty is refused at save. The code gets the list as `notes` or
`cards` as well as under its own name; in `select_card` that makes `cards` the list being
picked from rather than the note's cards, as any binding called `cards` would. When nothing
is at the index, `if_missing` decides: `empty`, the default, binds "nothing selected" -- a
reference to one of its values reads as empty text, code sees `None` (and, for a result
called `note`, no `cards`), an `edit_note` or `edit_card` on it does nothing, and a call or
a search condition given it fails naming it -- `skip_block` stops the rest of the block, and
`error` fails the definition. The result may be called `note` or `card` respectively, the
one reserved name each may take, as a loop's item may; `note` is what a migrated one-source
definition calls it.

## Rules worth knowing before writing one

* **Queries read the saved collection.** A field a stage wrote but nobody has saved yet
  cannot change which notes a query matches. Its value is still available for building the
  query. Without that rule, what a definition did would depend on when a flush happened.
* **Reads see pending edits.** A later stage, and code, read the working note, so an edit an
  earlier stage made is visible. Within one `edit_note` stage every right-hand side reads the
  note as it was when the stage started, under whatever name it reads it by -- the target's
  own, or a query's or loop's that found the same note -- which is what lets one stage swap
  two fields.
* **Two references to one note converge.** However a note was reached, a run holds one
  working copy of it, so two stages editing it both land.
* **Nothing is written until the definition finishes.** Notes, cards and files are committed
  together once the whole definition has run; a failure anywhere commits none of it. Files
  reach the disk only after the notes and cards they go with are saved, and are outside
  Anki's undo: a bulk run or a hook collects them and writes them after its `update_notes`,
  and a later trigger note of the same bulk run reads the ones already collected as if they
  were on disk. A media write that fails (a full disk, a read-only folder) is reported with
  the file's name: the note and card changes are kept, the files queued before it are on
  disk, and it and the files queued after it are not written.
* **A run that commits nothing leaves the trigger note as it was.** The trigger is the one
  note the run edits in place: it is the caller's own object, and the Add dialog and the
  editor save that object whatever the run's result. So a run that fails, is cancelled, is
  skipped by a copy condition or is refused by the add-note backstop puts the trigger's
  fields and tags back to what they were when this definition started; an earlier
  definition's writes to it stay. A run that commits never restores, the media failure above
  included.
* **Only edited cards are handed over, and a later trigger note's edit wins.** A run hands
  its caller the notes it wrote and the cards a card action changed; a card it only looked
  at, or whose note it wrote, is not handed over. A bulk run saves those once per
  definition, after every trigger note has run, and each trigger note starts from the saved
  note, so when two trigger notes of one run write the same note, or edit the same card, the
  later one's copy replaces the earlier one's and the earlier edit is lost, as in format 1.
  A trigger note that writes a note but leaves its card alone does not undo another one's
  edit of that card. This is an accepted limitation: saving after every trigger note was far
  slower and made the result depend on their order, and merging two copies of a note or a
  card edit by edit is more than is worth maintaining. A definition that gathers many notes
  into one is better written from the one note's side -- a query and a loop over the others
  -- so that only one trigger note writes it. The bulk-run test in `follow-ups.md` would point
  such definitions out.
* **Counts are what a trigger note committed.** The destinations and cards a run reports
  are the distinct notes and cards it handed over for that trigger note, so a note two
  stages wrote counts once and a card three actions changed counts once. Across trigger
  notes they add up, as format 1 counted: a note two trigger notes wrote counts twice.
* **Files are UTF-8, no BOM, no newline translation**, and a filename resolves inside the
  media folder -- a path separator or a `..` segment is refused, as is anything Windows would
  not write as given (`< > : " | ? *`, a control character, a trailing dot), on every
  system. Names that differ only in case are one file to a run. There is no append mode:
  `read_file`, build the new content, `write_file` with `overwrite: true`.
* **Every file name starts with `_`.** Reading and writing both add one to a name that has
  none -- a stage's filename and each name file code returns -- and a missing-file error
  names the file actually looked for. CopyAnywhere's files are text files no note field
  refers to, so the prefix is what keeps Anki's unused-media check from deleting them; a
  file without it is not one CopyAnywhere reads or writes. Both file stage editors say so.
* **A file this run has queued counts as already there.** Both `skip_if_exists` and
  `overwrite: false` ask about the pending write as well as the media folder, so a second
  stage naming a file an earlier one wrote skips or refuses rather than replacing it. Asking
  only about the folder made the answer depend on whether a previous run had committed: the
  same two stages overwrote silently the first time and refused the second.
* **A called definition is isolated.** It gets its trigger note and nothing else of the
  caller's -- no variables, no lists, no loop bindings. It shares the working notes, cards
  and files, and returns only what it declares in `exports`. Call cycles are refused, and a
  chain deeper than 32 is refused whatever the guids say. The definition the run started
  with is on the call stack too, so one that calls itself is refused before it runs a
  second time, and the message names the cycle from that definition: `call cycle: a -> b ->
  a`, by guid. A disabled call stage, or one under a disabled stage, does not close a
  cycle: disabling the call is how a user breaks one.
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
* **A search condition is asked of one note, as a whole.** For a saved note it is
  `find_notes(f"({search}) nid:{id}")`, through the session's cache, and like any query it
  reads the saved collection, not this run's pending edits. The parentheses matter: Anki
  binds `OR` looser than the implicit AND, so without them `a OR b` matched whenever `a`
  found any note at all. An empty search fails the stage, naming the condition.
* **A search condition on a note being added is judged against that note.** It has id 0
  and no row, so `nid:0` would never find it. `logic/unsaved_note_search.py`, a port of
  Anki 25.9's search parser and SQL writer checked against real searches (and following
  26.8 in reading any whitespace as a space when it runs there), judges the same
  parenthesised text against the note as it stands -- its fields, tags and note type,
  including this run's earlier writes -- and `deck:` against the deck it is being added to,
  subdecks included as Anki does. A term whose answer needs what the note does not have yet
  fails the definition with a message naming the term: anything about cards, reviews, ids
  or collection state (`is:`, `card:`, `flag:`, `prop:`, `rated:`, `added:`, `nid:`,
  `cid:`, `dupe:`, `preset:` and the rest), the regex and accent forms (`re:`, `nc:`, `w:`,
  `sc:`, `field:re:`, `tag:re:`), `deck:current` and `deck:filtered`, any other `deck:`
  but `deck:*` when the caller handed over no deck, the deck is filtered, or the note type
  sends some cards to a deck of its own, and `tag:` when one of the note's tags is one Anki
  would rewrite on save. A term refused for its key or form is refused even where the rest
  of the search would already decide; a search Anki itself would refuse fails the same
  way. The editor warns about a term it can see in the literal text of a trigger's search
  condition, in a definition that runs for the note being added.
* **A stage that only feeds one migrated field write shares its gates.** Migrating
  Destination-to-sources with more than one source note (one is read directly; see *What
  migration changes on purpose*) moves each write's per-source read in front of it, into a
  list, a loop and a reduce, and that is where the work is -- the code or the process chain runs once
  per source note there. So the migrator copies the write's `unfocus_trigger_fields` and its
  two `unfocus_when_*` flags onto those stages, and a `write_if: "empty"` along with the
  field it asks about as `write_if_field`, and the executor skips them the same way it
  skips the write. Without it an unfocus of one field evaluated every other write's
  right-hand side once per source note, and so did a write whose field was already filled,
  and a raising one failed the definition, discarding the writes that would have been
  applied.
* **A card action on a cloze card type reaches every cloze card.** A cloze note type has one
  card type and makes every cloze card from it, so an Edit Note stage's action for that card
  type applies to c1, c2, c3 and the rest alike, and there is one action to set, not one per
  cloze. The card action editor says so under the card type. To act on one cloze, loop over
  a Card Query and use an Edit Card stage: either narrow the query (`card:2` is the c2 card)
  or branch on `return card.ord == 1` in code, since `card.ord` is the cloze number less one.
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
changed". So no rename hook is used. Instead, the objects with a stable id -- note types,
decks and card types -- are stored as references and resolved by id (above), so renaming
one changes nothing about what a definition does; only the name it shows goes stale. A
field has no reference: it is the name you spelled, in a field slot or a `{{trigger.Word}}`
token, and so is a card type inside `{{trigger.Recognition__Card_Due}}`. And any name can be
spelled as text: `deck:JP` in a search, `"Word"` in code. A rename of one of those has to be
carried into the definition or the definition goes on spelling a name that is gone. One
**reconcile pass** does that where it can be done mechanically, leaves a **warning** where
the definition still spells the old name, and reports the rest.

**When it runs.** When the collection is opened, which is where a rename made on another
device and synced in before Anki started is seen; after any operation that reports changing
a note type -- every dialog that can rename a field, a card type or a note type, its undo
and its redo, and the everything-changed operation a sync ends with; and after an operation
that reports changing only a deck, if the decks' ids or names differ from what the last
completed pass saw. That last condition is there because answering a card reports a deck
change (the deck's counts changed), so without it every answer would read the config and
scan every definition to find nothing; comparing the decks costs one listing. A pass that
failed remembers no decks, so the next deck change tries again.

The pass:

1. **binds** a reference whose `id` is null but whose name resolves, which is how an example
   definition, a hand-written one and a definition kept for a note type you had not made
   yet pick up their ids;
2. **refreshes** the cached `name` of every reference whose id still resolves, so a picker
   never shows a name the collection has stopped using;
3. **follows** a field or card type renamed in a trigger note type into a definition that
   stores **one** trigger note type: its field slots -- a field write on the trigger, the
   unfocus lists, a write's only-if-empty field -- and every `{{trigger.Word}}` and
   `{{trigger.Recognition__Card_Due}}` token in an expression's **text**. The whole rename
   map is applied in one step, so two fields that swap names swap correctly. A definition
   that stores **several** trigger note types is **never rewritten**. It spells each field
   once for all of them, so a rename in one leaves it wrong whichever name it spells, and
   every rule that tried to decide for it -- follow once all of them agree, hold back while
   another note type has both names -- found a way to decide wrongly and quietly: a swap
   traded back, a name redirected to a field that already had it. The note types are
   counted as stored, resolved or not: a definition whose second note type is missing today
   still spells its names for both;
4. **warns** at every place a definition still spells a renamed or deleted name that it did
   not follow (below);
5. **reports** everything into its log: a reference that resolves to nothing, a note type,
   deck or card action's card type that was deleted, a field or template with no id (note
   types saved before Anki 23.10 can have them, and nothing can follow a name with no id
   behind it), every warning a definition carries, and the stale names in its searches.

*Deleted* means the snapshot (below) knew the object's id and the collection no longer has
it; the log says it "has been deleted". A name the snapshot never knew -- a typo, a note type
you have not made yet, a definition imported from another collection -- is one "this
collection does not have" instead. For a *reference* to a note type, a deck or a card
action's card type the difference lasts one pass: the pass rebuilds the snapshot from what is
still there, so from the next pass on a reference left naming a deleted one reads as one this
collection does not have. A deleted name the definition *spells* gets a warning, which stays.

### Where it warns

Every text and slot of a definition that can spell a name is a **location**, and a warning
is filed under the location that spells the name, so the editor can show it on the part
that holds it. The locations:

- the trigger's unfocus lists, a field write's unfocus fields, and a migrated stage's
  only-if-empty and unfocus fields;
- an expression's text: `{{trigger.Word}}`, `{{trigger.<Card type>__<Key>}}`, and
  `{{<binding>.Word}}` for a note a query found;
- a search -- a query stage's search and a search condition: `deck:`, `note:` and `card:`
  terms and field terms (`Word:neko`), quoted, escaped or negated. A deck is matched without
  regard to case, as Anki does, and renaming a parent deck renames each child deck too, so
  `deck:JP::Vocab` is found through the child's rename;
- code: a string literal whose value is the name, or that reads as a search naming it.
  Python's own tokenizer finds the literals, so a comment, an identifier or a longer name
  holding the old one is never a hit. Each literal part of an f-string is held to the same
  rule: `f"Wordlist {x}"` is not a hit for `Word`;
- a query's sort field, a field write's target field;
- a card action's code and its deck.

Some locations are not read by any run: the side of an expression its mode does not use
(the code in text mode, the text in code mode), a card action's code while it does not use
code, its deck while its code decides the move, and everything in a switched-off stage or
under one. A name left there breaks nothing until it is switched back, so a warning there
never blocks; it is still filed and kept, or switching back would run the old name with no
warning at all. Once a save finds the location read again, the warning blocks as it would
anywhere.

A `{{trigger.Word}}` in a definition that does not trigger on the renamed field's note type
spells its own trigger's `Word`, so a warning about another note type's `Word` never counts
it: the entry says so (`through_trigger: false`), and the editor, a save and Replace all
leave that token alone.

A field or card type of note type N is looked for in every definition that triggers on N,
except in a slot or token the pass has just followed; and in the searches, other bindings'
tokens, sort fields and code of *every* definition, since any of them can reach N's notes. A
deck or a note type is looked for in the searches, code and card action decks of every
definition. Its references follow it by id and are never warned about. A rename that only
changes the case of a field, deck or note type name is no rename here: everything that reads
those names ignores case.

**Block or warn.** A warning either stops the definition from running (**blocking**) or
only tells you (**warn-only**):

| blocks the run | warns only |
| --- | --- |
| a `deck:` or `note:` term; a card action's deck | a `card:` term; a field term |
| a search in code naming a deck or a note type | a query's sort field; a field write's target on another note |
| code spelling what the definition uses: a field or card type of a note type it triggers on, a deck or note type it references | code spelling anything else |
| a trigger slot or token of a definition with several trigger note types | `{{<binding>.Word}}` for a note a query found |
| a trigger slot or token naming a field or card type that was deleted | |

A deck or note type name is unique in the collection, so a term that spells it means that
object, and a run with it would search for something that is not there. A field or card type
name is not unique across note types, and the pass cannot know which note types a search,
another binding or a sort field reaches: the hit is a guess, which is worth a warning and
not a refusal. That holds for a deleted one too, since another note type may still have the
name. Code blocks by choice where the definition is known to use the object, although a
code hit is only a string that equals a name: a wrong read in code is the hardest to
notice. Anywhere else a string in code that equals a name is too likely something else --
`'default'` is a deck's name and also any setting's, a field called `Word` another note
type's -- so it only warns, unless it reads as a search, whose `deck:` or `note:` term
names its object unmistakably.

**The warning.** A definition's warnings are stored under `rename_warnings`, a dict from a
location key to its entries, each
`{"kind", "object_id", "note_type_id", "old", "new", "blocks_run", "through_trigger",
"message"}`, `new` null for a deletion, plus `"inactive": true` while no run reads the location
(`blocks_run` then says what it would do once one does). A key is anchored on the guid of what holds the text -- `<stage guid>.query.text`,
`<field write guid>.value.code`, `<card action guid>.change_deck`,
`triggers.on_unfocus.edit_fields` -- and is built only by `logic/rename_locations.py`, for the
pass and the editor alike: a key spelled any other way matches nothing and shows nothing.
There is one entry per location and object. Its message says what happened, not where, since
the editor shows where:

    Field "Word" of note type "A" was renamed to "Term"
    Card type "Recognition" of note type "A" was renamed to "Reading"
    Deck "JP" was renamed to "JP::Vocab"
    Note type "A" was deleted

The pass never re-derives a warning: it cannot tell an edit that fixed the definition from one
that did not. Two things change one by themselves. The same object renamed again updates the
entry's new name and message. And an object called by its old name again -- the rename
undone, or renamed back by hand -- **removes** the entry. Renaming the field in the other
trigger note types too does not.

### A definition with a blocking warning is not run

On any path: its checkbox in the definition list is cleared and cannot be ticked, the
browser's "Copy anywhere" menu lists it disabled, the add, review and unfocus hooks and a sync
pass it over, and a `call_definition` of it fails (below). Each refusal logs one error per
blocking warning: the definition's name, the stored message, and what to do.

    Error in copy fields: 'both' was not run: Field "Word" of note type "A" was renamed to
    "Term". Replace the old name in the definition editor and save, or dismiss the warning
    there.

The add, review and unfocus hooks log it the first time they meet the definition in a
session, and again only when its blocking messages change, since each of those events opens
its own log file and one refusal per answer would soon fill the kept log files with the
same line. A sync logs it at every sync, and stops that definition after the one line rather
than repeating it for every note; the other definitions of the run still run. None of these
runs opens the log, so the definition list and the dialog after the operation (below) are
what say that a definition is not being run. The note being added is still added, and the
other definitions still run on it.

A card whose review refused a definition keeps its `fc` as the scheduler set it, and a
sync's closing sweep leaves the cards of a refused definition's trigger note types waiting,
so the first sync after the last blocking warning goes runs it on the cards it missed. The
cost, meanwhile: another definition that runs on both review and sync runs again on such a
note at every sync.

A definition that calls a blocked one fails at its `call_definition` stage with the same
explanation, and its run on that note writes nothing -- not even what it changed before the
call -- rather than running the rest of a chain around the gap. The editor opens a blocked
definition, since that is where it is fixed, and its preview still runs it, so a fix can be
tried before it is saved; a call to a blocked definition fails in the preview too.

A warn-only warning changes nothing about a run.

### In the editor

**On the part.** Each part that holds a location -- an expression's text or code box, a
query's sort field, a field write's target, a card action's code and deck, the unfocus
lists -- shows a red ✖ beside it when a blocking warning is filed there and a blue ⓘ when
only warn-only ones are, with the messages on hover. It follows the live text: once the part
no longer spells the old name, as the pass's scanner reads it, the icon hides, although the
warning stays stored until you save. The sort field and a field write's target keep a stored
field name the note types no longer have, under "Renamed or deleted in Anki", while a warning
there is about it; a card action keeps a deck name the collection does not have, and the
unfocus lists keep a name the note types do not offer. So a renamed name is shown
rather than silently blanked and saved away. A migrated stage that carries a write's gate
shows it as two rows of its own, "Only when leaving" and "Skipped when filled", each with its
icon and Replace.

**Replace.** Where a warning has a new name and the scanner can spell it in, a **Replace**
button sits beside the icon. It opens a read-only dialog with the part's whole text, each old
spelling struck through and its replacement highlighted, in colours taken from the theme;
for a picker, just `old → new`. **Apply** writes exactly what it shows, **Close** writes
nothing. A text box takes the change through its own undo stack, so **Ctrl+Z** takes it
back; a picker simply changes its choice. Replace quotes and escapes a search term the way
Anki's own search writer does, keeps a leading `-`, and keeps a field term's value as
written. It never touches a deletion, which has no new name, or an f-string that
interpolates, whose meaning depends on what it interpolates: a part holding only those has no
Replace button, and beside a replaceable hit the dialog lists them as left to fix by hand.
Two renames trading names (`A` → `B` and `B` → `A`, or a chain) leave text that spells the old
names again after Replace, so the editor remembers that Apply answered them: their icon hides,
and Save drops them unless the text has been put back as it was.

**Duplicating a stage** copies the warnings about its texts to the copy, which spells the same
names.

**The banner.** Above the triggers, the editor says that the definition is not run while any
✖ is left and that an ⓘ does not stop it, then lists every warning grouped by location, each
group named as you know the part: `Loop Over Notes > Edit Note → field write 'Meaning' →
value`, `Triggers → unfocus fields (editing a note)`. Clicking a group's name opens its stage
and scrolls to it. A location whose stage, field write or card action has since been deleted
is listed as "(no longer in this definition)". Each warning has a **Dismiss**, and **Dismiss
all** appears for two or more. Dismissing edits the document only: Save stores the
definition without it, Cancel keeps it.

**How a warning goes away.**

- **Save**: a warning whose location no longer spells the old name -- because you replaced it
  or edited it by hand -- or whose stage, field write or card action you deleted, is dropped
  when the editor saves, read by the same scanner the pass used. Switching an expression to
  its other side or a stage off keeps it, not blocking (see above). Code that does not parse
  keeps its warnings: it cannot be read, so it cannot be seen to be fixed. Cancel drops
  nothing.
- **Dismiss**, for a hit that is not the renamed name, or one you have decided is fine.
- **Undoing the rename** in Anki, or renaming the object back.

A warning does not stop the save, but a definition on several note types still has to pass
the next check.

**The dialog after a note type or deck change.** After an operation that changed a note type,
or a deck change the pass acted on, a dialog lists the blocking warnings that pass added, one
line per definition and message, and says how to clear them. Only the new
blocking ones: a warning stays until it goes, and listing it again after every unrelated
note type edit would teach you to close the dialog unread. There is none when the collection
is opened: a warning a synced-in rename left there is shown by the definition list and the
editor.

**A definition on several note types can only use a field all of them have.** It spells a
trigger field once for every note type it triggers on, so the editor refuses to save one
that uses a field some of those note types lack, and says which: `Field "Term" is not on
note type "B", which this definition also triggers on; use a field all of them have, or
rename it in the others too.` Every place a trigger field is named is checked -- a field
write on the trigger, the unfocus lists, a write's only-if-empty field, and each
`{{trigger.X}}` in a value -- and a card type spelled in a `{{trigger.<Card type>__<Key>}}`
token is held to the same rule. This is what a definition warned about a rename made in only
some of its note types runs into when it is opened, and it is what stops a half-fix:
`{{trigger.Word}}` replaced by `{{trigger.Term}}` cannot be saved while another trigger
note type still has no `Term`. A field none of the trigger note types has is reported as
before, as a reference the note types cannot answer to; a definition on one note type is
not affected.

### Where the report reaches you

The pass writes all of it into one operation log, which you only see with the log level
turned up, so the things you can act on are also put where you already look:

- a reference that resolves to nothing is shown in the editor under the name it was
  written with, marked `(not found)`, and listed under the stages as worth knowing, with
  what a run does about it: `Note type 'X' is not in this collection, so nothing triggers
  this definition on it until it is.` It does not stop the save. At run time such a
  reference fails quietly -- a note type matches no note, a whitelist deck no deck, a card
  action's card type no card -- and it may be a note type you have not made yet or one a
  sync will bring. Picking a live entry from the same box clears it;
- a query stage shows an amber note under the search naming what it spells that this
  collection does not have -- `deck:`, `note:` and `card:` terms and field searches, exact
  names only: a term with a wildcard, a regex or a `{{...}}` reference in it is left alone.
  It is a note, not a blocker: a query may name something you have not made yet;
- the definition list marks a definition with a reference that resolves to nothing with an
  amber ⚠ after its name, the names in its tooltip. It is asked of the collection as the
  list is drawn, never taken from a pass, so a later pass or a restart cannot lose it. A
  deleted note type, deck or card action's card type is one of these;
- the definition list marks a definition that runs but deserves a look with a blue ⓘ: its
  searches name something the collection does not have -- the same terms the query stage's
  note lists, from every query stage and every search condition -- or it carries warn-only
  rename warnings, whose messages follow under "Renamed or deleted in Anki (the definition
  still runs):". Each message is listed once, without its location; the editor has that;
- a definition with a blocking warning shows a red ✖ after its name, and its checkbox is
  cleared and cannot be ticked. Both carry the same tooltip: that it is not run, the
  blocking messages, and what to do. Edit, Duplicate and Delete still work. The browser's
  "Copy anywhere" context menu lists it the same way: disabled, with the same tooltip. A
  definition with only warn-only warnings stays enabled in both, and one with both kinds
  shows ✖ and ⓘ.

All three follow an edit made from the definition list: when you save a definition there,
its row is redrawn from the definition as saved, so a fix clears the ⚠ or the ⓘ, and a save
that leaves no blocking warning clears the ✖ and gives back a checkbox you can tick.

A sort field that names no field still sorts a note that lacks it as empty -- a query
legitimately mixes note types -- but a run where *no* selected note had the field logs one
warning, because then the sort did nothing at all.

### The snapshot

Both names of a rename come from `name_snapshot` in the addon config: the name of every note
type and every deck by id, and, for each note type a definition triggers on or names in a
card action, the names of its fields and templates by their ids. Every deck and note type,
because a search or code can spell any of them; fields and templates only of those note
types, because that is what a definition's own slots and tokens spell. It is written by the
same pass and refreshed whenever definitions are saved, so it is never older than the last
save, and a changed name under an unchanged id is what a rename *is*. A config whose
definitions hold no reference at all gets an empty snapshot, and the pass does not run for
it. The pass writes the config only when something changed.

**Several profiles.** The addon's config is one file for the whole add-on, shared by every
profile, while a note type id, a deck id and a field id belong to the collection that issued
them -- and a collection restored from a backup as a second profile answers to the first
one's ids under whatever names it has been given since. So the snapshot records which
collection it was taken of, and a pass that opens on a different one does not read it at
all: no name in it is an old name here, nothing is rewritten or warned about, every
reference is re-bound by the usual rule -- the id while it still exists, the name when it
does not -- and the snapshot is replaced with this collection's names. A rename is only ever
followed inside the collection it happened in, so switching profiles leaves each one's
definitions as they were.

The collection is recorded by its path. Its creation time, which would travel with the
collection, was tried and dropped: Anki rounds it to the start of the day, so two profiles
made on the same day would read as one.

### Known limitations

- **Wildcards.** A search term with a wildcard (`*`, or an unescaped `_`), a regex or a
  `{{...}}` reference is not an exact name and is never a hit: `deck:JP*` is not found by a
  rename of `JP`.
- **Fields of note types no definition triggers on** (or names in a card action). Their names
  are not in the snapshot, so a rename of one is not seen, even where a search, another
  binding or code spells it.
- **A config synced between desktops** (`addon_config_sync` copies it whole) carries the
  other desktop's path, so the first pass after it arrives reads its snapshot as another
  collection's. A rename that reached this collection without a pass seeing it -- one made
  on a phone, say -- is then neither followed nor warned about. The definitions go on
  resolving their note types, decks and card types by id; only the names they spell are
  left as they were.
- **Code hits that happen to equal a name.** Any string in code that equals the old name is
  a hit, whatever it was for, and may block. Dismiss it; or, if the string changes, the next
  save drops it.
- **Code that does not parse** finds nothing, so a pass that meets code mid-edit files no
  warning in it, and a save keeps the warnings it already has.

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

Separately from the version steps, every start gives a guid to any stage that has none, and
points the exports at the repaired stages. Definitions converted before the conversion
minted guids for every part could lack them, and a hand edit can drop one.

## What migration changes on purpose

* Across-note selection searches notes rather than cards, so a note with two matching cards
  is selected once. `select_card_by: None` becomes `first` and follows search order rather
  than the reverse of the card search.
* `Least_reps` is gone; it migrates to `random` and the migrator says so.
* A Destination-to-sources definition that reads exactly one source note -- `first` or
  `random` with a count of 1 -- becomes a query, a `select_note` that binds the found note
  as `note`, and an `edit_note` on the trigger whose writes read `{{note.Word}}` directly,
  with its file writes after it reading the same note. The list, loop, store and join that
  more than one source needs are left out: joining one value is that value. Not with
  `run_also_if_no_sources_found`, which with no source wrote the joined empty text and ran
  no field code, where a bound "no note" would run the code once. One difference remains:
  field or file code there that returns a list or a tuple now fails the write, as it does
  everywhere else, where the join used to turn it into text.
* In Source-to-destinations code, `note` and `cards` are the destination's rather than the
  trigger's (see *What migration does to a format-1 expression* below).
* A copy condition is judged against a note being added. Format 1 searched `nid:0`, which
  never found it, so such a definition was skipped on add (unless an unscoped `OR` found
  some other note); it now runs when the condition matches, or fails naming the term it
  cannot judge.
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
the stage's note or the loop's, `cards` that note's cards, and the facades where format 1
had views of its own. A card facade has the properties format 1's card view had, the cloze
number on `template_name` and the creation and review times included, so code reading a
card keeps working unchanged. In Source-to-destinations the edits sit
in a loop whose item is `note`, so field and file code that read the trigger as `note` now
reads the destination; Destination-to-sources loops over the sources under that name, or with
one source binds it under that name with a `select_note`, which is the note format 1 gave its
code there. Code that reached for a note by `note`, or by any
other means, is yours to check by hand; the user guide's section for an AI agent
([`ADDON_README.md`](../ADDON_README.md#for-an-ai-agent-rewriting-converted-code)) lists what
every name means in each migrated shape. Card-action code is not affected: it runs as
format 1 ran it. A config an earlier version already staged is rewritten in place by the
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
* **Add stage** offers the sixteen types. *Edit Card* appears only where a card binding is
  in scope, because it names one card and there would be nothing to name.
* `↑`/`↓` reorder a stage among its siblings and never take it out of its block. Every row
  also has a checkbox, and the bar above the list acts on the checked stages together; the
  `⋮` menu on a row offers the same moves for that row alone, checked or not, as well as
  duplicating and deleting it. There is no drag-and-drop; the menus do the same work.
  *Wrap in* puts neighbouring stages of one block inside a new condition (as its `then`) or
  loop (as its `body`), standing where they stood; stages with a gap between them, or from
  two blocks, have no one place for it to stand and cannot be wrapped together. *Move out*
  puts stages that share a block just above or just below the stage that holds it. *Move
  into* lists every other block, a condition named by its predicate and a loop by its list:
  no editor sets a stage's own name, so two conditions both read "Condition". The stages
  land as near as the block allows to where they were -- at the top of a block below them,
  at the bottom of one above, just below the stage that held them in a block enclosing them
  -- because always appending sent a stage taken out of a branch to the end of the
  definition. That is one rule, not three: they go in after every stage of the block that
  ran before them, which also puts a stage moved from a condition's `then` at the top of its
  `else`. *Remove this condition* (or *loop*) *, keep its stages* is the reverse of a
  wrap: the stage's blocks take its place, a condition's `then` followed by its `else`, both
  now run every time, and a migrated copy condition's skipping of the whole note goes with
  it. A condition or loop that was off keeps its stages off, since they never ran before and
  running them is a separate decision. Checking a stage and something inside
  it acts on the outer one, which already carries the rest. The bar also turns the checked
  stages on or off and deletes them; a move keeps them checked, and a wrap or a move into a
  block opens the block they landed in and every block around it. Closing a condition or a
  loop unchecks the stages inside it. Both are for the same reason: the bar acts on what is
  checked without asking, so a checked stage has to be one that is on screen.
* None of those moves rewrites a reference. A result produced inside a branch or a loop body
  does not escape it, so wrapping its producer leaves every reader further down marked and
  the save blocked until the reader moves in too or the producer back out; unwrapping can
  bring together two names that lived in separate branches, which is reported the same way.
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
  the flag stored in `effects` and the one the add hook checks, and it has amber notes to
  go with it. One is about what is *impossible*: a card action on the note being
  added has no card to reach, so it will not run for that note and nothing runs it later --
  the stage is named. One is about what is *unanswerable*: a trigger search condition using
  a term the note being added cannot answer, one note per condition, naming the term, for a
  definition that runs for that note. The last is about what is *forbidden*: an edit to
  another note, to a card that already exists, or to a file would outlive a cancelled add,
  so that work happens after the add rather than as part of it (or, for an unfocus-only
  trigger, not at all), and the stages responsible are listed after "Because of". They are
  independent: a definition can earn any of them or none, and none of them blocks the save.

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
| `logic/unsaved_note_search.py` | judging a search against a note not in the collection yet |
| `logic/copy_fields.py` | the bulk operation: which notes each definition runs for, undo, the sync tail |
| `logic/object_refs.py` | note type, deck and card type references: id first, name after it |
| `logic/rename_reconcile.py` | the reconcile pass, the name snapshot, save-time dropping of cleared warnings |
| `logic/rename_scan.py` | where a text spells a renamed name, whether that blocks, and its replacement |
| `logic/rename_locations.py` | the location keys warnings are filed under |
| `logic/rename_warnings.py` | the stored warnings and their readers: blocking, messages, advice |
| `logic/query_terms.py` | the names a search spells that the collection does not have |
| `hooks/rename_hooks.py` | when the pass runs, and the dialog after a note type or deck operation |
| `configuration.py` | the config, the trigger accessors, and the startup migration |
| `ui/stage_document.py` | the editable stage tree: add, move, wrap, unwrap, duplicate, delete, save readiness |
| `ui/stage_editor_context.py` | one scope per stage, turned into its menus and Add Stage entries |
| `ui/stage_edit_state.py` | the state object that lets the older shared widgets be reused |
| `ui/value_expression_editor.py` | the one editor every value expression uses |
| `ui/stage_editors.py` | one editor per stage type |
| `ui/stage_list.py` | the ordered, indented stage list, and the bar that acts on the checked stages |
| `ui/stage_triggers_editor.py` | the trigger settings at the top |
| `ui/stage_exports_editor.py` | the definition-level exports panel |
| `ui/stage_preview.py` | the preview pane: note picker, run control, trace |
| `ui/edit_staged_definition_dialog.py` | the dialog, what blocks a save and what it warns of |
| `ui/rename_marks_banner.py` | the warnings grouped by location, one Dismiss each, atop the dialog |
| `ui/rename_indicator.py` | the ✖ / ⓘ and Replace button beside each part holding a location |
| `ui/rename_replace_dialog.py` | the read-only diff Replace shows before Apply |
| `ui/pick_copy_definition_dialog.py` | the definition list, and its ✖, ⚠ and ⓘ |
