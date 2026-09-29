# japanese_note_ai_ops (Japanese Note AI Ops)

Read the root [AGENTS.md](../AGENTS.md) first.

Runs LLM prompts and local operations over Japanese notes, from the browser's "AI helper"
right-click submenu, from the browser's Edit > "Japanese AI ops..." dialog (several ops in a
row, see "Chains" below) and from a few automatic triggers: adding a "Japanese vocab note" runs
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
| `configuration.py` | `ADDON_USER_FILES_DIR`, word tuple types, tag constants, TypedDicts, `capture_versions()` (the addon, Anki, Python and platform versions every capture run records). Importing it creates `user_files/` and imports `anki` |
| `call_logging.py` | per-call log files in `user_files/logs/`, `<name>_<timestamp>.log`: `start_call_log(name)` is called with the op's `OpSpec.key` by the menu action and by each chain step that starts it (a `MenuOnlyAction` has a key too), with the op's key by the editor's field-unfocus hook, `add_note` for a note added by hand, `browser_menu` for building the context menu; a phase's file adds its name to the run's (`match_words_add_note_phase_...`, `phase_log_name`). `bulk_op_logging()`, `phase_log()`, `in_bulk_op()`. Every handler it makes gets `LOG_FORMAT` and a `CaptureContextFilter`, so each line carries the capture ids (`[r12 n1712345678901 c4567]`, `[-]` for none); `current_log_path()` is the file a capture run records |
| `generator_resources.py` | `with_generator_resources(parent, then, chain=None)`: asks before the ~83 MB Sudachi dictionary + JMdict download, fetches via `QueryOp`; with a chain, each way of not running fails the step |
| `op_registry.py` | `OPS`: the 21 ops that run through `selected_notes_op`, in menu order, as `OpSpec(key, label, start(nids, parent, chain), needs_generator, group)`; `OP_BY_KEY`. The menu and the dialog both read it |
| `ai_helper_menu.py` | builds the "AI helper" submenu: "Run several ops...", then `OPS` plus two `MENU_ONLY_ACTIONS` (name lexicon, kanjify export). Out of `__init__.py` so it can be tested |
| `multi_op_dialog.py` | the multi-op dialog: `OpSelection` (Qt-free model of the chosen ops and order), `MultiOpDialog`, `show_multi_op_dialog(browser)` |
| `html_stripping.py` | aqt-free on purpose, so research scripts can import it |
| `kana_conv.py` | local copy of AJT `kana_conv` (duplicate of the submodule's; see shared-code.md) |
| `async_api_ops/base_ops.py` | the operation framework and provider dispatch; `get_response` records each call (see "Capture store"), `note_context(note)` names the note for errors and capture together |
| `async_api_ops/api_client.py` | HTTP sessions, retry, rate-limit cooldowns, per-run cancellation and pause. stdlib + `requests` only |
| `async_api_ops/capture_store.py` | the capture store's SQLite file (`runs`, `calls`, `blobs`), its one writer thread, schema, `prune` and keys (`request_key`, `prompt_key`, `research_cache_key`). Recording methods only enqueue: `get_response` runs in hundreds of pool threads. Imports nothing of the addon's |
| `async_api_ops/capture.py` | what the addon calls: `install`/`shutdown`, the run, note, task and call ContextVars and their scopes, `begin_run`/`end_run`, `call(...)` (one `calls` row), `note_attempt`/`note_response`/`note_outcome` for the providers, a notes run's `snapshot_note`, `event`, `reference_notes`, `note_added`, `scrub_config`, `CaptureContextFilter`. A cheap no-op until a store is installed. Imports only `capture_store`, so `api_client` and `terminal_client` can note their sends and outcomes through it |
| `async_api_ops/capture_notes.py` | a notes run's records built from Anki notes and the collection (`note_record`, `fetch_unread`, `record_final`, `record_collection`, `MeaningsRecorder`); nothing outside a notes run. anki only under TYPE_CHECKING |
| `async_api_ops/concurrency.py` | `ConcurrencyGate`, `MemoryEstimator`, `cpu_bound_section`; optional `psutil` |
| `async_api_ops/collection_access.py` | the one thread that owns collection reads during a run |
| `async_api_ops/word_index.py`, `note_cache.py`, `sentence_cache.py` | per-run read caches |
| `async_api_ops/terminal_client.py`, `diagnostics.py` | `claude -p` subprocess provider (its usage limit pauses the run, an expired login or unusable model stops it); cancel watchdog and stack dumps |
| `async_api_ops/chain_types.py` | `ChainStep(label, on_done, op_label)` (`.title` is "Step i/n: <op>"), `StepOutcome` and its `STEP_*` statuses, `fail_step(chain, error)`; aqt- and anki-free |
| `async_api_ops/step_failure.py` | `failed_step_outcome(parent, error, title, context=None)`: the one way a step is failed on an exception; shows it (pane, else `show_exception`; aqt's `Interrupted` neither), takes the stop reason, never raises |
| `async_api_ops/run_errors.py` | the errors a run meets without failing, as data: `report_error(text, where)` from any thread, titled by the chain step (`set_step`) and the task's note (`error_subject(NoteSubject(note))`, a ContextVar that follows `create_task` and `to_thread`; the drivers enter it through `base_ops.note_context`, with the capture note); `ErrorList` groups repeats of one text with a count (MAX_KINDS listed, the rest counted); `start_run`/`take_run` keep what the pane showed for the run's end message. aqt- and anki-free, so `terminal_client` reports through it |
| `async_api_ops/op_chain.py` | `run_op_chain(specs, nids, parent)`; `OpChain`, the sequencing with every Anki dependency passed in as a hook; `existing_note_ids(col, nids)` |
| `async_api_ops/progress_controls.py` | Pause/Resume and Cancel buttons in Anki's progress dialog, through private `mw.progress._win`; main thread; no buttons if Anki changes the dialog |
| `async_api_ops/progress_errors.py` | `report_run_error(title, text) -> bool`: an error pane in that dialog; the first error widens it, progress and buttons on the left, the list on the right. State on the dialog, so a chain's steps share one pane and the next dialog starts clean. Main thread; False (nothing shown) off it or with no dialog, and the caller falls back to its own error box. Also `report_run_error_from_any_thread` (hops via `mw.taskman.run_on_main`; `run_errors` delivers through it), `report_exception(error, what, where)` (skips `Interrupted` and `RunCancelled`), and `show_run_end(text, parent, errors)`: the end message of a run that met errors, one box whose "Show errors" button opens them all in `showText` |
| `async_api_ops/<op>.py` | the operations; `match_words_to_notes.py` is about 2800 lines |
| `sync_local_ops/` | operations with no API call; `mdx_dictionary.py` (uses vendored `mdict_query`), `mdx_memo.py` (aqt-free) |
| `word_array/` | the generator package; **anki- and aqt-free** |
| `word_array/research/` | dev-only scripts; excluded from the zip by `build.json` |
| `dev/` | dev-only, excluded from the zip, run from the addon root like the research scripts. `headless.py` runs an op over a collection file without Anki's main window (the stub `mw`, the user's config with secrets removed and only `terminal-` models allowed, a profile folder and capture store of the caller's, Ctrl+C as Cancel); `capture_run.py` is its CLI for capture runs. **It writes to the collection it is given**: a copy, never a profile's collection while Anki has it open. `replay.py` exports a notes run as a fixture and replays it (below); `export_fixture.py` and `export_evals.py` are their CLIs; `benchmark.py` replays a fixture or a corpus (by name from the test data checkout, below: `corpora/` holds the runs too big to replay strictly, where contending notes ask in lock order, which is timing) with timed answers (the recorded latency scaled, fixed, or none), a lenient cassette (a request the capture never made gets an answer of its kind, counted), optionally a fixed free memory for the gate and the corpus's CopyAnywhere add definitions (`--copy-anywhere`, around the run only), and appends each run's figures, and the added notes' fields, to `user_files/benchmarks/<fixture>.jsonl` with the commit, the machine and the hash of the corpus's inputs. Each run's `work` (calls by kind, decisions, new notes, errors) is the same on any machine: `--record-work` writes it to the corpus's work.json, and every later run, on this machine or another, is checked against it (`work_as_recorded`). `--background N` adds N notes shaped like the corpus's with their Japanese moved into Hangul and their note ids replaced (`replay.background_notes`): every scan and the word index pay for them as for a real collection's, no request can find one, and the run's work is the same with them (test_pipeline). A replay keeps the gate's learned per-task costs in memory (`replay.memory_estimates`), never in the user's `memory_estimates.json`: a benchmark's first run is cold, its repeats warm. `benchmark_compare.py` compares two sets of summaries of one profile and corpus by where the time went: async (planning, running the plans), collection (held seconds, turns), writes (the cleanup's, merges included) and hooks (the adds). Only `headless` installs the stub `mw`, and only over none or a stub, so the other dev modules import inside a running Anki too |
| `test_anki/` | the write path in a running Anki (pytest-anki2, `anki_shared/testing/running_anki.py`), in the root `testpaths`: `test_real_anki_add_path.py` runs a stand-in op that prepares one new note through `selected_notes_op` and a real `CollectionOp`, with CopyAnywhere's real `init_note_hooks()` and both configs read off disk, and pins that the add hook runs in the add phase and that the run stays one undo step, a hook writing another note under its own undo entry included; `test_real_anki_replay.py` is opt-in (`JNAIO_REAL_ANKI_REPLAY=<fixture name or path>`, `JNAIO_REAL_ANKI_SCALE`, `JNAIO_REAL_ANKI_COPY_ANYWHERE`) and replays a fixture or corpus there (`replay.replay_in_anki`, the corpus's CopyAnywhere definitions attached by `before_run`, after the corpus is built; strict, the notes must match expected.json or expected_copy_anywhere.json), appending its figures and the added notes' fields to `user_files/benchmarks/<fixture>-real-anki.jsonl` |
| `test_replay/` | replays in real collections, in the root `testpaths` (real_anki mode): `test_pipeline.py` captures, exports and replays a run over notes made up in the test, and benchmarks it twice (the counts, never the seconds); `test_replay.py` replays every fixture in `test_replay/fixtures/` (committed) and in the test data checkout's `fixtures/` (the private repo's), each twice, and twice more with CopyAnywhere when it has expected_copy_anywhere.json. Excluded from the zip |
| `test/` | the addon's suite, run separately (below) |

### `__init__.py` order is load-bearing

1. `add_vendor_paths(ADDON_DIR)`. Nothing that imports a vendored package may come first.
2. `VENDOR_HEALTH = vendor_health(...)`.
3. All operation imports inside one `try/except ImportError`, which sets `MISSING_PACKAGE`.
   **New operation imports go inside this block**, and so do `op_registry`, `ai_helper_menu`,
   `multi_op_dialog` and `op_chain`, which import op modules.
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
3. `*_selected_notes(nids, parent, chain=None)` ending in `selected_notes_op(..., chain=chain)`
   with an `AsyncTaskProgressUpdater`. Every path that returns before that call must
   `fail_step(chain, reason)`, or a chain waits forever; a wrapper in
   `with_generator_resources` passes it `chain=chain`, which does that for its no-run paths.

Registration: an `OpSpec` in `op_registry.OPS`. Without one an op is in neither the "AI
helper" submenu nor the multi-op dialog; with one it is in both, and `__init__.py` needs no
change. Its position is its menu position, `group` picks the side of the async/sync
separator, `needs_generator` is set when the entry function goes through
`with_generator_resources`, and `start` is a lambda that looks the entry function up in
`op_registry`'s globals when called (tests patch it there) and passes `chain` on. An entry
that runs no `selected_notes_op` and writes no notes is a `MenuOnlyAction` in
`ai_helper_menu.py` instead, never a chain step.

`selected_notes_op` wraps the run in one `CollectionOp`: a fresh asyncio loop, one
`ThreadPoolExecutor` whose workers call `join_run(run)`, and all collection writes
(`update_notes`, `remove_notes`, `add_note`, `merge_undo_entries`) in a cleanup phase after
every op has finished. The run itself is `notes_run(...)`, which returns the function the
`CollectionOp` runs and the `RunResult` it fills in (edited ids, new-note counts, cancelled)
for the success handler; `selected_notes_op` adds only the UI: the `run_errors` start, the
dialog's controls and title, the end message. A script runs the same run by calling that
function on its own thread with a collection it opened and a stand-in `mw`
(`anki_shared/testing/real_anki.py`): no `CollectionOp`, no dialog, no `tooltip`.

### Chains: the multi-op dialog

Selecting thousands of rows makes the browser lag, and the right click again. The dialog
needs one selected row and the search. It opens from the browser's Edit menu, "Japanese AI
ops..." (`__init__.add_browser_edit_menu_action` on `browser_menus_did_init`; shortcut from
`multi_op_dialog_shortcut`, read once per browser window), and from "Run several ops..." at
the top of the "AI helper" submenu. A click on an available op moves it to the end of the
numbered run order (each op once); drag, Up/Down, Remove, double-click and Clear edit it.
Below: the shared `NoteSourceButtons` and a count of the notes (`find_notes` on opening and
on each mode switch). Footer: Run bottom left, Close bottom right. Close is the default
button and Run has `autoDefault` off, so Enter anywhere (a list ignores it and the dialog
takes it) closes rather than starts a chain. Run needs one op and one note and takes the
ids the count resolved, without searching again (the dialog is window-modal to the browser
only, so they can go stale like a captured selection; the chain's check below covers it).
The chain starts after `exec()` returns, so its first progress dialog is not under a modal
one.

`run_op_chain(specs, nids, parent)`, per step:

- One full, ordinary run: its own `selected_notes_op`, cleanup and undo entry, and the title
  `"Step i/n: ..."`. The progress dialog is the chain's: it holds a progress level from
  before step 1 to after the last, each step's `CollectionOp` nests in it, and the modal
  dialog never leaves the screen between steps, so the browser cannot be closed or edited
  mid-chain. Cleanup commits (added notes included) before
  the next step starts, so a later step that queries the collection sees added notes, but
  they never join the chain's ids. `OpPhase` does not join steps: that would share one undo
  entry and hold the added notes back.
- Before it, the chain's ids are re-filtered by `existing_note_ids` (a cleanup can remove
  notes, and before step 1 they are as old as the dialog's count or the captured selection;
  `selected_notes_op` raises on a removed id). None left stops the chain.
- A cancelled, stopped or failed step, or a `start` that raises, stops the chain; a
  cancelled step still saves what it did, and the summary says so and that the steps before
  it ran to the end.
- Every exception that fails a step (the op's run, its success handler, a raising `start`,
  the step's word array download or the start after it) goes through
  `step_failure.failed_step_outcome`. The chain's dialog is still open then, so the error and
  its traceback go to its pane (`report_run_error`), titled with `ChainStep.title`; only if
  that returns False does aqt's `show_exception` box open. The dialog closes right after,
  since a failed step stops the chain, so the summary is where the user reads it: it names the
  step and carries `StepOutcome.error` (message only, escaped), and the traceback is listed
  from it (below).
- No tooltip per step. One summary at the end: `showInfo`, or `showWarning` when stopped
  early, naming the step, why, and the steps that did not run. When the pane showed errors
  during the chain, `show_run_end` instead: a warning with the same text, the error count and
  a "Show errors" button.
- Errors that do not fail a run (a note's request refused or unreadable, an op raising for one
  note, a claude CLI failure, a save in the cleanup) are reported through `run_errors` as they
  happen, in chain and menu runs alike, and reach the pane via
  `report_run_error_from_any_thread`. What the pane showed is kept (`run_errors.record`) from
  `start_run` (the chain's `hold_progress`, or `selected_notes_op` for a menu run) to
  `take_run` (the chain's summary, or `on_bulk_success`), so a run's errors never reach the
  next one's end message. A report that arrives after its dialog closed is only logged. What
  counts as a failure and the run's control flow are unchanged; only the reporting is new.
- If any op `needs_generator`, the downloads are asked about once, before step 1; declined,
  no SudachiPy or a failed download starts nothing and shows no summary.

### Providers

No SDKs; raw `requests` POSTs through `api_client.post_with_retry`. `get_response(model,
prompt, ...)` dispatches on the model name: `terminal-` to the Claude CLI, `gemini`, `gpt` /
`o1` / `o3` to OpenAI, `claude` / `anthropic` to Anthropic (accepts `effort`), any name
containing `/` to Together. Keys and per-operation `*_model` names are in the addon config.
There is no configured rate; throttling reacts to `retry-after`. Concurrency is
memory-driven, with learned per-operation costs in `user_files/memory_estimates.json`.
Never log, print or commit an API key, and never read the user's `meta.json` to find one.
Every `get_response` call is recorded in the capture store, below.

### Capture store

A record of every AI call, for debugging and for building tests and evals from real runs:
`user_files/capture.sqlite3`, one file per addon install, so every profile writes to it and each
run records which (`runs.profile`, `mw.pm.name`: a note id means nothing without its collection).
`__init__.py` installs it at `profile_did_open` when config `capture_calls` is on (the default)
and shuts it down at `profile_will_close`. When its writer starts, it deletes the runs older
than `capture_keep_days` (default 90; 0 or less, or null, keeps everything), their calls, and the
blobs no call uses. A call goes with its run only: one whose run row is missing (the store went off
before writing it) has no age and stays. The store reads `capture_keep_days` as a number or a numeric string
(`"30"`); anything else (`true`, text) is 90, with a warning. Tests and research scripts install
no store unless they mean to (tests: in a temp dir); without one every capture function is a
cheap no-op and nothing is recorded.

| table | one row is |
| --- | --- |
| `runs` | one `notes_run` (a chain step is one run): `label` (its done text), `ops_json`, `chain_step`, `note_count`, `config_json` (keys naming an API key, token, secret or password removed), `versions_json`, `log_path`, `started`/`ended`, `outcome` (`completed`, `cancelled`, `failed`, `abandoned` for a caught `RunCancelled`), `profile` (the Anki profile's name), `notes` (1 when it recorded its notes), `dropped` (a notes run's records lost: dropped at a full queue, or a note whose record could not be built; a replay needs 0). A call made outside any run opens an *implicit* run for itself alone (`implicit` 1, labelled with its kind): the editor hooks' calls are recorded that way |
| `calls` | one `get_response`, retries included: `run_id`, `note_id`, `task_id`/`parent_task_id`, `kind`, `inputs_json`, `request_key`, `prompt_key`, `model`, `params_json`, `prompt`, `instructions_hash`/`schema_hash`, `response_raw` (the answer text before parsing), `response_json` (what `get_response` returned), `outcome`, `error`, `started` (seconds into the run), `latency_ms`, `attempts`, `usage_json`, `extra_json` (`corrected` when the corrector made the result), `context_json` (below) |
| `blobs` | instructions, response schemas and snapshotted notes, stored once by sha1 |
| `note_snapshots` | a notes run's note at one stage, once per run, note and stage: `run_id`, `note_id` (a new note's negative placeholder before it is added), `stage`, `t` (seconds into the run), `mid`, `note_hash` (the blob of `capture_notes.note_record`: id, guid, mid, fields by name, tags) |
| `events` | a notes run's other facts, in the order recorded: `run_id`, `note_id`, `task_id`, `kind`, `t`, `payload_json` |

- Schema version 4: 2 added `calls.context_json`, 3 `runs.profile`, 4 `runs.notes`/`dropped`
  and the two notes tables, each column last in its table. An older file is brought up to date
  when it opens (`capture_store._MIGRATIONS`, one transaction, step by step), its old rows NULL
  in the new columns (`notes` 0). A run's snapshots and events are pruned with it, and a note blob
  stays while any snapshot names it.
- A **notes run** (config `capture_notes`, off by default, read at each run's start: a run's
  notes are a copy of much of the collection) records what a replay of it needs besides its calls
  (`capture.begin_run(notes=True)`; `capture_notes` builds the records and does nothing in any
  other run). Stages: `selected` (the run's notes as loaded), `read` (every note it first fetched
  through `collection_access`, on the calling thread, never the cleanup's reads, which come after
  its writes), `proposed` (what the cleanup is about to write, new notes by placeholder),
  `final` (every note saved, added or tidied, re-read after the cleanup). A note the run only
  learned the id of (a search, a word index lookup: `capture_notes.found`) is fetched as `read` at
  the cleanup's start, before its first write, or right after the marker tidying's lookup
  (`word_index.sort_base_note_ids`), which finds notes the run did not write, still as they were;
  never a note the run added (`note.added`). Event kinds: `environment` (dictionary files, the
  collection's size, and `records`, `capture_notes.RECORDS`: the kinds of record this capture
  makes, so an exporter tells a run that looked nothing up from one captured before lookups were
  recorded; add to it with a new kind), `search`, `note.missing`, `note.added` (placeholder ->
  id), `note.removed`, `match.decision` and `match.rated` (per word target, its `word_path`,
  result, quality), `meanings.read`/`meanings.final` (the first value read and the last written
  of each key of the generated meanings file: `load_meanings_dict_from_file` hands a notes run a
  `capture_notes.MeaningsRecorder`, and it is the only loader of that file),
  `dictionary.lookup` (each `mdx_helper.get_definition_text` answer, the text the prompt is built
  from, or its error), `phase` (`log_phase`, with the process's `rss` at the phase's end), `undo`
  (at the cleanup's start and end), `notetype` and `decks` (of every note recorded, at the end),
  and the run's figures, which otherwise only reach DEBUG lines or die with the run:
  `metrics.gate` (`ConcurrencyGate.stats`: raises, halvings, holds under pressure, collection
  latches, each ceiling change, the seconds at each limit, the collection's share and turns,
  counted where each move is logged; `base_ops.record_gate` after `gate.finish()`) and
  `metrics.caches` (the match op's note and sentence caches and word indexes, at its `on_end`).
  `note.add` is each note of the cleanup's adding (`capture_notes.record_note_add`): the add's
  seconds, which hold every addon's `note_will_be_added` hook, apart from the merge's into the
  run's undo entry, the undo queue's head before and after, and an `add_error` or a
  `merge_error`, never both: a failed merge is recorded and raised as it always was. The add
  loop's phase event carries `add_seconds` and `merge_seconds`. The capture never infers: an
  exporter that finds a note it needs without a snapshot has found a capture gap, to be fixed
  here and recorded again.
- **Replays** (`dev/replay.py`). `export_fixture(store, run_id)` makes a fixture of a notes run:
  `corpus.json` (the notes the run read, their note types and decks under generic names but the
  hardcoded ones, note ids synthetic in every field, the run's config, meanings read, dictionary
  lookups), `cassette.json` (the answers by `request_key`, in the order received) and
  `expected.json` (the notes as the run left them, new notes' ids and placeholders as symbols).
  It raises `CaptureGap` rather than guess. `replay(fixture)` builds the corpus in a fresh
  collection, points every module's `mdx_helper` at the corpus's lookups, answers every
  `get_response` from the cassette through `base_ops.set_responder` (the one seam: a responder
  replaces the provider inside the capture block, on the calling thread; None from it is a
  failed call), runs the op through its `NotesRunSpec`, and reports each difference, a request
  the cassette cannot answer, an answer never asked for and a lookup it lacks. expected.json
  holds only the notes the run changed, added or removed (format 2; `Fixture.read` fills in
  the rest from the corpus), and a corpus's files are gzipped. `export_fixture.py
  --copy-anywhere` also puts the user's CopyAnywhere config into the corpus, and for a fixture
  writes expected_copy_anywhere.json from a replay with its add definitions on
  (`replay.copy_anywhere_on_add`); the capture run had none on, so that file is a regression
  baseline, not a capture.
- **Where fixtures live.** A fixture of a real collection holds its note text and excerpts of
  the MDX dictionaries it looked words up in, so it never goes into this public repo. They
  live in the private test data repo (`jhhr/anki_addons_test_data`), cloned to the gitignored
  `<repo>/test_data/`, or wherever `ANKI_ADDONS_TEST_DATA` points (`dev/data_paths.py`), under
  `japanese_note_ai_ops/fixtures/` (replayed strictly by test_replay),
  `japanese_note_ai_ops/corpora/` (benchmark.py's) and `japanese_note_ai_ops/evals/` (the
  research scripts' eval data; see the research scripts under "word_array"). The exporter writes there by default; a
  checkout without the clone skips test_replay's real fixtures and runs only the synthetic
  pipeline test. `test_replay/fixtures/` is for a fixture its collection's owner has chosen to
  publish; there is none yet. Commit and push a new fixture in the test data repo: the capture
  store it came from is one local file, never backed up.
- Outcomes: `ok`; `refused` (final non-200, body in `error`); `unreadable` (a 200 whose answer
  text could not be found); `unparseable` (not JSON even after the corrector); `no_response`
  (every attempt timed out or lost its connection, or the CLI gave up retrying); `cancelled`;
  `error` (an exception, recorded and re-raised; no config, unsupported model, a CLI failure or
  dead end). The helper that reports each (`post_to_api`, `report_refused`,
  `report_unreadable`, `decode_answer`, the terminal client's failure paths) also notes it with
  `capture.note_outcome`, and the last noted wins; a new failure path in a provider notes its own.
- Keys: `request_key` is the sha1 of `kind` and `canonical_json(inputs)`: what was asked, the
  same after the prompt is reworded. `prompt_key` hashes what was requested: the model,
  instructions, prompt, schema and params as `get_response` was given them, not as sent, since a
  provider drops a parameter its model does not take (Anthropic's temperature fallback, OpenAI's
  fixed-temperature models, the claude CLI). `capture_store.research_cache_key(model, prompt)`
  is the research scripts' answer cache key (`judge_eval.prompt_key` and its siblings); no
  column holds it.
- Log ids: every line of the addon's log carries `[r<run> n<note> c<call>]`, only the ids there
  are (`n` shows with capture off too), or `[-]`. Each captured call ends with
  `call <id> <kind> <outcome> <seconds>s`, the line that names its row, logged at INFO inside
  the call; the default `log_level` ERROR hides it, and the store's own warnings too. A profile
  switch in one process carries the ids on from the previous store's, whose writer may still be
  finishing rows the file does not hold yet, so the two never hand out the same id.
- The ids travel in ContextVars, not arguments. `selected_notes_op`'s `run_bulk_op` begins the
  run and enters `capture.run_scope` around `run_until_complete`; each driver enters
  `base_ops.note_context(note)` (`error_subject` and `capture.note_scope` together) where it
  starts a note's work; `asyncio.create_task` and `asyncio.to_thread` copy both into the pool
  thread that calls `get_response`. `loop.run_in_executor`, `executor.submit` and a plain
  `threading.Thread` start without them: a call made there gets an implicit run and no note.
  The match op adds `capture.task_scope(f"{word}|{reading}")` around each word target.
- `note_id` is the note the driver or hook works on, not always the one a call changes: the
  clean_meaning and make_all_meanings calls a match target makes for a word note carry the
  sentence note's id and the target's task; the notes a call changes are in its context
  (below). A note not added yet (id 0) is recorded as none.
- Context (`context_json`, schema version 2): what reading or applying the answer needs that
  the prompt does not show, e.g. which note each numbered meaning came from. Not in either key,
  and nothing the prompt is built from (that is `inputs`). Note ids are ints; a note not added
  yet is its negative placeholder (`new_note_id_field`), which is what a word array links it by
  until cleanup, or 0 when it has none (a vocab note added by hand, which clean_meaning keys by
  0). Per kind:
  - `match.meanings`: `word_path` (the word's element, `arr[p[0]][5][p[1]]...`;
    `match_targets.word_path`); `meanings`, one per `inputs.meanings` item in the same order,
    each `note_id` (null for a generated meaning), `m_number` (its sort field's (mN), 0 without;
    a generated one has the largest) and `gen_index` (its index in the word's generated
    meanings, else null), so the answer's `meaning_number` n is `meanings[n - 1]`;
    `copy_note_id`, the note copied for a new one (a CREATE NEW, or a MATCH of a generated
    meaning).
  - `match.rating`: `word_path`, and `note_id`, the linked note whose meaning is rated.
  - `clean_meaning.rework` and `map`: `note_ids`, the notes in the order the answer's meaning
    indexes count (note id order, so placeholders first); `map` also `depth` (1: its second
    try, after a `make_all_meanings.revise`). Map's possible meaning indexes count in
    `inputs.possible_meanings`.
  - `clean_meaning.extract` and `generate`: `note_id`, the note the meaning is for (null when
    its caller does not say). The other kinds have none: `make_all_meanings.*` answers replace
    the generated meanings of the inputs' word and reading.

**A new AI call site** (`translate_field.py` is the smallest example):

- Pass `kind="<op>.<what one request is about>"`: `translate.sentence`, `judge.word`,
  `clean_meaning.map`. The prefix is the module whose prompt it is, not the op the user ran (a
  low map score in clean_meaning makes a `make_all_meanings.revise` call).
- Pass `inputs`, the values the prompt is built from, as plain JSON: no note ids (the row has its
  own), no API keys, a looked-up value as the text the prompt shows (a dictionary entry, not its
  key). Add nothing the prompt does not show but what makes the case, with a comment saying why
  (`match.meanings` records `reading`, which its prompt leaves out).
- Pass `context` when the answer refers to things by position (a numbered list of notes'
  meanings) or the call changes notes other than `note_id`: short snake_case keys, note ids as
  ints, read off the list as the prompt shows it (after any sort), and a line for the kind
  above. Test that it names the note the op then changes.
- Build the prompt with a pure module-level builder from exactly those inputs (with no such
  extra, `prompt = builder(**inputs)`), and test that the builder given the recorded inputs,
  also after `canonical_json`'s round trip (it sorts dict keys), returns the prompt sent.
  `test_capture_kinds.py` and `test_capture_meaning_kinds.py` have the shape.
- `test_capture_kinds.py`'s AST scan fails a call of `get_response` in `async_api_ops/`, or a
  call handed it (`asyncio.to_thread(get_response, ...)`), that passes no `kind` or `inputs`.

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
  Cleanup's `begin_cleanup()` only greys the buttons, and waits for that on the main thread
  so the op thread's read of the flag right after it counts every press made while Cancel
  said it cancels the run as the run's cancel; `add_new_notes` re-arms the dialog
  (`arm_cleanup_cancel`, only when there are notes to add, reset on the main thread by
  `progress_controls.rearm_cleanup_cancel` and waited for), because the dialog's flag stays
  set for the rest of a cancelled run. It is reset in a run not cancelled too: a press before
  Cancel says it stops the adding is the run's cancel, which keeps every prepared note. While
  Cancel is grey, Escape and the close box do nothing either
  (`progress_controls.swallows_cancel`, an event filter on Anki's dialog), so a press during
  the edited notes' write, the resolving or the unlinking is dropped. A reset that could not happen, or lands after the op thread
  stopped waiting or closed the window (a generation under `_arm_lock`), arms nothing, so a
  stale first cancel is never taken for a second one. From then on a cancel
  (`cleanup_cancel_requested()`, never `run_cancelled()`) stops the adding, checked before the
  dedupe, between its merges, after it and before each `add_note` - never inside one, where
  copy_anywhere's on-add definitions run. `end_cleanup_cancel()` closes it in a `finally`.
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
- **A chain step's `chain.on_done` is called exactly once**, on the main thread, after its
  progress is finished, on every path: completed, cancelled, stopped (a stop reason wins over
  the cancel it causes), failed (exception in the op or in the success handler), a caught
  `RunCancelled`, and `fail_step` for an entry function that gives up before running. A
  second call is ignored with a warning; a missing one leaves the chain waiting forever
  (there is no timeout). `fail_step`'s call is synchronous, from inside `spec.start`.
- **Nothing is started from inside `on_done`.** The next step and the end go through a
  `QTimer.singleShot(0)`, not aqt's `single_shot`, which holds a call back while any progress
  is open and the chain's always is. The summary does use `single_shot`, after the chain lets
  go of its progress, so it is not shown under a dialog still closing.
- **`on_bulk_success` never finishes the progress.** aqt's `with_progress` has done that
  before the success handler; a second `finish()` ended whichever progress was open by then,
  which in a chain is the chain's own, and closed its dialog between steps.
- **Escape and the close box cancel only while Cancel is enabled** (see the cancel invariant
  above). A chain's dialog gets greyed buttons before its first step
  (`install_idle_run_controls`), and each step's `start_run_controls` brings them back
  after the step before left them greyed.
- **A run without a chain behaves as before the chain existed**: its tooltip or stop warning
  (or `show_run_end` when its pane showed errors), and no `.failure` handler, so aqt shows an
  exception itself. Only with a chain is one set,
  and `failed_step_outcome` then shows the error, in the chain dialog's pane.
- **The search is the one the browser ran, not the search box text.**
  `note_source_buttons.browser_search` reads aqt's private `_lastSearchTxt`, which is what the
  rows show. `Browser.current_search()` is the box: text typed without Enter, or `""` under
  the default search, where `find_notes("")` is every note. The box stands in only if aqt
  drops `_lastSearchTxt`, and the dialog's count then says in red that an empty search is
  every note in the collection.
- These stay free of `aqt` and `anki`: `api_client.py`, `concurrency.py`, `capture_store.py`,
  `capture.py`, `sync_local_ops/mdx_memo.py`, `html_stripping.py`, all of `word_array/*.py`. An
  `aqt` import in one of them takes the test suite offline (`test/addon_modules.py` says so).
  `async_api_ops/chain_types.py` is kept free of both too, so the chain's types need nothing
  of Anki.
- **Nothing the addon logs reaches stderr**, which Anki shows as an error dialog. The addon
  logger does not propagate, and `__init__.setup_addon_logging` gives it a `NullHandler` at
  import, unflagged, so that opening and closing log files never leaves it with no handler
  (a record with none goes to logging's last resort, stderr). Keep both.
- **Capture never fails or changes an op.** The store is diagnostics: a file it cannot open or
  write turns it off for the session (a warning, then nothing recorded), a full queue drops the
  record (but a run's own two rows, which wait beside the queue until the writer takes them:
  every other record of a run is read by its row), and its recording methods only enqueue,
  never block or raise, from any thread; past
  the open at install, no sqlite call happens off its writer thread. The capture code around
  `get_response` catches its own failures and lets the op's result and exception through.
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
that run. The eval data is not in `output/`: the eval sets, the judge's hand labels, the
pre-migration export and its hand-checked subset (`corpora.py`), `kanjify_sentence_data.jsonl`
and the answer caches (`*_eval_results.jsonl`, `vocab_reading_judge_results.jsonl`) are hand
work and paid answers found nowhere else, so they live in `japanese_note_ai_ops/evals/` of the
private test data checkout (below), and a script reaches each through
`_bootstrap.eval_file(name)`, which falls back to `output/` on a machine without the checkout.
Commit and push there after a script changes one. The menu's "Export kanjify test data" still
writes `output/kanjify_sentence_data.jsonl`; `eval_file` uses it from there only while evals/
has none, and warns while it is newer than evals/' copy, which stays the eval set until the new
export is moved over it. Commit the tooling; do not commit one-off reports or plans it produces
(`generated_examples.md` and `gold_examples.md` are the committed exceptions).

## Tests and types

- `test/` (about 60 files, `unittest.TestCase`) is **not** in the root `testpaths`. Run it from
  this directory: `python -m pytest test`. `test/pytest.ini` makes `test/` the rootdir so pytest
  never imports the addon's aqt-importing `__init__.py`, and sets `--import-mode=importlib`.
  `test/addon_modules.py` provides `load_addon_module`, `load_ops_module(name, subdir)`
  (synthetic package + `anki_stubs.install()`), `FakeClock` and `PausingClock` (calls back after
  each sleep, so a test can resume or cancel a pause), and the fakes that drive a real
  `bulk_nested_notes_op` (`RunGate`, `RunProgress`, `RunCollection`, `patch_nested_run`,
  `wait_until`). Word-array tests skip when SudachiPy or the downloaded dictionaries are
  missing; a skip is not a pass, so say which ran.
- Chains: `test_op_chain_step.py` drives `selected_notes_op`'s chain paths through a fake
  `CollectionOp`; `test_op_chain.py` runs `OpChain` with fake ops and hooks;
  `test_op_registry.py` pins the menu's labels, order and wiring. `__init__.py`'s Edit-menu
  hook has no test.
- The suite's `aqt.qt` is a stub of empty classes. `test_multi_op_dialog.py` still runs real
  widgets: `load_with_real_qt()` loads the dialog module a second time with an `aqt.qt` built
  from PyQt6, for that load only, then restores `sys.modules`; without PyQt6 those tests
  skip. Copy it for another dialog rather than un-stubbing the suite.
- Capture: `test_capture_store.py` (the file, its migration, writer, prune, keys),
  `test_capture.py` (the API), `test_capture_calls.py` (`get_response` through each provider
  over a fake session or Popen), `test_capture_runs.py` (a real `selected_notes_op` run, and
  `notes_run` called directly as a script does), `test_capture_notes.py` (a notes run's records
  and the read points),
  `test_capture_kinds.py` and `test_capture_meaning_kinds.py` (each call site's kind, inputs and
  context, the prompts pinned byte for byte, the AST scan); `test_call_logging.py` covers the
  ids in the log format. A test that installs a store puts it in a
  `tempfile.TemporaryDirectory()` and shuts it down in a cleanup, or it records the next test's
  calls; it reads rows with its own `sqlite3` connection after `flush()`, never by waiting on the
  batch timer, and clears `capture._quiet_until` in `setUp` (capture's warnings are rate limited
  per process).

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
current). `config.md` lists outdated models, and `log_to_console` is `true` in
`config.json` while the code default is `False`.
`anthropic_api_key` is read in `base_ops.py` but has no default in `config.json`.

## Shared code

Declares `jp_text_processing`, `ui`, `utils`, `word_array`. Uses `utils.vendor_path` and
`utils.vendor_rebuild_ui`, `word_array.field_text`, `ui.note_source_buttons` (the multi-op
dialog), and from the submodule `kana_highlight`,
`make_furigana_from_reading`, `check_word_reading_type`, `main_types`. It does not use the
shared `utils/logger.py`, and its config access is inline `getConfig(__name__)`. General
Japanese text logic belongs in the `jp_text_processing` repo, kept free of anything specific
to this addon. Before adding a generic helper, check
[docs/shared-code.md](../docs/shared-code.md).
