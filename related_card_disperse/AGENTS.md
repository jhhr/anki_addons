# related_card_disperse

Read the root [AGENTS.md](../AGENTS.md) first.

Keeps "related" review cards from falling due on the same day. The user writes **rules** in
a dialog (Tools → Related Card Disperse): target note types and optionally card types;
either an interpolated search query (default `nid:{{__Note_ID}}`, i.e. siblings) or Python
"code mode" returning a query or card ids; on-review and/or on-sync triggers; a cap on
related cards. A run nudges future due dates **inside each card's existing fuzz window** to
maximise the minimum gap between them, marks moved cards with custom data `v="d"`, and
buries related cards already in today's pool, at most one per group per day. A deck-gear
command, "Disperse due cards", buries colliding cards within today's session. An optional
toggle gives every multi-card note type without a rule of its own a derived sibling rule.

There is no README or `config.md`. The docstrings are extensive and are the specification;
read the one on the function you are changing.

## Map

| path | role |
| --- | --- |
| `__init__.py` | calls `addon.init_addon()` in `try/except ModuleNotFoundError`, re-raising unless the missing module is `aqt`/`anki`, so the package imports without Anki |
| `addon.py` | the four `init_*_hook` calls and the Tools action |
| `core.py` | **the Anki-free half.** `anki` only under `TYPE_CHECKING`. Id normalising, `split_quoted_names` / `join_quoted_names`, `CARD_TYPE_SEPARATOR`, `reviewed_card_variables`, `cap_card_ids`, `remaining_note_cards`, `group_overlapping_sets`, `select_cards_to_bury`, `select_backlog_cards_to_bury`, `order_session_blocks`, rule-error aggregation |
| `configuration.py` | `RelatedRule` / `ConfigData` TypedDicts, `migrate_data`, `Config`, derived-rule helpers (`derived_sibling_rule(s)`, `is_derived_rule`, `DERIVED_RULE_GUID_PREFIX`) |
| `logic.py` | `get_applicable_rules`, `resolve_rule_candidates`, `build_disperse_plan`, `apply_disperse_plan`, `run_rule_for_reviewed_card`, the sync and browser runs, and the optimiser `maximize_due_gap` / `find_max_min_gap_and_arrangement` (binary search for the largest feasible minimum gap) |
| `bury.py` | models today's review session (`_review_order_clause`, session order, `deck_limit_map`), `run_deck_bury_disperse` |
| `hooks_review.py`, `hooks_browser.py`, `hooks_deck_browser.py`, `sync_hook.py` | the four triggers |
| `ui.py` | `RelatedCardDisperseDialog(ScrollableQDialog)`, `RuleEntry` |

Flow: trigger → `Config().load()` → `rules_for_model` → `get_applicable_rules` →
`resolve_rule_candidates` (interpolate or run code, `find_cards`, keep live review cards,
cap; the anchor card is exempt from the cap) → `build_disperse_plan` →
`apply_disperse_plan`.

## Invariants

- **The interval is never re-decided.** The window is the fuzz range around
  `due - last_review`, not around `card.ivl`, because a custom scheduler may move `due` and
  leave `ivl` alone.
- Cards due today or earlier are pinned as backlogged; only burying disperses them. The
  per-card skips in `apply_disperse_plan` stop the `today + 1` floor from stamping unmoved
  overdue cards onto tomorrow. That was a real bug (commits `10244b7`, `faa6802`); keep the
  skips.
- `v="d"` is written only on cards whose due actually changed, through the shared
  `write_custom_data`. It exists for `custom_schedule_helper`, whose auto-reschedule filter
  excludes `'r'` and `'d'`. See [docs/anki-patterns.md](../docs/anki-patterns.md).
- Buries use `bury_cards(manual=False)`, so Anki's "Unbury manually buried" keeps its
  meaning. The deck run is one backend call: one undo step, idempotent.
- Undo: the review hook merges into `col.undo_status().last_step` (the answer's own entry);
  sync and browser runs use `add_custom_undo_entry` + per-card `merge_undo_entries`.
- Threading: sync, browser and deck runs use `mw.taskman.run_in_background` with
  `mw.progress`, cancellation, and `mw.reset()` when done; not `CollectionOp`. The review
  hook is synchronous on the main thread, so it must stay cheap. Tooltips are deferred with
  `mw.progress.single_shot(100, ...)` because the closing progress dialog would kill them.
  The browser run calls `browser.table.reset()`.
- Loop guards: `processed_rule_card_pairs` prevents re-running a (rule, card) pair;
  `remaining_note_cards` always drops the anchor. The sync run resolves candidates uncapped,
  groups overlapping sets when `dedupe_sync_groups` is on, then caps per group. Rule errors
  are reported once per rule, not once per card.
- `sync_hook._existing_card_ids` drops ids of deleted cards: revlog rows outlive their cards
  and `get_card` would raise `NotFoundError`.
- `bury._review_order_clause` mirrors rslib's `review_order_sql` clause for clause and relies
  on SQL functions rslib registers (`fnvhash`, `extract_fsrs_*`), falling back to due order
  on exception. Deck-position orders are approximated by deck id. For a filtered deck, `due`
  is the row position. Limits come per top-level deck from one `deck_due_tree` build. A new
  Anki release can change any of this; check rslib before "fixing" the SQL.
- `core.py` stays importable without Anki. Put pure decisions there and test them there.
- `CARD_TYPE_SEPARATOR = "<::>"` deliberately equals copy_anywhere's. Note and card type
  lists use the quoted `A", "B` format that `MultiComboBox` emits.
- User code runs through the shared `execute_code_core`, with the reviewed card and note
  wrapped read-only. `__Reviewed_Card_Ord` is 1-based.

## Config

`Config.load()` runs `migrate_data` every time: it sets defaults, applies the renames
`show_no_overlap_outcome` → `show_unchanged_outcome` → the inverted `hide_review_unchanged`,
and **force-disables any rule whose active query is empty**, because `find_cards("")`
matches the whole collection. Config is loaded fresh on each trigger. Writes happen only in
the dialog's `_save_all` via `update_global` and `replace_rules`. Derived rules are never
stored: `replace_rules` filters them out, and the dialog promotes a derived row to a stored
rule (fresh uuid) only on a real edit. A saved but disabled rule opts its note type out of
default sibling dispersal. A new key goes into the TypedDict, `migrate_data` and
`config.json`.

## Tests

`related_card_disperse/test` is in the root `testpaths`: `python -m pytest -q
related_card_disperse/test`. Five files cover `core.py`, the bury selection functions, the
browser run's planning (`plan_note_rule_runs`, with a monkeypatched `logic.mw.col`), derived
rules (including that they never reach the config), and rules. They import as
`from related_card_disperse.core import ...`. Not covered: the `bury.py` SQL, `ui.py`, and
the date optimiser against a real collection. New pure logic gets a test; for collection
behaviour use `anki_shared.testing.real_anki` as `copy_anywhere/test/conftest.py` does.

## Shared code

Declares `interpolate`, `ui`, `scheduling`, `anki`. The interpolation engine, `execute_code`
and the dialog widgets are shared with `copy_anywhere`; `write_custom_data`,
`sync_hook_base` and `due_dates` with `custom_schedule_helper`. Edit them in `anki_shared/`
and run all suites. Already duplicated here: `logic._filter_revlogs` and
`logic._last_review_date` mirror `shared/scheduling/due_dates.py` but take pre-fetched
revlogs from a stats cache; the background-run wrapper appears three times (`logic.py`
twice, `bury.py`). See [docs/shared-code.md](../docs/shared-code.md) before adding to that.
