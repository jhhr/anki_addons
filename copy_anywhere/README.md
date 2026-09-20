# CopyAnywhere

An Anki addon for filling fields from other fields, other notes, other cards and files —
the kind of editing [Advanced Copy Fields](https://ankiweb.net/shared/info/1898445115),
[Batch Editing](https://ankiweb.net/shared/info/291119185) and Anki's own find-and-replace
each do a part of, in one place and in one pass.

You build a **copy definition**: a list of steps, called *stages*, that run in order. One
stage searches for notes, the next loops over them, the next writes something into the note
you started from. A definition can run by hand over a browser selection, or by itself when
you add a note, leave a field, answer a card, or sync.

The examples in this file are real: they live in [`docs/examples/`](docs/examples), the test
suite loads and checks them, and every screenshot below is generated from them. They are written against a note type called
**CA Vocab** — fields *Word*, *Reading*, *Meaning*, *Freq* and *Note*, and card types
*Recognition* and *Recall* — so the field names in the pictures have somewhere to come from.

The full specification lives in
[`docs/staged-definitions.md`](docs/staged-definitions.md). This file is the tour.

## 1. What it does

A definition is one screen: what makes it run, the stages it runs in order, what it hands
back to a definition that calls it, and a preview of what it would do to one real note.

![The stage editor](docs/images/stage-editor.png)

The definition in the picture finds notes related to the one being edited, collects their
*Meaning* fields, joins them into one line and writes that line into the trigger note's
*Note* field. It is [`collect-meanings.json`](docs/examples/collect-meanings.json), and
[section 4](#4-a-walkthrough) builds it one stage at a time.

## 2. Install and setup

The addon lives in this repository rather than on AnkiWeb. With Anki closed, clone the
repository into your `addons21` folder and run `python build.py install` from the repository
root; see the [repository README](../README.md) for what that does. `python build.py dist`
packages a `.ankiaddon` file instead.

Once it is installed, open the card browser. **Edit → Copy anywhere...** (by default
`Ctrl+Shift+C`) opens the list of definitions:

![The definitions list](docs/images/definitions-list.png)

From here you add, edit, duplicate and delete definitions, and you run them: tick the ones
you want, then **Use selected notes** or **Use all notes from current search**, and
**Apply**. To run a single definition without opening this dialog, right-click a selection
in the browser and pick it from the **Copy anywhere** submenu.

Two things are set in Anki's own addon configuration screen (Tools → Add-ons → Config):

| key | what it is |
| --- | --- |
| `copy_fields_shortcut` | the shortcut for **Copy anywhere...**, `Ctrl+Shift+C` by default |
| `log_level` | how much a run records: `error` (the default), `warning`, `info`, `debug` |

`copy_definitions` in that same file holds the definitions themselves. It is readable, and
the examples in `docs/examples/` can be pasted into it, but the editor is easier.

### Custom scheduling, for "Run on sync for reviewed cards"

A definition that runs for cards you reviewed on another device needs the reviews flagged as
they happen, which only the scheduler can do. Open any deck's options, **Advanced → Custom
scheduling**, and add:

```
customData.again.fc = 0;
customData.hard.fc = 0;
customData.good.fc = 0;
customData.easy.fc = 0;
```

That marks each answered card unprocessed. The sync sweep picks up the marked cards, runs
the definitions that asked for it, and clears the mark. Nothing else needs this.

## 3. The model

### What makes a definition run

At the top of the editor you name the definition and say which notes it considers: a
**trigger note type** (one or more) and, optionally, a **trigger deck limit** — cards belong
to decks, so a note passes the limit when any of its cards is in a listed deck. That
filtering happens before any stage runs.

Then you tick when it should run. A definition can have any number of these, or none at all,
in which case you run it by hand from the browser:

- **Run when adding a new note** — when the note is added through the Add dialog,
  AnkiConnect, or anything else calling `col.add_note()`. [Section 5](#5-adding-notes) is
  about what a definition may do at that moment.
- **Run when leaving a field, editing a note** and **Run when leaving a field, adding a
  note** — pick the fields. Leaving one of them runs the whole definition, not just the
  stages that mention it.
- **Run on review** — after you answer a card, for its note.
- **Run on sync for reviewed cards** — reviewed cards are gathered at sync time and the
  definition runs over them then, including the ones you reviewed on another device. This
  needs the scheduling setup above. With *Run on review* on as well, the desktop runs it on
  review and the sync sweep leaves those cards alone, so nothing is done twice.

### The stages

Stages run top to bottom. A stage that produces a value gives it a name, and every stage
below it can use that name. There are fourteen kinds, and **Add stage...** offers them all
except *Edit Card*, which appears only where an earlier stage has named a card for it to act
on:

| stage | what it does |
| --- | --- |
| **Variable** (`variable`) | computes one value and names it |
| **Query Notes** (`note_query`) | an Anki search, producing a list of notes |
| **Query Cards** (`card_query`) | the same, producing a list of cards |
| **Edit Note** (`edit_note`) | writes fields, tags and card actions to one named note |
| **Edit Card** (`edit_card`) | card actions on one named card |
| **Read File** (`read_file`) | reads one file from the media folder, as text |
| **Write File** (`write_file`) | replaces one file in the media folder |
| **List** (`list_variable`) | declares an empty list |
| **Store** (`store`) | appends one value to a list |
| **Loop Over Notes** (`for_each_note`) | runs its body once per note in a list |
| **Loop Over Cards** (`for_each_card`) | runs its body once per card, binding the card's note too |
| **Reduce** (`reduce`) | joins a list into one text, or folds it with an expression |
| **Condition** (`condition`) | runs exactly one of its two branches |
| **Call Definition** (`call_definition`) | runs another definition and keeps what it hands back |

A query stage also says how many of the matches to take, in what order, and what to do when
nothing matches. Loops bind the position (`index`, one-based) and the total (`count`)
alongside each item. A name declared inside a loop body or a branch is gone when that block
ends — which is why collecting anything out of a loop means a **List** before it, a **Store**
inside it, and a **Reduce** after it.

<details>
<summary>One editor per stage type</summary>

![Variable](docs/images/stage-variable.png)
`variable` — computes one value and names it.

![Query notes](docs/images/stage-note_query.png)
`note_query` — an Anki search, with how many of the matches to take and in what order.

![Query cards](docs/images/stage-card_query.png)
`card_query` — the same, counting cards instead of notes.

![Edit note](docs/images/stage-edit_note.png)
`edit_note` — fields, tags and card actions on one named note.

![Edit card](docs/images/stage-edit_card.png)
`edit_card` — card actions on one named card, with no card type to choose.

![Read file](docs/images/stage-read_file.png)
`read_file` — one media file's contents, named.

![Write file](docs/images/stage-write_file.png)
`write_file` — replaces one media file.

![List](docs/images/stage-list_variable.png)
`list_variable` — declares an empty list.

![Store](docs/images/stage-store.png)
`store` — appends one value to a declared list.

![Loop over notes](docs/images/stage-for_each_note.png)
`for_each_note` — a pass per note in a note list.

![Loop over cards](docs/images/stage-for_each_card.png)
`for_each_card` — a pass per card, binding the card's note too.

![Reduce](docs/images/stage-reduce.png)
`reduce` — joins a list into one text, or folds it with an expression.

![Condition](docs/images/stage-condition.png)
`condition` — runs exactly one of its two branches.

![Call definition](docs/images/stage-call_definition.png)
`call_definition` — runs another definition and keeps the results it exports.

</details>

### Values

Everywhere a definition computes something — a field's new content, a search, a filename, a
condition — you get the same editor. Left as text, it is interpolated:

![A text value](docs/images/value-text.png)

References name what they read: `{{trigger.Word}}` is a field of the note the definition ran
for, `{{note.Meaning}}` a field of the note a loop is on, `{{card.deck_name}}` a card value,
and a bare `{{Summary}}` is a result some stage above produced. The right-click menu lists
exactly what is in scope at that stage — a loop's note is offered inside the loop and not
above it — and anything you type that is not on the menu is marked in red underneath. Lists
are never offered: turning several values into one piece of text is a decision, so a
**Reduce** or some code has to make it.

Turn on **Execute content as Python code** and the box becomes the body of a function that
returns the value instead:

![A code value](docs/images/value-code.png)

Code gets read-only views of the notes and cards in scope, plus `find_notes`, `find_cards`,
`get_note` and `get_card`. Assigning through one of those views raises: every change a
definition makes goes through a stage, which is what keeps it visible to the preview and to
the undo entry. After either form, the optional **Extra processing** chain runs — the same
regex and text processes as before.

Expressions that came from an older definition are a third case; they keep their old
spelling until you edit them, and [section 8](#8-migrating-from-format-1) says what happens
when you do.

### Inside an Edit Note stage

One **Edit Note** stage names the note it writes to and can do three things to it: write
fields, add and remove tags, and act on its cards.

![The tag editor](docs/images/tag-editor.png)

![The card actions editor](docs/images/card-actions-editor.png)

A card action picks one card type of that note and moves the card to a deck, flags it,
suspends or buries it, sets its desired retention — or runs Python. (**Edit Card** is the
same thing for a card a query or a loop already named, so it has no card type to pick.)

### Handing values to another definition

A definition that is called by another can hand back any result produced at its top level.
Tick it in the **Exports** panel at the bottom of the editor and give it the name the caller
will see:

![The exports panel](docs/images/exports-panel.png)

[`flag-and-export.json`](docs/examples/flag-and-export.json) exports a result called
`Status`; [`archive-to-a-file.json`](docs/examples/archive-to-a-file.json) calls it and binds
that export, then writes it into a file. A called definition is otherwise sealed off: it gets
its trigger note and nothing else of the caller's — no variables, no lists, no loop
positions — and hands back only what it exports. Definitions that call each other in a circle
are refused, and so is a chain more than 32 deep.

## 4. A walkthrough

[`collect-meanings.json`](docs/examples/collect-meanings.json) fills the trigger note's
*Note* field with the meanings of the other notes whose *Meaning* mentions this note's word.
It runs when you leave the *Word* field of a `CA Vocab` note in the *JP vocab* deck. Here it
is, a stage at a time.

**1. Find the notes.** A **Query Notes** stage runs
`note:"CA Vocab" deck:"JP vocab" Meaning:*{{trigger.Word}}*`, takes the first five by *Freq*
descending, and calls the result `Related`. If nothing matches, the rest of the definition is
skipped, so nothing overwrites the field with an empty line.

![Step 1: find related notes](docs/images/walkthrough-1-find-related-notes.png)

**2. Make somewhere to put the meanings.** A **List** stage declares an empty list of text
called `Meanings`. It is outside the loop because a name declared inside a loop does not
survive the pass that declared it.

![Step 2: an empty list of meanings](docs/images/walkthrough-2-an-empty-list-of-meanings.png)

**3. Go through them.** A **Loop Over Notes** stage walks `Related`, calling each one `note`,
and a **Store** stage in its body appends `{{note.Meaning}}` to `Meanings`.

![Step 3: for each related note](docs/images/walkthrough-3-for-each-related-note.png)

**4. Join them.** A **Reduce** stage joins `Meanings` with `; ` into one piece of text called
`Summary`.

![Step 4: join them into one line](docs/images/walkthrough-4-join-them-into-one-line.png)

**5. Write it.** An **Edit Note** stage on `trigger` writes `{{Summary}}` into *Note*.

![Step 5: write them into this note](docs/images/walkthrough-5-write-them-into-this-note.png)

Nothing is written to the collection until every stage has run. Press **Run preview** on the
right at any point to see what this definition would do to one real note, without doing it.

## 5. Adding notes

While a note is being added you can still press Escape, and the note never existed. So a
definition that runs at that moment may edit **only the note being added** — its fields and
its tags. Anything else it did would survive a cancelled add and there would be nothing to
undo it.

That gives three rules:

- **Its own cards cannot be reached.** The note has no cards until the add goes through, so a
  card action on it has nothing to act on. It is skipped, a line is written to the log, and
  nothing runs it later. This does not hold the definition back: a skip leaves nothing
  behind.
- **Another note, another card, or a file is out of bounds.** An **Edit Note** on any note
  but the trigger, an **Edit Card**, a card action on another note's cards, a **Write File**,
  or a call to a definition that does one of those — any one of them means the definition
  cannot run as part of an add.
- **Reading is always fine.** Reading the trigger's card values on a note that has no cards
  answers with a new card's defaults rather than failing, exactly as it always did.

The editor says where a definition stands, under the stage list: *Can run while a note is
being added: **yes** / **no***, with up to two amber notes. They are independent — a
definition can earn both, one or neither — and neither of them stops you saving.

The first is about what is **impossible**:

> The card action on *&lt;stage&gt;* needs a card the note being added does not have yet, so
> it will not run for that note, and nothing runs it later.

![The impossible warning](docs/images/add-note-impossible.png)

The second is about what is **forbidden**, and names the stages responsible after "Because
of". With *Run when adding a new note* on, it reads:

> This definition runs when a note is added, and it reaches beyond the note being added —
> another note, a card that already exists, or a file. That work happens after the add rather
> than as part of it, with its own undo entry for the notes and cards it touches.

If the definition only runs on leaving a field while adding, the same message instead says
that it is skipped there, and that turning on *Run when adding a new note* is what gives it a
moment to run in.

There is no picture of the forbidden note on its own — it is the second bullet here, below
the impossible one, in a definition that earns both:

![Both warnings at once](docs/images/add-note-both.png)

## 6. What the editor checks

Every edit re-reads the whole definition, and **Save** is disabled while anything is wrong.
What is wrong is listed under the stage list, each item with the path of the stage it belongs
to, and the stage's own row says it too.

The checking is about names and shapes: a result whose name is missing, reserved, or shadows
one already in scope; a field read through something that is not a note or a card; a list
interpolated into text, or stored into something that is not a list; a **Condition** whose
predicate is empty; an export that a skip earlier in the definition could leave unset; and
definitions calling each other in a circle, which is checked across the whole config and
names the whole circle.

How a reference is *spelled* is the value box's own job. Anything you type there that the
menu does not offer is marked in red underneath as you type:

![An unknown reference](docs/images/value-unknown-reference.png)

That marker is the only warning you get. A name nothing in scope answers to is looked up as a
note or card value when the stage runs, and if there is no such value either, the stage
writes nothing and says so in the log.

Alongside that, saving records what each definition *does* — whether it edits the trigger
note, other notes, the trigger's cards, other cards, whether it reads or writes files,
searches the collection, or calls another definition — following calls into the definitions
they call. That record is what the hooks read at run time, so it is recomputed for every
definition whenever any one of them is saved, added or deleted, not just for the one that
changed. Its most visible part is the add-note answer in [section 5](#5-adding-notes).

The right-hand half of the editor is the preview. Pick one of the notes the triggers would
consider and press **Run preview**: the definition runs for real — real searches, real code,
called definitions and all — but nothing is committed and nothing reaches the media folder.
The trace lists every stage in the order it ran, with a pass per iteration inside loops, and
selecting one shows what it could see, what it produced and what it would have changed.

None of this is a substitute for the log. A run that has something to report writes a file
under the addon's `user_files/logs/`, named after what triggered it; a run you started from
the browser opens it when it is finished. At the default `log_level` of `error` a clean run
writes nothing at all and no file is created. Raise it to `info` or `debug` when you want to
see every stage of every note. The newest fifty files are kept.

## 7. Files

**Write File** replaces one file in your collection's media folder, and **Read File** reads
one back. Both take a filename as a value like any other, so it can be built from the note.
[`archive-to-a-file.json`](docs/examples/archive-to-a-file.json) writes
`archive-{{trigger.Word}}.txt`.

What to know before using them:

- Files are written as UTF-8, with no byte-order mark and no newline translation.
- A filename resolves inside the media folder. A path separator or a `..` segment is refused.
- There is no append. Read the file, build the new content, write it back with *overwrite*.
- A stage can be told to skip if the file exists, or to refuse to overwrite. Both questions
  count a file an earlier stage of the same run has queued as already there.
- File writes happen after the collection changes and are **outside Anki's undo**. Undoing
  the operation puts the notes and cards back; it does not put the files back.
- If a write fails — a full disk, a read-only folder — the run stops there and reports
  failure naming the file. The note and card changes it had already made are kept, the files
  written before it stay written, and that file and the ones queued after it are not
  attempted.

## 8. Migrating from format 1

Definitions written for the older CopyAnywhere are converted to stages the first time Anki
starts after the update. There is nothing to do by hand, and nothing to keep switched on. The
conversion is all or nothing: if any definition cannot be converted, none of them are, the
reason is printed, and the next start tries again — so a definition you can still open and
fix by hand is never dropped from the config.

Your originals are kept. Before converting anything, the addon copies the old definitions to
`pre_stage_migration_copy_definitions` in its configuration and never touches that key again.
They are there to read, and to convert again: copy them back over `copy_definitions`, set
`version` to `0.2.0`, and the next start migrates them afresh.

A few things change on purpose — note selection searches notes rather than cards, the
`Least_reps` selection becomes `random`, files stop translating newlines on Windows — and
[`docs/staged-definitions.md`](docs/staged-definitions.md#what-migration-changes-on-purpose)
lists them all. What does *not* change is a definition the old version refused to run. A
selection it could not read still selects nothing, and says so, until you tell it otherwise:

![A refused migrated selection](docs/images/migrated-selection-refused.png)

**The one thing to check by hand** is the wording inside converted values. They keep the old
unqualified spelling — `{{Word}}`, `{{__Dest__Word}}`, `{{Recognition__Card_Due}}` — and as
long as you leave them alone they keep being read that way and run exactly as before. But the
right-click menu in the box offers the current spelling, `{{trigger.Word}}`, so the moment you
change the text (or the code) of such a value, the whole value is read as current syntax from
then on. Any old spelling still sitting in the box then refers to nothing.

Nothing checks that for you except the box itself: an unqualified name is marked in red under
the value, exactly as a misspelled one is.

![A migrated value](docs/images/value-legacy.png)

So when you edit a converted value, replace *every* reference in it with one from the menu,
and make sure no red marker is left behind.

## Where the details are

| | |
| --- | --- |
| [`docs/staged-definitions.md`](docs/staged-definitions.md) | the specification: the shape of a definition, every rule, the editor and the preview |
| [`docs/examples/`](docs/examples) | the four definitions this file uses, as they are stored |
| [`docs/follow-ups.md`](docs/follow-ups.md) | decisions taken and work deliberately left for later |

## Roadmap ideas

- Copy a note's anything into card custom data, as a card action of its own rather than
  through code.
- Copy into new notes: create notes of existing note types and fill them.
- Copy into note types: new fields, new templates, and their names.
- Copy from card templates into anything.
- Code templates in the editor for the things people write over and over.

## Acknowledgments

- tatsumoto-ren at [Ajatt-tools](https://github.com/Ajatt-Tools) for `kana_conv.py`
- piazzatron for their [Smart Notes](https://ankiweb.net/shared/info/1531888719), where the
  [text interpolation code](https://github.com/piazzatron/anki-smart-notes/blob/main/src/prompts.py#L138)
  started
- [ijgnd](https://github.com/ijgnd) for their
  [Additional Card Fields](https://ankiweb.net/shared/info/744725736), whose card data
  gathering is implemented here
