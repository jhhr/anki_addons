# Shared code: what exists, when to promote, how to move it

The policy is in the root [AGENTS.md](../AGENTS.md): look before you write, promote on the
second use, update every caller in the same commit. The constraints on code once it lives in
`anki_shared/` are in [anki_shared/AGENTS.md](../anki_shared/AGENTS.md). This file is the
catalogue and the procedure.

## What is already shared

| package | contents | needs |
| --- | --- | --- |
| `anki/` | `write_custom_data` (card custom data, enforces the 100-byte limit); `sync_hook_base` (`create_comparelog`, `review_cid_remote`: which cards were reviewed on another device during a sync) | anki, aqt |
| `interpolate/` | `interpolate_fields`: the `{{Field}}` / `{{__Card_*}}` template engine, its marker constants and menu dicts. `execute_code`: the restricted runner for user-written "code mode" (`execute_code_core`, `ReadOnlyNote`, `ReadOnlyCard`). `to_lowercase_dict` | anki, aqt; optionally `jp_text_processing`, `word_array` |
| `scheduling/` | `due_dates`: `filter_revlogs`, `get_last_review_date`, `update_card_due_ivl`, `get_fuzz_range`, `due_to_date` | anki, aqt |
| `ui/` | Qt widgets, one per file: `MultiComboBox`, `GroupedComboBox`, `RequiredLineEdit`/`RequiredTextEdit`/`RequiredCombobox`, `AutoResizingTextEdit`, `PasteableTextEdit`, `ListInputWidget`, `LoadingIndicator`, `ScrollableQDialog`, `ToggleSwitch`, `InterpolatedTextEditLayout`, `CodeEditLayout`, and the `add_*_options_to_dict` menu builders | aqt; four modules need `interpolate` |
| `utils/` | `block_signals`, `make_query_string`, `adjust_width_to_largest_item`, `logger` (`LogLevel`, `log_level_to_int`), and the vendoring runtime: `vendor_path`, `vendor_rebuild`, `vendor_rebuild_ui` | mostly stdlib |
| `word_array/` | `field_text`: `decode_word_array`, `format_word_array`, the codec for the word-array field that `japanese_note_ai_ops` writes and `copy_anywhere` reads | stdlib |
| `jp_text_processing/` | submodule: furigana/kana highlighting, word highlighting via MeCab, `kana_conv` | its own repo |
| `testing/`, `test/`, `test_anki/` | the test harness and the shared packages' own tests; never declared by an addon, never shipped | dev only |

Who uses what is in each addon's `build.json` and summarised in the root `AGENTS.md` table.

## Deciding

Promote when **all** of these hold:

1. A second addon needs it now, or you are about to paste it into a second addon.
2. It can be written without knowing which addon is calling: no `addonFromModule(__name__)`,
   no `getConfig`, no addon-specific note type or field names. If it needs such a thing, it
   takes it as an argument.
3. It fits an existing package, or there is a coherent new package for it. `utils/` is not a
   dumping ground; a collection helper goes in `anki/`, a widget in `ui/`.

Leave it in the addon when it has one caller, when it encodes that addon's behaviour, or
when the two "copies" have the same shape but genuinely different lifecycles. In the last
case, extract only the part that is actually identical.

Anything that is about Japanese text rather than about Anki belongs in the
`jp_text_processing` repo, under that repo's rules, not in an `anki_shared` package.

## Procedure

1. Move the code to `anki_shared/<pkg>/<module>.py` with `git mv` where a whole file moves,
   so history follows. One widget or one cohesive function group per file.
2. Remove what made it addon-specific; pass those things in.
3. Replace **every** copy in every addon with `from .shared.<pkg>.<module> import ...`. Grep
   the sibling addons for the old function name and for near-identical bodies under
   different names.
4. Add `<pkg>` to `shared` in each affected `build.json`. A **new** package also needs
   `python build.py link` before Anki can import it; tell the user it is needed on each of
   their devices.
5. If the new shared module imports another shared package, either guard the import
   (`try/except ImportError`, degrade gracefully) or make sure every declaring addon also
   declares that sibling. See [architecture.md](architecture.md) for why `check` will not
   tell you.
6. Move or write its tests under `anki_shared/test/` (no Anki needed) or
   `anki_shared/test_anki/` (running Anki). They import it as `anki_shared.<pkg>.<module>`.
7. Run `python build.py check`, `python -m pytest -q` (all suites) and `python -m mypy .`.
8. One commit: the move plus all call sites. Say in the body which addons changed.

Changing the behaviour of something already shared follows steps 6 to 8, after reading every
call site. Addons without tests (`custom_schedule_helper`, the four small ones) will not
tell you that you broke them.

## Known duplication, not yet cleaned up

Recorded so that the next agent touching one of these recognises it, not as a work order.
Do not fold one of these into an unrelated change; do raise it, or do it as its own commit
when your task is already in that code. Delete an entry when it is resolved.

| what | where | note |
| --- | --- | --- |
| Config boilerplate: `tag = mw.addonManager.addonFromModule(__name__)`, `load_config`, `save_config`, `class Config` | `copy_anywhere/configuration.py`, `custom_schedule_helper/configuration.py`, `related_card_disperse/configuration.py`; variant in `addon_config_sync/utils.py`; `japanese_note_ai_ops` calls `getConfig(__name__)` inline in about 40 places | The identical part is a few lines and depends on the caller's `__name__`; a shared base must take the addon tag as an argument. The `Config` classes themselves differ on purpose. |
| Per-operation log files | `copy_anywhere/logging_setup.py`, `japanese_note_ai_ops/call_logging.py` | Same `ADDON_MODULE` / `addon_logger()` prelude and the same idea, different handler lifecycles (reference-counted vs closed when idle). The largest candidate; extract the common core, not a merged superset. |
| `_filter_revlogs`, `_last_review_date` | `related_card_disperse/logic.py` vs `anki_shared/scheduling/due_dates.py` | The local copies take pre-fetched revlogs from a stats cache. The shared functions could accept revlogs as an optional argument. |
| `kana_conv.py` | `japanese_note_ai_ops/kana_conv.py` vs `anki_shared/jp_text_processing/mecab_controller/kana_conv.py` | The submodule's copy is a superset and the research scripts already use it; the addon runtime still uses the local one. |
| `build_action`, `add_action_to_gear`, `add_separator_to_gear` | `custom_schedule_helper/__init__.py`; verbatim and unused in `copy_anywhere/hooks/browser_hooks.py`; a typed `build_action` in `addon_config_sync/__init__.py` | Delete the dead copy before sharing anything. |
| Deleted-card filter after sync | `related_card_disperse/sync_hook.py` `_existing_card_ids` | Revlog rows outlive their cards. Generic; a candidate for `anki/sync_hook_base.py`. |
| Background-run wrapper: `run_in_background` + progress + cancel + `mw.reset()` | three times in `related_card_disperse` (`logic.py` twice, `bury.py`), same shape in `custom_schedule_helper` | |
| Progress updater with title and `want_cancel()` loop | `ProgressUpdater` in `copy_anywhere/logic/copy_fields.py`, `AsyncTaskProgressUpdater` in `japanese_note_ai_ops/async_api_ops/base_ops.py` | |
| Reading card custom data with `json.loads(card.custom_data)` | `copy_anywhere/logic/copy_fields.py`, `custom_schedule_helper/schedule/reschedule.py` | `interpolate_fields.get_card_custom_data_prop` exists, but it lives in `interpolate`; a reader next to `write_custom_data` in `anki/` would be the natural home. |
| The quoted-list format `A", "B` and its `.strip('""').split('", "')` parsing | about 15 sites in `copy_anywhere`; `split_quoted_names` / `join_quoted_names` in `related_card_disperse/core.py` | The format is what `ui/multi_combo_box.py` emits, so its parser arguably belongs beside it. A bare split of `""` yields `[""]`, which has caused bugs. |
| Deferred tooltip after sync (`single_shot(100, tooltip...)`) | `copy_anywhere/hooks/sync_hook.py`, `related_card_disperse/sync_hook.py` | Small. |
| Stub-package import trick for running code without the addon's `__init__.py` | root `conftest.py`, `japanese_note_ai_ops/test/addon_modules.py`, `japanese_note_ai_ops/word_array/research/_bootstrap.py` | Acknowledged in `_bootstrap`'s docstring. Three runtimes with different constraints; not obviously mergeable. |

Generic helpers with a single owner today, which should move when a second addon wants
them rather than being rewritten: in `copy_anywhere/utils/`, `merge_cards`,
`move_card_to_deck` (filtered-deck aware), `duplicate_note`, `write_to_media_folder`,
`file_exists_in_media_folder`; in `addon_config_sync/utils.py`, `show_non_blocking_info` and
`standard_icon`; in `japanese_note_ai_ops`, `html_stripping.py` and
`utils.print_error_traceback`.
