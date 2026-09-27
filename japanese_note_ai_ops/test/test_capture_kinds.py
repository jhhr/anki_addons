"""What the match op, the word matching judge and the proper noun op record of their AI calls:
each call's `kind`, and as `inputs` the values its prompt is built from, so that the prompt's
builder given the recorded inputs gives back the prompt that was sent. The prompts themselves
are pinned byte for byte (written before their builders were taken out of the ops).

`get_response` is the test's in most tests, as in test_word_array_match_targets and test_judge.
One runs a note's word array match through the real `get_response` with a store installed in a
temporary directory (test_capture_runs) and its provider faked, for the word targets' tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from typing import Any
from unittest import mock

from addon_modules import load_ops_module

configuration = load_ops_module("configuration", "")
match_targets = load_ops_module("match_targets", subdir="word_array")
judge = load_ops_module("judge", subdir="word_array")
proper_noun_llm = load_ops_module("proper_noun_llm", subdir="word_array")
capture = load_ops_module("capture")
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

    def match_book(self, get_response) -> tuple[bool, dict]:
        """match_single_word_in_word_tuple for 本 in PROMPT_SENTENCE."""
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
                    notes_to_add_dict={},
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

    def match_array(self, get_response) -> list:
        """plan_word_array_matching over `library_array`, every task run; the array saved."""
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
                notes_to_add_dict={},
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


if __name__ == "__main__":
    unittest.main()
