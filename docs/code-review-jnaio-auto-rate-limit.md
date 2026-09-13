# Code review: "JPAIOps: auto rate limit"

Review of PR [#1](https://github.com/jhhr/anki_addons/pull/1) (`jnaio-auto_rate_limit` → `main`)
at commit `20743bd`.

**Scope:** 52 files, +13,935/−829, of which roughly 7,300 added lines are non-test source.
The addon's own suite runs green where dependencies are present (410 passed); the remaining
failures are environmental — the `anki_shared/jp_text_processing` submodule is not checked
out and `mdict_query` is not vendored in the review environment — not code defects.

Findings are ordered most severe first. Each names the concrete path from the code to an
observable wrong result.

---

## 1. `word_index.py:261` — incomplete meaning group when one word value is empty

`japanese_note_ai_ops/async_api_ops/word_index.py:261`

`meaning_group_note_ids` falls back to the real collection search (`return None`) only when
**both** word values are empty:

```python
if not normal_key and not kanjified_key:
    return None
```

The query being reproduced is `... ("normal:V" OR "kanjified:W")`, and in Anki a `field:`
term with nothing after it is a real matching term meaning *"notes whose field is empty"* —
not a no-op. Lines 259-260 collapse an empty value to `""`, and lines 265-268 then skip that
map lookup entirely (`if normal_key:` / `if kanjified_key:`), because the word maps
deliberately hold no empty keys (`word_index.py:144-149`). So when exactly one value is
empty, the OR-branch the search would have used to pull in every note with an empty field is
simply missing — and because a non-`None` list is returned, `get_other_meaning_notes`
(`clean_meaning.py:168-183`) never falls back to `col_find_notes`.

**Scenario** — a kana-only vocab note whose kanjified field (`word_field`) is blank:

- Note 100: kanjified `""`, normal `おちゃ`, reading `おちゃ`, sort `おちゃ(m1)` — the note being cleaned
- Note 101: kanjified `""`, normal `御茶`, reading `おちゃ`, sort `御茶(m2)` — same meaning group

The search `sort:re:m\d+ -sort:re:x\d+ -nid:100 "reading:おちゃ" ("normal:おちゃ" OR "kanjified:")`
returns `[101]`, because 101's empty kanjified field matches the `kanjified:` term. Verified
against the real class with the PR's own test harness, the index returns `[]`.

`clean_meaning_in_note` then treats note 100 as having no meaning-group siblings, so
`all_meaning_notes` is just `[note]` and the group's meanings are regenerated and renumbered
from a partial set — exactly the "silently split a meaning group" failure the docstring at
lines 249-252 says it is guarding against.

**Fix:** the guard needs `or`, not `and` — fall back to the search whenever *either* word
value is empty.

---

## 2. `mdx_dictionary.py:843` — transient dictionary outage becomes a permanent tag

`japanese_note_ai_ops/sync_local_ops/mdx_dictionary.py:843`

`get_definition_text` collapses `MDXLookupError` back into `None`, which is the same value a
genuine dictionary miss returns. The failure this PR went to some trouble to make
un-cacheable is therefore still persisted — and now permanently, because of the new
`needs_meaning_mapping`.

**Scenario** — the exact outage the `MDXLookupError` docstring documents (36
`unable to open database file` errors inside 600 ms) happens mid-run. For each affected word
`lookup` raises, line 843 returns `None`, and callers cannot distinguish that from "not in
any dictionary":

- `make_all_meanings_for_word` returns `MakeMeaningsResult.NO_DICTIONARY_ENTRY`
- `make_meanings_in_note` (`make_all_meanings.py:471-473`) tags the note *and every sibling
  meaning note* with `NO_DICTIONARY_ENTRY_TAG` and adds them to `notes_to_update_dict`, so the
  tag is written to the collection at cleanup
- `clean_meaning.py:877-881` does the same for the single note

The memo correctly forgets the failure; the collection does not. The new
`needs_meaning_mapping` (`match_words_to_notes.py:1313`) then makes `NO_DICTIONARY_ENTRY_TAG`
terminal, so on every subsequent run those notes are skipped by both `make_meanings_in_note`
(`make_all_meanings.py:425`) and the matching path, and never get their meanings mapped
again. Before this PR the matching path only checked `MEANING_MAPPED_TAG`, so a later run
still retried.

**Fix:** the distinction has to reach the tagging decision — let `MDXLookupError` propagate to
(or be signalled to) `make_all_meanings_for_word` / `clean_meaning_in_note` so they return an
error result instead of `NO_DICTIONARY_ENTRY` and write no tag.

---

## 3. `vendor_path.py:103` — stale `user_files/lib` permanently shadows a healthy shipped `lib/`

`anki_shared/utils/vendor_path.py:103`

`add_vendor_paths()` puts `user_files/lib` ahead of the shipped tree unconditionally —
including in exactly the case `vendor_health()` is about to declare it unfit — and nothing
ever demotes or removes it:

```python
candidates = [user_lib(addon_dir)]
if tag:
    candidates.append(os.path.join(lib, "_platform", tag))
candidates.append(lib)
```

`vendor_health()` (lines 168-175) deliberately judges the user tree "on its own and does not
fall through", so a stale rebuilt tree yields a reason string — but by then
`add_vendor_paths()` has already placed it first, shadowing a shipped tree that is fine.

**Scenario** — a user on a pip-installed Anki (Python 3.10) accepts the rebuild, so
`user_files/lib` gets cp310 wheels. Later Anki's launcher moves to Python 3.13. The shipped
`lib/` is built for 3.13 and would work perfectly, but `user_files/lib` still wins on
`sys.path`, so `psutil` (`concurrency.py:73`) and `rapidfuzz`
(`match_words_to_notes.py:25`) load their cp310 extensions, fail, and silently fall back — a
static concurrency limit and pure-Python Levenshtein.

The startup offer fires once; if the user declines, `record_attempt(addon_dir, "declined")`
keys the refusal on `(python, addon_version)`, so `prompt_is_due()` returns False for the rest
of that Python/addon combination and they are never asked again. Worse, if `can_rebuild()` is
blocked (offline, PyInstaller Anki, no pip), `install_rebuild_ui` records `"unavailable: ..."`
and returns **without showing anything at all** (`vendor_rebuild_ui.py:68-74`), so the user is
permanently degraded with no indication while the correct shipped tree sits unused behind the
stale one.

`vendor_health` detects the condition; nothing acts on it in `add_vendor_paths`.

---

## 4. `concurrency.py:1287` — `ESTIMATE_BLEND` applied several times per run

`japanese_note_ai_ops/async_api_ops/concurrency.py:1287`

The new persist-on-every-rising-refit calls `self.estimator.persist()` inside the refit path,
and `finish()` persists again, so a single run re-applies `ESTIMATE_BLEND` repeatedly instead
of once. One run's measurement therefore carries far more than the documented 40% weight.

Reproduced:

- 1 MB stored + 2 MB measured → **1.64 MB** stored, where a single blend gives 1.4 MB
- a rising run 2 MB → 10 MB → **7.87 MB** stored, where a single blend gives 5.2 MB

Since the stored estimate feeds `max_concurrency_for`, this directly shifts the next run's
concurrency ceiling: one atypical run skews the estimate much harder than intended.

---

## 5. `build.py:636` — unguarded per-platform `copy_tree` between vendor clear and manifest write

`build.py:636`

The per-platform copy loop has no existence or type guard, unlike the flat loop five lines
above it (line 631 checks `if not src.exists(): continue`):

```python
for tag, tree in trees.items():
    for name in sorted(per_platform):
        copy_tree(tree / name, lib / "_platform" / tag / name)
```

`per_platform` comes from `platform_specific_packages()`, which flags a top-level name as soon
as *any two* trees disagree about a shared relative path — it does not require the name to be
present in all five trees. Adding a dependency that is both binary and platform-gated by an
environment marker (e.g. `uvloop` with `; sys_platform != 'win32'`) is enough: uv installs it
into the four non-Windows trees, the two macOS trees disagree on
`uvloop/loop.cpython-313-darwin.so`, so `uvloop` lands in `per_platform`, and
`copy_tree(trees["win_amd64"] / "uvloop", ...)` raises `FileNotFoundError`. The same crash
happens if the differing top-level entry is a file rather than a directory (a distribution
installing a bare extension module such as `_cffi_backend`), where `shutil.copytree` raises
`NotADirectoryError`.

**Aggravating factor — ordering.** This runs *after* `clear_previous_vendoring()` (line 623)
has deleted the old tree and *before* the manifest is rewritten (line 639). A crash here
leaves `lib/` half-rebuilt while `.vendored.json` still describes the **previous** vendoring.
The same hole applies to the `sys.exit()` calls inside `check_per_platform_output`
(lines 503 and 510, reached at line 637): `lib/` has been fully rewritten by then, but its
manifest still lists the old `flat` / `per_platform` / `python_version`. Running `build.py dist`
afterwards ships a `lib/` whose manifest does not describe it — and that manifest is exactly
what `vendor_path.vendor_health()` trusts at runtime.

Latent with today's requirements; the guard is a one-line fix.

---

## 6. `match_words_to_notes.py:2992` — list mutated while iterating (pre-existing)

`japanese_note_ai_ops/async_api_ops/match_words_to_notes.py:2992`

`word_tuples.remove(wt)` inside `for wt in word_tuples:` mutates the list being iterated. This
is the identical defect the PR just fixed — and documented at length — in
`match_words_to_notes_for_note`, left untouched in
`match_single_word_to_notes_from_selected`'s `bulk_op`.

**Scenario** — a word list `[["あ","ア"], ["あ","ア"], ["あ","ア"]]`. At index 0 the key is
recorded; at index 1 the duplicate is detected and `remove` deletes the *first* equal element
(index 0, not necessarily `wt` itself), shifting everything left; the iterator then moves to
index 2, now past the end, so the loop exits and the third duplicate is never removed. The
note keeps a duplicate word the run believed it had deduplicated.

The same one-pass-into-a-new-list fix used elsewhere in this PR applies.

---

## 7. `match_words_to_notes.py:2974` — `word_list_dict` read unconditionally (pre-existing)

`japanese_note_ai_ops/async_api_ops/match_words_to_notes.py:2974`

`word_list_dict` is bound only inside `if word_list_field in cur_note:` (line 2960) but read
unconditionally at line 2974.

**Scenario** — `word_list_field` resolves to `""` or to a field the notetype lacks (a
misconfigured `word_list_field`), so line 2960 is false. On the first selected note this
raises `NameError: word_list_dict` out of `bulk_op` — an unhandled crash rather than the
intended error log. If an earlier note in the same loop *did* bind it, the later note silently
re-reads and dedups the *previous* note's word lists instead.

Hoisting `word_list_dict = {}` above the `if` fixes both.

---

## Checked and clean

Verified in detail, no defects found:

- **Concurrency / networking:** the `ConcurrencyGate` acquire/release and limit-shrink paths;
  `post_with_retry`, rate-limit tracking and the socket-abort machinery; the rolling driver and
  `wait_for_completions` queue handoff; `base_ops.py` cancellation teardown.
- **Caches and collection access:** `collection_access`'s single-worker queue, settle-on-all-paths,
  nested-call self-deadlock guard and cancellation drain; `note_cache` and `sentence_cache`
  single-flight and concurrency arguments; `diagnostics`' one-watchdog-per-run guard;
  `call_logging`'s thread-local bulk state and handler restore; `mdx_memo`'s single-flight and
  LRU accounting.
- **`word_index` query equivalence:** term-for-term against the three replaced Anki searches —
  the `\(x\d\)` exclusion including notes whose notetype lacks the sort field, `nid:`
  narrowing, `する` / honorific / kana-reading alternatives, `m\d+` and `x\d+` markers, Anki's
  case and NFC folding, short `flds` for notes predating a field, and notetype filtering by
  `mid`. `marker_note_ids`' `by_sort_base` bucket and its `re.escape` fallback are sound.
- **New mdx SQL:** properly parameterised; every `sqlite3` connection and `.mdx` handle closed
  in `finally`/`with`; the half-open range bound (`chr(last+1)`, guarded at `0x10FFFF`) is
  correct and UTF-8 byte order matches code-point order; `PREFIX_NEEDS_LIKE` correctly refuses
  the cases where a range and `LIKE` diverge.
- **Deferred-spawn rework:** `make_word_task_spawner` binds per-word arguments in a separate
  frame; `final_update_task`'s `gather(*note_tasks)` is evaluated after all spawners have run
  synchronously; no task gathers a list containing itself.
- **Config and packaging:** the removed `rate_limits` key has no remaining readers and all four
  replacements (`max_concurrent_requests`, `memory_limit`, `max_request_retries`,
  `max_retry_wait_seconds`) are read and documented with matching defaults; the forked
  `python_full_version` markers in `requirements.in`/`requirements.txt` are mutually exclusive;
  `build.json`, the new `EXCLUDE_DIRS` entries, and `vendor_rebuild.py`'s swap and
  manifest-written-last ordering are correct.
- **Changed signatures against every call site:** the `rate_limit`/`task_index` removal and
  `gate=` threading in `make_inner_bulk_op`; `get_matching_notes_for_word_and_reading` becoming
  `async` with a new parameter list; `match_words_to_notes`' 2-tuple return; the new
  `Optional[NotePlan]` contract and `plan.task_count`/`plan.spawn` against `run_plans_rolling`;
  `MatchOpArgs`' three new keys; and the removed `progress_updater.increment_counts` in
  `run_dummy_task`, which was a genuine double-count.

## Noted, not filed as a finding

`note_cache.py:89-90` promises that missing ids are omitted rather than raising, but
`collection_access._fetch_notes:302` lets `NotFoundError` escape. Unreachable in practice —
notes are only removed in `base_ops`' cleanup phase (`base_ops.py:2075`) — so this is a
docstring inaccuracy, not a defect.
