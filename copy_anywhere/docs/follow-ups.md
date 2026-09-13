# Decided, not yet done

Two findings from the review of the staged-definitions work were deliberately left out of
that change. Both have a decision behind them; this is the record of what was chosen and
why, so the work can be picked up without re-arguing it.

## Card actions and the Add dialog

**The problem.** The analyser sets `edits_cards` for any card action, which makes
`add_note_compatible` false, and both the unfocus hook and the editor act on that flag.
Format 1 ran a within-note definition on every unfocus in the Add dialog and let a card
action on the not-yet-added note (id 0) be a no-op. So a migrated definition that fills a
field *and* flags the card now fills the field only once the note is saved -- and, worse,
opening it in the editor shows a red "cannot be saved yet" that the user cannot clear
without deleting either the card action or a trigger they never set.

**The decision.** The compatibility rule stays: a note with id 0 has no cards, and format 1
"working" here only meant the action silently did nothing. What changes is how the rule
surfaces.

1. Move the add-note case out of `StageDocument.save_blockers()` and into
   `analysis.warnings`, which the dialog already renders in amber above the red blockers.
   Word it as what happens rather than as a refusal: this runs while a note is being added,
   but the card action on *&lt;stage&gt;* needs a card, so it will run when the note is
   saved instead. The definition then saves, and the behaviour is unchanged either way --
   the hook defers it regardless of what the editor said, and the commit refuses cross-note
   and card mutations while adding whatever the stored `effects` claim. The editor's refusal
   was never the thing protecting anyone.
2. Name the offending stage in that message. `incompatible_stage_paths()` already computes
   it; the current "it edits other notes or cards" leaves the user hunting.
3. Add it to "What migration changes on purpose" in `staged-definitions.md`: the field fills
   a moment later rather than as you type.

**Rejected:** treating a card action on the *trigger* note as add-compatible, restoring
format 1 exactly. It makes `edits_cards` stop meaning what it says, and the commit-time
backstop would need a special case for "cards of the note being added" -- two rules where
there is now one, to preserve a no-op.

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

**Also do, and do first:** have the migrator warn for any chain with `use_all_notes` on a
definition with more than one potential source. It already collects warnings. This matters
independently of the above, because a config already migrated on a user's machine will not
be migrated again, so those definitions need to be reported rather than fixed.

**Worth checking before writing the migrator branch:** how many real definitions set
`use_all_notes` together with a multi-note query. If it is rare enough, the warning alone --
and rebuilding the few affected definitions by hand in the new editor -- is a better trade
than a migrator branch that is hard to test and runs once.

**Rejected:** giving `ExpressionContext` a notes list (reintroduces the ambient "current
source notes" format 2 was designed to remove, and needs an answer for nested loops that
would come back as a bug); and making the process name a binding to interpolate across
(honest, and it would work in natively authored definitions too, but it is a schema change
plus new UI in `edit_extra_processing_dialog.py` -- the largest of the four).
