"""What the meaning ops record of their AI calls: clean_meaning's four and make_all_meanings'
three. Each call's `kind`, and as `inputs` the values its prompt is built from, so that the
prompt's builder given the recorded inputs gives back the prompt that was sent - also after the
store's round trip, whose canonical JSON sorts every dict's keys. clean_meaning's calls also
record a `context`, the notes their answers go to, which must name the note the op then writes.

The prompts are pinned byte for byte by their sha1, being kilobytes each: the digests were taken
from the code before the builders were taken out of the ops. A pin that fails means the prompt
text changed; if that was meant, take the new digest.

`get_response` is the test's, patched on the op module as in test_make_all_meanings, except in
one test that runs the real one with a store installed in a temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from typing import Any, Callable
from unittest import mock

from addon_modules import load_ops_module

cm = load_ops_module("clean_meaning")
mam = load_ops_module("make_all_meanings")
capture = load_ops_module("capture")
capture_store = load_ops_module("capture_store")
base_ops = load_ops_module("base_ops")

CONFIG = {"word_meaning_model": "model", "make_meanings_model": "model"}
WORD, READING = "引く", "ひく"
WORD_KEY = f"{WORD}_{READING}"
PULL = {"jp_sentence": "犬が<b>綱</b>を引く。", "en_sentence": "The dog pulls the leash."}
DRAW = {"jp_sentence": "定規で線を<b>引く</b>。", "en_sentence": "I draw a line with a ruler."}
LOTS = {"jp_sentence": "おみくじを<b>引いた</b>。", "en_sentence": "I drew a fortune slip."}
ENTRY = (
    "ひ・く【引く】\n1 物に手をかけて近くへ寄せる。「綱を―・く」\n"
    "2 線を描く。「線を―・く」\n3 くじなどを抜き取る。「くじを―・く」"
)
PULL_NOTE, DRAW_NOTE = 1712000000001, 1712000000002


def note_meanings() -> dict:
    """Two notes' meanings and sentences, not in note id order: the prompts list them by id."""
    return {
        DRAW_NOTE: {"jp_meaning": "線を描く。", "en_meaning": "to draw", "sentences": [DRAW]},
        PULL_NOTE: {
            "jp_meaning": "手元へ寄せる。",
            "en_meaning": "to pull",
            "sentences": [PULL, LOTS],
        },
    }


def generated_meanings() -> list[dict]:
    """The word's generated meanings: the pull note's English is the first's, so that note is
    mapped already and the draw note is not."""
    return [
        {"jp_meaning": "物に手をかけて近くへ寄せる。", "en_meaning": "to pull"},
        {"jp_meaning": "線や図をかく。", "en_meaning": "to draw (a line)"},
        {"jp_meaning": "くじなどを抜き取る。", "en_meaning": "to draw (lots)"},
    ]


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class Recorder:
    """The op module's get_response: records what it was given and gives the answers in turn,
    the last one again after that."""

    def __init__(self, answer: Any, *later: Any):
        self.answers = [answer, *later]
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, model: str, prompt: str, **kwargs: Any) -> Any:
        self.calls.append((model, prompt, kwargs))
        return self.answers[min(len(self.calls), len(self.answers)) - 1]

    def one(self) -> tuple[str, dict[str, Any]]:
        [(_, prompt, kwargs)] = self.calls
        return prompt, kwargs


def no_mdx_load():
    return mock.patch.object(
        mam.mdx_helper, "load_mdx_dictionaries_if_needed", lambda *args, **kwargs: None
    )


def mdx_entry(entry: str):
    return mock.patch.object(mam.mdx_helper, "get_definition_text", lambda **kwargs: entry)


class Calls:
    """Each op called through its public function, as clean_meaning_in_note and the bulk ops
    call them, with the test's get_response."""

    @staticmethod
    def rework(entry, answer: Any = None) -> tuple[Recorder, Any]:
        recorder = Recorder({"meanings": []} if answer is None else answer)
        with mock.patch.object(cm, "get_response", recorder):
            result = cm.update_all_meanings_for_word(
                CONFIG, WORD, READING, note_meanings(), entry
            )
        return recorder, result

    @staticmethod
    def map(generated: list[dict], answer: Any = None) -> tuple[Recorder, Any]:
        recorder = Recorder({"meanings": []} if answer is None else answer)
        with mock.patch.object(cm, "get_response", recorder):
            result = cm.match_meanings_to_generated_meanings(
                CONFIG, WORD, READING, note_meanings(), {WORD_KEY: generated}
            )
        return recorder, result

    @staticmethod
    def extract(sentences: list[dict], prev_en_meaning: str) -> tuple[Recorder, Any]:
        recorder = Recorder({"cleaned_meaning": "線を描く。", "english_meaning": "to draw"})
        with mock.patch.object(cm, "get_response", recorder):
            result = cm.get_single_meaning_from_mdx_dict_entry(
                CONFIG, WORD, READING, sentences, ENTRY, prev_en_meaning
            )
        return recorder, result

    @staticmethod
    def generate(sentences: list[dict], prev_en_meaning: str) -> tuple[Recorder, Any]:
        recorder = Recorder({"new_meaning": "線を描く。", "english_meaning": "to draw"})
        with mock.patch.object(cm, "get_response", recorder):
            result = cm.get_new_meaning_from_model(
                CONFIG, WORD, READING, sentences, prev_en_meaning
            )
        return recorder, result

    @staticmethod
    def make(meanings_dict: dict) -> tuple[Recorder, Any]:
        recorder = Recorder({"meanings": generated_meanings()})
        with no_mdx_load(), mdx_entry(ENTRY), mock.patch.object(mam, "get_response", recorder):
            result = mam.make_all_meanings_for_word(CONFIG, WORD, READING, meanings_dict)
        return recorder, result

    @staticmethod
    def merge(meanings_dict: dict) -> tuple[Recorder, Any]:
        recorder = Recorder({"meanings": generated_meanings()[:2]})
        with mock.patch.object(mam, "get_response", recorder):
            result = mam.merge_existing_meanings_for_word(CONFIG, WORD, READING, meanings_dict)
        return recorder, result

    @staticmethod
    def revise(meanings_dict: dict) -> tuple[Recorder, Any]:
        recorder = Recorder({"meanings": generated_meanings()})
        # The draw note's usage, which none of the generated meanings covered
        unmatched = {DRAW_NOTE: note_meanings()[DRAW_NOTE]}
        with no_mdx_load(), mdx_entry(ENTRY), mock.patch.object(mam, "get_response", recorder):
            result = mam.revise_meanings_for_word(
                CONFIG, WORD, READING, unmatched, meanings_dict
            )
        return recorder, result


class PromptPinTests(unittest.TestCase):
    """Every variant of each prompt, byte for byte, as the ops built them before."""

    def assert_pinned(self, call: tuple[Recorder, Any], digest: str) -> None:
        prompt, _ = call[0].one()
        self.assertEqual(sha1(prompt), digest, prompt)

    def test_rework(self):
        self.assert_pinned(Calls.rework(ENTRY), "5a2177718f8d1cc74cec774db595c1c0fd6595c2")
        # Without a dictionary entry, the other set of rules and no dictionary section
        self.assert_pinned(Calls.rework(None), "55515740f5c8331717ffdb5075d3b5f1ae0abd48")

    def test_map(self):
        self.assert_pinned(
            Calls.map(generated_meanings()), "68fb24577c6e2df0a40f4b8f980f872c6f168f14"
        )
        # None of the notes mapped already: the other rule
        unmapped = [{"jp_meaning": "引っ張る。", "en_meaning": "to tug"}] + generated_meanings()[1:]
        self.assert_pinned(Calls.map(unmapped), "78c66f10cdb8358cedccc1a777d2177c95fae84c")

    def test_extract(self):
        self.assert_pinned(
            Calls.extract([PULL, LOTS], "to pull"), "7e81fa8043c3dbe3f0fb2003a47a7062a6839bd2"
        )
        # One sentence and no English meaning yet
        self.assert_pinned(Calls.extract([PULL], ""), "365aa1654a86ca4b4ae1d861859064f56e475b79")

    def test_generate(self):
        self.assert_pinned(
            Calls.generate([PULL, LOTS], "to pull"), "c775404b815b0278f24c662c9ff26142317c0990"
        )
        self.assert_pinned(Calls.generate([PULL], ""), "fd1d6ee648cedb77e70e4157a7a5d4a02f261671")

    def test_make(self):
        self.assert_pinned(Calls.make({}), "cbee400820702443b08ea7fd145323c0f80980d1")

    def test_merge(self):
        self.assert_pinned(
            Calls.merge({WORD_KEY: generated_meanings()}),
            "a094ff5fb7f0992d1f4e8c81f8116817ec8c2ab4",
        )

    def test_revise(self):
        self.assert_pinned(
            Calls.revise({WORD_KEY: generated_meanings()}),
            "44dc47d268d8c66de7b07dc918399eba3a9c42eb",
        )


def pull_meaning() -> dict:
    return note_meanings()[PULL_NOTE]


def draw_meaning() -> dict:
    return note_meanings()[DRAW_NOTE]


class CallTests(unittest.TestCase):
    """Each call's kind and inputs, and the op's result from the answer as before."""

    def assert_recorded(
        self,
        call: tuple[Recorder, Any],
        kind: str,
        builder: Callable[..., str],
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        prompt, kwargs = call[0].one()
        self.assertEqual(kwargs["kind"], kind)
        self.assertEqual(kwargs["inputs"], inputs)
        recorded = kwargs["inputs"]
        # Plain JSON comes back from a round trip unchanged: no tuple, no object
        self.assertEqual(json.loads(json.dumps(recorded)), recorded)
        self.assertEqual(builder(**recorded), prompt)
        # And as the store keeps them, every dict's keys sorted
        stored = json.loads(capture_store.canonical_json(recorded))
        self.assertEqual(builder(**stored), prompt)
        return kwargs

    def test_rework(self):
        answer = {
            "meanings": [
                {
                    "meaning_index": 2,
                    "jp_meaning": "線や図をかく。",
                    "en_meaning": "to draw (a line)",
                    "dictionary_reference": "2 線を描く。",
                }
            ]
        }
        call = Calls.rework(ENTRY, answer)
        kwargs = self.assert_recorded(
            call,
            "clean_meaning.rework",
            cm.rework_meanings_prompt,
            # In note id order, which is the order the answer's indexes count in
            {
                "word": WORD,
                "reading": READING,
                "meanings": [pull_meaning(), draw_meaning()],
                "dictionary_entry": ENTRY,
            },
        )
        self.assertEqual(call[1], {DRAW_NOTE: ("線や図をかく。", "to draw (a line)")})
        # The notes the indexes count, which the prompt does not show
        self.assertEqual(kwargs["context"], {"note_ids": [PULL_NOTE, DRAW_NOTE]})
        items = kwargs["response_schema"]["properties"]["meanings"]["items"]
        self.assertIn("dictionary_reference", items["required"])

        # No dictionary entry: recorded as None, which selects the prompt's other rules
        call = Calls.rework(None)
        self.assert_recorded(
            call,
            "clean_meaning.rework",
            cm.rework_meanings_prompt,
            {
                "word": WORD,
                "reading": READING,
                "meanings": [pull_meaning(), draw_meaning()],
                "dictionary_entry": None,
            },
        )
        self.assertEqual(call[1], {})

    def test_map(self):
        answer = {
            "meanings": [{"used_meaning_index": 2, "possible_meaning_index": 2, "mapping_score": 5}]
        }
        call = Calls.map(generated_meanings(), answer)
        kwargs = self.assert_recorded(
            call,
            "clean_meaning.map",
            cm.map_meanings_prompt,
            {
                "word": WORD,
                "reading": READING,
                "possible_meanings": generated_meanings(),
                "meanings": [pull_meaning(), draw_meaning()],
            },
        )
        self.assertEqual(call[1], {DRAW_NOTE: ("線や図をかく。", "to draw (a line)", 5)})
        # used_meaning_index 2 is the second note in the context's order: the one mapped
        self.assertEqual(kwargs["context"], {"note_ids": [PULL_NOTE, DRAW_NOTE], "depth": 0})
        self.assertEqual(set(call[1]), {kwargs["context"]["note_ids"][2 - 1]})
        # What the recorded inputs alone decide: the pull note shows as mapped already
        self.assertIn(
            "Meaning index 1 (ALREADY MAPPED to possible meaning index 1):", call[0].one()[0]
        )

    def test_a_low_score_revises_the_meanings_and_maps_again(self):
        revised = [
            {"jp_meaning": "物を引き寄せる。", "en_meaning": "to pull"},
            {"jp_meaning": "線をかく。", "en_meaning": "to draw"},
        ]
        def mapping(possible_index: int, score: int) -> dict:
            return {
                "meanings": [
                    {
                        "used_meaning_index": 2,
                        "possible_meaning_index": possible_index,
                        "mapping_score": score,
                    }
                ]
            }

        # The draw note scored 2 against the lots meaning, then 5 against the revised one
        maps = Recorder(mapping(3, 2), mapping(2, 5))
        revises = Recorder({"meanings": revised})
        meanings_dict = {WORD_KEY: generated_meanings()}
        with (
            no_mdx_load(),
            mdx_entry(ENTRY),
            mock.patch.object(cm, "get_response", maps),
            mock.patch.object(mam, "get_response", revises),
        ):
            result = cm.match_meanings_to_generated_meanings(
                CONFIG, WORD, READING, note_meanings(), meanings_dict
            )

        self.assertEqual(result, {DRAW_NOTE: ("線をかく。", "to draw", 5)})
        self.assertEqual([kwargs["kind"] for _, _, kwargs in maps.calls], ["clean_meaning.map"] * 2)
        # The second mapping is the recursion's: the same notes, one level down
        self.assertEqual(
            [kwargs["context"] for _, _, kwargs in maps.calls],
            [
                {"note_ids": [PULL_NOTE, DRAW_NOTE], "depth": 0},
                {"note_ids": [PULL_NOTE, DRAW_NOTE], "depth": 1},
            ],
        )
        # The usage the first mapping scored low, against the meanings it had
        self.assert_recorded(
            (revises, None),
            "make_all_meanings.revise",
            mam.revise_meanings_prompt,
            {
                "word": WORD,
                "reading": READING,
                "meanings_json": mam.generated_meanings_json(generated_meanings()),
                "usages": [draw_meaning()],
                "dictionary_entry": ENTRY,
            },
        )
        # The second mapping is against the revised meanings
        _, prompt, kwargs = maps.calls[1]
        self.assertEqual(kwargs["inputs"]["possible_meanings"], revised)
        self.assertEqual(cm.map_meanings_prompt(**kwargs["inputs"]), prompt)

    def test_extract(self):
        call = Calls.extract([PULL, LOTS], "to pull")
        kwargs = self.assert_recorded(
            call,
            "clean_meaning.extract",
            cm.extract_meaning_prompt,
            {
                "word": WORD,
                "reading": READING,
                "sentences": [PULL, LOTS],
                "dictionary_entry": ENTRY,
                "prev_en_meaning": "to pull",
            },
        )
        self.assertEqual(call[1], ("線を描く。", "to draw"))
        # Called without the note it is for, as here, the context says none
        self.assertEqual(kwargs["context"], {"note_id": None})

    def test_generate(self):
        call = Calls.generate([PULL, LOTS], "to pull")
        kwargs = self.assert_recorded(
            call,
            "clean_meaning.generate",
            cm.generate_meaning_prompt,
            # The sentences' Japanese only: the prompt shows no translation
            {
                "word": WORD,
                "reading": READING,
                "sentences": [PULL["jp_sentence"], LOTS["jp_sentence"]],
                "prev_en_meaning": "to pull",
            },
        )
        self.assertEqual(call[1], ("線を描く。", "to draw"))
        self.assertEqual(kwargs["context"], {"note_id": None})

    def test_make(self) -> None:
        meanings_dict: dict = {}
        call = Calls.make(meanings_dict)
        # The dictionary entry itself, not only the word it was looked up by: the inputs
        # rebuild the prompt without the dictionaries
        self.assert_recorded(
            call,
            "make_all_meanings.make",
            mam.make_meanings_prompt,
            {"word": WORD, "reading": READING, "dictionary_entry": ENTRY},
        )
        self.assertEqual(call[1], mam.MakeMeaningsResult.SUCCESS)
        self.assertEqual(meanings_dict, {WORD_KEY: generated_meanings()})

    def test_merge(self):
        meanings_dict = {WORD_KEY: generated_meanings()}
        call = Calls.merge(meanings_dict)
        self.assert_recorded(
            call,
            "make_all_meanings.merge",
            mam.merge_meanings_prompt,
            {
                "word": WORD,
                "reading": READING,
                "meanings_json": json.dumps(generated_meanings(), ensure_ascii=False, indent=2),
            },
        )
        self.assertEqual(call[1], mam.MakeMeaningsResult.SUCCESS)
        self.assertEqual(meanings_dict, {WORD_KEY: generated_meanings()[:2]})
        # Why the text and not the list: the list as the store keeps it shows en_meaning first
        stored = json.loads(capture_store.canonical_json(generated_meanings()))
        self.assertNotEqual(
            mam.generated_meanings_json(stored), mam.generated_meanings_json(generated_meanings())
        )

    def test_revise(self):
        meanings_dict = {WORD_KEY: generated_meanings()}
        call = Calls.revise(meanings_dict)
        self.assert_recorded(
            call,
            "make_all_meanings.revise",
            mam.revise_meanings_prompt,
            {
                "word": WORD,
                "reading": READING,
                "meanings_json": mam.generated_meanings_json(generated_meanings()),
                "usages": [draw_meaning()],
                "dictionary_entry": ENTRY,
            },
        )
        self.assertEqual(call[1], mam.MakeMeaningsResult.SUCCESS)


class WordNote:
    """A word note as clean_meaning_in_note reads it; `placeholder` in its new note id field."""

    def __init__(self, note_id: int, meaning: str, en_meaning: str, placeholder: str = ""):
        self.id = note_id
        self.fields = {
            "meaning_field": meaning,
            "english_meaning_field": en_meaning,
            "word_field": WORD,
            "word_reading_field": READING,
            "sentence_field": f"{meaning}の文。",
            "new_note_id_field": placeholder,
        }
        self.tags: list[str] = []

    def note_type(self) -> dict:
        return {"name": "Word"}

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]

    def __setitem__(self, field: str, value: str) -> None:
        self.fields[field] = value

    def add_tag(self, tag: str) -> None:
        self.tags.append(tag)

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def key(self) -> int:
        """The id the note's meaning is known by: its own, or its placeholder until added."""
        return self.id or int(self.fields["new_note_id_field"])


NEW_MEANING = ("新しい意味。", "new meaning")


class CleanNoteTests(unittest.TestCase):
    """clean_meaning_in_note over fake notes: the context of each call it makes names the note
    the op then writes the answer into."""

    CONFIG = {
        "Word": {key: key for key in WordNote(0, "", "").fields},
        "word_meaning_model": "model",
    }

    def clean(self, note: WordNote, others: list[WordNote], entry, answer) -> Recorder:
        recorder = Recorder(answer)

        def sentences(config, n: WordNote, **kwargs) -> list[dict]:
            return [{"jp_sentence": n["sentence_field"], "en_sentence": ""}]

        with (
            no_mdx_load(),
            mdx_entry(entry),
            mock.patch.object(cm, "get_other_meaning_notes", lambda **kwargs: others),
            mock.patch.object(cm, "get_sentences_for_note", sentences),
            mock.patch.object(cm, "get_response", recorder),
        ):
            cm.clean_meaning_in_note(
                config=self.CONFIG,
                note=note,
                notes_to_add_dict={},
                notes_to_update_dict={},
                all_generated_meanings_dict={},
            )
        return recorder

    def test_a_reworked_meaning_goes_to_the_note_the_context_names_at_its_index(self):
        for index in (1, 2, 3):
            with self.subTest(meaning_index=index):
                pull = WordNote(PULL_NOTE, "手元へ寄せる。", "to pull")
                draw = WordNote(DRAW_NOTE, "線を描く。", "to draw")
                # Made by this run and not added yet: known by its placeholder, which sorts first
                lots = WordNote(0, "くじを抜く。", "to draw lots", placeholder="-5550001")
                old = {n.key(): n["meaning_field"] for n in (pull, draw, lots)}
                jp, en = NEW_MEANING
                reworked = {"meaning_index": index, "jp_meaning": jp, "en_meaning": en}

                _, kwargs = self.clean(pull, [draw, lots], ENTRY, {"meanings": [reworked]}).one()

                self.assertEqual(kwargs["kind"], "clean_meaning.rework")
                note_ids = kwargs["context"]["note_ids"]
                self.assertEqual(note_ids, [-5550001, PULL_NOTE, DRAW_NOTE])
                [updated] = [n for n in (pull, draw, lots) if n["meaning_field"] == jp]
                self.assertEqual(updated.key(), note_ids[index - 1])
                # The prompt's meaning at that index was that note's
                listed = kwargs["inputs"]["meanings"][index - 1]
                self.assertEqual(listed["jp_meaning"], old[updated.key()])

    def test_a_meaning_extracted_or_generated_names_the_note_it_is_for(self):
        jp, en = NEW_MEANING
        cases = [
            # A dictionary entry: extracted from it
            ("clean_meaning.extract", ENTRY, {"cleaned_meaning": jp, "english_meaning": en}),
            # None: generated from the sentences
            ("clean_meaning.generate", None, {"new_meaning": jp, "english_meaning": en}),
        ]
        for kind, entry, answer in cases:
            for note in (
                WordNote(PULL_NOTE, "", ""),
                WordNote(0, "", "", placeholder="-5550002"),
            ):
                with self.subTest(kind=kind, note_id=note.id):
                    _, kwargs = self.clean(note, [], entry, answer).one()

                    self.assertEqual(kwargs["kind"], kind)
                    self.assertEqual(kwargs["context"], {"note_id": note.key()})
                    written = (note["meaning_field"], note["english_meaning_field"])
                    self.assertEqual(written, (jp, en))


class StoreTests(unittest.TestCase):
    """Two of the calls through the real get_response with a store installed: the rows' inputs
    give back the rows' prompts."""

    def setUp(self) -> None:
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

    def test_the_rows_give_back_the_prompts_that_were_sent(self):
        def answer(model: str, prompt: str, **kwargs: Any) -> dict:
            # What either op reads from its answer
            return {
                "meanings": generated_meanings()[:2],
                "cleaned_meaning": "線を描く。",
                "english_meaning": "to draw",
            }

        run_id = capture.begin_run("Cleaned meanings")
        with (
            capture.run_scope(run_id),
            capture.note_scope(DRAW_NOTE),
            mock.patch.object(base_ops, "_dispatch_response", answer),
        ):
            cm.get_single_meaning_from_mdx_dict_entry(
                CONFIG, WORD, READING, [DRAW], ENTRY, "to draw", note_id=DRAW_NOTE
            )
            mam.merge_existing_meanings_for_word(
                CONFIG, WORD, READING, {WORD_KEY: generated_meanings()}
            )
        capture.end_run(run_id, "completed")

        rows = {row["kind"]: row for row in self.calls()}
        builders = {
            "clean_meaning.extract": cm.extract_meaning_prompt,
            "make_all_meanings.merge": mam.merge_meanings_prompt,
        }
        self.assertEqual(set(rows), set(builders))
        for kind, builder in builders.items():
            with self.subTest(kind=kind):
                row = rows[kind]
                self.assertEqual((row["run_id"], row["note_id"]), (run_id, DRAW_NOTE))
                self.assertEqual(row["outcome"], "ok")
                self.assertEqual(builder(**json.loads(row["inputs_json"])), row["prompt"])
        self.assertEqual(
            json.loads(rows["clean_meaning.extract"]["context_json"]), {"note_id": DRAW_NOTE}
        )
        # Nothing to read make_all_meanings' answers against that the inputs do not say
        self.assertIsNone(rows["make_all_meanings.merge"]["context_json"])


