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
- [ ] **Finding 2 — `MDXLookupError` becomes a permanent `NO_DICTIONARY_ENTRY_TAG`.** Make the
      dictionary-outage case distinguishable from a genuine miss all the way to the tagging
      decision, so `make_meanings_in_note` (`make_all_meanings.py:464-473`) and
      `clean_meaning.py:877-881` write no tag on an outage. Touches
      `sync_local_ops/mdx_dictionary.py:843`, `async_api_ops/make_all_meanings.py`,
      `async_api_ops/clean_meaning.py`, and probably `MakeMeaningsResult` in
      `configuration.py`. Largest task in the queue; if it splits naturally, land the
      propagation and queue the rest. Done when a test shows an outage leaves the note
      untagged and a genuine miss still tags it.
- [ ] **Findings 3 and 5 — vendoring robustness.** Two small packaging fixes, reviewed
      together. (3) `anki_shared/utils/vendor_path.py:103`: `add_vendor_paths()` must not put
      `user_files/lib` ahead of a healthy shipped tree when `vendor_health()` judges the user
      tree unfit — consult the health verdict before ordering the candidates. (5)
      `build.py:636`: give the per-platform `copy_tree` loop the same existence/type guard the
      flat loop has at line 631, and close the ordering hole where a crash between
      `clear_previous_vendoring()` and the manifest write leaves `lib/` describing itself with
      the previous manifest. Done when `anki_shared/test/test_vendor_path.py` covers the stale
      user-tree case and the build guard is exercised.
- [ ] **Finding 4 — `concurrency.py:1287` `ESTIMATE_BLEND` applied several times per run.**
      A run must apply the 40% blend once, not once per rising refit plus again in `finish()`.
      Keep the persist-on-rising-refit behaviour the comment at 1280-1285 argues for (a killed
      run must not lose its measurement) while making the stored value what a single blend
      would give. Done when a test in `test_concurrency.py` shows the review's two
      reproductions land on 1.4 MB and 5.2 MB, not 1.64 MB and 7.87 MB.
- [ ] **Findings 6 and 7 — `match_single_word_to_notes_from_selected`'s `bulk_op`.** Both in
      the same function in `async_api_ops/match_words_to_notes.py`, so one task. (6) line 2992
      `word_tuples.remove(wt)` mutates the list being iterated — apply the same one-pass-into-a-
      new-list fix this PR already used in `match_words_to_notes_for_note`. (7) line 2974 reads
      `word_list_dict`, which line 2960 binds only inside `if word_list_field in cur_note:` —
      hoist `word_list_dict = {}` above the `if`. Done when a test covers the three-identical-
      duplicates list and the missing-`word_list_field` note.

## State

Finding 1 fixed; findings 2-7 remain, grouped into four tasks. All seven were re-verified
against the working tree at `2bf37d7` before the chain was set up; every line number in the
spec is still accurate for the files not yet touched.

The review's "Checked and clean" and "Noted, not filed as a finding" sections are **not** work.
Do not open tasks from them. The `note_cache.py:89-90` docstring inaccuracy was judged
unreachable and deliberately left alone.

## Session log

- `setup` — chain directory, notes and task prompt added; findings re-verified against the tree.
- `task-1` `5a69655` — finding 1: `meaning_group_note_ids` guard `and` -> `or`; docstring now
  says why *either* empty value is unanswerable. Regression test replaces the one that asserted
  the bug. Suite at baseline (438 passed, 6 `mdict_query`).

## Decisions made

- **One lane, one branch.** All work lands on `copilot/jnaio-auto-rate-limit2`; the user's
  instructions forbid pushing to any other branch without explicit permission. Do not create a
  work branch of your own, and do not open a PR unless the user asks.
- **Group by review seam, not by file count.** Findings 3+5 (vendoring) and 6+7 (one function)
  are single tasks because a reviewer would want them in one commit each.
- **Every fix ships with a regression test** reproducing the review's stated scenario.
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
  - `japanese_note_ai_ops/test`: **438 passed, 6 failed**. All 6 are
    `ModuleNotFoundError: No module named 'mdict_query'` in `test_mdx_dictionary.py`.
    `mdict_query` is hand-vendored, has no PyPI release, and cannot be installed in a cloud
    container — it is not obtainable, so do not spend a link trying.
  - `anki_shared/test`: **82 passed, 7 failed**, all `test_execute_code.py` with
    `'MainWindow' object has no attribute 'col'` (wants a real Anki collection).
    `test_vendor_path.py` + `test_vendor_rebuild.py` alone: **37 passed, 0 failed**.
- Formatting is Black at line length 100 (`python -m black --line-length 100 <files>`); match
  the surrounding style if Black is not installed.
- Tests are plain `unittest.TestCase` classes on purpose, so they run under `python -m unittest`
  as well as pytest. Keep new tests that way.
- `japanese_note_ai_ops/test/` has its own `pytest.ini`; the root one does not list that path.
- The codebase's comments explain *why*, at length, in full sentences. Match that register —
  several findings are about behaviour a comment already argues for, so update the comment when
  you change what it describes.
