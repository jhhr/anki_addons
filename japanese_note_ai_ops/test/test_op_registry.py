"""The op registry, the `chain` the entry functions pass on, and the menu built from both.

The browser menu and the multi-op dialog both start ops through `op_registry.OPS`, so a start
that calls the wrong entry function, drops the variant or loses the chain would misfire in
both. A chain waits for `on_done`, so an entry function that gives up before its run starts
must still call it - these check the ones that can. And the menu, which used to be written out
by hand in `__init__.py`, must keep its labels, order and separator exactly: the literal lists
below are the menu as it was before the registry.
"""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from addon_modules import load_ops_module

chain_types = load_ops_module("chain_types")
op_registry = load_ops_module("op_registry", subdir="")
ai_helper_menu = load_ops_module("ai_helper_menu", subdir="")
generator_resources = load_ops_module("generator_resources", subdir="")
match_flags = load_ops_module("match_flags", subdir="word_array")

# The ops D7 of the spec lists, in the menu's order
OP_LABELS = [
    "Clean dictionary meaning",
    "Translate sentence",
    "Generate kanji story",
    "Kanjify sentence",
    "Extract words",
    "Extract words + Judge matchability",
    "Regenerate words over the current array",
    "Find proper nouns in word arrays",
    "Judge words matchability",
    "Re-judge matched words",
    "Re-judge matched/judged words",
    "Match extracted words to notes",
    "Rematch all single word to notes",
    "Rematch processed single words to notes",
    "Match remaining unprocessed single words to notes",
    "Generate all meanings for selected notes",
    "Merge existing meanings for selected notes",
    "Run all ops for new notes",
    "Find missing matched note ids for selected notes",
    "Tag notes matched status",
    "Deduplicate existing meaning notes",
]

SEPARATOR = "---"

# The whole "AI helper" submenu, separators included
MENU_LABELS = ["Run several ops...", SEPARATOR] + OP_LABELS[:18] + [SEPARATOR] + [
    "Find missing matched note ids for selected notes",
    "Tag notes matched status",
    "Build name lexicon from selected notes",
    "Deduplicate existing meaning notes",
    "Export kanjify test data",
]

# key -> (the entry function its start calls, the variant keyword arguments it binds)
STARTS = {
    "clean_meaning": ("clean_selected_notes", {}),
    "translate_sentence": ("translate_selected_notes", {}),
    "kanji_story": ("make_stories_for_selected_notes", {}),
    "kanjify_sentence": ("kanjify_selected_notes", {}),
    "extract_words": ("extract_words_from_selected_notes", {}),
    "extract_words_and_judge": ("extract_words_and_judge_from_selected_notes", {}),
    "regenerate_words": ("regenerate_words_from_selected_notes", {}),
    "find_proper_nouns": ("find_proper_nouns_from_selected_notes", {}),
    "judge_words": ("word_matching_judge_from_selected_notes", {"states": match_flags.JUDGE_NEW}),
    "rejudge_matched_words": (
        "word_matching_judge_from_selected_notes",
        {"states": match_flags.REJUDGE_MATCHED},
    ),
    "rejudge_all_words": (
        "word_matching_judge_from_selected_notes",
        {"states": match_flags.REJUDGE_ALL},
    ),
    "match_words": ("match_words_to_notes_from_selected", {}),
    "rematch_single_word": (
        "match_single_word_to_notes_from_selected",
        {"reprocess_words": "both"},
    ),
    "rematch_processed_single_word": (
        "match_single_word_to_notes_from_selected",
        {"reprocess_words": "only_processed"},
    ),
    "match_remaining_single_word": (
        "match_single_word_to_notes_from_selected",
        {"reprocess_words": "only_unprocessed"},
    ),
    "make_all_meanings": ("make_meanings_selected_notes", {}),
    "merge_meanings": ("merge_meanings_selected_notes", {}),
    "new_note_all_ops": ("new_note_all_ops_selected_notes", {}),
    "find_missing_matched_note_ids": ("find_missing_matched_note_ids_selected_notes", {}),
    "tag_notes_matched_status": ("tag_notes_matched_status_from_selected", {}),
    "deduplicate_existing_meaning_notes": (
        "deduplicate_existing_meaning_notes_selected_notes",
        {},
    ),
}

# The entry functions by the module that defines them, and whether they wrap their run in
# with_generator_resources
ENTRY_MODULES = {
    ("clean_meaning", "async_api_ops"): (["clean_selected_notes"], False),
    ("translate_field", "async_api_ops"): (["translate_selected_notes"], False),
    ("make_kanji_story", "async_api_ops"): (["make_stories_for_selected_notes"], False),
    ("kanjify_sentence", "async_api_ops"): (["kanjify_selected_notes"], False),
    ("extract_words", "async_api_ops"): (
        [
            "extract_words_from_selected_notes",
            "extract_words_and_judge_from_selected_notes",
            "regenerate_words_from_selected_notes",
        ],
        True,
    ),
    ("find_proper_nouns", "async_api_ops"): (["find_proper_nouns_from_selected_notes"], True),
    ("word_matching_judge", "async_api_ops"): (["word_matching_judge_from_selected_notes"], False),
    ("match_words_to_notes", "async_api_ops"): (
        ["match_words_to_notes_from_selected", "match_single_word_to_notes_from_selected"],
        False,
    ),
    ("make_all_meanings", "async_api_ops"): (
        ["make_meanings_selected_notes", "merge_meanings_selected_notes"],
        False,
    ),
    ("new_note_all_ops", "async_api_ops"): (["new_note_all_ops_selected_notes"], True),
    ("find_missing_matched_note_ids", "sync_local_ops"): (
        ["find_missing_matched_note_ids_selected_notes"],
        False,
    ),
    ("tag_notes_matched_status", "sync_local_ops"): (
        ["tag_notes_matched_status_from_selected"],
        False,
    ),
    ("deduplicate_existing_meaning_notes", "sync_local_ops"): (
        ["deduplicate_existing_meaning_notes_selected_notes"],
        False,
    ),
}

NIDS = [11, 12, 13]
PARENT = object()


def patch_entry_functions(module, names):
    patchers = {name: mock.patch.object(module, name) for name in names}
    mocks = {name: p.start() for name, p in patchers.items()}
    return mocks, lambda: [p.stop() for p in patchers.values()]


def recording_chain() -> tuple:
    outcomes: list = []
    return chain_types.ChainStep("Step 1/1", outcomes.append), outcomes


class RegistryTests(unittest.TestCase):
    def test_keys_are_unique_and_snake_case(self):
        keys = [spec.key for spec in op_registry.OPS]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertRegex(key, r"^[a-z][a-z0-9_]*$")
        self.assertEqual(set(op_registry.OP_BY_KEY), set(keys))
        for spec in op_registry.OPS:
            self.assertIs(op_registry.OP_BY_KEY[spec.key], spec)

    def test_labels_are_the_menu_entries_in_menu_order(self):
        self.assertEqual([spec.label for spec in op_registry.OPS], OP_LABELS)

    def test_groups_split_where_the_menu_separator_was(self):
        groups = [spec.group for spec in op_registry.OPS]
        self.assertEqual(groups, ["async"] * 18 + ["sync"] * 3)

    def test_needs_generator_exactly_for_the_ops_that_run_it(self):
        self.assertEqual(
            {spec.key for spec in op_registry.OPS if spec.needs_generator},
            {
                "extract_words",
                "extract_words_and_judge",
                "regenerate_words",
                "find_proper_nouns",
                "new_note_all_ops",
            },
        )

    def test_every_start_forwards_nids_parent_chain_and_its_variant(self):
        self.assertEqual(set(STARTS), set(op_registry.OP_BY_KEY))
        names = {name for name, _ in STARTS.values()}
        mocks, stop = patch_entry_functions(op_registry, names)
        self.addCleanup(stop)
        for key, (name, variant) in STARTS.items():
            with self.subTest(op=key):
                for m in mocks.values():
                    m.reset_mock()
                chain, _ = recording_chain()
                op_registry.OP_BY_KEY[key].start(NIDS, PARENT, chain)
                mocks[name].assert_called_once_with(NIDS, parent=PARENT, chain=chain, **variant)
                others = [n for n, m in mocks.items() if n != name and m.called]
                self.assertEqual(others, [])


class EntryFunctionChainTests(unittest.TestCase):
    def test_every_entry_function_passes_its_chain_to_selected_notes_op(self):
        def run_at_once(parent, then, chain=None):
            then()

        for (module_name, subdir), (functions, wrapped) in ENTRY_MODULES.items():
            module = load_ops_module(module_name, subdir=subdir)
            for function in functions:
                for chain in (None, recording_chain()[0]):
                    with self.subTest(function=function, chain=chain), contextlib.ExitStack() as s:
                        run = s.enter_context(mock.patch.object(module, "selected_notes_op"))
                        # The single-word match reads the config before it starts its run
                        mw = s.enter_context(mock.patch.object(module, "mw"))
                        mw.addonManager.getConfig.return_value = {"a": "config"}
                        if wrapped:
                            resources = s.enter_context(
                                mock.patch.object(
                                    module, "with_generator_resources", side_effect=run_at_once
                                )
                            )
                        getattr(module, function)(NIDS, PARENT, chain=chain)
                        run.assert_called_once()
                        self.assertIs(run.call_args.kwargs["chain"], chain)
                        self.assertEqual(run.call_args.args[2], NIDS)
                        self.assertIs(run.call_args.args[3], PARENT)
                        if wrapped:
                            self.assertIs(resources.call_args.kwargs["chain"], chain)

    def test_single_word_match_without_config_fails_the_step(self):
        match = load_ops_module("match_words_to_notes")
        chain, outcomes = recording_chain()
        with mock.patch.object(match, "mw") as mw, mock.patch.object(
            match, "selected_notes_op"
        ) as run:
            mw.addonManager.getConfig.return_value = None
            result = match.match_single_word_to_notes_from_selected(
                NIDS, PARENT, reprocess_words="both", chain=chain
            )
        self.assertIsNone(result)
        run.assert_not_called()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, chain_types.STEP_FAILED)
        self.assertTrue(outcomes[0].stops_chain)
        self.assertIn("configuration", outcomes[0].error)

    def test_single_word_match_without_config_or_chain_just_returns(self):
        match = load_ops_module("match_words_to_notes")
        with mock.patch.object(match, "mw") as mw, mock.patch.object(
            match, "selected_notes_op"
        ) as run:
            mw.addonManager.getConfig.return_value = None
            self.assertIsNone(match.match_single_word_to_notes_from_selected(NIDS, PARENT))
        run.assert_not_called()


class GeneratorResourcesChainTests(unittest.TestCase):
    """with_generator_resources is where a generator op's run may never start."""

    def setUp(self):
        self.then = mock.Mock()
        patcher = mock.patch.multiple(
            generator_resources, showWarning=mock.DEFAULT, askUser=mock.DEFAULT
        )
        self.ui = patcher.start()
        self.addCleanup(patcher.stop)
        self.resources = mock.patch.object(generator_resources, "resources").start()
        self.addCleanup(mock.patch.stopall)

    def test_missing_sudachipy_fails_the_step(self):
        self.resources.has_sudachipy.return_value = False
        chain, outcomes = recording_chain()
        generator_resources.with_generator_resources(PARENT, self.then, chain=chain)
        self.then.assert_not_called()
        self.ui["showWarning"].assert_called_once()
        self.assertEqual([o.status for o in outcomes], [chain_types.STEP_FAILED])

    def test_declined_download_fails_the_step(self):
        self.resources.has_sudachipy.return_value = True
        self.resources.missing.return_value = [mock.Mock(size_mb=80)]
        self.ui["askUser"].return_value = False
        chain, outcomes = recording_chain()
        generator_resources.with_generator_resources(PARENT, self.then, chain=chain)
        self.then.assert_not_called()
        self.assertEqual([o.status for o in outcomes], [chain_types.STEP_FAILED])

    def test_present_resources_run_the_step_without_an_outcome(self):
        self.resources.has_sudachipy.return_value = True
        self.resources.missing.return_value = []
        chain, outcomes = recording_chain()
        generator_resources.with_generator_resources(PARENT, self.then, chain=chain)
        self.then.assert_called_once_with()
        self.assertEqual(outcomes, [])

    def test_a_failed_download_fails_the_step_only_in_a_chain(self):
        self.resources.has_sudachipy.return_value = True
        self.resources.missing.return_value = [mock.Mock(size_mb=80)]
        self.ui["askUser"].return_value = True
        for chain, outcomes in (recording_chain(), (None, None)):
            with self.subTest(chain=chain), mock.patch.object(
                generator_resources, "QueryOp"
            ) as query_op, mock.patch.object(generator_resources, "show_exception") as shown:
                op = query_op.return_value
                op.failure.return_value = op
                op.with_progress.return_value = op
                generator_resources.with_generator_resources(PARENT, self.then, chain=chain)
                op.run_in_background.assert_called_once()
                if chain is None:
                    # aqt's own error display, as before chains existed
                    op.failure.assert_not_called()
                    continue
                on_failure = op.failure.call_args.args[0]
                on_failure(OSError("no network"))
                shown.assert_called_once()
                self.then.assert_not_called()
                self.assertEqual([o.status for o in outcomes], [chain_types.STEP_FAILED])
                self.assertIn("no network", outcomes[0].error)

    def test_without_a_chain_a_skip_only_warns(self):
        self.resources.has_sudachipy.return_value = False
        generator_resources.with_generator_resources(PARENT, self.then)
        self.then.assert_not_called()
        self.ui["showWarning"].assert_called_once()


    def test_a_step_that_raises_starting_after_the_download_fails_the_step(self):
        self.resources.has_sudachipy.return_value = True
        self.resources.missing.return_value = [mock.Mock(size_mb=80)]
        self.ui["askUser"].return_value = True
        self.then.side_effect = RuntimeError("no config")
        chain, outcomes = recording_chain()
        with mock.patch.object(generator_resources, "QueryOp") as query_op, mock.patch.object(
            generator_resources, "show_exception"
        ) as shown:
            op = query_op.return_value
            op.failure.return_value = op
            op.with_progress.return_value = op
            generator_resources.with_generator_resources(PARENT, self.then, chain=chain)
            on_success = query_op.call_args.kwargs["success"]
            # Would otherwise reach Qt from the download's handler, the chain left waiting
            on_success(None)
        shown.assert_called_once()
        self.assertEqual([o.status for o in outcomes], [chain_types.STEP_FAILED])
        self.assertIn("no config", outcomes[0].error)

    def test_outside_a_chain_the_download_s_then_raises_as_before(self):
        self.resources.has_sudachipy.return_value = True
        self.resources.missing.return_value = [mock.Mock(size_mb=80)]
        self.ui["askUser"].return_value = True
        self.then.side_effect = RuntimeError("no config")
        with mock.patch.object(generator_resources, "QueryOp") as query_op:
            op = query_op.return_value
            op.with_progress.return_value = op
            generator_resources.with_generator_resources(PARENT, self.then)
            with self.assertRaises(RuntimeError):
                query_op.call_args.kwargs["success"](None)

class FakeSignal:
    def __init__(self):
        self.slot = None


class FakeAction:
    def __init__(self, label, parent):
        self.label = label
        self.parent = parent
        self.triggered = FakeSignal()


def fake_qconnect(signal, slot):
    signal.slot = slot


class FakeMenu:
    def __init__(self) -> None:
        self.items: list = []

    def addAction(self, action):
        self.items.append(action)

    def addSeparator(self):
        self.items.append(SEPARATOR)


class MenuTests(unittest.TestCase):
    def build_menu(self):
        menu = FakeMenu()
        with mock.patch.object(ai_helper_menu, "QAction", FakeAction), mock.patch.object(
            ai_helper_menu, "qconnect", fake_qconnect
        ):
            ai_helper_menu.add_ai_helper_actions(menu, NIDS, parent=PARENT)
        return menu

    def test_menu_keeps_its_labels_order_and_separator(self):
        menu = self.build_menu()
        labels = [item if item == SEPARATOR else item.label for item in menu.items]
        self.assertEqual(labels, MENU_LABELS)

    def test_actions_are_parented_to_the_submenu_so_they_go_with_it(self):
        # Parented to mw, every right-click kept its actions and their selection for good
        menu = self.build_menu()
        for item in menu.items:
            if item != SEPARATOR:
                self.assertIs(item.parent, menu)

    def test_each_action_runs_its_own_op_on_the_captured_selection(self):
        menu = self.build_menu()
        names = {name for name, _ in STARTS.values()}
        mocks, stop = patch_entry_functions(op_registry, names)
        self.addCleanup(stop)
        menu_only, stop_menu_only = patch_entry_functions(
            ai_helper_menu, ["build_name_lexicon_from_selected", "make_kanjify_sentence_data"]
        )
        self.addCleanup(stop_menu_only)
        every = {**mocks, **menu_only}
        actions = {item.label: item for item in menu.items if item != SEPARATOR}
        for spec in op_registry.OPS:
            name, variant = STARTS[spec.key]
            with self.subTest(op=spec.key):
                for m in every.values():
                    m.reset_mock()
                # Triggered with no arguments, as PyQt calls a slot that takes none
                actions[spec.label].triggered.slot()
                every[name].assert_called_once_with(NIDS, parent=PARENT, chain=None, **variant)
                self.assertEqual([n for n, m in every.items() if m.called], [name])
        for label, name in (
            ("Build name lexicon from selected notes", "build_name_lexicon_from_selected"),
            ("Export kanjify test data", "make_kanjify_sentence_data"),
        ):
            with self.subTest(action=label):
                for m in every.values():
                    m.reset_mock()
                actions[label].triggered.slot()
                every[name].assert_called_once_with(NIDS, parent=PARENT)
                self.assertEqual([n for n, m in every.items() if m.called], [name])

    def test_first_action_opens_the_dialog_over_the_browser(self):
        menu = self.build_menu()
        self.assertEqual(menu.items[0].label, ai_helper_menu.MULTI_OP_LABEL)
        names = {name for name, _ in STARTS.values()}
        mocks, stop = patch_entry_functions(op_registry, names)
        self.addCleanup(stop)
        with mock.patch.object(ai_helper_menu, "show_multi_op_dialog") as show:
            menu.items[0].triggered.slot()
        # The selection the menu already read, so the dialog does not read it again
        show.assert_called_once_with(PARENT, NIDS)
        self.assertEqual([n for n, m in mocks.items() if m.called], [])

    def test_menu_only_actions_are_no_ops_of_the_registry(self):
        registry_labels = {spec.label for spec in op_registry.OPS}
        for action in ai_helper_menu.MENU_ONLY_ACTIONS:
            self.assertNotIn(action.label, registry_labels)
            self.assertIn(action.after_key, op_registry.OP_BY_KEY)


if __name__ == "__main__":
    unittest.main()
