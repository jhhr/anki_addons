# Decided, not yet done

Two findings from the review of the staged-definitions work were deliberately left out of
that change. Both have a decision behind them; this is the record of what was chosen and
why, so the work can be picked up without re-arguing it. Sections marked **Done** have since
been implemented and are kept here for the reasoning, not as work outstanding.

## Card actions and the Add dialog — Done

**The problem.** The analyser sets `edits_cards` for any card action, which makes
`add_note_compatible` false, and both the unfocus hook and the editor act on that flag.
Format 1 ran a within-note definition on every unfocus in the Add dialog and let a card
action on the not-yet-added note (id 0) be a no-op. So a migrated definition that fills a
field *and* flags the card now fills the field only when the note is added -- and, worse,
opening it in the editor showed a red "cannot be saved yet" that the user could not clear
without deleting either the card action or a trigger they never set.

The first pass moved that refusal into a warning, but worded the warning as a promise: "it
runs once the note is saved". It does not. `note_will_be_added` fires *before* the insert,
the hook's second pile runs in the same call with the note still at id 0, and a card action
on that note has no card to reach then and is never retried. The field write lands (the
add saves the mutated note object), the action is silently lost, and nothing says so. The
format-1 fallback in `definition_effects` had the same hole from the other side: it
computed `add_note_compatible` from `edits_other_notes` alone, so a format-1 definition
with only card actions on *found* notes went into the trigger-only pile, where the
commit-time backstop refused its card edits and discarded the run.

**The decision.** Trigger-note card actions at add time are not supported, and nothing
promises otherwise. A note with id 0 has no cards, and format 1 "working" here only meant
the action silently did nothing. The compatibility rule stays, in both formats:
`edits_cards` makes `add_note_compatible` false whether the definition is format 2 (the
analyser) or format 1 (the fallback now reads `not modifies_other and not edits_cards`).
What changes is what the user is told, and what the log says.

1. The editor's warning names the stage and says what happens, not when: this runs when a
   note is added, but the card action on *&lt;stage&gt;* needs a card the note being added
   does not have yet, so that action will not run for it; edits to other notes, and card
   actions on their cards, still do. `trigger_card_action_paths()` finds the stage -- an
   Edit Note targeting `trigger` with card actions -- and the existing
   `incompatible_stage_paths()` list still follows as "Because of".
2. At run time, `run_edit_note` on a note with id 0 applies the field writes and skips the
   card actions with a warning-level log line ("skipped: the note is being added and has no
   cards yet"). Nothing else in the definition is discarded, and the `add_note_compatible_only`
   backstop is untouched: it still refuses a definition that claims compatibility and queues
   changes to another note or card.
3. The word "deferred" is gone from the hook, `definition_is_add_note_compatible`, the
   editor and the docs. The second pile is still real -- definitions writing to other notes
   or cards need the hook to write and undo those changes itself -- but it runs in the same
   call, and the vocabulary now says "runs after the trigger-only ones, under its own undo
   entry" rather than "waits until the note exists".

**Rejected:** treating a card action on the *trigger* note as add-compatible, restoring
format 1 exactly. It makes `edits_cards` stop meaning what it says, and the commit-time
backstop would need a special case for "cards of the note being added" -- two rules where
there is now one, to preserve a no-op. Also rejected: running those actions later (a flag
on the note and a sync-time pass, or a `note_was_added` hook that does not exist) -- the
earlier promise was made on the assumption that this happened, and nothing ever did it.

**As built,** the two halves of "runs while a note is added" still end differently. With
`on_add` the definition runs when the note is added, minus the card action on it. An
unfocus-only trigger is skipped in the Add dialog and `on_add` is off, so there is no
later moment either; that case is told to turn on "Run when adding a new note" and, when
the definition has such a card action, that even then it will not run for the new note.
The format-1 change has one visible consequence beyond the add hook: a stored format-1
within-note definition with a card action is now skipped on unfocus while a note is being
added, like its migrated form, and runs on add instead.

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
