# Decided, not yet done

Two findings from the review of the staged-definitions work were deliberately left out of
that change. Both have a decision behind them; this is the record of what was chosen and
why, so the work can be picked up without re-arguing it. Sections marked **Done** have since
been implemented and are kept here for the reasoning, not as work outstanding.

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
