# anki_addons

A monorepo of Anki addons plus the code they share. It lives **inside** Anki's `addons21`
folder but is not itself an addon. Each directory holding a `build.json` is one addon;
`anki_shared/` is shared packages; `build.py` links and packages them.

Read this file first, then the `AGENTS.md` of the directory you are changing. Deeper
material is in [`docs/`](docs/):

| doc | read it when |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | you need to know how `shared/` links, imports, packaging or the type-check setup work |
| [docs/shared-code.md](docs/shared-code.md) | you are about to write a helper, or noticed one that exists twice |
| [docs/anki-patterns.md](docs/anki-patterns.md) | you are touching hooks, undo, background ops, card custom data or addon config |
| [docs/vendoring.md](docs/vendoring.md) | an addon needs a third-party package |
| [anki_shared/testing/README.md](anki_shared/testing/README.md) | you are writing or debugging tests |

## Layout

    build.py                 link / install / vendor / dist / check (stdlib only)
    build.local.json         this device's build.py preferences; gitignored, may be absent
    conftest.py              makes every addon importable under pytest without Anki
    pytest.ini  mypy.ini  requirements-dev*.txt
    anki_shared/             shared packages; no __init__.py, not an addon
      jp_text_processing/    git submodule, its own repo (see below)
    <addon>/
      build.json             package id, name, version, declared shared packages, excludes
      shared/                GENERATED junctions into anki_shared/; gitignored
      lib/ user_files/       vendored deps and per-install state; gitignored

| addon dir | what it is | shared packages |
| --- | --- | --- |
| [copy_anywhere](copy_anywhere/AGENTS.md) | saved "copy definitions" that fill fields from templates, code and other notes | interpolate, ui, utils, anki, jp_text_processing, word_array |
| [japanese_note_ai_ops](japanese_note_ai_ops/AGENTS.md) | AI and local operations that generate fields on Japanese notes | jp_text_processing, utils, word_array |
| [custom_schedule_helper](custom_schedule_helper/AGENTS.md) | rescheduling helpers for a custom FSRS scheduler | anki, scheduling |
| [related_card_disperse](related_card_disperse/AGENTS.md) | keeps related cards from coming due together | interpolate, ui, scheduling, anki |
| [addon_config_sync](addon_config_sync/AGENTS.md) | syncs addon `meta.json` configs through the media folder | none |
| [desired_retention](desired_retention/AGENTS.md) | adds a Desired Retention row to the card info dialog | none |
| [spotify_desktop_link](spotify_desktop_link/AGENTS.md) | opens `spotify:` pycmd links in the desktop app | none |
| [untracked_media_syncer](untracked_media_syncer/AGENTS.md) | makes edited media files upload on sync | none |

## Rules that break things when ignored

- **Never create `__init__.py` at the repo root or directly in `anki_shared/`.** Anki loads
  every `addons21` child that has one, so a root `__init__.py` makes the whole monorepo
  load as a single addon. `build.py` hard-fails if it appears.
- **Never edit anything under `<addon>/shared/`.** It is a junction into `anki_shared/`;
  the real file is `anki_shared/<pkg>/...`, and every addon that declares that package
  sees the edit. Search and edit at the real path.
- **Never delete `<addon>/shared/<pkg>` or an addon's entry in `addons21` recursively.**
  They are junctions; a recursive delete follows them into the working tree. Remove the
  link itself (`rmdir` / `os.rmdir`), or rerun `python build.py link`.
- **Addon code imports shared code only as `from .shared.<pkg>...` / `from ..shared.<pkg>...`**,
  never as `anki_shared...`. The relative form is the only one that also works inside a
  released zip. Tests are the exception: they import `anki_shared.<pkg>` absolutely.
- **Importing a shared package means declaring it** in that addon's `build.json` `shared`
  list, then running `python build.py link <addon>`. `python build.py check` fails on an
  undeclared import, and `dist` refuses to package until it passes.
- **Do not commit `meta.json`, `lib/`, `user_files/`, `dist/`, `build/`.** `meta.json` holds
  the user's real config and the enabled flag, per device.
- **Nothing about one person's machine goes into a committed file**, these docs included:
  no `addons21` folder names, absolute paths, deck, note type or profile names taken from
  someone's collection, or API keys. The repo is public. Per-device settings belong in the
  gitignored `build.local.json`; code must work when it is absent. A name is fine when the
  code itself hardcodes it, and then the doc should say that it is hardcoded.
- Anki is usually running against this working tree. Python changes take effect on the
  next Anki restart; nothing reloads live. Do not start, stop or drive the user's Anki.

## Sharing code between addons

This repo exists so that a change to shared code is atomic with its call sites. You are
**allowed and expected** to move code into `anki_shared/` rather than copy it:

- Before writing a generic helper in an addon (Qt widget, collection/card helper, config,
  logging, progress, text utility), look in `anki_shared/` and in the sibling addons.
- If the helper already exists in a sibling addon, move it to `anki_shared/` and update
  **both** addons in the same commit. Do not leave a second copy "for now".
- If it is only plausibly reusable and has one caller, keep it in the addon. Promote on the
  second use, not on speculation.
- An addon-specific feature does not belong in `anki_shared/` just because it is large.

The procedure, the catalogue of what is already shared, and the known duplication that has
not been cleaned up yet are in [docs/shared-code.md](docs/shared-code.md). The constraints on
code that lives there are in [anki_shared/AGENTS.md](anki_shared/AGENTS.md).

Touching `anki_shared/` changes every addon that declares the package. Run the whole test
suite, not just the suite of the addon you started in.

## The submodule

`anki_shared/jp_text_processing` is a separate repository
(`github.com/jhhr/jp_text_processing`, which nests `mecab_controller`). It is an API provider
with its own `AGENTS.md`, tests, `pyproject.toml` and mypy run; this repo's `mypy.ini` and
pytest config deliberately skip it.

- Keep changes to it general. Nothing in it may know about an addon in this repo.
- A change there is committed in that repo first, then the pointer is bumped here in its own
  commit (`update subproject commit reference in jp_text_processing`).
- A dirty or ahead submodule in `git status` is normal during such work. Do not reset it,
  and do not bump the pointer as a side effect of an unrelated commit.

## Commands

Run everything from the repo root, with the interpreter that has the dev dependencies
(`anki`, `aqt`, pytest; see `requirements-dev.txt`) installed. If `python -m pytest` cannot
import `anki`, you are on the wrong interpreter; do not "fix" that by installing into it.

| command | purpose |
| --- | --- |
| `python -m pytest -q` | every suite; needs no `build.py link` first |
| `python -m pytest -q <addon>/test` | one suite |
| `python -m mypy .` | type check the repo (config in `mypy.ini`) |
| `python build.py check` | undeclared or unused shared-package declarations |
| `python build.py link [addon...]` | (re)create `<addon>/shared/` junctions |
| `python build.py install [addon...]` | `link` plus a junction per addon in `addons21`; Anki closed |
| `python build.py vendor [addon...]` | rebuild `<addon>/lib` from `requirements.txt` for all platforms; needs `uv` |
| `python build.py dist [addon...]` | `check`, then write `dist/<addon>-<version>.ankiaddon` |

Dev dependencies install in two steps because `pytest-anki2` must go in with `--no-deps`;
see the README. Tests under `anki_shared/jp_text_processing` are run from that directory
with `python -B`.

Before calling work done: `python -m pytest -q`, `python -m mypy .` and
`python build.py check` for anything beyond a docs change. Not every addon has tests;
say so in your report instead of implying coverage that does not exist.

## Code conventions

- Target Python 3.10 syntax and stdlib for anything that ships: users' Anki can run 3.9+ and
  the type checkers are pinned to 3.10. No `match`, no `typing.NotRequired`, no `tomllib`.
  New modules use `from __future__ import annotations` with builtin generics.
- The tree is mypy-clean from the root. Keep it that way; narrow Qt's Optionals rather than
  adding blanket `# type: ignore`.
- Import Qt from `aqt.qt`, never from `PyQt6` directly.
- Lines run to about 100 columns. There is no formatter config in this repo; match the file
  you are in and do not reformat code you are not changing.
- Comments and docstrings say **why**: the constraint, the failure that motivated the code,
  the alternative that was rejected. They do not narrate what the next line does. Many
  comments here record a bug that already happened; read them before simplifying.
- No new third-party dependency in an addon without the vendoring path in
  [docs/vendoring.md](docs/vendoring.md). `build.py` itself is stdlib-only and stays so.

## Commits

- Subject is `area: a sentence stating what is now true`, lowercase, no trailing period:
  `vendor: health asks whether every requirement arrived, not just one`. The area is an
  addon, a shared package or a topic (`mypy`, `logging`, `vendor`, `word_array`).
- The body explains the problem and the reasoning in prose, wrapped at about 76 columns.
- One logical change per commit. A move into `anki_shared/` and the call-site updates are
  one change.
- Commit research tooling; do not commit one-off plans, reports or generated output.
- Work happens on `main` or a short-lived feature branch merged into it. Do not push or open
  a PR unless asked. Use the `gh` CLI for anything on GitHub.

## Keeping these docs true

These files are instructions, so a stale line costs more than a missing one. When a change
makes a statement here or in an addon's `AGENTS.md` false (a moved module, a new shared
package, a duplication that got cleaned up), fix the statement in the same commit. Keep
addon-specific detail in the addon's file and cross-cutting rules here.

Do not add a `CLAUDE.md` next to any of these files. Claude Code reads a directory's
`AGENTS.md` only when that directory has no `CLAUDE.md`, so one would shadow the
instructions other tools still see. If Claude-specific notes are ever needed, the
`CLAUDE.md` must start with the line `@AGENTS.md`.
