# Decided, not yet done

Findings from the review of the staged-definitions work that were deliberately left out of
that change, or settled in a way worth writing down. Each has a decision behind it; this is
the record of what was chosen and why, so the work can be picked up without re-arguing it.
Sections marked **Done** have since been implemented and are kept here for the reasoning,
not as work outstanding.

## Card actions and the Add dialog — Done

**The rule.** While a note is being added, a definition may edit **only the note being
added** -- its fields and its tags. Card actions on that note are *impossible*: it has id 0
and has no cards, so they are skipped with a log line and nothing runs them later. They do
not disqualify the definition, because a skip leaves nothing behind. Edits to other notes,
card actions or Edit Card stages on other notes' cards, and file writes are *forbidden*,
because the add can still be cancelled and any of those would persist; each one of them
disqualifies the definition. Reads are not edits: a card value read off the note being
added answers from a new card's defaults, as it did in format 1, and is allowed.

The cancellable window is the Add dialog before the user presses Add -- the unfocus hook on
a new note, which is where the flag and the backstop both matter most. `note_will_be_added`
fires inside `Collection.add_note`, past cancellation, so the add hook's second pile
(other-note edits, under their own undo entry) is not constrained by any of this.

**The problem it came from.** The analyser set `edits_cards` for any card action, which
made `add_note_compatible` false, and both the unfocus hook and the editor acted on that
flag. Format 1 ran a within-note definition on every unfocus in the Add dialog and let a
card action on the not-yet-added note be a no-op. So a migrated definition that fills a
field *and* flags the card filled the field only when the note was added -- and, worse,
opening it in the editor showed a red "cannot be saved yet" the user could not clear
without deleting either the card action or a trigger they never set. The format-1 fallback
in `definition_effects` had the same hole from the other side: it computed
`add_note_compatible` from `edits_other_notes` alone, so a format-1 definition with card
actions on *found* notes went into the trigger-only pile, where the commit-time backstop
refused its card edits and discarded the run. Neither side counted file writes at all.

**What changed.**

1. The analyser tells the trigger's cards from everyone else's: `edits_trigger_cards` and
   `edits_other_cards` beside `edits_cards` (which still means either), and
   `add_note_compatible = not (edits_other_notes or edits_other_cards or writes_files)`.
   The format-1 fallback answers the same question from the mode -- card actions reach the
   trigger's own cards except in Source-to-destinations -- and from `field_to_file_defs`.
2. At run time, `run_edit_note` on a note with id 0 applies the field writes and skips the
   card actions with a warning-level log line ("skipped: the note is being added and has no
   cards yet"). Nothing else in the definition is discarded.
3. The `add_note_compatible_only` backstop refuses a definition that claims compatibility
   and queues a change to another note, card **or file**, and both hooks arm it on a new
   note -- the unfocus one as well as the add one, since the Add dialog is where the add can
   still be cancelled. The refusal is a `logger.error` and a failed run; at the default
   `log_level` of `"error"` it lands in the operation log, which is the same channel the
   add hook's backstop has always used.
4. The editor says the two things separately, in two amber notes, because one definition
   can deserve both. The impossible one names the stage
   (`trigger_card_action_paths()`) and promises no later run. The forbidden one is the old
   "runs after the add, under its own undo entry" sentence for an `on_add` trigger and the
   old "skipped there, turn on Run when adding a new note" sentence for an unfocus-only
   one, and ends with "Because of" and `incompatible_stage_paths()`, which now follows the
   same split: an Edit Note with card actions is listed only when its target is not the
   trigger, a Write File stage is listed, and a callee is listed for `edits_other_notes`,
   `edits_other_cards` or `writes_files`. Neither note blocks the save.
5. The word "deferred" is gone from the hooks, `definition_is_add_note_compatible`, the
   editor and the docs. The second pile is still real -- definitions writing to other notes
   or cards need the hook to write and undo those changes itself -- but it runs in the same
   call, and the vocabulary says "runs after the trigger-only ones, under its own undo
   entry" rather than "waits until the note exists".

**How it got here.** The first pass moved the editor's refusal into a warning but worded it
as a promise: "it runs once the note is saved". It does not -- the second pile runs with
the note still at id 0, and the action is never retried. The next pass dropped the promise
and made *every* card action disqualify a definition, in both formats, which was correct
about the promise and too strict about the cards: it took the Add dialog away from the
fill-and-flag definition that format 1 had run for years. This pass separated the two
questions -- can it run, and may it -- which is the rule above.

**Rejected:** restoring format 1 exactly, by treating any card action as harmless at add
time. An action on a card that already exists really does persist through a cancelled add,
so "cards" is not one answer. Also rejected: running the impossible actions later (a flag
on the note and a sync-time pass, or a `note_was_added` hook that does not exist) -- the
original promise was made on the assumption that something did this, and nothing ever did.
Also rejected: refusing the save for either case, which protected nothing; the hooks and
the commit read the stored `effects`, never the editor, so a definition that arrived by
migration has always been judged without passing through the dialog at all.

**One visible consequence beyond the add hook:** a stored format-1 within-note definition
with a card action, which the strict pass had started skipping on unfocus while a note is
being added, runs there again -- filling its field as you type, with the flag a logged
skip -- exactly as its migrated form does. A format-1 definition that writes a file is
newly skipped there instead.

## Format-1 syntax at run time — Done

**The problem.** A migrated expression was stored in format 1's own syntax and marked
`syntax_version: 1`, and the executor kept a second resolver for it. `{{Word}}` meant "a
field of the note at hand", and which note that was came off the stage's `legacy_source` /
`legacy_destination`; `{{__Dest__Word}}` meant the other one. The review found the two
resolvers disagreeing in the search predicate, where the marker chose between
`interpolate_from_text` -- which dropped a name it could not resolve and ran the truncated
search anyway -- and the current resolver, which refuses a search that resolved to nothing.
But the predicate was only where it was noticed. The same fork ran through every expression
the executor evaluated, and it cost more than the disagreement: the editor's menu is built
from a stage's format-2 scope, so a migrated expression was shown in one language and read
in another (the `{{trigger.Word}}` a user picked from the menu was a field no note had, and
was silently dropped), and every rule about references had to be written twice.

**The decision.** Retire format-1 syntax, at migration time rather than at run time. The
migrator already records which note each stage read and wrote, so it spends that record on
the references themselves: `{{Word}}` becomes `{{trigger.Word}}` or `{{note.Word}}`,
`{{__Dest__Word}}` becomes the note the stage writes, a name the migrator itself bound stays
bare, and the two values the run supplies -- `{{__Target_Notes_Count}}`,
`{{__Query_Note_Index}}` -- stay bare as runtime values, which is what they always were.
Nothing that has been loaded speaks format 1, so the second resolver, the `legacy_*` keys
and the editor's legacy mode are deleted rather than maintained.

Alongside it, the protection format 1 never had: a reference that resolves to nothing -- an
unknown binding, an unknown field, an unknown runtime value -- is a stage error, in text and
in code, in queries and in conditions. Format 1's answer was an empty string, which is how a
typo became a truncated search over the whole collection or a field quietly written empty.
The rule an empty answer is still allowed is the one about *data*: an expression that
resolves to an empty value is empty, and a query that resolves to nothing goes through the
stage's `if_empty` policy as before.

**What changed.**

1. `promote_definition()` in `definition_migration.py` rewrites every `{{...}}` in every
   expression of a migrated definition, text and code alike, clozes included, and drops
   `syntax_version` and `legacy_isolated_variables` with them. The migrator ends with it, so
   nothing leaves that module speaking format 1; a `0.4.0` config step promotes a store an
   earlier release already staged, using the `legacy_source` / `legacy_destination` it finds
   there.
2. `resolve_references` resolves a bare head to a binding, then to a runtime value, and
   otherwise fails the stage saying which name it was. The fallback to
   `interpolate_from_text` is gone, and with it the legacy branches in the expression
   dispatcher, in `evaluate_predicate` and in the file-write code path, the
   `legacy_source` / `legacy_destination` reads in the three actions, and the isolation
   plumbing behind `legacy_isolated_variables`.
3. The analyser says the same thing at edit time, so a definition that would fail this way
   cannot be saved: an unknown bare name is a problem, and so is `{{trigger.X}}` where `X`
   is neither a field of any note type the definition's trigger names nor one of the note
   and card value keys, matched by key rather than by shape so that a misspelling of one is
   caught too. Only the trigger can be checked that far -- a note from a query holds
   whatever the query matched.
4. The editor has one syntax. It no longer promotes an expression when the text changes,
   which is what finding 3 decided and what the editor did until now: there is no legacy
   expression left for it to meet, so the three fields that remembered what it was built
   with and the `syntax_version` it wrote back are gone. The red marker under a reference
   the menu does not offer is unchanged, and is now the only mode there is.
5. The four format-1 characterization suites flipped the pins this removes -- a typo'd
   condition that used to search on regardless, the "missing fields" wording of the empty
   search, the unset `__Target_Notes_Count` that read as nothing in Within-note mode -- and
   each flip is recorded with its old and new behaviour in the work plan for this branch.

**Rejected:** leaving both resolvers in place and documenting the difference, which is what
the first pass did. It is the cheapest change and the most expensive to keep: two languages
in one box, an editor writing one and an executor reading the other, and a second place to
remember every time a rule about references changes.

Also rejected: fixing the predicate alone, so that the two branches at least agree about an
unresolved search. It answers the finding as written and leaves the fork -- and the silently
dropped reference in every other kind of expression -- exactly where it was.

Also rejected: promoting at run time, leaving stored definitions in format-1 syntax and
rewriting each expression as the executor reaches it. The stored JSON would keep a spelling
the editor cannot show or check, the analyser would still be judging text that is not what
runs, and every note in a bulk run would pay for the rewrite.

**What this costs, and who pays it.** A promoted code expression runs as format-2 code:
every binding in scope under its own name, `note` meaning the stage's note or the loop's,
and read-only facades in place of the note object format 1 handed it. References inside the
code are rewritten, but nothing else is, so code that reached for a note by some other means
is the user's to check by hand -- which the README says, in the section about migrating.
With a handful of installs and one of them the author's, that was judged cheaper than
keeping an interpreter for the old language alive to serve it.

## Process chains and `use_all_notes`

**The problem.** Format 1 kept a global list of source notes and handed it to
`apply_process_chain`, so a regex process with `use_all_notes` interpolated its pattern
across all of them, joined by `regex_separator`. Format 2 removed that concept on purpose: a
stage reads one note. `run_process_chain` passes `notes=[ctx.source_note]`, so the
`len(notes) > 1` branch in `copy_primitives.py` is unreachable and the flag is dead for every
migrated definition -- silently, with a shorter or empty result where there used to be one
built from every source.

**The decision.** Option A: teach the *migrator* to build the multi-note value explicitly,
and let `use_all_notes` die with format 1.

The migrator already turns "across notes" into query -> loop -> store -> join. A regex whose
pattern uses all notes is asking for that same shape one level down: a loop evaluating the
pattern per source note, a join with `regex_separator`, then a regex process whose pattern is
the joined result. The replacement needs its own loop and join, with
`replacement_separator`. No runtime change, nothing new for the executor to know, and the
migrated definition is finally readable -- which the flag never was.

### Done: the warning

The migrator now reports it. Only Destination-to-sources is asked, and only when its
selection could hold more than one note, because that is the whole of what format 1 could do
here: Within note read a copy of the trigger note and Source-to-destinations read the trigger
note, so `len(notes) > 1` was already false in both. Variables are left out for the same
reason -- each was evaluated against a single note, so the flag never did anything there
either.

This stands on its own. A config already migrated on a user's machine will not be migrated
again, so a warning is the only thing that can reach those definitions at all.

### Still open: whether the migrator branch is worth writing

**How often this happens is not answerable from this repository.** The shipped `config.json`
carries no definitions, so the only corpora are users' own configs. Two things make the
question answerable on a real machine rather than by guessing:

* On a config that has not yet migrated, the warning above names the affected definitions the
  first time Anki starts.
* On one that already has, the format-1 originals are still under
  `pre_stage_migration_copy_definitions`, so the same predicate can be run over them.

**The recommendation is therefore the warning alone, for now.** Writing the branch first
would mean a code path that may never fire on any real config, cannot be tested against
reality, and runs exactly once per machine -- the combination that earns the least confidence
per line. Wait until at least one affected definition is known to exist; then either write the
branch or rebuild those few by hand in the new editor, whichever is smaller at that point.

**One design point, checked and worth keeping:** the hoist is sound. Neither the pattern nor
the replacement interpolation ever reads the text flowing through the chain -- both read only
note fields and variables (`copy_primitives.py`, the `is_regex_process` branch) -- so their
loops and joins can run as stages *before* the chain, with the process reading the two joined
results. If the branch does get written, that is why it does not need the chain taken apart.

**Rejected:** giving `ExpressionContext` a notes list (reintroduces the ambient "current
source notes" format 2 was designed to remove, and needs an answer for nested loops that
would come back as a bug); and making the process name a binding to interpolate across
(honest, and it would work in natively authored definitions too, but it is a schema change
plus new UI in `edit_extra_processing_dialog.py` -- the largest of the four).

## Following a rename in Anki

**The problem.** A definition stores every name it uses as text: the note types it triggers
on, the decks it is limited to, the fields it writes, the fields its expressions read, the
card types its card actions name, and whatever names the user typed into a search. Anki lets
all of those be renamed, and nothing tells the addon. Since the format-1 syntax was retired
a reference to a renamed field at least fails loudly -- the stage errors and the editor
refuses the save -- but that is only one of the six, and the other five change what the
definition does without saying anything at all.

**What was found**, against Anki 25.9.4.

*The hooks.* `anki/hooks_gen.py` has no rename hook of any kind: `deck_added` (161),
`note_type_added` (393), `notes_will_be_deleted` (518) and `schema_will_change` (546) are
the collection-level ones, and the first two are marked "Obsolete, do not use". On the GUI
side there is exactly one: `fields_did_rename_field(dialog, field, old_name)`
(`_aqt/hooks.py:3255`), fired from `aqt/fields.py:167` with the note type on
`dialog.model` -- old name and new name both in hand. Its two neighbours
`fields_did_add_field` (3174) and `fields_did_delete_field` (3211) cover the other two
things the Fields dialog does. Nothing else has one. A note type rename is
`nt["name"] = text` followed by `update_notetype_legacy` (`aqt/models.py:127-135`); a card
type rename is `template["name"] = name` followed by a redraw (`aqt/clayout.py:656-665`),
saved later by the dialog's `accept()`; a deck rename is
`col.decks.rename(deck_id, new_name)` (`aqt/operations/deck.py:45-53`), which takes the id
and the new name and never sees the old one. All three surface only as
`operation_did_execute(changes, initiator)` (`aqt/operations/__init__.py:159`), and
`OpChanges` carries booleans -- `notetype`, `deck` -- not names or ids. A rename made on
another device arrives with no hook at all beyond `sync_did_finish()`
(`aqt/main.py:1107`), which takes no arguments, followed by `mw.reset()` and its
everything-changed `operation_did_execute` (`aqt/main.py:842-850, 893-904`).

*The silent five.* A renamed note type leaves `triggers.note_types` naming nothing, and the
hooks compare it against the note's live name (`hooks/note_hooks.py:66, 251, 397`), so the
definition simply stops running; only the bulk path says anything, and only in the log
(`logic/copy_fields.py:495-500`). A renamed deck leaves `triggers.deck_names` matching no
deck id, so `note_passes_deck_whitelist` skips every note benignly
(`logic/copy_fields.py:582-600`). A renamed card type leaves a card action's
`NoteType<::>CardType` matching no template, and
`card_actions_by_template_name` drops it without a word (`logic/copy_primitives.py:459-481`).
A renamed field leaves `selection.sort_field` sorting every note equal
(`logic/copy_primitives.py:367-380`) and an unfocus trigger field never firing
(`configuration.py:589-601`). And every name inside a search -- `deck:"..."`, `note:"..."`,
`card:"..."`, `Field:value` -- matches nothing rather than erroring: Anki's search returns
zero results for all four, so the stage takes its `if_empty` path, which defaults to
`continue`.

*What is already there.* The analyser takes `known_fields` and refuses a reference to a
field the trigger's note types do not have (`logic/flow_analysis.py:346-365`), and the
editor fills it from the live note types (`ui/stage_editor_context.py:232-244`) -- so the
editor already reports one of the six, for the trigger binding only. `migrate_config` is the
pattern for a one-off pass over the stored definitions (`configuration.py:476-501`),
`Config._save_definitions` is the one way in for a rewrite (`configuration.py:732-750`), and
`operation_logging` opens a file for a message that has nowhere else to go
(`logging_setup.py:261-272`). None of them knows anything about note type, deck or card type
names: the analyser never reads them.

**The options.**

*(a) Report only.* On `collection_did_load`, walk the stored definitions against the live
collection and list every stale name in an operation log, and mark the stale ones in the
definition picker. Needs a new check for the five kinds the analyser does not cover, because
`analyze_definition` only ever looks at field references. Roughly one job. Fails only by
being noisy -- a definition kept deliberately for a note type the user has not made yet
reports forever.

*(b) Follow what the hooks make safe.* Register `fields_did_rename_field` and rewrite the
stored definitions: field writes, unfocus trigger lists, `sort_field`, and `{{trigger.X}}`
references whose binding is the trigger and whose note type is the renamed one. About one
job, but only for fields, and it carries two failure modes the report-only tier does not.
The hook fires while the Fields dialog is still open, before `accept()` -- the user can
still press Cancel and discard the rename (`aqt/fields.py:287-296`), leaving the definitions
rewritten to a name that does not exist. And a rewrite inside a search query or inside code
is a text substitution over free text: `Word:src` and `note['Word']` are the names a rewrite
would have to find, and `{{trigger.Word}}` is not the only spelling of them.

*(c) Snapshot comparison.* Keep the note type ids, template ids and deck ids the stored
definitions name, with the names they had, and on `collection_did_load` -- and after
`operation_did_execute` with `notetype` or `deck` set -- compare against the live ones. An id
whose name changed is a rename with both names in hand, which is exactly what the hooks do
not give; it is also the only thing that sees a rename made on another device. Two jobs:
the snapshot has to be stored, kept in step with the definitions, and reconciled with a
deletion, a re-creation under the old name, and a swap of two names.

**The recommendation is (a), on its own, first.** It is the only tier that covers all six
kinds of name, it is the only one that needs nothing from a hook Anki does not have, and it
is the one that can be built without deciding anything the other two would have to decide.
Every rewrite tier needs to know which names are safe to rewrite mechanically -- a note type
name in `triggers.note_types` is a whole structured value and is; a note type name inside
`note:"..."` in a query the user typed is not -- and the report is what tells us, from a real
config, how often each kind actually goes stale. It also makes the recovery concrete: the
report names the definition and the stale name, and the editor is already where a name is
fixed. Tier (c) is the one to build second if the report says renames are common, because it
subsumes (b) and is the only one that survives a sync; (b) is worth building only for the
fields it can do safely, and only if the report shows field renames dominating.

One defect is pinned rather than only described:
`test/test_renamed_in_anki.py` says that a card action whose card type was renamed should
say so. It is the sharpest of the five, because a card action is pure loss -- the field
writes in the same definition still land, so the run reports success. It was an xfail until
the decision below was built; it is a plain test now.

**decision: a fourth option, (d): store the ids.** None of the three tiers was taken as it
stood. What a definition keeps for a note type, a deck and a card template is now a
reference -- `{"id", "name"}`, and `{"note_type_id", "template_id", "name"}` for a card type
-- and **the id wins where it still exists, the name being looked up only when it does not**
(`logic/object_refs.py`). That needs no rename hook at all, which settles what (b) could not:
the hook fires before the Fields dialog is accepted and a rename can be cancelled out from
under it, and a rename undone with Ctrl+Z fires nothing that names anything. It also follows
a rename made on another device, which (c) is the only other tier to do, and it costs
nothing per rename because there is nothing to rewrite.

Field names stay names, because a field is what the user types in expressions, queries and
code -- the row of the table above that a mechanical rewrite would mangle. What (a) and (c)
are still needed for is kept: one reconcile pass refreshes the cached names by id and
rewrites a renamed trigger field's parsed reference tokens, and what stays name-only is
reported rather than rewritten. Search text is never rewritten: `col.replace_in_search_node`
swaps every term of a kind, so it cannot rename one deck inside a query naming two.

Built in three commits on `feat/copyanywhere_rename_protection`: the references and the
readers that go through them, the reconcile pass, and the editor's reporting.

**What the reconcile pass turned out to be** (`logic/rename_reconcile.py`, registered in
`hooks/rename_hooks.py` on `collection_did_load` and on `operation_did_execute` when
`changes.notetype or changes.deck`). It binds a null id whose name resolves, refreshes the
cached name of every reference whose id still resolves, follows a renamed field or card
type of a trigger note type into that definition's field slots and its `{{trigger....}}`
tokens -- the whole rename map applied in one step, so a swap of two names is correct --
and reports the rest: a reference that resolves to nothing, a deleted object, a field or
template with no id to be followed by, and code still mentioning an old name. Only an
expression's `text` is rewritten, never its `code` and never a search term, which is (a)'s
job kept where a rewrite would be a guess.

Both names of a rename come out of `name_snapshot` in the addon config -- per referenced
note type id, its name and its fields' and templates' names by *their* ids, plus a name per
referenced deck id -- which is tier (c)'s snapshot, kept in step by `_save_definitions` so
it is never older than the last save. That is what makes a rename visible with no hook at
all, including one synced in from another device and one undone with Ctrl+Z, which is just
a second rename the same pass follows back. The pass writes the config only when something
changed, and never while a dialog is still open, so a cancelled Fields dialog is a
non-problem.

**Where (a)'s report ended up.** The pass writes everything into one operation log, which
is invisible at the default log level, so the three findings a user can act on were put
where they already look. A structured reference that resolves to nothing is shown in the
editor under the name it was written with, marked `(not found)`, and blocks the save; a
query stage carries an amber note listing what its search spells that the collection does
not have; and the definition picker marks a definition that names something unresolved,
with the names in its tooltip -- its references checked live as the picker is drawn, the
last pass's `gone` entries only while the definition still names them
(`rename_reconcile.still_names`), and redrawn when a definition is saved from the picker.
A definition marked `broken_by_rename` is not run on any run path (the editor's preview
still runs it), and the picker shows it with its own icon and a checkbox that cannot be
ticked (`docs/staged-definitions.md`, "Following a rename in Anki"). The search scan
(`logic/query_terms.py`) checks `deck:`, `note:` and `card:` terms and field searches by
exact name only -- a term holding
a wildcard, a regex or an unresolved `{{...}}` reference is skipped rather than guessed at,
because a scan that cried wolf over a working query would be ignored. The same scan runs in
the pass, so a query that went stale on another device is reported without anything being
renamed here. `selection.sort_field` is still not rewritten and a note without the field
still sorts as empty -- a query legitimately mixes note types, and the characterization
suites pin that fallback -- but a run where no selected note had the field logs one warning,
because then the sort did nothing at all.

## A bulk-run test

**The problem.** In a bulk run every trigger note starts from the saved notes and cards, and
the caller saves a definition's notes and cards once, after every trigger note has run. So
when two trigger notes write the same note, or edit the same card, the later one's copy
replaces the earlier one's and the earlier write is lost, while the run reports success.
That is kept on purpose (decision 5 of the PR 14 review):
`test_copy_fields_op.py::TestOneDestinationFromSeveralTriggerNotes::test_the_first_trigger_notes_write_is_lost`
pins it, and its comment says why -- saving after every trigger note was far slower, and made
the result depend on the order of the trigger notes. Since that review only a card a card
action edited is handed over, so a trigger note that merely writes a card's note no longer
undoes another one's edit of the card; what is left is the real double write, and nothing
tells the user a definition does one.

**The decision** (the user's design). A test the user runs from the editor, over a sample of
trigger notes from their own collection, that says what to fix.

1. A new log level, `problem`, that is written when `log_level` is `debug` and not when it is
   `warning` or `error` (where `info` falls is not decided). The runtime logs a `problem`
   when it sees such a thing -- first of all, a note or card that an earlier trigger note
   in the same bulk run already wrote being written again. Only the bulk loop in
   `copy_fields` sees every trigger note's output, so that is where it is noticed: the same
   note id arriving twice in `copied_into_notes`, or an edited card replacing one already in
   `copied_into_cards_dict`.
2. A "Run bulk-run test" button in the preview pane runs a preview bulk run over the sample:
   real searches and code, nothing committed, nothing written to the media folder. The
   preview then reads its own log file and looks for problems, and perhaps errors.
3. A report element, a dialog or inline in the pane, whichever fits, says for each kind of
   problem found what to change in the definition -- for the double write, that only the
   last trigger note's write is kept, and that a narrower query or a Destination-to-sources
   definition reading many notes into one would avoid it -- rather than showing the log
   lines.

That button and the report are the only new UI.

**What there is to build on.** A preview runs one trigger note through `PreviewCommitter`,
which publishes into its own run rather than into shared lists, so a bulk preview needs the
lists the bulk loop keeps without its saves. The pane collects a preview's errors in memory
today (`preview.py`, `_MessageCollector`), not from a file; operation logs are files under
`user_files/logs/` (`logging_setup.py`).

**decision: agreed, not built**

## Open points left by the PR 14 review fixes

Small things the fixes found and deliberately did not settle. None is a regression.

* **A `)` in an interpolated value can close a search condition's group early.** The
  condition is searched as `({search}) nid:{id}` so that an `OR` stays scoped to the note,
  but a field value interpolated into it -- `Word:{{trigger.Word}}` with a word containing
  `) OR (` -- can still end the group, and the search then reaches other notes. The text is
  the author's data, and the add path's matcher adds the same parentheses, so the two paths
  agree. Whether to escape interpolated values is not decided: a search may interpolate
  search syntax on purpose.
* **The add-path search matcher does not port Anki's tag rewriting.** A note whose tags Anki
  would rewrite on save (whitespace, control characters, empty `::` parts) cannot be judged
  for a `tag:` term, which fails the definition naming the term. Anki's unification of a
  tag's case with an existing tag is ignored, which is harmless while tag search ignores
  case.
* **The warning about unjudgeable search terms is partial.** The editor warns only about a
  trigger search condition whose literal text it can parse (no `{{...}}`, no code, no
  process chain) in the definition being edited: a condition in a called definition is not
  warned about, and a definition nobody opens in the editor gets no warning at all. The run
  still fails naming the term, so the gap is only in how early the user hears.
* **Some card reads still fetch per read.** Code mode's `cards` is fetched once per
  evaluation, but `note.cards` on a facade fetches on every read, and card-action code
  (which runs with format 1's names, without the stage's bindings) fetches its note's
  cards on every run.
