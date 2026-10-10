# custom_schedule_helper

Read the root [AGENTS.md](../AGENTS.md) first.

The Python companion to a custom scheduling script (`custom_scheduler.js`, which the user
pastes into Anki's deck options). It retroactively applies that scheduler to existing
cards: Reschedule (all, or reviewed in the last N days), Postpone, Advance, Load Balance,
Free Days, and auto-reschedule of cards reviewed on another device after sync. It also
recomputes ease from the whole review history ("Auto Ease Factor") and can replay a card's
revlog through FSRS to write a difficulty-derived `factor` into each revlog row.

Origin: a fork of FSRS4Anki Helper, brought into this repo as a subtree. `.github/` and the
README's images are upstream leftovers (the funding file names the upstream author) and are
inert here; do not extend them. Sibling dispersal was removed from this addon;
`related_card_disperse` does that now.

## Map

| path | role |
| --- | --- |
| `__init__.py` | everything runs at import: Tools submenu, deck gear entries, browser context submenu, `state_did_change`, config-change callback, `init_sync_hook()` |
| `configuration.py` | key constants; `Config` with one property per key, every setter saves |
| `utils.py` | parses the scheduler JS out of the collection config (`check_custom_scheduler`, `get_version`, `get_deck_parameters`, `get_skip_decks`, `DeckParamError` family); `get_rev_conf` |
| `schedule/reschedule.py` | `Scheduler` (`set_load_balance`, `apply_fuzz`, `next_interval`); `reschedule` → `reschedule_background` → `reschedule_card` |
| `schedule/postpone.py`, `advance.py`, `free_days.py` | the other commands; free days calls `reschedule(filter_flag=True)` |
| `ease/ease_calculator.py` | pure: `calculate_ease`, `get_success_rate`, `moving_average` |
| `ease/auto_ease_factor.py` | `suggested_factor`, `adjust_ease`, review-hook functions, stats tooltip |
| `ease/fsrs_calculator.py`, `ease/fsrs_ops.py` | revlog replay through `py_fsrs`; `adjust_fsrs_revlog` |
| `ease/export.py` | ease export/import; uses legacy Anki and Qt5-era APIs, likely broken on current Anki |
| `sync_hook.py` | detects remotely reviewed cards via the shared `sync_hook_base`, then reschedules |
| `custom_scheduler.js` | the scheduler itself |
| `py_fsrs/` | vendored, modified py-fsrs; see below |
| `tools/sync_py_fsrs.sh` | pulls upstream py-fsrs changes into `py_fsrs/` |
| `test/run_with_setup.py` | a launcher for running a module with relative imports; not a test |

## The JavaScript and the Python are one algorithm, written twice

- `Scheduler.next_interval` in `schedule/reschedule.py` is a hand port of `adjustIvl` and the
  again/hard logic in `custom_scheduler.js`. **Change one, change the other**, in the same
  commit, and say in the body that you did.
- `utils.CUR_SCHEDULER_VERSION` must equal the `// Custom Scheduler vX.Y.Z` comment in the JS.
- Python finds things in the user's pasted JS by regex: the version comment,
  `const deckParams = [...];`, `skipDecks = ...;`, and the parameter names `deckName`,
  `daysUpper`, `minAgainMult`, plus the global config's deck name string. Renaming any of
  them in the JS breaks the Python side.
- `get_deck_parameters` feeds the `deckParams` literal to `json.loads` (after stripping
  comment lines and trailing commas). The `custom_scheduler.js` in this repo writes it with
  unquoted keys, which that parse rejects (`MalFormedDeckParamsError`). Known inconsistency;
  do not assume the file in the repo is what the user's collection contains.
- Known divergence: JS fuzz starts at interval 2.5, Python's at 7. Python uses
  `col.fuzz_delta` when `int_version() >= 231001` and a seeded fallback below that.
- The user's live scheduler is in their collection config (`cardStateCustomizer`), not in
  this file. Editing `custom_scheduler.js` changes nothing for them until they paste it;
  tell them when a change requires that.

## Custom data

All writes go through the shared `write_custom_data` (100-byte limit). Keys and their
cross-addon meaning are in [docs/anki-patterns.md](../docs/anki-patterns.md). Here:

- `v`: `"r"` reschedule, `"p"` postpone, `"a"` advance. The JS resets it to `0` on answer.
- `e`: `"a"` after auto-ease. `sr`: success rate, written by `suggested_factor`, read by the
  Again branch in both `next_interval` and the JS.
- Auto-reschedule and free days select with `... '$.v') NOT IN ('r','d')`; postpone with
  `!= 'p'`; `adjust_ease(marked_only=True)` with `$.e = 0`. A card without the key yields
  NULL and is excluded, so only cards that have been through the scheduler JS are touched.
  That is intended. `'d'` is written by `related_card_disperse` and must stay in the filter.
- `update_card_due_ivl` (shared) deliberately does not set `card.ivl`; repeated rescheduling
  would otherwise ratchet it upward.

## Threading and undo

- Reschedule, adjust-ease and adjust-FSRS use `mw.taskman.run_in_background`, not
  `CollectionOp`, and write the collection from the worker. Progress goes through
  `run_on_main`; `mw.reset()` runs in `on_done`. Postpone and advance run synchronously.
- `sync_hook.auto_reschedule` calls `fut.result()` inside `sync_did_finish`, blocking the UI
  until rescheduling ends. `related_card_disperse` also acts on `sync_did_finish`, and the
  two should not move the same cards concurrently. Which handler runs first follows Anki's
  addon load order, which is the alphabetical order of the install folder names: a property
  of each user's install, not of this repo. Nothing in the code enforces an order, so do not
  write code that assumes one, and think about the overlap before making this call
  asynchronous.
- Reschedule, postpone and advance: `add_custom_undo_entry`, then per card `update_card` +
  `merge_undo_entries`.
- `adjust_ease` and `adjust_fsrs_revlog` run raw `UPDATE revlog SET factor ...` through
  `db.execute`. No undo entry, no usn bump; the comments say this breaks undo.

## py_fsrs

A trimmed, locally modified copy of `open-spaced-repetition/py-fsrs` v6.3.2, as ordinary
tracked files. `py_fsrs/README.md` is the authoritative record. `optimizer.py` is omitted
(torch). There are six algorithm changes in `fsrs/scheduler.py`, each marked with a
`Vendored fork:` comment; the hoisted initialisation that falls through the state `match`
is described as load-bearing, and `ease/fsrs_ops.py` depends on it by forcing card state
from the revlog type. It relies on the `typing_extensions` Anki bundles.

Do not hand-edit `py_fsrs/` to match upstream. Use `tools/sync_py_fsrs.sh`, which 3-way
applies the upstream diff and runs upstream's tests against the result; four failures are
expected and listed in the README. `py_fsrs` uses `match`, so it is the one place in the
repo that already needs Python 3.10+.

## Config

Keys in `config.json`, documented in `config.md`. There is no migration and there are no
defaults in code: properties index `self.data[...]`, so a key missing from the user's
`meta.json` is a `KeyError`. Operations build a fresh `Config()` and `load()` each run; the
module-level `config` in `__init__.py` reloads on config change and on each gear-menu open.

## Tests

None. The addon is not in the root `testpaths`; mypy does cover it.
`ease/ease_calculator.py`, `ease/fsrs_calculator.py` and `py_fsrs/` are Anki-free and
testable today. Most other modules touch `mw` at import, and `ease/auto_ease_factor.py` uses
`card=mw.reviewer.card` as a default argument, which is evaluated at import, so it cannot be
imported without a live reviewer. When you change scheduling maths, add a test for the pure
part (create `custom_schedule_helper/test/test_*.py` and add the path to the root
`pytest.ini`), and state plainly in your report that the rest was not exercised.

## Known defects, unfixed

Verified by reading, not by running. Do not build on them; fix them as their own change.

- `schedule/reschedule.py`, Again branch: `return min(int(round(new_interval)), 1)` can never
  exceed 1; `max` was probably meant.
- `utils.get_rev_conf`: the last `except KeyError` assigns `deck_max_ivl = 0` instead of
  `deck_again_fct`, so the return raises `UnboundLocalError` on that path.
- `get_skip_decks` appears to return deck dicts that `reschedule_background` then passes to
  `decks.by_name(...)`; a non-empty `skipDecks` likely fails. Unconfirmed.
- `init_ease_adjust_review_hook()` is commented out in `__init__.py`, so the
  `auto_adjust_ease_on_review` / `auto_adjust_ease_after_review` keys do nothing.
  `mature_ivl` and `debug_notify` are read nowhere. `Config.scheduler_stats` has no key in
  `config.json`.
- No callers found: `reset_ivl_and_due`, `has_again`, `has_manual_reset`,
  `power_forgetting_curve`, `compress_review_list`, `MAX_REVIEWS` in `utils.py`.

## Shared code

Declares `anki` (`write_custom_data`, `sync_hook_base`) and `scheduling` (`due_dates`, which
was extracted from this addon's `utils.py`). `related_card_disperse` uses the same three
modules: a change to them is a change to both. The gear-menu helpers in `__init__.py` exist
verbatim in `copy_anywhere`, and the config boilerplate in three addons; see
[docs/shared-code.md](../docs/shared-code.md) before adding another copy of anything.
