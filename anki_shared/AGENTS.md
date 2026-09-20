# anki_shared

Packages shared by the addons in this repo. Not an addon, not installable, not importable
under its own name at runtime: each addon sees the packages it declares as
`<addon>/shared/<pkg>`. **Whether** something belongs here is decided by the root
[AGENTS.md](../AGENTS.md) and [docs/shared-code.md](../docs/shared-code.md), which also has
the catalogue and the move procedure. This file is the rules for code that lives here.

`jp_text_processing/` is a git submodule with its own `AGENTS.md`; nothing below applies to
it. Do not edit it as part of a change to the other packages.

## Every edit here is an edit to several addons

Before changing a signature or behaviour, grep all addons for the name (exclude `*/shared/`,
which is this directory again through junctions). Update every caller in the same commit.
Then run the **whole** suite from the repo root, `python -m mypy .`, and
`python build.py check`. `custom_schedule_helper` and the small addons have no tests, so for
them the grep and a careful read are the only check there is.

## Constraints

- **No `__init__.py` directly in `anki_shared/`.** Packages below it have them as needed.
- **No addon identity.** No `mw.addonManager.addonFromModule(__name__)`, `getConfig`,
  `writeConfig`, addon folder paths, note type names or field names. Here `__name__` resolves
  to a different addon per copy. The caller passes in what is needed: `addon_dir`,
  `addon_name`, a config value, a callback. See `utils/vendor_rebuild_ui.install_rebuild_ui`.
- **No module-level mutable state that outlives a call.** Each addon gets its own copy of the
  module, so such state is per addon anyway, and it makes the code untestable. The caller
  owns state and passes it in (the `local_rids` list in `anki/sync_hook_base.py`).
- **Imports.** Within a package, relative (`from .to_lowercase_dict import ...`). To a sibling
  shared package, `from ..<sibling>.<module> import ...`, and either inside
  `try/except ImportError` with a graceful fallback (as `interpolate/execute_code.py` does),
  or as a documented hard requirement. Today the only hard one is: `ui/interpolated_text_edit`,
  `ui/code_edit_layout`, `ui/add_model_options_to_dict` and
  `ui/add_intersecting_model_field_options_to_dict` require `interpolate`. `build.py check`
  does not verify this, so do not add another hard edge without listing it here and in
  `docs/architecture.md`. Never `import anki_shared...` outside `test/`, `test_anki/` and
  `testing/`; it cannot resolve inside a released addon.
- **No third-party imports** outside `testing/`. Shared code ships in every declaring
  addon's zip and only `japanese_note_ai_ops` vendors anything.
- **Keep Anki-free code Anki-free.** `word_array/field_text.py`, `utils/vendor_path.py`,
  `utils/vendor_rebuild.py`, `utils/make_query_string.py`, `utils/block_signals.py`,
  `interpolate/to_lowercase_dict.py` import neither `anki` nor `aqt`, and
  `anki/write_custom_data.py` does not touch `mw`. That is what lets them be tested and used
  from scripts. Put logic that needs no collection in a module that does not import one.
- **Qt from `aqt.qt` only.** Overrides of Qt virtuals carry `# type: ignore[override]` where
  the stubs demand it.
- **Logging** is `logging.getLogger(__name__)` with no stderr handler; stderr inside Anki is
  an error dialog. No logger objects threaded through call signatures; `utils/logger.py`
  records why that class was removed.
- Python 3.10-compatible, mypy-clean. Newer modules use `from __future__ import annotations`.
- One widget or one cohesive group of functions per file, the file named after it. A new
  module opens with a docstring saying what it is for, who uses it and why it is shared.

## Tests

| directory | for |
| --- | --- |
| `test/` | everything that runs without a running Anki. Import absolutely: `from anki_shared.interpolate.interpolate_fields import ...` |
| `test_anki/` | the running-Anki harness's own tests |
| `testing/` | the harness itself: `anki_stubs`, `real_anki`, `running_anki`, `pytest_plugin`. Read its [README](testing/README.md) before touching it; most lines guard a race or crash that already happened |

Code moved here from an addon brings its tests along, rewritten to the absolute import.

## Generated fields and the word array

`word_array/field_text.py` is the single codec for a field that one addon writes and another
reads. A format change is a change to `japanese_note_ai_ops` (generator, research tooling),
to `copy_anywhere` code-mode definitions stored in the user's `meta.json`, and to data
already in the user's collection. Treat it as a data migration, not a refactor.
