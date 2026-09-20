# Anki patterns these addons rely on

What is true of Anki addons in general and of how this repo uses the API. Each addon's
`AGENTS.md` says which of these it follows and where it deviates.

## Import time

- Anki imports every addon's root `__init__.py` once at startup, on the main thread, with
  `aqt.mw` constructed but **no collection loaded** (`mw.col` is `None` until a profile
  opens). Module-level code may build menus and append to hooks; it may not touch `mw.col`.
- An exception during import disables the addon for the session and shows the user an error
  dialog. Nothing registered after the failing line exists. `japanese_note_ai_ops` guards its
  feature imports for this reason: an addon that dies at import cannot offer its own repair.
- `from aqt import mw` binds at import. That is fine inside Anki and is the reason tests need
  `real_anki.rebind_mw`.
- Anything written to stderr inside Anki becomes an error-report dialog. Use `logging` with a
  handler that does not write to stderr (shared code attaches a `NullHandler`), never `print`
  for diagnostics in shipped code, and never let a logger propagate to the root logger's
  stderr handler.

## Hooks

- `gui_hooks.<name>.append(fn)` registers for the life of the process. Registering twice runs
  the handler twice; there is no dedupe. Keep registration in one `init_*` function called
  once from the addon's entry point.
- Filter hooks must return the (possibly modified) first argument. Examples here:
  `editor_did_unfocus_field` returns `changed`; `webview_did_receive_js_message` returns the
  `handled` tuple.
- Hook lists are class attributes. A handler attached in one test fires in every later test
  in the process; `running_anki.stub_mw_restored` exists to undo that.
- Monkeypatching Anki classes is a last resort and is version-fragile. Where the repo does it
  (`copy_anywhere/hooks/note_hooks.py`, `desired_retention`), the wrap is guarded against
  double application and keeps the original's signature. Say so in the addon's `AGENTS.md`
  when you add one.

## Main thread, background work, and the collection

- Qt widgets and `mw` UI calls are main-thread only. Long work must leave the main thread or
  Anki freezes.
- The supported way to change the collection in the background is
  `CollectionOp(parent, op).success(...).failure(...).run_in_background()`, where `op(col)`
  returns `OpChanges`. Anki then refreshes the UI itself. `QueryOp` is the read-only
  equivalent. `copy_anywhere` and `japanese_note_ai_ops` work this way.
- `custom_schedule_helper` and `related_card_disperse` use the older
  `mw.taskman.run_in_background` with `mw.progress` and a manual `mw.reset()` when done. It
  works, but every UI refresh is the addon's job. Match the addon you are in; do not mix the
  two styles inside one operation.
- Worker threads must not call `mw.col` while another thread may be using it.
  `japanese_note_ai_ops` funnels all reads through one collection thread
  (`async_api_ops/collection_access.py`) and defers all writes to a cleanup phase.

## Undo

- A bulk change gets one undo step: `pos = col.add_custom_undo_entry("name")`, then after
  each `update_note(s)` / `update_card(s)` call, `col.merge_undo_entries(pos)`. Skipping the
  merge leaves one undo step per call and eventually a "target undo op not found" error.
- A change made in response to a review merges into the answer's own undo step, so one undo
  takes back both. `related_card_disperse` reads `col.undo_status().last_step`;
  `copy_anywhere` records the step when `V3Scheduler.answer_card` runs.
- Raw SQL through `col.db.execute` bypasses undo and does not mark rows for sync. It is used
  deliberately in `custom_schedule_helper/ease/` for revlog `factor` rewrites, with comments
  that say so. Do not add new raw writes without the same explicit note.

## Card custom data

`card.custom_data` is a JSON object string that syncs with the card and is visible to the
custom scheduling JavaScript as `customData`. Anki rejects it above **100 bytes**, with
short keys. Always write through `anki_shared/anki/write_custom_data.py`, which round-trips
the JSON, dumps it compactly and raises `ValueError` over the limit.

The keys are a shared namespace across addons and the user's scheduler JS
(`custom_schedule_helper/custom_scheduler.js`). Current allocation:

| key | meaning | written by | read by |
| --- | --- | --- | --- |
| `v` | who last set the schedule: `0` answered in the reviewer, `"r"` rescheduled, `"p"` postponed, `"a"` advanced, `"d"` dispersed | scheduler JS, `custom_schedule_helper`, `related_card_disperse` | `custom_schedule_helper` SQL filters |
| `e` | `"a"` after auto-ease; JS resets to `0` | `custom_schedule_helper`, JS | `custom_schedule_helper` |
| `sr` | success rate, 3 decimals | `custom_schedule_helper` | `custom_schedule_helper`, JS |
| `s` | fuzz seed | JS | JS |
| `fc` | copy_anywhere's "fields copied" flag: JS sets `0` on answer; `1` done, `-1` sync-only definitions still pending | JS, `copy_anywhere` | `copy_anywhere` sync sweep |

Before adding a key, check this table and the 100-byte budget, and add the key here. SQL
that filters on a key (`json_extract(json_extract(data,'$.cd'),'$.v') NOT IN (...)`) yields
NULL, and therefore excludes the card, when the key is absent. `custom_schedule_helper`
depends on `'d'` staying in its filter so that it does not undo a dispersal.

## Addon config

- `config.json` is the shipped default. The user's actual config lives in `meta.json`, which
  is gitignored, per device, and may be large (copy_anywhere's definitions). Never edit or
  commit a `meta.json`; never assume the defaults are what the user has.
- A new key needs a default in `config.json`, an entry in `config.md` if the addon has one,
  and code that tolerates its absence, because existing `meta.json` files will not have it.
  Addons that version their config (`copy_anywhere`, `related_card_disperse`) migrate on
  load; add a migration step instead of reading the old shape in place.
- `addon_config_sync` copies other addons' `meta.json` between devices. A config value that
  is only valid on one machine (an absolute path) will travel.

## Compatibility

- Anki 25.09 ships CPython 3.13; pip-installed Ankis can be on 3.9 or 3.10. Shipped code
  stays 3.10-compatible (see the root `AGENTS.md`).
- Guard version-specific API use explicitly, as `custom_schedule_helper` does with
  `int_version() >= 231001`, and say in a comment which Anki version the branch is for.
- Filtered decks: a card's home deck is `odid` when non-zero and its real due is `odue`.
  Several SQL snippets here use `CASE WHEN odid==0 THEN due ELSE odue END`; keep that when
  editing them.
