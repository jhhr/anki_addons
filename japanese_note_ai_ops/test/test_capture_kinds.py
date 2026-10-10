"""What the match op, the word matching judge, the proper noun, translate, kanjify and kanji
story ops record of their AI calls: each call's `kind`, and as `inputs` the values its prompt is
built from, so that the prompt's builder given the recorded inputs gives back the prompt that
was sent. The prompts themselves are pinned byte for byte (written before their builders were
taken out of the ops). The match op's two calls also record a `context`, the word and notes their
answers are read against, which must name what the op then does with the answer; and which of a
word's notes the op sends to clean_meaning before it matches the word. The meaning ops' calls are
test_capture_meaning_kinds'. Last, a scan of async_api_ops that every call site names a kind and
inputs.

`get_response` is the test's in most tests, as in test_word_array_match_targets and test_judge.
One runs a note's word array match through the real `get_response` with a store installed in a
temporary directory (test_capture_runs) and its provider faked, for the word targets' tasks.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import types
import unittest
from contextlib import closing
from typing import Any, Optional
from unittest import mock

from addon_modules import OPS_DIR, load_ops_module

configuration = load_ops_module("configuration", "")
match_targets = load_ops_module("match_targets", subdir="word_array")
judge = load_ops_module("judge", subdir="word_array")
proper_noun_llm = load_ops_module("proper_noun_llm", subdir="word_array")
capture = load_ops_module("capture")
capture_store = load_ops_module("capture_store")
base_ops = load_ops_module("base_ops")

MODEL = "model"
NOTE_ID = 1

# One word with two notes and a generated meaning neither note has: the prompt lists the notes'
# meanings by their (mN), then the generated one, which has no example sentence
MEANINGS_PROMPT = (
    "MEANINGS AND EXAMPLE SENTENCES\n"
    "Meaning number 1:\n"
    "- *match_word*: 本\n"
    "- *jp_meaning*: 書物。文字や絵を印刷して綴じたもの。\n"
    "- *en_meaning*: book\n"
    "- *example_sentence*: <b>本</b>を読むのが好きだ。\n"
    "Meaning number 2:\n"
    "- *match_word*: 本\n"
    "- *jp_meaning*: 物事のもと。根本。\n"
    "- *en_meaning*: origin; basis\n"
    "- *example_sentence*: 農は国の<b>本</b>である。\n"
    "Meaning number 3:\n"
    "- *match_word*: 本\n"
    "- *jp_meaning*: 細長い物を数える語。\n"
    "- *en_meaning*: counter for long, cylindrical things\n"
    "- *example_sentence*: (no example sentence)\n"
    "\n"
    "\n"
    "_Targeted word_: 本\n"
    "_Current sentence_: 図書館で<b>本</b>を借りた。"
)
PROMPT_SENTENCE = "図書館で<b>本</b>を借りた。"
MEANINGS_INPUTS = {
    "word": "本",
    "reading": "ほん",
    "sentence": PROMPT_SENTENCE,
    "meanings": [
        {
            "match_word": "本",
            "jp_meaning": "書物。文字や絵を印刷して綴じたもの。",
            "en_meaning": "book",
            "example_sentence": "<b>本</b>を読むのが好きだ。",
        },
        {
            "match_word": "本",
            "jp_meaning": "物事のもと。根本。",
            "en_meaning": "origin; basis",
            "example_sentence": "農は国の<b>本</b>である。",
        },
        {
            "match_word": "本",
            "jp_meaning": "細長い物を数える語。",
            "en_meaning": "counter for long, cylindrical things",
            "example_sentence": "",
        },
    ],
}
RATING_INPUTS = {
    "word": "借りる",
    "reading": "かりる",
    "jp_meaning": "人の物を一時使わせてもらう。",
    "en_meaning": "to borrow",
    "sentence": "図書館で本を<b>借りた</b>。",
}
# What the meanings call's context says each listed meaning is, in the prompt's order: the notes
# by their (mN) - found m2 first, so the sort moves them - then the generated meaning neither
# note has, the second of the word's generated meanings, which sorts with the largest (mN)
MEANINGS_CONTEXT = {
    "word_path": [2],
    "meanings": [
        {"note_id": 101, "m_number": 1, "gen_index": None},
        {"note_id": 102, "m_number": 2, "gen_index": None},
        {"note_id": None, "m_number": 2, "gen_index": 1},
    ],
    # The note with the largest (mN)
    "copy_note_id": 102,
}
RATING_CONTEXT = {"word_path": [4], "note_id": 222}

PROPER_NOUNS_PROMPT = (
    "Does the Japanese sentence below contain any proper nouns? Respond with a JSON object"
    ' whose "proper_nouns" is the list of the proper nouns, each written as it is in the'
    " sentence without its furigana, or an empty list if there are none.\n\n"
    " 山田[やまだ]さんが 来[き]た。"
)


def meanings_prompt_args(inputs: dict) -> dict:
    """meanings_prompt's arguments of a meanings call's inputs: all but the reading, which the
    prompt does not show."""
    return {key: value for key, value in inputs.items() if key != "reading"}


class FakeNote:
    def __init__(self, note_id: int, fields: dict[str, str], tags: tuple[str, ...] = ()):
        self.id = note_id
        self.fields = fields
        self.tags = list(tags)

    def note_type(self) -> dict:
        return {"name": "Word"}

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]

    def __setitem__(self, field: str, value: str) -> None:
        self.fields[field] = value


def word_note(
    note_id: int,
    sort: str,
    meaning: str,
    en_meaning: str,
    sentence: str,
    word: str = "本",
    reading: str = "ほん",
) -> FakeNote:
    """A word note as the match op reads it, mapped to its generated meanings already, so
    matching makes no clean_meaning call."""
    fields = {
        "word_sort_field": sort,
        "meaning_field": meaning,
        "english_meaning_field": en_meaning,
        "word_kanjified_field": word,
        "word_reading_field": reading,
        "furigana_sentence_field": sentence,
        "word_list_field": "",
    }
    return FakeNote(note_id, fields, (configuration.MEANING_MAPPED_TAG,))


def book_notes() -> list[FakeNote]:
    return [
        word_note(102, "本(m2)", "物事のもと。根本。", "origin; basis", "農は国の<b>本</b>である。"),
        word_note(
            101,
            "本(m1)",
            "書物。文字や絵を印刷して綴じたもの。",
            "book",
            "<b>本</b>を読むのが好きだ。",
        ),
    ]


def generated_meanings() -> dict:
    return {
        "本_ほん": [
            # The first note's meaning, so not listed again
            {"jp_meaning": "書物。文字や絵を印刷して綴じたもの。", "en_meaning": "book"},
            {
                "jp_meaning": "細長い物を数える語。",
                "en_meaning": "counter for long, cylindrical things",
            },
        ]
    }


# 図書館で本を借りた。: 本 to match, 借りた linked to note 222 without a quality, so rated
def library_array() -> list:
    return [
        ["図書館", "noun", "図書館", "としょかん", ["dontmatch"], []],
        ["で", "particle", "で", "で", ["dontmatch"], []],
        ["本", "noun", "本", "ほん", ["match"], []],
        ["を", "particle", "を", "を", ["dontmatch"], []],
        ["借りた", "verb", "借りる", "かりる", [222], []],
        ["。"],
    ]


def answer(model: str, prompt: str, **kwargs) -> dict:
    """The rating prompt rates 4; the meanings prompt picks meaning 1 and rates it 5."""
    if kwargs.get("instructions") == match_targets.RATING_INSTRUCTIONS:
        return {"match_quality": 4}
    return {"is_matched_meaning": True, "meaning_number": 1, "match_quality": 5}


class Progress:
    def increment_counts(self, notes_done: int = 0) -> None:
        pass


class WordIndexCache:
    async def get(self, _):
        return None


def passthrough_inner_bulk_op(config, op, **_):
    """make_inner_bulk_op without its gate and progress: the op in the note's own task."""

    async def process(**op_args):
        called = op(config, **op_args)
        return await called if asyncio.iscoroutine(called) else called

    return process


class MatchHarness(unittest.TestCase):
    """The real match op over word notes a stand-in lookup finds (`book_notes` for 本), with
    the test's `get_response`."""

    def setUp(self) -> None:
        self.mwtn = load_ops_module("match_words_to_notes")
        self.config = {
            "Word": {key: key for key in self.mwtn.MATCH_FIELD_KEYS},
            "match_words_model": MODEL,
        }

    async def find_notes(self, word: str, **_) -> list:
        return book_notes() if word == "本" else []

    def match_book(
        self, get_response, notes_to_add: Optional[dict] = None
    ) -> tuple[bool, dict]:
        """match_single_word_in_word_tuple for 本 in PROMPT_SENTENCE. `notes_to_add`: the run's
        notes to add so far, by word."""
        results: dict = {}

        async def run() -> bool:
            return await self.mwtn.match_single_word_in_word_tuple(
                config=self.config,
                word_lock=asyncio.Lock(),
                word_locks_dict={},
                log_prefix="",
                match_op_args=self.mwtn.MatchOpArgs(
                    **{key: key for key in self.mwtn.MATCH_FIELD_KEYS},
                    current_note=None,
                    note_type={"name": "Word"},
                    word_index=0,
                    part_of_speech="Noun",
                    multi_meaning_index=None,
                    word="本",
                    reading="ほん",
                    sentence="図書館で本を借りた。",
                    prompt_sentence=PROMPT_SENTENCE,
                    match_qualities={},
                    processed_word_tuples=results,
                    all_generated_meanings_dict=generated_meanings(),
                    notes_to_add_dict=notes_to_add if notes_to_add is not None else {},
                    notes_to_update_dict={},
                    word_note_index=None,
                    note_cache=None,
                    sentence_cache=None,
                    cancel_state=None,
                ),
            )

        with (
            mock.patch.object(self.mwtn, "get_response", get_response),
            mock.patch.object(
                self.mwtn, "get_matching_notes_for_word_and_reading", self.find_notes
            ),
        ):
            return asyncio.run(run()), results

    def match_array(self, get_response, notes_to_add: Optional[dict] = None) -> list:
        """plan_word_array_matching over `library_array`, every task run; the array saved.
        `notes_to_add`: the run's notes to add so far, by word."""
        note = FakeNote(NOTE_ID, {"word_list_field": json.dumps(library_array())})
        linked = word_note(
            222, "借りる", RATING_INPUTS["jp_meaning"], "to borrow", "", "借りる", "かりる"
        )

        class NoteCache:
            async def get_notes(self, ids):
                return {i: linked for i in ids if i == linked.id}

        async def run() -> None:
            plan = self.mwtn.plan_word_array_matching(
                config=self.config,
                note=note,
                arr=library_array(),
                sentence="図書館で本を借りた。",
                edited_nids=[],
                notes_to_add_dict=notes_to_add if notes_to_add is not None else {},
                notes_to_update_dict={},
                progress_updater=Progress(),
                cancel_state=None,
                gate=None,
                all_generated_meanings_dict=generated_meanings(),
                word_locks_dict={},
                word_lock=asyncio.Lock(),
                word_note_index_cache=WordIndexCache(),
                note_cache=NoteCache(),
                sentence_cache=None,
                limit_words_and_readings=None,
                log_prefix="",
            )
            self.assertEqual(plan.task_count, 2)
            tasks: list[asyncio.Task] = []
            plan.spawn(tasks)
            await asyncio.gather(*tasks)

        with (
            mock.patch.object(self.mwtn, "get_response", get_response),
            mock.patch.object(
                self.mwtn, "get_matching_notes_for_word_and_reading", self.find_notes
            ),
            mock.patch.object(self.mwtn, "make_inner_bulk_op", passthrough_inner_bulk_op),
        ):
            asyncio.run(run())
        return json.loads(note["word_list_field"])


class MatchCallTests(MatchHarness):
    def test_the_meanings_prompt_is_unchanged(self):
        calls: list[tuple[str, dict[str, Any]]] = []

        def get_response(model, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return answer(model, prompt, **kwargs)

        matched, results = self.match_book(get_response)

        [(prompt, _)] = calls
        self.assertEqual(prompt, MEANINGS_PROMPT)
        self.assertTrue(matched)
        self.assertEqual(results, {0: ("本", "ほん", "本(m1)", 101)})

    def test_each_call_records_its_kind_and_what_its_prompt_is_built_from(self):
        calls: dict[Any, tuple[str, dict[str, Any]]] = {}

        def get_response(model, prompt, **kwargs):
            calls[kwargs.get("kind")] = (prompt, kwargs)
            return answer(model, prompt, **kwargs)

        saved = self.match_array(get_response)

        self.assertEqual(set(calls), {"match.meanings", "match.rating"})
        prompt, kwargs = calls["match.meanings"]
        self.assertEqual(kwargs["inputs"], MEANINGS_INPUTS)
        self.assert_plain_json(kwargs["inputs"])
        rebuilt = match_targets.meanings_prompt(**meanings_prompt_args(kwargs["inputs"]))
        self.assertEqual(rebuilt, prompt)
        self.assertEqual(prompt, MEANINGS_PROMPT)

        prompt, kwargs = calls["match.rating"]
        self.assertEqual(kwargs["inputs"], RATING_INPUTS)
        self.assert_plain_json(kwargs["inputs"])
        self.assertEqual(match_targets.rating_prompt(**kwargs["inputs"]), prompt)
        self.assertEqual(kwargs["instructions"], match_targets.RATING_INSTRUCTIONS)
        # The run itself as before: 本 matched to note 101 at 5, 借りた rated 4
        self.assertEqual(saved[2][4], [101, 5])
        self.assertEqual(saved[4][4], [222, 4])

    def assert_plain_json(self, value: Any) -> None:
        """Plain JSON comes back from a round trip unchanged: no tuple, no object."""
        self.assertEqual(json.loads(json.dumps(value)), value)


def matched(meaning_number: int) -> dict:
    """The meanings prompt's MATCH of a listed meaning, rated 5."""
    return {"is_matched_meaning": True, "meaning_number": meaning_number, "match_quality": 5}


def at_path(arr: list, path: list[int]) -> list:
    """The element a context's `word_path` names."""
    elem = arr[path[0]]
    for index in path[1:]:
        elem = elem[5][index]
    return elem


class MatchContextTests(MatchHarness):
    """The match calls' `context`, and that it names what the op then does with the answer:
    `context["meanings"][meaning_number - 1]` is the note the word gets linked to, or the
    generated meaning a new note is made from, as a copy of `copy_note_id`."""

    def run_match(
        self, meanings_answer: dict, notes_to_add: Optional[dict] = None
    ) -> tuple[list, dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """The array matched with `meanings_answer` as the meanings call's answer: the saved
        array, each call's arguments by kind, and each new note asked for."""
        calls: dict[str, dict[str, Any]] = {}
        created: list[dict[str, Any]] = []

        def get_response(model, prompt, **kwargs):
            calls[kwargs["kind"]] = kwargs
            if kwargs["kind"] == "match.rating":
                return {"match_quality": 4}
            return meanings_answer

        def create_new_note_from_matched_note(**kwargs) -> bool:
            created.append(kwargs)
            return True

        with mock.patch.object(
            self.mwtn, "create_new_note_from_matched_note", create_new_note_from_matched_note
        ):
            saved = self.match_array(get_response, notes_to_add)
        return saved, calls, created

    def test_the_contexts_name_the_word_and_each_listed_meaning_in_the_prompts_order(self):
        saved, calls, _ = self.run_match(matched(1))

        meanings, rating = calls["match.meanings"], calls["match.rating"]
        self.assertEqual(meanings["context"], MEANINGS_CONTEXT)
        self.assertEqual(rating["context"], RATING_CONTEXT)
        for context in (meanings["context"], rating["context"]):
            self.assertEqual(json.loads(json.dumps(context)), context)
        # Each word's path names the element its answer was saved on
        self.assertEqual(at_path(saved, MEANINGS_CONTEXT["word_path"])[4], [101, 5])
        self.assertEqual(at_path(saved, RATING_CONTEXT["word_path"])[4], [222, 4])

    def test_a_match_to_a_listed_note_links_the_word_to_the_note_the_context_names(self):
        for number in (1, 2):
            with self.subTest(meaning_number=number):
                saved, calls, created = self.run_match(matched(number))

                context = calls["match.meanings"]["context"]
                listed = context["meanings"][number - 1]
                self.assertIsNone(listed["gen_index"])
                self.assertEqual(at_path(saved, context["word_path"])[4], [listed["note_id"], 5])
                self.assertEqual(created, [])

    def test_a_match_to_the_generated_meaning_makes_the_note_the_context_names(self):
        saved, calls, created = self.run_match(matched(3))

        context = calls["match.meanings"]["context"]
        listed = context["meanings"][3 - 1]
        self.assertIsNone(listed["note_id"])
        [new_note] = created
        made_from = {key: new_note[key] for key in ("jp_meaning", "en_meaning")}
        self.assertEqual(made_from, generated_meanings()["本_ほん"][listed["gen_index"]])
        self.assertEqual(new_note["note_to_copy"].id, context["copy_note_id"])
        # Linked once the new note exists, which this fake does not make
        self.assertEqual(at_path(saved, context["word_path"])[4], ["match"])

    def test_a_new_meaning_copies_the_note_the_context_names(self):
        answer = {
            "is_matched_meaning": False,
            "meaning_number": None,
            "jp_meaning": "本当の。",
            "en_meaning": "real",
        }
        _, calls, created = self.run_match(answer)

        [new_note] = created
        self.assertEqual((new_note["jp_meaning"], new_note["en_meaning"]), ("本当の。", "real"))
        context = calls["match.meanings"]["context"]
        self.assertEqual(new_note["note_to_copy"].id, context["copy_note_id"])

    def test_a_note_not_added_yet_is_named_by_its_placeholder(self):
        # A note this run made for the word before, not added until cleanup: id 0
        pending = word_note(0, "本(m3)", "当の。この。", "this; the present", "<b>本</b>日")
        pending["new_note_id_field"] = "-1234567"

        saved, calls, _ = self.run_match(matched(3), {"本": [pending]})

        context = calls["match.meanings"]["context"]
        self.assertEqual(
            context["meanings"][2:],
            [
                {"note_id": -1234567, "m_number": 3, "gen_index": None},
                # Still last: generated meanings take the largest (mN), now the new note's
                {"note_id": None, "m_number": 3, "gen_index": 1},
            ],
        )
        self.assertEqual(context["copy_note_id"], -1234567)
        # The placeholder is what the word is linked to until cleanup adds the note
        self.assertEqual(at_path(saved, context["word_path"])[4], [-1234567, 5])


class MatchMappingTests(MatchHarness):
    """The notes the match op cleans before it matches a word: each one it found that is not
    mapped yet, to be mapped and nothing else, once; never a note this run made."""

    def setUp(self) -> None:
        super().setUp()
        self.found = book_notes()
        # 本(m2), not mapped yet
        self.found[0].tags = []

    async def find_notes(self, word: str, **_) -> list:
        return self.found if word == "本" else []

    def test_a_note_not_mapped_is_mapped_and_a_note_this_run_made_is_left_alone(self):
        # A new note made for the word earlier in the run was cleaned again at the word's next
        # target, and that cleaning rewrote the word's other notes with it. A note the loops
        # could not map was reworked instead, and again by the second loop
        cleaned: list[dict] = []

        def clean_meaning_in_note(**kwargs):
            cleaned.append(kwargs)
            return load_ops_module("clean_meaning").CleanResult(False)

        pending = word_note(0, "本(m3)", "当の。この。", "this; the present", "<b>本</b>日")
        pending.tags = []
        pending["new_note_id_field"] = "-1234567"
        with mock.patch.object(self.mwtn, "clean_meaning_in_note", clean_meaning_in_note):
            matched, _ = self.match_book(answer, {"本": [pending]})

        self.assertTrue(matched)
        [kwargs] = cleaned
        self.assertIs(kwargs["note"], self.found[0])
        self.assertTrue(kwargs["map_only"])
        # Its other notes are fetched as context; the loop gives none
        self.assertNotIn("other_meaning_notes", kwargs)


def two_type_config(mwtn: types.ModuleType) -> dict:
    """The two-type layout: the vocab notes hold their example sentence note's id, and their
    sentence and array from before the split under the sentence type's field names, never read
    now."""
    return {
        "Sentence": {
            "vocab_note_type": "Word",
            "word_list_field": "s_array",
            "sentence_field": "s_sentence",
            "sentence_audio_field": "s_audio",
            "furigana_sentence_field": "s_furigana",
            "kanjified_sentence_field": "s_kanjified",
        },
        "Word": {
            "sentence_note_type": "Sentence",
            "example_sentence_id_field": "v_example_id",
            **{
                key: key
                for key in mwtn.MATCH_FIELD_KEYS
                if key not in mwtn.SENTENCE_MATCH_FIELD_KEYS
            },
        },
        "match_words_model": MODEL,
    }


def sentence_array(*words: tuple[str, str, str, list]) -> str:
    """A sentence note's array of (raw text, dict form, reading, match data) words."""
    return json.dumps(
        [[raw, "noun", form, reading, data, []] for raw, form, reading, data in words],
        ensure_ascii=False,
    )


def sentence_note(note_id: int, array: str, furigana: str = "") -> FakeNote:
    return FakeNote(note_id, {"s_furigana": furigana, "s_array": array})


def reading_sentence(linked: int) -> FakeNote:
    """本を読むのが好きだ。, 本 linked to `linked`."""
    return sentence_note(
        201,
        sentence_array(
            ("本", "本", "ほん", [linked]),
            ("を", "を", "を", ["dontmatch"]),
            ("読むのが", "読む", "よむ", ["dontmatch"]),
            ("好きだ。", "好き", "すき", ["dontmatch"]),
        ),
        "本[ほん]を 読[よ]むのが 好[す]きだ。",
    )


def country_sentence(linked: int) -> FakeNote:
    """農は国の本である。, 本 linked to `linked`."""
    return sentence_note(
        202,
        sentence_array(
            ("農は国の", "農", "のう", ["dontmatch"]),
            ("本", "本", "ほん", [linked]),
            ("である。", "だ", "だ", ["dontmatch"]),
        ),
        "農[のう]は 国[くに]の 本[もと]である。",
    )


def two_type_book_notes(example_ids: tuple[str, str] = ("202", "201")) -> list[FakeNote]:
    """book_notes as vocab notes: each with its example sentence note's id, and an old sentence
    and array linking it in a sentence its example is not."""
    notes = book_notes()
    for note, example_id in zip(notes, example_ids):
        del note.fields["furigana_sentence_field"], note.fields["word_list_field"]
        note.fields.update(
            v_example_id=example_id,
            s_furigana="本[ほん] 棚[だな]",
            s_array=sentence_array(("本", "本", "ほん", [note.id]), ("棚", "棚", "たな", [])),
        )
    return notes


class SentenceNoteCache:
    """The run's note cache over the sentence notes: an id with no note is left out of what it
    returns, as NoteCache does (test_note_cache.py)."""

    def __init__(self, *notes: FakeNote) -> None:
        self.notes = {note.id: note for note in notes}
        self.fetches: list[list[int]] = []

    async def get_notes(self, ids) -> dict:
        ids = list(ids)
        self.fetches.append(ids)
        return {nid: self.notes[nid] for nid in ids if nid in self.notes}


class TwoTypeExampleSentenceTests(MatchHarness):
    """A word's notes' example sentences in the main prompt, in the two-type layout (SPEC 6
    P6): each from the note's example sentence note, made from that note's array with the
    note's own word in <b> as a one-type note's is made from its own array; "" without one."""

    def setUp(self) -> None:
        super().setUp()
        self.config = two_type_config(self.mwtn)
        self.found = two_type_book_notes()
        self.cache = SentenceNoteCache(reading_sentence(101), country_sentence(102))

    async def find_notes(self, word: str, **_) -> list:
        return self.found if word == "本" else []

    def listed_examples(self, notes_to_update: Optional[dict] = None) -> list[str]:
        """match_single_word_in_word_tuple for 本 in a sentence note: the example sentences of
        the meanings its prompt lists, in the prompt's order."""
        calls: list[dict[str, Any]] = []

        def get_response(model, prompt, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return answer(model, prompt, **kwargs)

        async def run() -> bool:
            return await self.mwtn.match_single_word_in_word_tuple(
                config=self.config,
                word_lock=asyncio.Lock(),
                word_locks_dict={},
                log_prefix="",
                match_op_args=self.mwtn.MatchOpArgs(
                    **self.mwtn.get_match_fields(self.config, {"name": "Sentence"}),
                    current_note=None,
                    note_type={"name": "Sentence"},
                    vocab_note_type="Word",
                    word_index=0,
                    part_of_speech="Noun",
                    multi_meaning_index=None,
                    word="本",
                    reading="ほん",
                    sentence="図書館で本を借りた。",
                    prompt_sentence=PROMPT_SENTENCE,
                    match_qualities={},
                    processed_word_tuples={},
                    all_generated_meanings_dict=generated_meanings(),
                    notes_to_add_dict={},
                    notes_to_update_dict=notes_to_update or {},
                    word_note_index=None,
                    note_cache=self.cache,
                    sentence_cache=None,
                    cancel_state=None,
                ),
            )

        with (
            mock.patch.object(self.mwtn, "get_response", get_response),
            mock.patch.object(
                self.mwtn, "get_matching_notes_for_word_and_reading", self.find_notes
            ),
        ):
            self.assertTrue(asyncio.run(run()))
        [call] = calls
        self.prompt = call["prompt"]
        return [meaning["example_sentence"] for meaning in call["inputs"]["meanings"]]

    def test_for_migrated_notes_the_prompt_is_unchanged(self):
        # The same arrays and note ids as the one-type notes held give the same text
        self.listed_examples()

        self.assertEqual(self.prompt, MEANINGS_PROMPT)
        # Read in one turn, in the order the word's notes were found
        self.assertEqual(self.cache.fetches, [[202, 201]])

    def test_the_occurrence_linked_to_the_note_is_marked(self):
        twice = sentence_note(
            201,
            sentence_array(
                ("本", "本", "ほん", ["dontmatch"]),
                ("の", "の", "の", ["dontmatch"]),
                ("本", "本", "ほん", [101, 4]),
            ),
        )
        self.cache.notes[201] = twice

        examples = self.listed_examples()

        self.assertEqual(examples[0], "本の<b>本</b>")

    def test_an_example_the_run_edited_is_read_as_edited(self):
        edited = reading_sentence(101)
        edited.fields["s_array"] = sentence_array(
            ("この", "この", "この", ["dontmatch"]), ("本", "本", "ほん", [101])
        )

        examples = self.listed_examples({201: edited})

        self.assertEqual(examples[0], "この<b>本</b>")
        self.assertEqual(self.cache.fetches, [[202]])

    def test_a_note_without_an_example_sentence_note_gets_none(self):
        no_array = sentence_note(203, "", "本[ほん]です。")
        self.cache.notes[203] = no_array
        for example_ids in (("", "201"), ("not an id", "201"), ("99", "201"), ("203", "201")):
            with self.subTest(example_id=example_ids[0]):
                self.found = two_type_book_notes(example_ids)
                self.cache.fetches.clear()

                examples = self.listed_examples()

                # The note with (m2), listed second; the generated meaning has none either
                self.assertEqual(examples, ["<b>本</b>を読むのが好きだ。", "", ""])

    def test_a_gone_example_costs_only_its_own_note(self):
        self.found = two_type_book_notes(("99", "201"))

        examples = self.listed_examples()

        self.assertEqual(examples[0], "<b>本</b>を読むのが好きだ。")
        # One fetch for both; the cache leaves the gone one out
        self.assertEqual(self.cache.fetches, [[99, 201]])


class MatchTaskTests(MatchHarness):
    """A note's word array matched through the real get_response, with a store installed: the
    calls made for one word target are one task."""

    def setUp(self) -> None:
        super().setUp()
        self.assertIsNone(capture.current_store(), "a store was left installed")
        # Capture's warnings are rate limited per process (test_capture)
        capture._quiet_until.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "capture.sqlite3")
        self.assertTrue(capture.install(self.path))
        self.addCleanup(capture.shutdown)

    def calls(self) -> list[dict]:
        self.assertTrue(capture.current_store().flush())
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(r) for r in connection.execute("SELECT * FROM calls ORDER BY call_id")]

    def test_each_word_targets_calls_are_one_task(self):
        run_id = capture.begin_run("Matched words")
        with (
            capture.run_scope(run_id),
            capture.note_scope(NOTE_ID),
            mock.patch.object(base_ops, "_dispatch_response", answer),
        ):
            saved = self.match_array(base_ops.get_response)
        capture.end_run(run_id, "completed")

        self.assertEqual(saved[2][4], [101, 5])
        self.assertEqual(saved[4][4], [222, 4])
        calls = {row["kind"]: row for row in self.calls()}
        self.assertEqual(set(calls), {"match.meanings", "match.rating"})
        meanings, rating = calls["match.meanings"], calls["match.rating"]
        # Tasks side by side, not one inside the other: each is set in its own asyncio task
        self.assertEqual((meanings["task_id"], meanings["parent_task_id"]), ("本|ほん", None))
        self.assertEqual((rating["task_id"], rating["parent_task_id"]), ("借りる|かりる", None))
        for row in (meanings, rating):
            self.assertEqual(
                (row["run_id"], row["note_id"], row["outcome"]), (run_id, NOTE_ID, "ok")
            )
        # The rows give back the prompts that were sent
        self.assertEqual(meanings["prompt"], MEANINGS_PROMPT)
        recorded = json.loads(meanings["inputs_json"])
        rebuilt = match_targets.meanings_prompt(**meanings_prompt_args(recorded))
        self.assertEqual(rebuilt, meanings["prompt"])
        recorded = json.loads(rating["inputs_json"])
        self.assertEqual(match_targets.rating_prompt(**recorded), rating["prompt"])
        # And what the answers are read against
        self.assertEqual(json.loads(meanings["context_json"]), MEANINGS_CONTEXT)
        self.assertEqual(json.loads(rating["context_json"]), RATING_CONTEXT)


# 日本語学校に通う。: a word inside a word inside a word, so one prompt has a parent and
# components and another two parents
def school_array() -> list:
    def w(form: str, reading: str, pos: str = "noun", subs=None) -> list:
        return [form, pos, form, reading, [], subs or []]

    nihongo = w("日本語", "にほんご", subs=[w("日本", "にほん", "proper noun"), w("語", "ご", "suffix")])
    return [
        w("日本語学校", "にほんごがっこう", subs=[nihongo, w("学校", "がっこう")]),
        w("に", "に", "particle"),
        w("通う", "かよう", "verb"),
        ["。"],
    ]


def judge_prompt(group: str, entry: str) -> str:
    return f"{judge.INTRO}\n\n{judge.POS_RULES[group]}\n\n{entry}\n\n{judge.OUTRO}"


class JudgeCallTests(unittest.TestCase):
    def test_the_word_prompts_are_unchanged(self):
        asks = {ask.elem[2]: ask for ask in judge.plan_judgements(school_array()).asks}

        self.assertEqual(
            asks["日本語"].prompt,
            judge_prompt(
                "noun-sub",
                "Sentence: <b>日本語</b>学校に通う。\n"
                "Word: 日本語 [にほんご], noun\n"
                "Part of: 日本語学校 [にほんごがっこう], noun\n"
                "Made of: 日本 [にほん] + 語 [ご]",
            ),
        )
        self.assertEqual(
            asks["語"].prompt,
            judge_prompt(
                "suffix",
                "Sentence: 日本<b>語</b>学校に通う。\n"
                "Word: 語 [ご], suffix\n"
                "Part of: 日本語 [にほんご], noun\n"
                "Part of: 日本語学校 [にほんごがっこう], noun",
            ),
        )

    def test_each_word_call_records_its_kind_and_what_its_prompt_is_built_from(self):
        op = load_ops_module("word_matching_judge")
        arr = school_array()
        # Linked to a note, so not asked about, but still 日本語's parent
        arr[0][4] = [98765]
        note = FakeNote(NOTE_ID, {"word_list_field": json.dumps(arr, ensure_ascii=False)})
        config = {
            "Word": {"word_list_field": "word_list_field"},
            "word_matching_judge_model": MODEL,
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        def get_response(model, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"reason": "a word of its own", "decision": "match"}

        async def run() -> None:
            plan = op.plan_word_matching_judge(
                config=config,
                note=note,
                edited_nids=[],
                notes_to_add_dict={},
                notes_to_update_dict={},
                progress_updater=Progress(),
                cancel_state=None,
                gate=None,
            )
            tasks: list[asyncio.Task] = []
            plan.spawn(tasks)
            await asyncio.gather(*tasks)

        with (
            mock.patch.object(op, "get_response", get_response),
            mock.patch.object(op, "make_inner_bulk_op", passthrough_inner_bulk_op),
        ):
            asyncio.run(run())

        by_word = {kwargs["inputs"]["word"]: (prompt, kwargs) for prompt, kwargs in calls}
        self.assertEqual(sorted(by_word), sorted(["日本語", "日本", "語", "学校", "通う"]))
        for word, (prompt, kwargs) in by_word.items():
            with self.subTest(word=word):
                self.assertEqual(kwargs["kind"], "judge.word")
                inputs = kwargs["inputs"]
                self.assertEqual(json.loads(json.dumps(inputs)), inputs)
                self.assertNotIn("98765", json.dumps(inputs))
                self.assertEqual(judge.word_prompt(**inputs), prompt)
        self.assertEqual(
            by_word["日本語"][1]["inputs"],
            {
                "sentence": "<b>日本語</b>学校に通う。",
                "word": "日本語",
                "reading": "にほんご",
                "pos": "noun",
                "parents": [{"word": "日本語学校", "reading": "にほんごがっこう", "pos": "noun"}],
                "subs": [{"word": "日本", "reading": "にほん"}, {"word": "語", "reading": "ご"}],
                "group": "noun-sub",
            },
        )
        # Outermost first, as the prompt's builder takes them
        self.assertEqual(
            [p["word"] for p in by_word["語"][1]["inputs"]["parents"]], ["日本語学校", "日本語"]
        )


def yamada_array() -> list:
    return [
        [" 山田[やまだ]", "noun", "山田", "やまだ", [], []],
        ["さん", "suffix", "さん", "さん", [], []],
        ["が", "particle", "が", "が", [], []],
        [" 来[き]た", "verb", "来る", "くる", [], []],
        ["。"],
    ]


class ProperNounsCallTests(unittest.TestCase):
    def test_the_prompt_is_unchanged(self):
        self.assertEqual(proper_noun_llm.prompt(yamada_array()), PROPER_NOUNS_PROMPT)

    def test_the_call_records_its_kind_and_the_sentence(self):
        op = load_ops_module("find_proper_nouns")
        calls: list[tuple[str, dict[str, Any]]] = []

        def get_response(model, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"proper_nouns": []}

        with mock.patch.object(op, "get_response", get_response):
            changed = op.add_proper_nouns({"proper_nouns_model": MODEL}, yamada_array(), "")

        self.assertEqual(changed, [])
        [(prompt, kwargs)] = calls
        self.assertEqual(prompt, PROPER_NOUNS_PROMPT)
        self.assertEqual(kwargs["kind"], "proper_nouns.sentence")
        self.assertEqual(kwargs["inputs"], {"sentence": " 山田[やまだ]さんが 来[き]た。"})
        self.assertEqual(proper_noun_llm.sentence_prompt(**kwargs["inputs"]), prompt)


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


TRANSLATE_SENTENCE = "図書館で<b>本</b>を借りた。"
TRANSLATE_PROMPT = (
    "sentence_to_translate_into_english: 図書館で<b>本</b>を借りた。\n\nIgnore any HTML in"
    " the sentence.\nReturn an HTML-free English translation of the sentence in a JSON string"
    ' as the value of the key "english_sentence".'
)


class TranslateCallTests(unittest.TestCase):
    def translate(self) -> tuple[str, dict[str, Any], Any]:
        op = load_ops_module("translate_field")
        calls: list[tuple[str, dict[str, Any]]] = []

        def get_response(model, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"english_sentence": "I borrowed a book from the library."}

        with mock.patch.object(op, "get_response", get_response):
            result = op.get_translated_field_from_model(
                {"translate_sentence_model": MODEL}, TRANSLATE_SENTENCE
            )
        [(prompt, kwargs)] = calls
        return prompt, kwargs, result

    def test_the_prompt_is_unchanged(self):
        prompt, kwargs, result = self.translate()

        self.assertEqual(prompt, TRANSLATE_PROMPT)
        self.assertEqual(result, "I borrowed a book from the library.")

    def test_the_call_records_its_kind_and_the_sentence(self):
        op = load_ops_module("translate_field")
        prompt, kwargs, _ = self.translate()

        self.assertEqual(kwargs["kind"], "translate.sentence")
        self.assertEqual(kwargs["inputs"], {"sentence": TRANSLATE_SENTENCE})
        self.assertEqual(op.translate_sentence_prompt(**kwargs["inputs"]), prompt)


class KanjifyCallTests(unittest.TestCase):
    def test_the_call_records_its_kind_and_the_sentence(self) -> None:
        # The prompt's builder was pure already and is research's too (kanjify_eval): the call
        # only names it and its sentence
        op = load_ops_module("kanjify_sentence")
        sentence = "これを 読[よ]む。"
        calls: list[tuple[str, dict[str, Any]]] = []

        def get_response(model, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"kanjified_sentence": "<k> 此[こ]れ</k>を 読[よ]む。"}

        config = {"kanjify_sentence_model": MODEL, "kanjify_sentence_temperature": "0.3"}
        with mock.patch.object(op, "get_response", get_response):
            result = op.get_kanjified_sentence_from_model(config, sentence)

        self.assertEqual(result, ["<k> 此[こ]れ</k>を 読[よ]む。"])
        [(prompt, kwargs)] = calls
        self.assertEqual(kwargs["kind"], "kanjify.sentence")
        self.assertEqual(kwargs["inputs"], {"sentence": sentence})
        self.assertEqual(op.get_kanjify_sentence_prompt(**kwargs["inputs"]), prompt)
        # A request parameter, recorded with the params rather than the inputs
        self.assertEqual(kwargs["temperature"], 0.3)


# The file's words for the note's components, for three of the prompt's examples' (裾 is
# 衤 + 居, 諭 is 言 + 俞), and for a component the prompt does not show
STORY_WORDS = {
    "木": "き",
    "卯": "うさぎの みみ",
    "衤": "ころも",
    "居": "いる",
    "言": "いいたい",
    "氵": "みず",
}


class KanjiStoryCallTests(unittest.TestCase):
    """The story op reads the component words from a file in the profile's media folder: a
    temporary one here."""

    def setUp(self) -> None:
        self.op = load_ops_module("make_kanji_story")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        media = os.path.join(directory.name, "collection.media")
        os.mkdir(media)
        words_file = os.path.join(media, configuration.KANJI_STORY_COMPONENT_WORDS_LOG)
        with open(words_file, "w", encoding="utf-8") as f:
            json.dump(STORY_WORDS, f, ensure_ascii=False)
        profile = types.SimpleNamespace(profileFolder=lambda: directory.name)
        patcher = mock.patch.object(self.op, "mw", types.SimpleNamespace(pm=profile))
        patcher.start()
        self.addCleanup(patcher.stop)

    def story(self, current_story: str) -> tuple[str, dict[str, Any], Any]:
        calls: list[tuple[str, dict[str, Any]]] = []

        def get_response(model, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"new_story": "<i>き</i> の <i>うさぎの みみ</i>、<b>やなぎ</b>"}

        with mock.patch.object(self.op, "get_response", get_response):
            result = self.op.get_kanji_story_from_model(
                {"kanji_story_model": MODEL}, "柳", "木,卯", current_story
            )
        [(prompt, kwargs)] = calls
        return prompt, kwargs, result

    def test_the_prompts_are_unchanged(self):
        # Pinned by their sha1, taken before the builder was taken out of the op: the prompt is
        # 3.6 KB of examples. Its head is the case's own part.
        prompt, _, _ = self.story("")
        self.assertEqual(sha1(prompt), "8ac6094ee661490105e325dd19ce7c314d37ffa5", prompt)
        self.assertTrue(
            prompt.startswith(
                "kanji: 柳\n  component_radicals_or_kanji: ['木', '卯']\n"
                "  words_to_use_in_story_for_components: き, うさぎの みみ\n\nThe kanji is"
            )
        )
        # With a story to keep: its own line and the other instruction
        prompt, _, _ = self.story("<i>き</i> の そば")
        self.assertEqual(sha1(prompt), "d2173d5b10a4ffc09967c50bb32029012fc66733", prompt)
        self.assertIn(
            "words_to_use_in_story_for_components: き, うさぎの みみ\n"
            "current_story_in_japanese: <i>き</i> の そば\n\nThe kanji is",
            prompt,
        )

    def test_the_call_records_its_kind_and_the_words_the_prompt_shows(self):
        prompt, kwargs, result = self.story("<i>き</i> の そば")

        self.assertEqual(result, "<i>き</i> の <i>うさぎの みみ</i>、<b>やなぎ</b>")
        self.assertEqual(kwargs["kind"], "kanji_story.kanji")
        inputs = kwargs["inputs"]
        # The words, not only the components they were looked up by, so the inputs rebuild the
        # prompt without the file; and of the file's words only those the prompt shows (no 氵)
        self.assertEqual(
            inputs,
            {
                "kanji": "柳",
                "components": "木,卯",
                "current_story": "<i>き</i> の そば",
                "component_words": {
                    "木": "き",
                    "卯": "うさぎの みみ",
                    "衤": "ころも",
                    "居": "いる",
                    "言": "いいたい",
                },
            },
        )
        self.assertEqual(json.loads(json.dumps(inputs)), inputs)
        self.assertEqual(self.op.kanji_story_prompt(**inputs), prompt)
        stored = json.loads(capture_store.canonical_json(inputs))
        self.assertEqual(self.op.kanji_story_prompt(**stored), prompt)


def names_get_response(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "get_response") or (
        isinstance(node, ast.Attribute) and node.attr == "get_response"
    )


class EveryCallSiteTests(unittest.TestCase):
    def test_every_ai_call_of_the_ops_records_a_kind_and_inputs(self) -> None:
        """A call of `get_response`, or a call handed it to run (`asyncio.to_thread`), in any
        module of async_api_ops, names its kind and inputs: without them its row in the store
        says neither what it was for nor what case it was."""
        sites: list[str] = []
        missing: list[str] = []
        for path in sorted(OPS_DIR.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                if not (names_get_response(node.func) or any(map(names_get_response, node.args))):
                    continue
                site = f"{path.name}:{node.lineno}"
                sites.append(site)
                keywords = {k.arg: k.value for k in node.keywords}
                kind = keywords.get("kind")
                if kind is None or (isinstance(kind, ast.Constant) and not kind.value):
                    missing.append(f"{site} kind")
                if "inputs" not in keywords:
                    missing.append(f"{site} inputs")

        self.assertEqual(missing, [])
        # The fourteen there were when this was written: a scan that finds none proves nothing
        self.assertGreaterEqual(len(sites), 14, sites)


if __name__ == "__main__":
    unittest.main()
