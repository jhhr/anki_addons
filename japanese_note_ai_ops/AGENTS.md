# japanese_note_ai_ops (Japanese Note AI Ops)

Read the root [AGENTS.md](../AGENTS.md) first.

Runs LLM prompts and local operations over Japanese notes, from the browser's "AI helper"
right-click submenu, from the browser's Edit > "Japanese AI ops..." dialog (several ops in a
row, see "Chains" below) and from a few automatic triggers. A note added by a person: in the
one-type layout a "Japanese vocab note" gets `clean_meaning` and `extract_words`; in the
two-type layout (below) a note of that type's sentence type gets `extract_words`, and its cards
are suspended when the classic Add dialog added it (`add_cards_did_add_note`; the experimental
Add dialog fires neither hook), a vocab note nothing. A note an op adds gets nothing
(`in_bulk_op()`), and so does an imported one: Anki's import runs no add hooks, and the user's
sentences arrive by .tsv import, so the ops are run on them afterwards (config.md, "Moving to
sentence notes"). Unfocusing an empty story field on a "Kanji draw" note writes a story;
unfocusing an empty translation field translates, on the vocab type in the one-type layout and
on the sentence type in the two-type one. Those two note type names are hardcoded in the hooks;
what each hook does is decided in `note_hooks.py`. After a sync, the words of the notes tagged
`vocab::split-compound-word` go on the config's list of compounds to split
(`split_compound_tag.py`). Operations: clean/generate a meaning from
MDX dictionary entries, translate, kanji mnemonic story, kanjify a furigana sentence, extract a
**word array** from a sentence (rule-based: SudachiPy + JMdict, one LLM call for proper nouns),
judge which words deserve a note, match words to vocab notes or create them, give each note a
link ID of its own, and for the two-type layout move the sentences out of the vocab notes and
refresh their example copies.
Bulk runs are asynchronous, parallel, memory-aware, pausable and cancellable.

## Map

| path | role |
| --- | --- |
| `__init__.py` | strict order, see below |
| `configuration.py` | `ADDON_USER_FILES_DIR`, word tuple types, tag constants, TypedDicts, `capture_versions()` (the addon, Anki, Python and platform versions every capture run records). Importing it creates `user_files/` and imports `anki` |
| `call_logging.py` | per-call log files in `user_files/logs/` (`logs_dir()`; a replay's go to `replay_logs/`, below), `<name>_<timestamp>.log`: `start_call_log(name)` is called with the op's `OpSpec.key` by the menu action and by each chain step that starts it (a `MenuOnlyAction` has a key too), with the op's key by the editor's field-unfocus hook, `add_note` for a note added by hand, `browser_menu` for building the context menu, `split_compound_tag` for a sweep that has something to list; a phase's file adds its name to the run's (`match_words_add_note_phase_...`, `phase_log_name`). `bulk_op_logging()`, `phase_log()`, `in_bulk_op()`. Every handler it makes gets `LOG_FORMAT` and a `CaptureContextFilter`, so each line carries the capture ids (`[r12 n1712345678901 c4567]`, `[-]` for none); `current_log_path()` is the file a capture run records |
| `generator_resources.py` | `with_generator_resources(parent, then, chain=None)`: asks before the ~83 MB Sudachi dictionary + JMdict download, fetches via `QueryOp`; with a chain, each way of not running fails the step |
| `op_registry.py` | `OPS`: the 26 ops that run through `selected_notes_op`, in menu order, as `OpSpec(key, label, start(nids, parent, chain), needs_generator, group)`; `OP_BY_KEY`. The menu and the dialog both read it |
| `ai_helper_menu.py` | builds the "AI helper" submenu: "Run several ops...", then `OPS` plus two `MENU_ONLY_ACTIONS` (name lexicon, kanjify export). Out of `__init__.py` so it can be tested |
| `multi_op_dialog.py` | the multi-op dialog: `OpSelection` (Qt-free model of the chosen ops and order), `MultiOpDialog`, `show_multi_op_dialog(browser)` |
| `split_compound_tag.py` | the `vocab::split-compound-word` sweep (see "word_array"): `tagged_compounds`, `list_compounds` (the `CollectionOp`'s body), `add_to_list` (the config write), `sweep`, the two sync hook handlers, `init_split_compound_tag()` |
| `html_stripping.py` | aqt-free on purpose, so research scripts can import it |
| `kana_conv.py` | local copy of AJT `kana_conv` (duplicate of the submodule's; see shared-code.md) |
| `note_roles.py` | which configured note type plays which role (see "Sentence and vocab note types"): `is_sentence_type`/`is_vocab_type`, `vocab_type_of`/`sentence_type_of` (the type itself in the one-type layout), `layout_error` (the pre-flight message) and `LayoutError`, `role_error`/`roles_of` (what an op of a role may run on), `example_field_pairs`, `example_id_field`, `link_id_field` (`sentence_id_field`, else `new_note_id_field`), `configured_field`/`configured_value` (the field a type's block names for a key and a note's value of it, "" for a value that names no field, never a raise), `copy_example` (a sentence note into a vocab note, every field checked before any write), `note_type_search` (the quoted, escaped `note:` term every typed search uses), `search_escape` (the one escaping of a name a search holds: a note type's, a field's, a show's). Every function takes a type **name**. anki- and aqt-free |
| `note_hooks.py` | what `__init__.py`'s note hooks do, decided without Anki so it can be tested: `ops_on_added_note`, `translates_on_unfocus`, `suspends_added_cards`. anki- and aqt-free |
| `link_ids.py` | link IDs, what every link holds (see Invariants): `parse_link_id`, `link_id(value, note_id)`, `note_link_id` (a note in hand), `new_link_id(key, taken)` (16 digits from the key's sha1), `LinkIndex` (one type's IDs from (note id, field value) rows: `note_id(s)`, `in`, `duplicates`, `invalid`, `new_link_id`), `placeholder_holders`, `plan_id_fills`, `duplicates_error`/`DuplicateLinkIdsError`/`FILL_LINK_IDS`, `field_search` and `link_ids_search` (escaped; a field named like a search keyword refused). anki- and aqt-free |
| `async_api_ops/base_ops.py` | the operation framework and provider dispatch; `get_response` records each call (see "Capture store"), and `get_transcript` each transcription, the same way; `note_context(note)` names the note for errors and capture together |
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
| `async_api_ops/role_gate.py` | `notes_of_role(config, notes, role)`: the notes an op of one role may run on, the rest left out with one `run_errors` report per note type; `notes_passing` for any other refusal. anki- and aqt-free |
| `async_api_ops/link_lookup.py` | link IDs read from the collection: `link_index(config, T, col=None)` (one SQL pass over a type), `LinkIndexCache` (a run's indexes, one per type), `find_note_ids`/`find_note_id` (one search, field and `nid:` together, the found notes' fields read back). Through `collection_access` on a run's worker threads; a sync op or hook passes `col`. A missing type or field, or one named like a search keyword: `LayoutError` |
| `async_api_ops/<op>.py` | the operations; `match_words_to_notes.py` is about 3250 lines |
| `async_api_ops/set_furigana.py` | "Set furigana from audio": an empty `furigana_sentence_field` written from the caption and the transcripts (`word_array/audio_furigana.py`), with the show's names (the captions' katakana readings over the show's notes, `captions.show_readings`, and `user_files/show_names.json`; the show is `<show>` of the note's tag `ep::<show>::<episode>`, `configuration.EPISODE_TAG_PREFIX`, hardcoded, and a note with none, or two shows', is read alone and reported once a run, `no_show_report`) and JMdict's readings (`jmdict_index.spelling_readings`). The words the audio leaves in review that a transcript heard something at (`furigana_arbiter.asked`) go to the config's `furigana_models`, one call a note and model (kind `furigana_arbiter.note`; none for a note without such words), and a reading every model gives, heard at the word, is written (`word_array/furigana_arbiter.py`). A word still unsettled is written with the readings to choose between, `方[ほう？かた]` (`audio_furigana.doubt_furigana`), and its note tagged `furigana-review` (`configuration.FURIGANA_REVIEW_TAG`); kanjify, word extraction and the match op skip a note with the tag or with such a word, and the kanjify export leaves it out (`furigana_review.unchecked_furigana`). Notes run on the pool's threads, sharing the show's names and Sudachi's lock |
| `async_api_ops/furigana_review.py` | `unchecked_furigana(config, note, *also)`: why a sentence note's furigana is not to be read yet, its `furigana-review` tag or a word still marked `？` (`audio_furigana.has_doubt`) in its furigana field or a field the caller reads instead; the one check kanjify, word extraction (below its skip of a note holding an array), the match op (as it plans each note, counted done), the kanjify export and "Run all ops for new notes" make. Light, so that they need nothing of set_furigana's |
| `async_api_ops/kanjify_policy.md` | the kanjification policy, read at run time: kanjify_sentence's prompt holds it whole (see "word_array") |
| `sync_local_ops/` | operations with no API call; `mdx_dictionary.py` (uses vendored `mdict_query`), `mdx_memo.py` (aqt-free) |
| `sync_local_ops/refresh_example_sentences.py` | "Refresh example sentences" (two-type only): a vocab note copies its example sentence note again; a gone example is replaced by the oldest sentence note whose array links the note, else the id is emptied and the note tagged `example-sentence-missing` |
| `sync_local_ops/fill_link_ids.py` | "Fill link IDs": each selected note of a type with a link ID field left holding an ID of its own (a field holding none gets the note id; each holder of a duplicated ID but its keeper its own note id, or a generated `dup\|<note id>` one when another note holds that), and legacy placeholders in the arrays of the selected notes of the sentence role resolved. Report `user_files/fill_link_ids_<timestamp>.txt`, written before any change; one undo entry |
| `sync_local_ops/sentence_migration.py`, `migrate_to_sentence_notes.py` | "Move sentences to sentence notes", single-use (see "Sentence and vocab note types"): the pure planner (`plan_migration`, `preflight_error`, the report's `CASES`; anki- and aqt-free) and the op that reads every vocab and sentence note in one SQL pass each, writes the report to `user_files/sentence_migration_<timestamp>.txt` and applies the plan (`SentenceMigration`, `migrate_spec()`) |
| `word_array/` | the generator package; **anki- and aqt-free** |
| `word_array/research/` | dev-only scripts; excluded from the zip by `build.json` |
| `dev/` | dev-only, excluded from the zip, run from the addon root like the research scripts. `headless.py` runs an op over a collection file without Anki's main window (the stub `mw`, the user's config with secrets removed and only `terminal-` models allowed, a profile folder and capture store of the caller's, Ctrl+C as Cancel); `capture_run.py` is its CLI for capture runs, `sentence_migration_run.py` for the migration's practice run (every note of the vocab type, not `capture_run`'s notes holding an array; prints a summary naming the report). **It writes to the collection it is given**: a copy, never a profile's collection while Anki has it open. `replay.py` exports a notes run as a fixture and replays it (below); `export_fixture.py` and `export_evals.py` are their CLIs; `benchmark.py` replays a fixture or a corpus (by name from the test data checkout, below: `corpora/` holds the runs too big to replay strictly, where contending notes ask in lock order, which is timing) with timed answers (the recorded latency scaled, fixed, or none), a lenient cassette (a request the capture never made gets an answer of its kind, counted), optionally a fixed free memory for the gate and the corpus's CopyAnywhere add definitions (`--copy-anywhere`, around the run only), and appends each run's figures, and the added notes' fields, to `user_files/benchmarks/<fixture>.jsonl` with the commit, the machine and the hash of the corpus's inputs. Each run's `work` (calls by kind, decisions, new notes, errors) is the same on any machine: `--record-work` writes it to the corpus's work.json, and every later run, on this machine or another, is checked against it (`work_as_recorded`). `--background N` adds N notes shaped like the corpus's with their Japanese moved into Hangul and their note ids and generated link IDs replaced (`replay.background_notes`): every scan and the word index pay for them as for a real collection's, no request can find one, and the run's work is the same with them (test_pipeline). A replay keeps the gate's learned per-task costs in memory (`replay.memory_estimates`), never in the user's `memory_estimates.json`: a benchmark's first run is cold, its repeats warm. `benchmark_compare.py` compares two sets of summaries of one profile and corpus by where the time went: async (planning, running the plans), collection (held seconds, turns), writes (the cleanup's, merges included) and hooks (the adds). `transcribe_server.py` is the local server "Transcribe sentence audio" sends clips to: kana-anime-whisper and Ruby-ASR's mora mode, pinned to the revisions the furigana audio eval measured, in an environment of its own (`transcribe_server_requirements.txt`; setup in its docstring), over HTTP on 127.0.0.1, so Anki's Python never loads PyTorch; the eval's runner loads its models through it. Only `headless` installs the stub `mw`, and only over none or a stub, so the other dev modules import inside a running Anki too |
| `test_anki/` | the write path in a running Anki (pytest-anki2, `anki_shared/testing/running_anki.py`), in the root `testpaths`: `test_real_anki_add_path.py` runs a stand-in op that prepares one new note through `selected_notes_op` and a real `CollectionOp`, with CopyAnywhere's real `init_note_hooks()` and both configs read off disk, and pins that the add hook runs in the add phase and that the run stays one undo step, a hook writing another note under its own undo entry included, and that a hook writing `new_note_id_field` still leaves the new note holding its link ID (`base_ops.link_id_kept`, the note saved again within the run's undo step); `test_real_anki_replay.py` is opt-in (`JNAIO_REAL_ANKI_REPLAY=<fixture name or path>`, `JNAIO_REAL_ANKI_SCALE`, `JNAIO_REAL_ANKI_COPY_ANYWHERE`) and replays a fixture or corpus there (`replay.replay_in_anki`, the corpus's CopyAnywhere definitions attached by `before_run`, after the corpus is built; strict, the notes must match expected.json or expected_copy_anywhere.json), appending its figures and the added notes' fields to `user_files/benchmarks/<fixture>-real-anki.jsonl`; `test_real_anki_split_compound_tag.py` runs the `vocab::split-compound-word` sweep there: what Anki's searches find, the tags and their one undo step, the list in the config the real `AddonManager` reads, and that an older config copied back in the same hook call does not undo it. It calls the two handlers directly, because `media_sync_did_start_or_stop` still holds the handlers of main windows earlier tests closed |
| `test_replay/` | replays in real collections, in the root `testpaths` (real_anki mode): `test_pipeline.py` captures, exports and replays a run over notes made up in the test, and benchmarks it twice (the counts, never the seconds), and does the same for a two-type match run (a new vocab note with its example copied, a failed add replayed in a stand-in note type); `test_migrate_to_sentence_notes.py` runs the migration in a made-up two-type collection (fields, arrays, tags, decks, suspension, one undo step, the add hook running nothing, a cancel then a rerun, the pre-flight); `test_sentence_note_ops.py` runs Refresh and Translate's copy to vocab notes; `test_link_lookup.py` pins what the link ID searches find and that the index and the lookups answer alike; `test_fill_link_ids.py` runs Fill over both types (copies, an empty field, placeholders, one undo step), after which the migration's pre-flight and find_missing pass; `test_find_missing_matched_note_ids.py` runs find_missing over link IDs; `test_set_furigana.py` runs Set furigana from audio with the real Sudachi and JMdict (skipped without them, as the word array tests are), its arbiter answered by a responder, and "Run all ops for new notes" over such notes; `test_furigana_review.py` runs the match op and the kanjify export over a note whose furigana awaits a check; `test_replay.py` replays every fixture in `test_replay/fixtures/` (committed) and in the test data checkout's `fixtures/` (the private repo's), each twice, and twice more with CopyAnywhere when it has expected_copy_anywhere.json. Excluded from the zip |
| `test/` | the addon's suite, run separately (below) |

### `__init__.py` order is load-bearing

1. `add_vendor_paths(ADDON_DIR)`. Nothing that imports a vendored package may come first.
2. `VENDOR_HEALTH = vendor_health(...)`.
3. All operation imports inside one `try/except ImportError`, which sets `MISSING_PACKAGE`.
   **New operation imports go inside this block**, and so do `op_registry`, `ai_helper_menu`,
   `multi_op_dialog` and `op_chain`, which import op modules, and `split_compound_tag`, which
   imports the generator.
4. Hooks and menus are registered only when `MISSING_PACKAGE is None`.
5. `install_rebuild_ui(...)`, unconditionally, so a broken install can still repair itself.

In `run_op_on_add_note` the `new_matched_jp_word` tag check stays first; a comment records
that checking later cost 25 minutes per bulk run. The `in_bulk_op()` exit comes right after it,
before the note type and config are read: an op's cleanup adds on the run's thread inside its
bulk op, and the migration adds thousands of notes the hook must leave alone.

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
   `is_sync_op=True`. Multi-phase operations pass a list of `OpPhase(name, bulk_op)`. An op
   that reads one role's fields passes `role_gate.notes_of_role(config, notes, SENTENCE_ROLE
   or VOCAB_ROLE)` instead of `notes`: in the two-type layout a note of the other type lacks
   them, and a selection or a chain's search holding both types is ordinary.
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
`get_transcript(model, audio, ...)` does the same for a transcriber: `local-<model>` posts the
clip, base64 in JSON, to the local transcription server (`dev/transcribe_server.py`) at
`transcribe_url`; its calls are recorded and replayed like `get_response`'s, the clip by its
file name and sha1 (kind `transcribe.audio`, `async_api_ops/transcribe_audio.py`).
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
| `note_snapshots` | a notes run's note at one stage, once per run, note and stage: `run_id`, `note_id` (a new note's link ID before it is added; a made-up negative id when it holds none), `stage`, `t` (seconds into the run), `mid`, `note_hash` (the blob of `capture_notes.note_record`: id, guid, mid, fields by name, tags) |
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
  its writes), `proposed` (what the cleanup is about to write, new notes by link ID),
  `final` (every note saved, added or tidied, re-read after the cleanup). A note the run only
  learned the id of (a search, a word index lookup: `capture_notes.found`) is fetched as `read` at
  the cleanup's start, before its first write, or once the marker tidying has looked its words
  up and before its renames are saved (`base_ops.tidy_markers`), which finds notes the run did
  not write, still as they were; never a note the run added (`note.added`). Event kinds: `environment` (dictionary files, the
  collection's size, and `records`, `capture_notes.RECORDS`: the kinds of record this capture
  makes, so an exporter tells a run that looked nothing up from one captured before lookups were
  recorded; add to it with a new kind), `search`, `note.missing`, `note.added` (the link ID
  the note was made with -> its id; the key is still `placeholder`, which the exporter reads),
  `note.removed`, `match.decision` and `match.rated` (per word target, its `word_path`,
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
  hardcoded ones, note ids synthetic in every field, the run's config but for what a replay never
  reads that names the user's things (`replay.PRIVATE_CONFIG_KEYS`, the `*_query` searches, and
  in a block `PRIVATE_BLOCK_KEYS`, the migration's tag lists), meanings read, dictionary
  lookups), `cassette.json` (the answers by `request_key`, in the order received) and
  `expected.json` (the notes as the run left them, with symbols for what differs from a run to
  its replay, numbered by the notes' content: `new-N` is what the arrays link a new note by,
  its link ID, or its note id in a run captured before link IDs; the other is `nid:new-N`, or
  `placeholder:new-N` before link IDs; a failed add's ID is `failed-N`, its placeholder
  `placeholder:failed-N`. So a fixture captured before link IDs replays as it did, except one
  with a failed add, whose words are unlinked now: re-record it). It raises `CaptureGap`
  rather than guess, and for a run a
  replay cannot reproduce: one that lost records or has no recorded end (`dropped` NULL), one
  not `completed`, one of another op. The config keeps the blocks of the note types the run
  recorded notes of and, through `vocab_note_type`/`sentence_note_type` in either direction,
  every type linked to them (`_linked_types`; a run over sentence notes that read and added no
  vocab note recorded nothing of the vocab type, and without its block the layout check fails
  every note; a wrong layout is kept whole and fails the replay as it failed the run); those
  two keys' values are renamed with the types, a partner the corpus has no name for gets a
  generic one of its own (a hardcoded name stays), and the corpus a stand-in note type with
  the fields its block names (`_stand_in_notetypes`), in which the match op makes its new
  vocab notes.
  `replay(fixture)` builds the corpus in a fresh collection, points every module's `mdx_helper`
  at the corpus's lookups, answers every `get_response` from the cassette through
  `base_ops.set_responder` (the one seam: a responder
  replaces the provider inside the capture block, on the calling thread; None from it is a
  failed call), runs the op through its `NotesRunSpec`, and reports each difference, a request
  the cassette cannot answer, an answer never asked for and a lookup it lacks. It records into
  a capture store of its own, so it refuses to start while one is installed (installing its own
  would close that one), and puts back the responder and run errors' deliverer it replaced. expected.json
  holds only the notes the run changed, added or removed (format 2; `Fixture.read` fills in
  the rest from the corpus), and a corpus's files are gzipped. `export_fixture.py
  --copy-anywhere` also puts the user's CopyAnywhere config into the corpus (only the definitions
  its add hook runs for the corpus's note types, their note types and decks renamed; a deck the
  corpus lacks, there or in an `insert_deck`, gets a name no replay's deck has), and for a fixture
  writes expected_copy_anywhere.json from a replay with its add definitions on
  (`replay.copy_anywhere_on_add`); the capture run had none on, so that file is a regression
  baseline, not a capture. What those definitions read the capture never recorded, so
  `replay.export_with_copy_anywhere` takes it from the capture's sources: it replays the run
  with them on, records every search they make (`find_notes`/`find_cards`), makes each of
  `--collection` (the collection the capture ran against), and carries what it finds that the
  collection held when the run started, unselected, under ids and names numbered after the
  run's own, until a round finds no more; and it carries each file a fonts check names from
  `--media`, cut to the characters the notes hold (`corpus["media"]`, written to the replay's
  media folder). A read_file stage names its file in a template, so a definition with one is
  refused. Without that, a kanji-grades search found nothing and a fonts check failed on every
  note, logged and swallowed, on every machine.
- **Logs.** A replay writes its logs to `user_files/replay_logs/` beside each addon's own
  `logs/` (`replay.REPLAY_LOGS`, by pointing `call_logging.logs_dir` and CopyAnywhere's
  `logging_setup.logs_dir` there): a replay's run is not one of the user's, and CopyAnywhere
  keeps only its newest 200, so a benchmark's adds pruned the logs of the user's own. The
  replay opens its own call log (`start_call_log(corpus["op"])`) and closes it at the end.
- **Where fixtures live.** A fixture of a real collection holds its note text and excerpts of
  the MDX dictionaries it looked words up in, so it never goes into this public repo. They
  live in the private test data repo (`jhhr/anki_addons_test_data`), cloned to the gitignored
  `<repo>/test_data/` (a linked worktree without one uses its main checkout's), or wherever
  `ANKI_ADDONS_TEST_DATA` points (`dev/data_paths.py`; taken from the repo root when relative,
  and an error when set to no directory), under
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
  column holds it. A transcription sends no prompt: its `request_key` holds the clip's name and
  sha1, its `prompt_key` names its model only, and `dev/export_evals.py` writes it no `key`.
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
  sentence note's id and the target's task; the note a clean_meaning call changes is in its
  context (below). A note not added yet (id 0) is recorded as none.
- Context (`context_json`, schema version 2): what reading or applying the answer needs that
  the prompt does not show, e.g. which note each numbered meaning came from. Not in either key,
  and nothing the prompt is built from (that is `inputs`). Note ids are ints; a note not added
  yet is the link ID it was made with (in its link ID field), which is what a word array links
  it by, or 0 when it has none (a vocab note added by hand, which clean_meaning keys by 0).
  `match.meanings` names every note by its link ID, as the array's link to it is written. Per
  kind:
  - `match.meanings`: `word_path` (the word's element, `arr[p[0]][5][p[1]]...`;
    `match_targets.word_path`); `meanings`, one per `inputs.meanings` item in the same order,
    each `note_id` (null for a generated meaning), `m_number` (its sort field's (mN), 0 without;
    a generated one has the largest) and `gen_index` (its index in the word's generated
    meanings, else null), so the answer's `meaning_number` n is `meanings[n - 1]`;
    `copy_note_id`, the note copied for a new one (a CREATE NEW, or a MATCH of a generated
    meaning).
  - `match.rating`: `word_path`, and `note_id`, the linked note whose meaning is rated.
  - `clean_meaning.map_note` and `rework_note`: `target_note_id`, the one note the answer is
    for and the only one the call can change; `other_note_ids`, the word's other notes, shown
    and never changed, in the order the answer's `same_sense_as` counts them (added notes in
    id order, then the run's new ones by link ID). `map_note` also `depth` (1: its second try,
    after a `make_all_meanings.add`); its possible meaning index counts in
    `inputs.possible_meanings`.
  - `clean_meaning.extract` and `generate`: `note_id`, the note the meaning is for (null when
    its caller does not say). The other kinds have none: `make_all_meanings.make` and `merge`
    answers replace the generated meanings of the inputs' word and reading, and `add`'s are
    appended to them.

**A new AI call site** (`translate_field.py` is the smallest example):

- Pass `kind="<op>.<what one request is about>"`: `translate.sentence`, `judge.word`,
  `clean_meaning.map_note`. The prefix is the module whose prompt it is, not the op the user
  ran (a low map score in clean_meaning makes a `make_all_meanings.add` call).
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
  the edited notes' write, `new_notes_op` or the unlinking is dropped. A reset that could not happen, or lands after the op thread
  stopped waiting or closed the window (a generation under `_arm_lock`), arms nothing, so a
  stale first cancel is never taken for a second one. From then on a cancel
  (`cleanup_cancel_requested()`, never `run_cancelled()`) stops the adding, checked before the
  dedupe, between its merges, after it and before each `add_note` - never inside one, where
  copy_anywhere's on-add definitions run. `end_cleanup_cancel()` closes it in a `finally`.
  The notes split three ways: added (each holds the link ID it was made with, which the
  arrays saved before the adding link it by: no array is rewritten), failed and not added
  (the words linked to them put back to `["match"]` by `clear_unadded_note_ids`, by their
  IDs, which no note will ever hold). The markers preparing either of the last two put on
  other notes are the tidying's, below. `new_notes_op` (the migration's) and the unlinking
  always run to the end; a `new_notes_op` that raises fails the op after the unlinking.
- **A new note's link ID field is what makes its ID its own, and the add must not change
  it.** `col.add_note` runs every addon's `note_will_be_added` hook on the note first, and a
  hook can write that field: a CopyAnywhere definition filling it with `{{__Note_ID}}` writes
  "0", the id of a note not added yet, which would make the note's link ID its note id, which
  no array links. `base_ops.link_id_kept` wraps each add and puts the ID back in the note in
  memory, and `add_new_notes` saves those notes again into the run's undo entry after the
  adding: the add wrote them as the hook left them. Anything new that needs a new note's ID
  after the add reads it off the note as handed on, never from the collection, which holds
  what the hook wrote until then.
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
  of Anki, and so are `note_roles.py`, `note_hooks.py`, `link_ids.py`,
  `async_api_ops/role_gate.py` and `sync_local_ops/sentence_migration.py`: their tests run on
  dicts, rows and hand-written arrays,
  and `note_hooks` exists because `__init__.py` cannot be imported by a test.
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
- **The one-type layout behaves as before roles existed**, its searches and prompts byte for
  byte: an untyped search stays untyped there (a note copied from a candidate of another
  configured type is of that type, linked from the processed note's), a new note copies the
  four sentence fields it always did, and `note_roles` answers each type as its own vocab and
  sentence type. Every two-type change is behind the test that the type's partner is another
  type (`sentence_type_of(config, V) != V`, `vocab_type_of(config, S) != S`), with tests of its
  own next to the one-type ones.
- **In the two-type layout the arrays are on the sentence type's notes only.** The vocab notes
  keep their old array and sentence fields, under the sentence block's names, until the user
  deletes them, and nothing may read them there: every search over the array field names the
  sentence type (`note_roles.note_type_search`), and a vocab note's sentence is its example
  sentence note's (`example_sentence_id_field`), read on worker threads through the run's
  note cache or `collection_access`.
- **A link is a link ID, not an Anki note id** (`link_ids.py`, config.md "link IDs"): an
  array's `match_data` holds the vocab note's, `example_sentence_id_field` the sentence
  note's. A note's link ID is the positive integer in its type's link ID field
  (`note_roles.link_id_field`), else its note id, which is what links written before link IDs
  hold. Resolve one through an index (the match op's `LinkIndexCache`, a sync op's
  `link_index`) or `link_lookup.find_note_id`, never `get_notes([ID])` or a bare `nid:` search;
  a note in hand gives its own by `link_ids.note_link_id`. A note an op makes holds a
  generated 16-digit ID from the moment it is made (`LinkIndex.new_link_id`: no note of the
  type holds it, and the run gives it out once): there are no placeholders, and no array is
  rewritten after the add. An ID two notes of a type hold answers with its keeper (the holder
  whose note id it is, else the oldest); the match op, find_missing and the migration refuse
  while one exists (`link_ids.duplicates_error`, which names "Fill link IDs"). A legacy
  negative placeholder in an old array is resolved by `match_targets.resolve_placeholder_ids`
  (the match op before gathering, Fill, the migration).
- A note that already holds a word array is skipped unless the Regenerate merge path is
  used; a field that does not parse as an array is never overwritten. Every note holds an
  array now, and the old per-part-of-speech dict format has no reader left: a field that is
  not an array is a broken one, and match ops tag it `invalid_word_list_json`.
- Most vocab note fields are **generated from one authored field**. `vocab-kanjified`,
  `vocab-furigana`, `vocab-processed-furigana` are derived; `vocab` is left alone;
  `vocab-kanji-grades` is regenerated by a copy_anywhere definition. Fix data in the authored
  field or the generator, or the next regeneration undoes it
  (`word_array/research/vocab_respell.py` docstring). The note lookup key is
  `(dict_form, reading)` against `vocab-kanjified` plus `vocab-kana`: the vocab block's
  `word_kanjified_field` and `word_reading_field`. `word_index` reads every note type that has
  a field named like its kanjified, normal or sort field, which is why a sentence type must
  have none (config.md).
- The sources contain Japanese text. On Windows set `PYTHONIOENCODING=utf-8` for scripts, and
  do not pass Japanese through an inline shell heredoc; write a script file.

## Sentence and vocab note types

A configured note type's role comes from its config block (`note_roles`): `word_list_field`
gives it the **sentence role** (its notes hold a sentence and its word array), `word_sort_field`
the **vocab role** (its notes are the words the arrays link). The **one-type layout** is one
block with both, "Japanese vocab note" holding the word and the sentence it was found in: every
config before this, and still valid. In the **two-type layout** a sentence type's block names
its `vocab_note_type` and the vocab type's names it back as `sentence_note_type`; a vocab note
keeps only copies of its **example sentence** note's fields that both blocks name (translation
and audio) and its link ID in `example_sentence_id_field` (`note_roles.copy_example`), and the
sentence notes are never studied. `layout_error` is stricter than "name each other": in a pair
the sentence block has `word_list_field` and no `word_sort_field`, the vocab block the reverse,
since the roles decide what is done to a type's notes. Only a pair's sentence block may name
`sentence_id_field` (elsewhere it would win over `new_note_id_field` as the link ID field,
which code reading that key and the user's CopyAnywhere definitions take for the ID), and no
block may name its own link ID field as `example_sentence_id_field`. config.md has the keys,
both layouts and the user's steps.

In the two-type layout, besides the hooks (top of this file) and the role gating ("An operation
is three functions"):

- Match op: `get_match_fields` reads `SENTENCE_MATCH_FIELD_KEYS` from the processed sentence
  type's block and the rest from its vocab type's; `MatchOpArgs.vocab_note_type` names the
  latter. A new note is of the vocab type, with `copy_example` from the processed note (a
  CREATE NEW copy first blanks every field the sentence block names that it has), and is
  cleaned with the target's highlighted sentence handed in (`own_sentence`). A candidate's
  example in the prompt is its example sentence note's array (`_sentence_note_example`; `""`
  without one, since no `<b>` is stored). The cleanup's searches go through `_ArrayLinks`.
- clean_meaning (`get_sentences_for_note`): the own sentence is the example note's, the others
  the sentence notes linking the word, each note once.
- "Translate sentence" on a sentence note copies the translation into the vocab notes whose
  example it is; the editor's unfocus translation does not (it saves only its note). "Refresh
  example sentences" copies the examples again.
- "Run all ops for new notes" gives each note its type's role's steps (`roles_of`). Where a
  sentence type keeps transcripts (`sentence_transcription_field`), its phases begin with
  "Transcribe sentence audio" and "Set furigana from audio" over the sentence notes with a clip
  and no furigana yet (`new_note_all_ops.audio_furigana_phase`,
  `set_furigana.wants_audio_furigana`), the first naming the run's undo entry
  (`bulk_notes_op(undo_label=...)`), the second leaving out without a word a note the first
  left lacking a transcript (the first said why, a server down once); a note whose furigana
  awaits a check (`furigana_review.unchecked_furigana`) then gets no kanjify or extraction,
  reported once.

**"Move sentences to sentence notes" is single-use.** It moves the one-type layout's sentences
into sentence notes once: the user runs it after a practice run on a copy, by the steps in
config.md "Moving to sentence notes", and it can be rerun after a cancel or over notes a first
run left out. Once the user's real run is done and the old fields are deleted, remove it:
`sync_local_ops/migrate_to_sentence_notes.py` and `sentence_migration.py`, its `OpSpec` in
`op_registry.OPS` and its entries in `test/test_op_registry.py`, its `headless.op_specs()`
entry, `dev/sentence_migration_run.py`, `test/test_sentence_migration.py`,
`test_replay/test_migrate_to_sentence_notes.py` (whose add-hook test is the real-collection
proof that an op's adds get nothing, which `test/test_note_hooks.py` points to: move that to
another op's adds), `test_replay/test_pipeline.py`'s import of the tag list keys,
`test_replay/test_fill_link_ids.py`'s import of `collection_preflight_error` and its check that
the migration's pre-flight passes after Fill, and the steps and the migration-only keys in
config.md (`migration_move_tags`, `migration_copy_tags`; nothing
then writes `sentence_seen_count_field`). Keep `replay.PRIVATE_BLOCK_KEYS` while a config may
still hold the lists.

## word_array

The field partitions a furigana sentence into elements
`[raw_text, pos, dict_form, reading, match_data, sub_words]`; tags and punctuation are
single-element arrays; concatenating the top-level `raw_text` values gives back the sentence
without `<b>` tags. `match_data` has five states (`match_flags.MatchState`): `[]`,
`["dontmatch"]`, `["match"]`, `[id]`, `[id, quality]`. Details: `word_array/README.md`.

Every sub-word is one more word the judge and the match op are asked about, so none is added by
a general rule that both pieces are dictionary words. A verb or adjective Sudachi has as one
unit (目立つ, 名付ける, 面白い) has no sub-words unless the user lists it in config
`split_compound_words`, which they edit in Anki: `generator.split_compounds_of(config)` reads it
and `generate(sentence, names, split_compounds)` takes it; the generator has no list of its own,
and `extract_words` passes the run's. The analysis never reads the sentence's own `<b>`: it is
left from when a note's word was marked by hand, and the card marks it from the array now. A
rule splitting every such compound, a split where the `<b>` ended, a set kept in the code and a
tag on the sentence's note, which split every compound of that sentence, were each tried and
taken back (README, "Sub-words of one word").

The list is also fed by a tag, because a compound is met on a phone too and a config is not
edited there: `split_compound_tag.py`. A note tagged `vocab::split-compound-word` (hardcoded)
is a note about a compound. After a sync, for each such note whose word the list lacks,
`sweep` adds the word (the note's `word_kanjified_field`, and the dict_form of every array
element linked to the note, which is what the generator looks a compound up by) and tags the
notes whose array still has the word whole `sentence::regenerate` (hardcoded,
`configuration.REGENERATE_TAG`): regenerating is the user's to start, and `extract_words` takes
the tag off a note once it has generated its array, by "Extract words" or "Regenerate words",
changed or not; a note it left alone or failed on keeps it. The arrays are the tagged note's
sentence type's (`note_roles.sentence_type_of`): in the two-type layout the tag is on a vocab
note, and the notes searched and tagged are sentence notes, never the vocab notes' old array
field. What holds it together:

- **The tag stays, and only an unlisted word is acted on.** The tag is what restores a word
  the list loses, so it is never consumed; passing over a listed word is what keeps the notes
  from being tagged again at every sweep, which matters for a compound the generator finds no
  two words in. Anki's `tag:` search also finds a tag's children, hence the `has_tag` check.
- **The sweep runs once a media sync has stopped, after every one, from one of Anki's
  timers.** Addon Config Sync copies each add-on's `meta.json` out to the media folder at
  `sync_will_start` and can copy it back at `media_sync_did_start_or_stop(False)`. A version
  from before it kept a device's unsent changes (`addon_config_sync/matched_copies.py`) does
  wherever the two differ, so a list written in between is undone, and so is one written
  after the sync as soon as a media sync ends with no collection sync having started first
  (Anki's periodic one, every 15 minutes by default). The current one does only for the
  rest of an Anki session in which it has already replaced this add-on's config. Sweeping
  after each media sync puts the word straight back either way. The timer puts the sweep
  after the other handlers of that hook call, whatever order the add-ons loaded in, keeps it
  from starting under a progress dialog, and drops it when the profile has closed.
- **A word put back is put back quietly.** `_listed_this_session` holds what this session's
  sweeps listed; a word in it that is off the list again is written back with no
  `CollectionOp`, no tagging and no tooltip, or a long session would tag and announce every
  15 minutes. It is module state: a test clears it (the unit tests) or replaces it (the
  running-Anki fixture). This and the sweep after every media sync are there for the
  copying back, which the current Addon Config Sync does far less of than an older one; the
  tag staying also covers another device's config winning.
- **Tags first, list second.** `list_compounds` tags in a `CollectionOp`, one undo step, and
  `add_to_list` runs in its success handler: notes tagged with no word listed is repaired by
  the next sweep, the reverse by nothing. `add_to_list` writes the user's own config with the
  one key changed, not `getConfig`'s merge, which would turn every shipped default into the
  user's setting.
- The two searches over the array field are `match_flags.word_array_link_regex` and
  `word_array_whole_word_regex`, beside `word_array_query_regex` and escaped like it.

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
that run. `number_counter_unlink.py` is the one that deletes notes: its `--revert` puts the
arrays back and no note, so it keeps what each deleted note held in `output/`. The eval data is not in `output/`: the eval sets, the judge's hand labels, the
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

The kanjify policy is written once, in `async_api_ops/kanjify_policy.md` (beside the op, since
the zip leaves `research/` out), and every kanjify prompt inlines it: the kanjify_sentence op's
(`kanjify_sentence.get_kanjify_sentence_prompt`, which has no rules of its own), the label-fix
agents' (`kanjify_agents/make_prompts.py`) and the **kanjify golden set**'s, a reference
labelling of every note sentence by headless agents for scoring kanjify_sentence
(`kanjify_eval.py --rows`, with `--sample` for a fixed part of it). A rule changes in that
file, never in a prompt. Its scripts (`kanjify_golden*.py`,
overview in `kanjify_golden.py`'s docstring) run from files only: `kanjify_golden_dump.py`
is the one that asks AnkiConnect, and nothing writes to the collection. Step 1 decides one
survey word per agent (`kanjify_agents/word_template.md`), step 2 labels batches of sentences
with those decisions (`batch_template.md`); `agent_queue.py` runs a rendered queue through
`claude -p` (resumable, shardable across machines by `--shard I/N`, stopped cleanly by the
usage limit or a STOP file), and agents look things up only through the read-only
`kanjify_lookup.py`. Everything they read and write is in `evals/kanjify_golden/` of the test
data checkout. A cloud session has no `claude` CLI, so it runs the same queues as its own
subagents instead (`agent_items.py` takes and saves items, the types in the repo's
`.claude/agents/kanjify-*.md` fix model, effort and tools; `kanjify_agents/cloud_runbook.md` is
what such a session follows), writing the same result files.

The **furigana audio eval** (`furigana_audio*.py`, overview in `furigana_audio.py`'s docstring)
is for "Set furigana from audio" (`async_api_ops/set_furigana.py`): it measures which
transcriber or reading check gets the reading the speaker used. Its corpus is a subs2srs export
of one show in `evals/furigana_audio/` of the test data checkout, not notes: the picking reads
the TSV, and the picked cards' clips are copied there from the subs2srs output on the machine
that has it (`furigana_audio_copy.py`). The export's Japanese closed captions have speaker
labels, sound descriptions and readings of their own (`猫猫(マオマオ)`), which
`word_array/captions.py` strips or keeps, and a line's words are `word_array/kanji_words.py`'s
(names, numbers with their counters and runs Sudachi cut, each taken whole): the op's own
modules, so the eval reads a line as the op will. `furigana_audio_run.py` runs one
transcriber over the clips into `runs/` with the models and loaders of the op's transcription
server (`dev/transcribe_server.py`, below), in an environment of its own (PyTorch,
transformers, Ruby-ASR's package: none of them is a dev dependency here, and the scripts
import them only when they run); `furigana_audio_score.py` scores the runs against the readings the captions
give and the labels by ear, through the op's own check (`word_array/reading_check.py`: the
morae, the alignment, what a word is heard as). With `--combine` it writes what the op's rule
would (`reading_check.decide`): the draft where the models hear it, a reading JMdict has for
the spelling or the show has for a name where they hear that instead, review otherwise.
`word_array/audio_furigana.py` is that rule for one caption, its transcripts in and its
furigana out; over the eval's cards it decides every word as `--combine` does. The
check's limits on a correction (`CORRECT`, a stretch `fit` may not shrink for one, and none
that only a vowel's length tells from another licensed reading) each came from readings the
runs got wrong, so a change to the check shows in the eval's numbers. `furigana_audio_label.py` gets the labels
by ear: `queue`, where the scored runs are, picks the words the rule corrects, sends to review
or writes on fewer than every model's hearing, and a sample of the rest; `serve` (standard
library only, so it runs on the machine with the clips) plays each card's clip in a page on
localhost and writes `labels.jsonl`, which the scorer then takes for the known reading. The
page also takes the show's names (`names.jsonl`: one reading for a surface wherever it is
written) and corrections of caption lines that are not what the clip says (`fixes.jsonl`);
the selection's `--reword` and the scorer read both, so the queue is built again after either.
`furigana_audio_arbiter.py` asks the arbiter's models (`word_array/furigana_arbiter.py`) about
the words the rule leaves in review, through the op's own `get_response` (a `terminal-` model
runs `claude -p`, so it runs in a cloud session too), caches the answers in the eval's
`arbiter_results.jsonl` and scores each model, and the models together as the op asks them,
against the labels.

## Tests and types

- `test/` (about 70 files, `unittest.TestCase`) is **not** in the root `testpaths`. Run it from
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
- Two note types: `test_note_roles.py` (roles, layouts, `copy_example`, the search term),
  `test_note_hooks.py` (what each hook does; the hooks in `__init__.py` themselves have no
  test), `test_role_gate.py` and `test_role_gating_ops.py` (every op's bulk function leaves the
  other role's notes out), `test_new_note_all_ops.py`, `test_translate_field.py`,
  `test_sentence_migration.py` (the planner and pre-flight), `test_link_ids.py` (the link ID
  rules, the generator's numbers, the index and its keeper, `plan_id_fills`, the search terms),
  `test_fill_link_ids.py` (Fill's plan and report), and two-type tests (mostly
  `TwoType*` classes) beside the one-type ones in `test_cleanup_new_notes.py`,
  `test_word_array_match_targets.py`, `test_match_word_lookup.py`, `test_sentence_cache.py`,
  `test_capture_kinds.py` and `test_capture_meaning_kinds.py`, where the one-type prompts stay
  pinned byte for byte. The ops' real-collection tests are in `test_replay/` (Map).
- `test_split_compound_tag.py` covers the sweep's decisions over a stand-in collection that
  answers a search with whatever the test hands it, so it says nothing about what a search
  finds; that is `test_anki/test_real_anki_split_compound_tag.py`'s (root suite). The two
  regexes are tested against `format_word_array`'s text in `test_word_array_flags.py`.
- The suite's `aqt.qt` is a stub of empty classes. `test_multi_op_dialog.py` still runs real
  widgets: `load_with_real_qt()` loads the dialog module a second time with an `aqt.qt` built
  from PyQt6, for that load only, then restores `sys.modules`; without PyQt6 those tests
  skip. Copy it for another dialog rather than un-stubbing the suite.
- Capture: `test_capture_store.py` (the file, its migration, writer, prune, keys, and the
  key `dev/export_evals.py` writes),
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

- Furigana from audio: `test_word_array_captions.py`, `test_word_array_kanji_words.py`,
  `test_word_array_reading_check.py` and `test_word_array_audio_furigana.py` (the caption
  cleaning, the word units, the check and the furigana written from them, on a stand-in
  tokenizer), `test_word_array_furigana_arbiter.py` (the arbiter's inputs, answers and the
  checks on what it writes), `test_set_furigana.py` (which notes the op writes, the arbiter's
  calls, the review tag, the show and
  its names), the check in `test_furigana_review.py` and its use in `test_kanjify_sentence.py`
  and `test_extract_words.py`,
  `test_transcribe_audio.py` (the transcription field, which transcribers
  a note is asked for, the server check before a run), `test_capture_calls.py`'s
  `TranscriptTests` (the request, its record without the audio, a replay answering it) and
  `test_transcribe_server.py` (the server's HTTP side, with stand-in models).
- `word_array/research/test/` is in the root `testpaths` and runs with the root
  `python -m pytest`. Most research scripts are tested in `test/` instead (`test_vocab_*.py`,
  `test_kanjify_*.py`), and `vocab_morphology` in both, so a change under
  `word_array/research/` runs both suites.
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
`make_furigana_from_reading`, `check_word_reading_type`, `main_types` and `all_kanji_data`
(the generator reads a lone kanji's on'yomi from it). It does not use the
shared `utils/logger.py`, and its config access is inline `getConfig(__name__)`. General
Japanese text logic belongs in the `jp_text_processing` repo, kept free of anything specific
to this addon. Before adding a generic helper, check
[docs/shared-code.md](../docs/shared-code.md).
