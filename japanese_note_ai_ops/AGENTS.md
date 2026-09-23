# japanese_note_ai_ops (Japanese Note AI Ops)

Read the root [AGENTS.md](../AGENTS.md) first.

Runs LLM prompts and local operations over Japanese notes, from the browser's "AI helper"
right-click submenu and from a few automatic triggers: adding a "Japanese vocab note" runs
`clean_meaning` and `extract_words`; unfocusing an empty story field on a "Kanji draw" note
writes a story; unfocusing an empty translation field translates. Those two note type names
are hardcoded in the hooks. Operations: clean/generate a meaning from MDX dictionary
entries, translate, kanji mnemonic story, kanjify a furigana sentence, extract a **word
array** from a sentence (rule-based: SudachiPy + JMdict, one LLM call for proper nouns),
judge which words deserve a note, match words to vocab notes or create them. Bulk runs are
asynchronous, parallel, memory-aware, pausable and cancellable.

## Map

| path | role |
| --- | --- |
| `__init__.py` | strict order, see below |
| `configuration.py` | `ADDON_USER_FILES_DIR`, word tuple types, tag constants, TypedDicts. Importing it creates `user_files/` and imports `anki` |
| `call_logging.py` | per-call log files in `user_files/logs/`; `bulk_op_logging()`, `phase_log()`, `in_bulk_op()` |
| `generator_resources.py` | `with_generator_resources(parent, then)`: asks before the ~83 MB Sudachi dictionary + JMdict download, fetches via `QueryOp` |
| `html_stripping.py` | aqt-free on purpose, so research scripts can import it |
| `kana_conv.py` | local copy of AJT `kana_conv` (duplicate of the submodule's; see shared-code.md) |
| `async_api_ops/base_ops.py` | the operation framework and provider dispatch |
| `async_api_ops/api_client.py` | HTTP sessions, retry, rate-limit cooldowns, per-run cancellation and pause. stdlib + `requests` only |
| `async_api_ops/concurrency.py` | `ConcurrencyGate`, `MemoryEstimator`, `cpu_bound_section`; optional `psutil` |
| `async_api_ops/collection_access.py` | the one thread that owns collection reads during a run |
| `async_api_ops/word_index.py`, `note_cache.py`, `sentence_cache.py` | per-run read caches |
| `async_api_ops/terminal_client.py`, `diagnostics.py` | `claude -p` subprocess provider (its usage limit pauses the run, an expired login or unusable model stops it); cancel watchdog and stack dumps |
| `async_api_ops/progress_controls.py` | Pause/Resume and Cancel buttons in Anki's progress dialog, through private `mw.progress._win`; main thread; no buttons if Anki changes the dialog |
| `async_api_ops/<op>.py` | the operations; `match_words_to_notes.py` is about 2500 lines |
| `sync_local_ops/` | operations with no API call; `mdx_dictionary.py` (uses vendored `mdict_query`), `mdx_memo.py` (aqt-free) |
| `word_array/` | the generator package; **anki- and aqt-free** |
| `word_array/research/` | dev-only scripts; excluded from the zip by `build.json` |
| `test/` | the addon's suite, run separately (below) |

### `__init__.py` order is load-bearing

1. `add_vendor_paths(ADDON_DIR)`. Nothing that imports a vendored package may come first.
2. `VENDOR_HEALTH = vendor_health(...)`.
3. All operation imports inside one `try/except ImportError`, which sets `MISSING_PACKAGE`.
   **New operation imports go inside this block.**
4. Hooks and menus are registered only when `MISSING_PACKAGE is None`.
5. `install_rebuild_ui(...)`, unconditionally, so a broken install can still repair itself.

In `run_op_on_add_note` the `new_matched_jp_word` tag check stays first; a comment records
that checking later cost 25 minutes per bulk run.

### An operation is three functions

`async_api_ops/translate_field.py` is the smallest example.

1. Per-note: `op(config, note, notes_to_add_dict, notes_to_update_dict) -> bool`. It mutates
   the note and registers it in `notes_to_update_dict[note.id]`; new notes go into
   `notes_to_add_dict`. It **never writes to the collection.**
2. `bulk_*_op(col, notes, edited_nids, progress_updater, ...)` returning
   `bulk_notes_op(message, config, op, ...)` (one task per note) or
   `bulk_nested_notes_op(...)` (several requests per note; the inner op returns a
   `NotePlan(task_count, spawn, flush=None)` and must not start work itself; a note saved once
   all its tasks are done gives a `flush`, see Invariants). Local ops pass
   `is_sync_op=True`. Multi-phase operations pass a list of `OpPhase(name, bulk_op)`.
3. `*_selected_notes(nids, parent)` calling `selected_notes_op(...)` with an
   `AsyncTaskProgressUpdater`.

Registration is manual in `__init__.py`: import, `QAction`, `qconnect`.
`selected_notes_op` wraps the run in one `CollectionOp`: a fresh asyncio loop, one
`ThreadPoolExecutor` whose workers call `join_run(run)`, and all collection writes
(`update_notes`, `remove_notes`, `add_note`, `merge_undo_entries`) in a cleanup phase after
every op has finished.

### Providers

No SDKs; raw `requests` POSTs through `api_client.post_with_retry`. `get_response(model,
prompt, ...)` dispatches on the model name: `terminal-` to the Claude CLI, `gemini`, `gpt` /
`o1` / `o3` to OpenAI, `claude` / `anthropic` to Anthropic (accepts `effort`), any name
containing `/` to Together. Keys and per-operation `*_model` names are in the addon config.
There is no configured rate; throttling reacts to `retry-after`. Concurrency is
memory-driven, with learned per-operation costs in `user_files/memory_estimates.json`.
Never log, print or commit an API key, and never read the user's `meta.json` to find one.

## Invariants

- **A run never writes to the collection before cleanup.** `word_index`, `note_cache`,
  `sentence_cache` and `mdx_memo` all assume it.
- On worker threads, collection reads go through `collection_access` (`find_notes`,
  `get_note`, `get_notes`, `run_on_collection[_async]`); never `mw.col`. Sync ops run
  sequentially on the op thread and do use `mw.col`. `collection_access` raises
  `RunCancelled` on a cancelled run, except inside `begin_cleanup_phase()`.
- **A cancelled run still saves everything it prepared, new notes included, and its note
  adding has a cancel of its own.** `bulk_nested_notes_op` runs `NotePlan.flush` for every
  started note after the driver returns (`flush_started_plans`): a note's own save waits for
  all its tasks, and a cancel cancels it with them, which lost the finished ones. Each op's
  flush and own save run once only, whichever comes first (the match op and the judge).
  The notes to add are those registered before the flush, which it answers with, and the
  cleanup adds that answer only, never the shared `notes_to_add_dict`: threads a cancel
  abandoned go on registering notes there that no saved result links to.
  Cleanup's `begin_cleanup()` only greys the buttons; `add_new_notes` re-arms the dialog
  (`arm_cleanup_cancel`, only when there are notes to add, reset on the main thread by
  `progress_controls.rearm_cleanup_cancel` and waited for), because the dialog's flag stays
  set for the rest of a cancelled run. It is reset in a run not cancelled too: a press before
  Cancel says it stops the adding is the run's cancel, which keeps every prepared note. A
  reset that could not happen, or lands after the op thread stopped waiting or closed the
  window (a generation under `_arm_lock`), arms nothing, so a stale first cancel is never
  taken for a second one. From then on a cancel (`cleanup_cancel_requested()`, never
  `run_cancelled()`) stops the adding, checked before the dedupe, between its merges, after it
  and before each `add_note` - never inside one, where copy_anywhere's on-add definitions run.
  `end_cleanup_cancel()` closes it in a `finally`.
  The notes split three ways: added (placeholders resolved by `update_fake_note_ids`),
  failed (placeholders kept, a debugging hint the next match run's `resolve_placeholder_ids`
  resets), not added (words put back to `["match"]` by `clear_unadded_note_ids`). The
  markers preparing either of the last two put on other notes are the tidying's, below.
  Resolving and unlinking always run to the end; a resolving that raises fails the op after
  the unlinking.
- **The cleanup's last stage tidies the sort field markers** (`tidy_markers`, given the match
  op's `tidy_sort_field_markers`), after every other write, cancelled or not and with or
  without notes to add. Every word a saved or added note carries `(kun)`/`(on)`/`(rN)`/`(mN)`
  for is read whole from the collection (`word_index.sort_base_note_ids`) and renumbered
  without gaps in creation (note id) order, meanings then readings, dropping a marker that
  tells nothing apart; the rules are the pure `sort_field_markers.tidy_word_markers`.
  Preparing a note renames its word's other notes, saved before the adding decides whether it
  will exist, so a note not added (cancelled, failed, a dedupe's duplicate) or a new reading
  whose meaning failed leaves such markers. This is the only thing that takes them back:
  there is no record of renames to undo. It cannot be cancelled, and a raise only logs.
- Cancellation is per run and per thread (`begin_run`, `join_run`, `end_run`); teardown never
  joins pool threads. `resize_run_executor` pokes the private `executor._max_workers`.
- **A paused run starts no new task, phase, request or `claude` process**; what is in flight
  finishes, and a retry waits for the resume. The gate (`is_paused=run_paused`),
  `post_with_retry`, `terminal_client._run_with_retry`, `sync_bulk_notes_op` and
  `run_op_phases` poll the pause, and every pause wait also polls cancel; the op-thread waits
  pass `DialogCancelState`, because Escape only sets the dialog's flag. Pause is per run like
  cancel: `run_paused()` reads only the calling thread's run, so the main thread is never held,
  and is the only place an automatic pause expires; the dialog reads `pause_state()`, which
  falls back to the run in progress. A run that ends while paused is cancelled in teardown,
  before `end_run()`.
- These stay free of `aqt` and `anki`: `api_client.py`, `concurrency.py`,
  `sync_local_ops/mdx_memo.py`, `html_stripping.py`, all of `word_array/*.py`. An `aqt`
  import in one of them takes the test suite offline (`test/addon_modules.py` says so).
- A progress **message is also a key**: `ConcurrencyGate` stores the learned memory cost
  under `op_key=message`, so rewording it resets the estimate.
- `bulk_*_op` signatures have mutable `{}` defaults, harmless only because
  `selected_notes_op` always passes fresh dicts. Do not call them without.
- A note that already holds a word array is skipped unless the Regenerate merge path is
  used; a field that does not parse as an array is never overwritten. Every note holds an
  array now, and the old per-part-of-speech dict format has no reader left: a field that is
  not an array is a broken one, and match ops tag it `invalid_word_list_json`.
- Most vocab note fields are **generated from one authored field**. `vocab-kanjified`,
  `vocab-furigana`, `vocab-processed-furigana` are derived; `vocab` is left alone;
  `vocab-kanji-grades` is regenerated by a copy_anywhere definition. Fix data in the authored
  field or the generator, or the next regeneration undoes it
  (`word_array/research/vocab_respell.py` docstring). The note lookup key is
  `(dict_form, reading)` against `vocab-kanjified` plus `vocab-kana`.
- The sources contain Japanese text. On Windows set `PYTHONIOENCODING=utf-8` for scripts, and
  do not pass Japanese through an inline shell heredoc; write a script file.

## word_array

The field partitions a furigana sentence into elements
`[raw_text, pos, dict_form, reading, match_data, sub_words]`; tags and punctuation are
single-element arrays; concatenating the top-level `raw_text` values gives back the sentence
without `<b>` tags. `match_data` has five states (`match_flags.MatchState`): `[]`,
`["dontmatch"]`, `["match"]`, `[id]`, `[id, quality]`. Details: `word_array/README.md`.

The codec (`decode_word_array`, `format_word_array`) lives in
`anki_shared/word_array/field_text.py` because `copy_anywhere` reads the field too;
`word_array/match_flags.py` re-exports it. A format change is a data migration across two
addons and the user's collection.

Research scripts run from the **addon root**: `python word_array/research/<script>.py`. The
script's directory is `sys.path[0]`, so siblings import each other by bare name. Addon
modules come through `_bootstrap.load(...)`, `load_root(...)`, `load_shared(...)`, which
register a fake package `jnaio_dev` rooted at the addon so that `..shared` imports resolve
without running `__init__.py`. `research/old_word_lists.py` is the one non-script there (loaded
as `research.old_word_lists`, relative imports, excluded from mypy); `research/corpora.py` is
the corpus loader the other scripts read their sentences through. Collection repair scripts talk to
a running Anki over AnkiConnect, list changes by default, write only with `--apply`, undo
with `--revert`, and log to `output/`. Never run one with `--apply` unless the user asked for
that run. Commit the tooling; do not commit one-off reports or plans it produces
(`generated_examples.md` and `gold_examples.md` are the committed exceptions).

## Tests and types

- `test/` (about 45 files, `unittest.TestCase`) is **not** in the root `testpaths`. Run it from
  this directory: `python -m pytest test`. `test/pytest.ini` makes `test/` the rootdir so pytest
  never imports the addon's aqt-importing `__init__.py`, and sets `--import-mode=importlib`.
  `test/addon_modules.py` provides `load_addon_module`, `load_ops_module(name, subdir)`
  (synthetic package + `anki_stubs.install()`), `FakeClock` and `PausingClock` (calls back after
  each sleep, so a test can resume or cancel a pause), and the fakes that drive a real
  `bulk_nested_notes_op` (`RunGate`, `RunProgress`, `RunCollection`, `patch_nested_run`,
  `wait_until`). Word-array tests skip when SudachiPy or the downloaded dictionaries are
  missing; a skip is not a pass, so say which ran.

- `word_array/research/test/` is in the root `testpaths` and runs with the root
  `python -m pytest`.
- The root `mypy.ini` puts `test/` and `word_array/research/` on `mypy_path` for the
  bare-name imports; the local `mypy.ini` mirrors it for a run started here.

## Dependencies

`requirements.in` (`requests`, `rapidfuzz`, `psutil`, `sudachipy`) compiles
to the pinned `requirements.txt`; `lib/` is built by `python build.py vendor
japanese_note_ai_ops` from the repo root and is gitignored. `mdict_query` is placed in
`lib/` by hand and protected by `vendor_keep`. `psutil` and `rapidfuzz` have fallbacks;
`sudachipy` does not. Full story: [docs/vendoring.md](../docs/vendoring.md).

## Stale documentation here

`README.md` still describes a manual `pip -t lib` install (only its `mdict_query` part is
current). `config.md` says logs go to `logs/` (they go to `user_files/logs`), lists outdated
models, and `log_to_console` is `true` in `config.json` while the code default is `False`.
`anthropic_api_key` is read in `base_ops.py` but has no default in `config.json`.

## Shared code

Declares `jp_text_processing`, `utils`, `word_array`. Uses `utils.vendor_path` and
`utils.vendor_rebuild_ui`, `word_array.field_text`, and from the submodule `kana_highlight`,
`make_furigana_from_reading`, `check_word_reading_type`, `main_types`. It does not use the
shared `utils/logger.py`, and its config access is inline `getConfig(__name__)`. General
Japanese text logic belongs in the `jp_text_processing` repo, kept free of anything specific
to this addon. Before adding a generic helper, check
[docs/shared-code.md](../docs/shared-code.md).
