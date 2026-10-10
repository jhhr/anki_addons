"""Every op of one role passes its notes through the role gate (async_api_ops/role_gate.py)
before its driver gets them: the sentence ops (extract, regenerate, judge, find proper nouns,
kanjify, translate, match, find missing ids) only sentence notes, the vocab ops (clean meaning,
make and merge meanings, tag matched status, deduplicate meaning notes) only vocab notes. A note
of the other role used to fail per note, or fail the match run as it planned; now the run goes
on without it and says so once. In the one-type layout every note goes through, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import unittest
from unittest import mock

from addon_modules import load_ops_module

gate = load_ops_module("role_gate")
match_flags = load_ops_module("match_flags", subdir="word_array")

VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"

TWO_TYPE = {
    SENTENCE: {"vocab_note_type": VOCAB, "word_list_field": "Words"},
    VOCAB: {"sentence_note_type": SENTENCE, "word_sort_field": "Sort"},
    "match_words_model": "model",
    "word_matching_judge_model": "model",
}
ONE_TYPE = {
    VOCAB: {"word_list_field": "Words", "word_sort_field": "Sort"},
    "match_words_model": "model",
    "word_matching_judge_model": "model",
}


class Note:
    def __init__(self, note_id: int, type_name: str) -> None:
        self.id = note_id
        self.type_name = type_name

    def note_type(self):
        return {"name": self.type_name}


# (module, subdir, the bulk op, the driver it hands its notes to)
SENTENCE_OPS = [
    ("extract_words", "async_api_ops", "bulk_extract_from_notes_op", "bulk_notes_op"),
    ("extract_words", "async_api_ops", "bulk_regenerate_from_notes_op", "bulk_notes_op"),
    ("find_proper_nouns", "async_api_ops", "bulk_find_proper_nouns_op", "bulk_notes_op"),
    ("kanjify_sentence", "async_api_ops", "bulk_kanjify_notes_op", "bulk_notes_op"),
    ("translate_field", "async_api_ops", "bulk_translate_notes_op", "bulk_notes_op"),
    (
        "match_words_to_notes",
        "async_api_ops",
        "bulk_match_words_to_notes",
        "bulk_nested_notes_op",
    ),
    (
        "find_missing_matched_note_ids",
        "sync_local_ops",
        "bulk_find_missing_matched_note_ids_op",
        "bulk_notes_op",
    ),
]
VOCAB_OPS = [
    ("clean_meaning", "async_api_ops", "bulk_clean_notes_op", "bulk_notes_op"),
    ("make_all_meanings", "async_api_ops", "bulk_make_meanings_op", "bulk_notes_op"),
    ("make_all_meanings", "async_api_ops", "bulk_merge_meanings_op", "bulk_notes_op"),
    (
        "tag_notes_matched_status",
        "sync_local_ops",
        "bulk_tag_notes_matched_status_op",
        "bulk_notes_op",
    ),
    (
        "deduplicate_existing_meaning_notes",
        "sync_local_ops",
        "bulk_deduplicate_existing_meaning_notes_op",
        "bulk_notes_op",
    ),
]


def mixed_notes() -> list:
    return [Note(1, SENTENCE), Note(2, VOCAB), Note(3, SENTENCE), Note(4, VOCAB)]


class OpsPassTheirRoleOnlyTests(unittest.TestCase):
    def handed(self, module_name, subdir, bulk_name, driver_name, config, notes):
        """The notes the bulk op hands its driver, and the run errors it reports."""
        module = load_ops_module(module_name, subdir=subdir)
        handed: list = []

        async def driver(*args, **kwargs):
            handed.extend(kwargs["notes"] if "notes" in kwargs else args[4])

        patches = [
            mock.patch.object(module, driver_name, driver),
            mock.patch.object(module, "mw"),
            mock.patch.object(gate, "report_error"),
        ]
        # What the ops read before they start their driver: files and the name lexicon
        for name in ("load_meanings_dict_from_file", "extract_words_op"):
            if hasattr(module, name):
                patches.append(mock.patch.object(module, name))
        with contextlib.ExitStack() as stack:
            entered = [stack.enter_context(patch) for patch in patches]
            entered[1].addonManager.getConfig.return_value = config
            if bulk_name == "make_bulk_op":
                bulk_op = module.make_bulk_op(match_flags.JUDGE_NEW)
            else:
                bulk_op = getattr(module, bulk_name)
            result = bulk_op(None, notes, [], mock.Mock(), {}, {})
            if inspect.iscoroutine(result):
                asyncio.run(result)
            reports = [call.args for call in entered[2].call_args_list]
        return [note.id for note in handed], reports

    def check(self, ops, kept_ids, refused_type):
        for module_name, subdir, bulk_name, driver_name in ops:
            with self.subTest(op=bulk_name):
                handed, reports = self.handed(
                    module_name, subdir, bulk_name, driver_name, TWO_TYPE, mixed_notes()
                )
                self.assertEqual(handed, kept_ids)
                [(text, where)] = reports
                self.assertEqual(where, f'Notes of "{refused_type}"')
                self.assertTrue(text.endswith("Left out of this run: 2 notes of it."), text)
                handed, reports = self.handed(
                    module_name, subdir, bulk_name, driver_name, ONE_TYPE, [Note(5, VOCAB)]
                )
                self.assertEqual((handed, reports), ([5], []))

    def test_the_sentence_ops_pass_sentence_notes_only(self):
        self.check(SENTENCE_OPS, [1, 3], VOCAB)

    def test_the_judge_passes_sentence_notes_only(self):
        self.check(
            [("word_matching_judge", "async_api_ops", "make_bulk_op", "bulk_nested_notes_op")],
            [1, 3],
            VOCAB,
        )

    def test_the_vocab_ops_pass_vocab_notes_only(self):
        self.check(VOCAB_OPS, [2, 4], SENTENCE)


if __name__ == "__main__":
    unittest.main()
