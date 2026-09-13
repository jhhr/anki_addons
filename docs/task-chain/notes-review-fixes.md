# JNAIO auto-rate-limit review fixes — lane `review-fixes` notes

| | |
| --- | --- |
| Spec | `docs/code-review-jnaio-auto-rate-limit.md` |
| Repo | `https://github.com/jhhr/anki_addons` |
| Work branch | `copilot/jnaio-auto-rate-limit2` |
| Files this lane owns | everything the review's findings name — `japanese_note_ai_ops/`, `anki_shared/utils/vendor_path.py`, `build.py`, and their tests |

## How this file is used

This drives a chained runner: each cloud session reads this file in full, does the first
unchecked task, updates this file, pushes, and launches the next session. Every line here is
re-read by every future link and costs money each time — keep it lean. A commit hash and one
clause beats a paragraph.

The **spec is the review document**. Each task below names the finding number it fixes; read
that finding's section of the spec in full before starting, and no more of it than that.

## Task queue

Ordered by the review's own severity ranking. Each task is one finding's fix plus the
regression test that proves it — the test matters as much as the fix, because the review
gives a concrete failing scenario for every finding and an untested fix cannot show it is
closed.

- [x] **Finding 1 — `word_index.py:261` empty word value splits a meaning group.** Change the
      `and` guard to `or` so `meaning_group_note_ids` returns `None` (falls back to the real
      search) whenever *either* word value is empty. Done when a test in
      `japanese_note_ai_ops/test/test_word_index.py` reproduces the kana-only note scenario
      (note 100 kanjified `""`, note 101 kanjified `""` same reading) and shows the index and
      the Anki search now agree. Update the docstring at lines 249-252, which currently
      describes the `and` behaviour.
      *Done in `5a69655`: guard is `or`, docstring rewritten. The existing
      `test_only_one_word_field_filled_in_is_still_answerable` asserted the buggy `[]`, so it
      became the regression test rather than a second test beside it.*
- [x] **Finding 2 — `MDXLookupError` becomes a permanent `NO_DICTIONARY_ENTRY_TAG`.** Make the
      dictionary-outage case distinguishable from a genuine miss all the way to the tagging
      decision, so `make_meanings_in_note` (`make_all_meanings.py:464-473`) and
      `clean_meaning.py:877-881` write no tag on an outage. Touches
      `sync_local_ops/mdx_dictionary.py:843`, `async_api_ops/make_all_meanings.py`,
      `async_api_ops/clean_meaning.py`, and probably `MakeMeaningsResult` in
      `configuration.py`. Largest task in the queue; if it splits naturally, land the
      propagation and queue the rest. Done when a test shows an outage leaves the note
      untagged and a genuine miss still tags it.
      *Done whole in `ff47058`, no split needed: `get_definition_text` re-raises,
      `MakeMeaningsResult.DICTIONARY_LOOKUP_FAILED` carries it, both tagging paths write no tag.
      New `test/test_dictionary_outage.py` — 10 tests, 6 of them red before the fix.*
- [x] **Findings 3 and 5 — vendoring robustness.** Two small packaging fixes, reviewed
      together. (3) `anki_shared/utils/vendor_path.py:103`: `add_vendor_paths()` must not put
      `user_files/lib` ahead of a healthy shipped tree when `vendor_health()` judges the user
      tree unfit — consult the health verdict before ordering the candidates. (5)
      `build.py:636`: give the per-platform `copy_tree` loop the same existence/type guard the
      flat loop has at line 631, and close the ordering hole where a crash between
      `clear_previous_vendoring()` and the manifest write leaves `lib/` describing itself with
      the previous manifest. Done when `anki_shared/test/test_vendor_path.py` covers the stale
      user-tree case and the build guard is exercised.
      *Done in `557e0cc`: new `_shipped_lib_leads()` decides the sys.path order and
      `vendor_health` reads it too, so both agree on which tree is live; the per-platform loop
      became `copy_per_platform()` and `clear_previous_vendoring()` drops the old manifest.
      5 new tests in `test_vendor_path.py`, new `test_build_vendor.py` (12 tests, 8 red before).*
- [x] **Finding 4 — `concurrency.py:1287` `ESTIMATE_BLEND` applied several times per run.**
      A run must apply the 40% blend once, not once per rising refit plus again in `finish()`.
      Keep the persist-on-rising-refit behaviour the comment at 1280-1285 argues for (a killed
      run must not lose its measurement) while making the stored value what a single blend
      would give. Done when a test in `test_concurrency.py` shows the review's two
      reproductions land on 1.4 MB and 5.2 MB, not 1.64 MB and 7.87 MB.
      *Done in `9fb93b8`: `MemoryEstimator.stored` holds the pre-run value and `persist()`
      passes it as `save_per_task_estimate(..., blend_from=)`, so the run's repeated writes are
      idempotent. 4 new tests in `EstimateBlendedOncePerRunTests`, both reproductions red
      before (1.64 MB exactly as the review says).*
- [ ] **Findings 6 and 7 — `match_single_word_to_notes_from_selected`'s `bulk_op`.** Both in
      the same function in `async_api_ops/match_words_to_notes.py`, so one task. (6) line 2992
      `word_tuples.remove(wt)` mutates the list being iterated — apply the same one-pass-into-a-
      new-list fix this PR already used in `match_words_to_notes_for_note`. (7) line 2974 reads
      `word_list_dict`, which line 2960 binds only inside `if word_list_field in cur_note:` —
      hoist `word_list_dict = {}` above the `if`. Done when a test covers the three-identical-
      duplicates list and the missing-`word_list_field` note.

## State

Findings 1-5 fixed; findings 6 and 7 remain, as the queue's last task. All seven were
re-verified against the working tree at `2bf37d7` before the chain was set up; the spec's line
numbers are still accurate for `match_words_to_notes.py`, which is untouched.

The review's "Checked and clean" and "Noted, not filed as a finding" sections are **not** work.
Do not open tasks from them. The `note_cache.py:89-90` docstring inaccuracy was judged
unreachable and deliberately left alone.

## Session log

- `setup` — chain directory, notes and task prompt added; findings re-verified against the tree.
- `task-1` `5a69655` — finding 1: `meaning_group_note_ids` guard `and` -> `or`; docstring now
  says why *either* empty value is unanswerable. Regression test replaces the one that asserted
  the bug. Suite at baseline (438 passed, 6 `mdict_query`).
- `task-2` `ff47058` — finding 2: `get_definition_text` re-raises `MDXLookupError` instead of
  returning `None`; new `MakeMeaningsResult.DICTIONARY_LOOKUP_FAILED`; the two
  `*_meanings_for_word` functions return it, and `make_meanings_in_note` and
  `clean_meaning_in_note` write no `NO_DICTIONARY_ENTRY_TAG` for it. New
  `test/test_dictionary_outage.py`. Suite 448 passed, same 6 `mdict_query`.
- `task-3` `557e0cc` — findings 3+5. `vendor_path`: `_user_lib_mismatch`/`_shipped_lib_mismatch`
  split out of `vendor_health`, and `_shipped_lib_leads()` gates both the sys.path order and
  which tree health judges. `build.py`: the per-platform loop became `copy_per_platform()`
  (skips a name a tree does not have, copies a bare `.so` as a file),
  `check_per_platform_output` tolerates an absent tag and a lone tag's build, and
  `clear_previous_vendoring` unlinks the old manifest last. New `test_build_vendor.py` imports
  `build` by putting the repo root on `sys.path`.

- `task-4` `9fb93b8` — finding 4: `save_per_task_estimate` grew a `blend_from` argument
  (sentinel-defaulted, since `None` means "nothing was stored before this run"), and
  `MemoryEstimator` keeps `self.stored` to pass into it. Every write a run makes now lands on
  the same single blend. New `EstimateBlendedOncePerRunTests`; suite 452 passed, same 6
  `mdict_query`.

## Decisions made

- **One lane, one branch.** All work lands on `copilot/jnaio-auto-rate-limit2`; the user's
  instructions forbid pushing to any other branch without explicit permission. Do not create a
  work branch of your own, and do not open a PR unless the user asks.
- **Group by review seam, not by file count.** Findings 3+5 (vendoring) and 6+7 (one function)
  are single tasks because a reviewer would want them in one commit each.
- **Every fix ships with a regression test** reproducing the review's stated scenario.
- **task-3: `vendor_health` changed along with the ordering.** The review only asks
  `add_vendor_paths` to act on the verdict, but leaving `vendor_health` judging a tree that no
  longer leads `sys.path` would offer a rebuild forever over a tree that is not in use, so the
  two now share one verdict. `test_a_stale_rebuilt_tree_is_not_rescued_by_a_healthy_shipped_one`
  and `test_rebuilt_tree_without_a_manifest_wants_a_rebuild` asserted the old behaviour; both
  were rewritten to hold the shipped tree stale, so they still test what they were written for.
- **task-3: the stale rebuilt tree is demoted, not dropped.** It is still a layer that may
  resolve something the shipped tree lacks, and demotion already ends the shadowing.
- **task-3: `check_per_platform_output` widened with the guard.** Guarding the copy alone would
  have turned a traceback into a misleading `sys.exit` two lines later (`uvloop has no extension
  module` for the platform that legitimately has no `uvloop`), so the check now tolerates a tag
  with no copy. Nothing beyond that — marker-gated dependencies are not otherwise supported.
- **`get_definition_text` raises rather than returning a sentinel** (task-2). The review allowed
  either. Raising keeps "not in any dictionary" as the plain `None` the three call sites already
  branch on, and the module raises `MDXLookupError` internally anyway; the cost is that a new
  caller must catch it, which the docstring's new `Raises:` section states.
- **An outage leaves the word out of `processed_words_set`** (task-2), so another note of the same
  word can still get a working lookup later in the same run.
- **task-4: the blend stayed in `save_per_task_estimate`.** Moving it into the estimator would
  have been the smaller diff, but the file is read fresh inside that function's `try`, so the
  blend there is against the value actually on disk; a caller-side blend would ignore anything
  another session wrote. The caller only overrides the baseline, and only because its own
  earlier writes are in the way.
- **task-4: the rising-run reproduction is driven with given measurements, not a fit.** A fit
  that actually reaches 10 MB is not reachable inside `MEASURE_SECONDS` (the shallow samples
  stay in the 120-sample window; it converges to ~8.1 MB), and the finding is about the
  sequence of writes rather than the fit that produced them. The 1 MB -> 2 MB reproduction is
  end-to-end through the gate and the real estimates file.
- **A test that asserts a finding's wrong behaviour is part of that finding's fix.** Finding 1
  had one; rewrite such a test rather than leaving it beside the new one.

## Notes for working in this repo

Container setup, needed once per link before tests will even collect (`<addon>/shared` is
gitignored and the submodule is not checked out in a fresh clone):

```
git submodule update --init --recursive
python -m pip install -q pytest requests json-repair rapidfuzz psutil
python build.py link
```

- Tests: `python -m pytest japanese_note_ai_ops/test -q` and, for the vendoring task,
  `python -m pytest anki_shared/test -q`.
- **Known-good baseline after that setup** — match it before and after your change, and treat
  only *new* failures as yours:
  - `japanese_note_ai_ops/test`: **452 passed, 6 failed** (438 before tasks 1-2 and 4). All 6 are
    `ModuleNotFoundError: No module named 'mdict_query'` in `test_mdx_dictionary.py`.
    `mdict_query` is hand-vendored, has no PyPI release, and cannot be installed in a cloud
    container — it is not obtainable, so do not spend a link trying.
  - `anki_shared/test`: **99 passed, 7 failed** (82 before task-3 added 17), all 7 in
    `test_execute_code.py` with `'MainWindow' object has no attribute 'col'` (wants a real Anki
    collection). `test_vendor_path.py` + `test_vendor_rebuild.py` alone: **42 passed**.
- Formatting is Black at line length 100 (`python -m black --line-length 100 <files>`); match
  the surrounding style if Black is not installed.
- Tests are plain `unittest.TestCase` classes on purpose, so they run under `python -m unittest`
  as well as pytest. Keep new tests that way.
- `japanese_note_ai_ops/test/` has its own `pytest.ini`; the root one does not list that path.
- The codebase's comments explain *why*, at length, in full sentences. Match that register —
  several findings are about behaviour a comment already argues for, so update the comment when
  you change what it describes.
