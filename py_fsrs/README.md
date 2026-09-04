# py_fsrs — vendored subset of py-fsrs

A trimmed, locally modified copy of
[open-spaced-repetition/py-fsrs](https://github.com/open-spaced-repetition/py-fsrs), used by
[`ease/fsrs_calculator.py`](../ease/fsrs_calculator.py) and
[`ease/fsrs_ops.py`](../ease/fsrs_ops.py) to replay Anki's `revlog` and recompute a
difficulty-derived `factor` for each review.

| | |
| --- | --- |
| Upstream | `https://github.com/open-spaced-repetition/py-fsrs` |
| Vendored version | **v6.3.2** |
| Last synced | 2026-09-04 |

Update those rows whenever you sync.

## What's here

These are ordinary tracked files, not a git submodule. A submodule records a single upstream
commit and cannot carry local modifications, and this copy has substantive ones that upstream
has not adopted (see [Local modifications](#local-modifications)).

```text
py_fsrs/
├── __init__.py        # makes the vendored directory a package (not an upstream file)
├── LICENSE            # upstream MIT license — keep it
├── README.md          # this file (not an upstream file)
└── fsrs/
    ├── __init__.py    # public exports
    ├── scheduler.py   # the algorithm; all local modifications live here
    ├── card.py
    ├── rating.py
    ├── review_log.py
    └── state.py
```

Only four names are imported by the addon — `Scheduler` and `Card` in `fsrs_calculator.py`,
plus `State` and `Rating` in both callers — but all five modules in `fsrs/` are reachable,
since `scheduler.py` imports the other four. Nothing inside `fsrs/` is dead code.

Nothing else upstream ships is vendored, since this tree is bundled into the addon zip.
Upstream's `.github/`, `tests/`, `pyproject.toml`, `setup.py`, `.gitignore`, `osr_logo.png`,
`CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md`, `README.md` and `fsrs/py.typed` are
all outside the vendored set. The directory is ~121 KB.

`LICENSE` is part of the vendored set. It is MIT, which requires the copyright notice to be
retained in redistributions, and this code is redistributed to every user who installs the
addon.

Two upstream files are deliberately *not* vendored even though they are part of the package:

- **`fsrs/optimizer.py`** — depends on torch/numpy/pandas, which Anki does not ship.
  `fsrs/__init__.py` correspondingly exports no `Optimizer`.
- **`tests/review_logs_josh_*.csv`** — a 683 KB fixture used only by `test_optimizer.py`.

> **Runtime dependency:** upstream imports `typing_extensions` at module scope in `card.py`,
> `review_log.py` and `scheduler.py`. This is satisfied by Anki's own bundled environment —
> verified as `typing_extensions` 4.14.0 in `AnkiProgramFiles/.venv`. It is an implicit
> dependency on Anki's bundle; if a future Anki drops it, vendor it here too.

## Local modifications

### Structural

Without these the package cannot run inside an Anki addon at all.

1. **Relative imports.** Upstream writes `from fsrs.card import Card`, which assumes `fsrs` is
   an installed top-level package. Nested here as `py_fsrs.fsrs` that does not resolve, so
   every intra-package import is relative.
2. **`py_fsrs/__init__.py` added.** Upstream has no file at this level. It is also why the
   directory is `py_fsrs` and not `py-fsrs` — a hyphen is not a legal Python identifier.
3. **`DEFAULT_PARAMETERS` re-exported** from `fsrs/__init__.py`.

### Algorithm

In `fsrs/scheduler.py`, each marked with a `Vendored fork:` comment at the site.

1. **`get_card_retrievability` returns `1`, not `0`, when `card.last_review is None`.** A card
   with no prior review must not be scored as though recall was maximally unlikely, which is
   what `0` would do once retrievability feeds into `_next_difficulty` (algorithm change 3).
2. **Fractional elapsed days** (`total_seconds() / 86400`) instead of upstream's whole-day
   `.days`, so same-day reviews — routine in Anki learning steps — are not all collapsed to an
   elapsed time of zero.
3. **`_next_difficulty` takes a `retrievability` argument** and applies a retrievability-aware
   adjustment after upstream's mean-reversion step: a card recalled despite low predicted
   retrievability is rewarded, and a card failed when recall was already unlikely is penalised
   less. Upstream's signature is `(*, difficulty, rating)`; ours is
   `(*, difficulty, rating, retrievability)`. `review_card` computes retrievability once, up
   front, from the card as it stood before the review mutated it, and threads it into all five
   `_next_difficulty` call sites and the three `_next_stability` call sites.
4. **Initialisation is hoisted out of the `match card.state`** block in `review_card`.
   `ease/fsrs_ops.py` forces `card.state` from each revlog row, so a card's *first* review can
   arrive already in the `Review` or `Relearning` state; upstream only seeds
   stability/difficulty inside the `Learning` branch, leaving them `None` and tripping the
   asserts in the other two branches.

   This deliberately **falls through**: after seeding `D0`/`S0` for the rating, the state
   branch still runs its own update on that same first review. Upstream instead makes
   initialisation exclusive (`elif`). The difference is observable — a first review rated Hard
   ends at difficulty ~6.74 here where upstream would leave it at `D0` ~5.11. Changing this
   shifts every `factor` written to the revlog, so treat it as load-bearing. To adopt
   upstream's behaviour, restore the `elif` on the initialisation branch.
5. **`card.step` defaults to `0`** when `None` in the `Learning`/`Relearning` states, rather
   than tripping upstream's `assert card.step is not None`. A forced state change during
   replay can leave `step` unset, which otherwise raises
   `TypeError: '>=' not supported between instances of 'NoneType' and 'int'`.
6. **`LOG` module flag** for tracing the replay.

## Updating to a newer upstream release

```sh
tools/sync_py_fsrs.sh --list      # show available upstream tags
tools/sync_py_fsrs.sh             # sync to the latest tag
tools/sync_py_fsrs.sh v6.4.0      # sync to a specific tag
```

[`tools/sync_py_fsrs.sh`](../tools/sync_py_fsrs.sh) clones upstream, shows what changed, and
applies the upstream-to-upstream diff on top of this tree with a 3-way merge — restricted to
the vendored paths, so files outside the vendored set are never created here. Anything
colliding with a local modification lands in the working tree as ordinary conflict markers to
resolve against the list above.

Because `tests/` is not vendored, the script also fetches upstream's suite at the target
version into a temporary directory, runs it against the vendored `fsrs/`, and reports. That
keeps the regression check without shipping test files in the addon.

Four failures are expected, three by design:

| Test | Why |
| --- | --- |
| `test_retrievability` | asserts `0` for an unreviewed card; we return `1` (algorithm change 1) |
| `test_memo_state` | asserts upstream difficulty/stability (algorithm changes 3 and 4) |
| `test_Optimizer_lazy_loading` | `Optimizer` is not vendored |
| `test_class___eq___methods` | **upstream flake**, not ours — reviews twice with `review_datetime=None` and asserts the logs differ, which only holds if the clock ticks between the two calls. Fails identically on pristine upstream v6.3.2. |

Anything beyond those four is a real regression.
