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


def lots_meaning() -> dict:
    """A third note's meaning, for the lots sense."""
    return {"jp_meaning": "くじを抜く。", "en_meaning": "to draw lots", "sentences": [LOTS]}


# The context the Calls give, as clean_meaning_in_note gives its own: passed through untouched
CALL_CONTEXT = {"target_note_id": DRAW_NOTE, "other_note_ids": [PULL_NOTE]}


class Calls:
    """Each op called through its public function, as clean_meaning_in_note and the bulk ops
    call them, with the test's get_response. The note cleaned is the draw note; the pull note is
    the word's other note."""

    @staticmethod
    def rework_note(entry, answer: Any = None) -> tuple[Recorder, Any]:
        recorder = Recorder(answer)
        with mock.patch.object(cm, "get_response", recorder):
            result = cm.rework_note_meaning(
                CONFIG, WORD, READING, draw_meaning(), [pull_meaning()], entry, CALL_CONTEXT
            )
        return recorder, result

    @staticmethod
    def map_note(generated: list[dict], answer: Any = None) -> tuple[Recorder, Any]:
        recorder = Recorder(answer)
        with mock.patch.object(cm, "get_response", recorder):
            result = cm.map_note_to_generated_meaning(
                CONFIG,
                WORD,
                READING,
                generated,
                draw_meaning(),
                [pull_meaning()],
                {**CALL_CONTEXT, "depth": 0},
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
    def add(meanings_dict: dict, answer: Any = None) -> tuple[Recorder, Any]:
        recorder = Recorder({"meanings": [ADDED]} if answer is None else answer)
        # The lots note's usage, which none of the generated meanings covered
        with no_mdx_load(), mdx_entry(ENTRY), mock.patch.object(mam, "get_response", recorder):
            result = mam.add_meanings_for_usage(
                CONFIG, WORD, READING, lots_meaning(), meanings_dict
            )
        return recorder, result


# The meaning an add answer gives for the lots note
ADDED = {"jp_meaning": "くじなどを引き当てる。", "en_meaning": "to draw (lots); to pick"}


class PromptPinTests(unittest.TestCase):
    """Every variant of each prompt, byte for byte. extract's, generate's, make's and merge's
    are as the ops built them before; the others were written with the one-note cleaning."""

    def assert_pinned(self, call: tuple[Recorder, Any], digest: str) -> None:
        prompt, _ = call[0].one()
        self.assertEqual(sha1(prompt), digest, prompt)

    def test_rework_note(self):
        self.assert_pinned(Calls.rework_note(ENTRY), "d09cbaa4e1122a4c01e4085beace68d008e95a57")
        # Without a dictionary entry, no rules for one and no dictionary section
        self.assert_pinned(Calls.rework_note(None), "5a873092f1b85c97823ed52a9a565fde158ed1fd")

    def test_map_note(self):
        self.assert_pinned(Calls.map_note(generated_meanings()), "ccd02a81a480912c0665fb944e5556eb9aa5a7d7")
        # The other note mapped to none of them
        unmapped = [{"jp_meaning": "引っ張る。", "en_meaning": "to tug"}] + generated_meanings()[1:]
        self.assert_pinned(Calls.map_note(unmapped), "c34e90e9a3942ec70c1f1cb7e03fde850ba261d8")

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

    def test_add(self):
        self.assert_pinned(
            Calls.add({WORD_KEY: generated_meanings()}),
            "067c44fa6275a433f36710903ce4733e5273bd03",
        )


def pull_meaning() -> dict:
    return note_meanings()[PULL_NOTE]


def draw_meaning() -> dict:
    return note_meanings()[DRAW_NOTE]


class CallTests(unittest.TestCase):
    """Each call's kind and inputs, and the op's result from the answer."""

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

    def test_rework_note(self):
        answer = {"jp_meaning": "線や図をかく。", "en_meaning": "to draw (a line)", "same_sense_as": 0}
        call = Calls.rework_note(ENTRY, answer)
        kwargs = self.assert_recorded(
            call,
            "clean_meaning.rework_note",
            cm.rework_note_prompt,
            {
                "word": WORD,
                "reading": READING,
                "target": draw_meaning(),
                "others": [pull_meaning()],
                "dictionary_entry": ENTRY,
            },
        )
        self.assertEqual(call[1], ("線や図をかく。", "to draw (a line)", 0))
        # The note the answer is for and the ones same_sense_as counts, which the prompt does
        # not show
        self.assertEqual(kwargs["context"], CALL_CONTEXT)
        self.assertEqual(
            kwargs["response_schema"]["required"], ["jp_meaning", "en_meaning", "same_sense_as"]
        )

        # No dictionary entry: recorded as None, which selects the prompt's other text
        call = Calls.rework_note(None, {**answer, "same_sense_as": 1})
        self.assert_recorded(
            call,
            "clean_meaning.rework_note",
            cm.rework_note_prompt,
            {
                "word": WORD,
                "reading": READING,
                "target": draw_meaning(),
                "others": [pull_meaning()],
                "dictionary_entry": None,
            },
        )
        self.assertEqual(call[1], ("線や図をかく。", "to draw (a line)", 1))
        self.assertNotIn("Dictionary entry", call[0].one()[0])

    def test_an_unusable_rework_answer_is_none(self):
        for answer in (
            None,
            {"jp_meaning": "", "en_meaning": "to draw", "same_sense_as": 0},
            {"en_meaning": "to draw", "same_sense_as": 0},
        ):
            with self.subTest(answer=answer):
                self.assertIsNone(Calls.rework_note(ENTRY, answer)[1])
        # A same_sense_as that names no note is read as none
        answer = {"jp_meaning": "線をかく。", "en_meaning": "to draw", "same_sense_as": 2}
        self.assertEqual(Calls.rework_note(ENTRY, answer)[1], ("線をかく。", "to draw", 0))

    def test_map_note(self):
        answer = {"possible_meaning_index": 2, "mapping_score": 5, "same_sense_as": 0}
        call = Calls.map_note(generated_meanings(), answer)
        kwargs = self.assert_recorded(
            call,
            "clean_meaning.map_note",
            cm.map_note_prompt,
            {
                "word": WORD,
                "reading": READING,
                "possible_meanings": generated_meanings(),
                "target": draw_meaning(),
                "others": [pull_meaning()],
            },
        )
        self.assertEqual(call[1], cm.NoteMapping(generated_meanings()[1], 5, 0))
        self.assertEqual(kwargs["context"], {**CALL_CONTEXT, "depth": 0})
        # What the recorded inputs alone decide: the other note uses the first possible meaning,
        # shown for reference, not barred as a mapping of the notes together barred it
        prompt = call[0].one()[0]
        self.assertIn("Other note 1 (uses possible meaning 1):", prompt)
        self.assertNotIn("ALREADY MAPPED", prompt)

    def test_an_unusable_map_answer_is_none(self):
        for answer in (
            None,
            {"possible_meaning_index": 4, "mapping_score": 5, "same_sense_as": 0},
            {"possible_meaning_index": 0, "mapping_score": 5, "same_sense_as": 0},
            {"possible_meaning_index": 2, "mapping_score": 6, "same_sense_as": 0},
            {"possible_meaning_index": 2, "same_sense_as": 0},
        ):
            with self.subTest(answer=answer):
                self.assertIsNone(Calls.map_note(generated_meanings(), answer)[1])

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

    def test_add(self):
        meanings_dict = {WORD_KEY: generated_meanings()}
        call = Calls.add(meanings_dict)
        self.assert_recorded(
            call,
            "make_all_meanings.add",
            mam.add_meanings_prompt,
            {
                "word": WORD,
                "reading": READING,
                "possible_meanings": generated_meanings(),
                "usage": lots_meaning(),
                "dictionary_entry": ENTRY,
            },
        )
        self.assertEqual(call[1], [ADDED])
        self.assertEqual(meanings_dict, {WORD_KEY: generated_meanings() + [ADDED]})

    def test_an_add_keeps_every_meaning_the_word_had_as_it_was(self):
        # Notes are mapped to a generated meaning by its exact English: a list rewritten for one
        # note left the others holding meanings it no longer had. An answer that rewords one is
        # an addition at most, and one that repeats one adds nothing
        reworded = {"jp_meaning": "物を引き寄せる。", "en_meaning": "to pull; to tug"}
        repeated = generated_meanings()[0]
        meanings_dict = {WORD_KEY: generated_meanings()}
        call = Calls.add(meanings_dict, {"meanings": [repeated, reworded, ADDED]})

        self.assertEqual(call[1], [reworded, ADDED])
        self.assertEqual(meanings_dict[WORD_KEY][:3], generated_meanings())

        # Nothing new: nothing added, the list as it was
        meanings_dict = {WORD_KEY: generated_meanings()}
        self.assertEqual(Calls.add(meanings_dict, {"meanings": [repeated]})[1], [])
        self.assertEqual(meanings_dict, {WORD_KEY: generated_meanings()})
        # A failed call likewise
        self.assertEqual(Calls.add(meanings_dict, {"no": "meanings"})[1], [])
        self.assertEqual(meanings_dict, {WORD_KEY: generated_meanings()})


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

    def meaning(self) -> tuple[str, str]:
        return self.fields["meaning_field"], self.fields["english_meaning_field"]


NEW_MEANING = ("新しい意味。", "new meaning")


def rework_answer(same_sense_as: int = 0) -> dict:
    jp, en = NEW_MEANING
    return {"jp_meaning": jp, "en_meaning": en, "same_sense_as": same_sense_as}


def map_answer(index: int, score: int, same_sense_as: int = 0) -> dict:
    return {"possible_meaning_index": index, "mapping_score": score, "same_sense_as": same_sense_as}


class Cleaning:
    """What one clean_meaning_in_note did: its result, and the calls each module made."""

    def __init__(self, result: Any, calls: Recorder, adds: Recorder, to_update: dict):
        self.result = result
        self.calls = calls
        self.adds = adds
        self.to_update = to_update

    def kinds(self) -> list[str]:
        return [kwargs["kind"] for _, _, kwargs in self.calls.calls + self.adds.calls]


class CleanNoteTests(unittest.TestCase):
    """clean_meaning_in_note over fake notes: it writes the note it is given and no other, and
    the context of each call it makes names that note."""

    CONFIG = {
        "Word": {key: key for key in WordNote(0, "", "").fields},
        "word_meaning_model": "model",
        "make_meanings_model": "model",
    }

    def clean(
        self,
        note: WordNote,
        others: list,
        entry,
        *answers: Any,
        given: bool = False,
        generated: Any = None,
        added: Any = None,
        to_update: Any = None,
        sentence_count: int = 1,
        **kwargs: Any,
    ) -> Cleaning:
        """`others` fetched as the meaning group, or `given` by the caller as the match op's
        CREATE NEW gives them; `answers` clean_meaning's, `added` make_all_meanings.add's;
        `generated` the word's generated meanings, or the dict that holds them; each note in
        `sentence_count` sentences."""
        if isinstance(generated, list):
            generated = {WORD_KEY: generated}
        calls = Recorder(*answers) if answers else Recorder(None)
        adds = Recorder({"meanings": [] if added is None else added})
        to_update = {} if to_update is None else to_update

        def sentences(config, n: WordNote, **kwargs) -> list[dict]:
            return [
                {"jp_sentence": f"{n['sentence_field']}{i or ''}", "en_sentence": ""}
                for i in range(sentence_count)
            ]

        def fetch(**kwargs):
            if given:
                raise AssertionError("the caller's notes replace the fetch")
            return others

        with (
            no_mdx_load(),
            mdx_entry(entry),
            mock.patch.object(cm, "get_other_meaning_notes", fetch),
            mock.patch.object(cm, "get_sentences_for_note", sentences),
            mock.patch.object(cm, "get_response", calls),
            mock.patch.object(mam, "get_response", adds),
        ):
            result = cm.clean_meaning_in_note(
                config=self.CONFIG,
                note=note,
                notes_to_add_dict={},
                notes_to_update_dict=to_update,
                all_generated_meanings_dict=generated if generated is not None else {},
                other_meaning_notes=others if given else None,
                **kwargs,
            )
        return Cleaning(result, calls, adds, to_update)

    def siblings(self) -> tuple[WordNote, WordNote]:
        pull = WordNote(PULL_NOTE, "物に手をかけて近くへ寄せる。", "to pull")
        draw = WordNote(DRAW_NOTE, "線を描く。", "to draw")
        return pull, draw

    def assert_as_they_were(self, *notes: WordNote) -> None:
        pull, draw = self.siblings()
        before = {PULL_NOTE: pull.meaning(), DRAW_NOTE: draw.meaning()}
        for note in notes:
            self.assertEqual(note.meaning(), before[note.id])
            self.assertEqual(note.tags, [])

    def test_a_reworked_meaning_goes_to_the_note_and_no_other(self):
        # The meaning group was reworked with the note and written back, the studied notes of a
        # word reworded whenever one of its notes was cleaned: by the add-note hook, the clean
        # op and the match op's loops, on every target of the word
        pull, draw = self.siblings()
        # Made by this run and not added yet: known by its placeholder, listed after the added
        lots = WordNote(0, "くじを抜く。", "to draw lots", placeholder="-5550001")
        note = WordNote(1712000000003, "引っ張る。", "to tug")

        cleaning = self.clean(note, [lots, draw, pull], ENTRY, rework_answer())

        _, kwargs = cleaning.calls.one()
        self.assertEqual(kwargs["kind"], "clean_meaning.rework_note")
        self.assertEqual(
            kwargs["context"],
            {"target_note_id": note.id, "other_note_ids": [PULL_NOTE, DRAW_NOTE, -5550001]},
        )
        listed = [m["en_meaning"] for m in kwargs["inputs"]["others"]]
        self.assertEqual(listed, ["to pull", "to draw", "to draw lots"])
        self.assertEqual(note.meaning(), NEW_MEANING)
        self.assertEqual(cleaning.result, cm.CleanResult(True, None))
        self.assertEqual(cleaning.to_update, {note.id: note})
        self.assert_as_they_were(pull, draw)
        self.assertEqual(lots.meaning(), ("くじを抜く。", "to draw lots"))

    def test_the_notes_a_caller_gives_are_shown_and_left_as_they_were(self):
        pull, draw = self.siblings()
        new = WordNote(0, "くじを抜く。", "to draw lots", placeholder="-5550009")

        cleaning = self.clean(new, [pull, draw], ENTRY, rework_answer(), given=True)

        _, kwargs = cleaning.calls.one()
        self.assertEqual(
            kwargs["context"], {"target_note_id": -5550009, "other_note_ids": [PULL_NOTE, DRAW_NOTE]}
        )
        self.assertEqual(kwargs["inputs"]["target"]["en_meaning"], "to draw lots")
        self.assertEqual(new.meaning(), NEW_MEANING)
        self.assert_as_they_were(pull, draw)
        # A note not added is saved by being added, never through the edited notes
        self.assertEqual(cleaning.to_update, {})

    def test_the_rework_names_the_note_whose_sense_the_note_repeats(self):
        pull, draw = self.siblings()
        new = WordNote(0, "線を引く。", "to draw a line", placeholder="-5550009")

        cleaning = self.clean(new, [pull, draw], ENTRY, rework_answer(same_sense_as=2), given=True)

        self.assertIs(cleaning.result.same_sense_as, draw)
        self.assertIn(f"meaning_same_sense_as::{DRAW_NOTE}", new.tags)
        self.assert_as_they_were(pull, draw)

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
                    _, kwargs = self.clean(note, [], entry, answer).calls.one()

                    self.assertEqual(kwargs["kind"], kind)
                    self.assertEqual(kwargs["context"], {"note_id": note.key()})
                    self.assertEqual(note.meaning(), (jp, en))

    def test_a_failed_call_leaves_the_note_as_it_was(self):
        # A failed extraction wrote the whole dictionary entry into the meaning, and a failed
        # generation emptied both fields; the add-note hook saved the note that way
        for entry, others in ((ENTRY, []), (None, []), (ENTRY, list(self.siblings()))):
            with self.subTest(entry=bool(entry), others=len(others)):
                note = WordNote(1712000000003, "引っ張る。", "to tug")

                cleaning = self.clean(note, others, entry, None)

                self.assertEqual(len(cleaning.calls.calls), 1)
                self.assertEqual(note.meaning(), ("引っ張る。", "to tug"))
                self.assertFalse(cleaning.result.changed)

    def test_a_note_is_mapped_alone_with_the_others_shown_by_the_meaning_they_use(self):
        # Shown as ALREADY MAPPED, a sibling's possible meaning was one the prompt forbade the
        # note, so a note whose sense a sibling held was pushed onto a wrong meaning
        pull, draw = self.siblings()
        new = WordNote(0, "くじを抜く。", "to draw lots", placeholder="-5550009")

        cleaning = self.clean(
            new, [pull, draw], ENTRY, map_answer(3, 5), given=True, generated=generated_meanings()
        )

        prompt, kwargs = cleaning.calls.one()
        self.assertEqual(kwargs["kind"], "clean_meaning.map_note")
        self.assertEqual(
            kwargs["context"],
            {"target_note_id": -5550009, "other_note_ids": [PULL_NOTE, DRAW_NOTE], "depth": 0},
        )
        self.assertIn("Other note 1 (uses possible meaning 1):", prompt)
        self.assertIn("Other note 2 (not mapped):", prompt)
        self.assertNotIn("ALREADY MAPPED", prompt)
        self.assertEqual(new["english_meaning_field"], "to draw (lots)")
        self.assertIn(cm.MEANING_MAPPED_TAG, new.tags)
        self.assertIn("meaning_mapping_score::5", new.tags)
        self.assertEqual(cleaning.result, cm.CleanResult(True, None))
        self.assert_as_they_were(pull, draw)

    def test_a_note_mapped_already_is_not_mapped_again(self):
        # A new note copied from a generated meaning holds it already: a sibling never mapped is
        # no reason for a call, which could only remap the sibling
        pull, draw = self.siblings()
        new = WordNote(0, "くじなどを抜き取る。", "to draw (lots)", placeholder="-5550009")

        cleaning = self.clean(new, [pull, draw], ENTRY, given=True, generated=generated_meanings())

        self.assertEqual(cleaning.calls.calls, [])
        self.assertEqual(cleaning.result, cm.CleanResult(False, None))
        self.assertEqual(new.tags, [cm.MEANING_MAPPED_TAG])
        self.assert_as_they_were(pull, draw)

    def test_a_note_mapped_to_the_meaning_another_note_holds_is_its_duplicate(self):
        # The match op's CREATE NEWs made most of the collection's notes that repeat another
        # note's meaning, and nothing compared the two. Taking the generated meaning a note holds
        # says so without the prompt's word for it
        pull, draw = self.siblings()
        new = WordNote(0, "物を引き寄せる。", "to pull toward oneself", placeholder="-5550009")

        cleaning = self.clean(
            new, [pull, draw], ENTRY, map_answer(1, 5), given=True, generated=generated_meanings()
        )

        self.assertIs(cleaning.result.same_sense_as, pull)
        self.assertEqual(new["english_meaning_field"], "to pull")
        self.assertIn(f"meaning_same_sense_as::{PULL_NOTE}", new.tags)
        self.assert_as_they_were(pull, draw)

        # The prompt's word for it names the note too
        new = WordNote(0, "物を引き寄せる。", "to pull toward oneself", placeholder="-5550009")
        cleaning = self.clean(
            new,
            [pull, draw],
            ENTRY,
            map_answer(2, 4, same_sense_as=2),
            given=True,
            generated=generated_meanings(),
        )
        self.assertIs(cleaning.result.same_sense_as, draw)

    def test_a_low_score_adds_a_meaning_and_maps_again_keeping_the_others(self):
        # The revision rewrote the whole list for the one note: in replays, three quarters of
        # the other notes' meanings were dropped from it, and a note mapped to a dropped one
        # keeps it with nothing to say so
        pull, draw = self.siblings()
        draw["english_meaning_field"] = "to draw (a line)"
        new = WordNote(0, "くじを引き当てる。", "to pick a lot", placeholder="-5550009")
        meanings = {WORD_KEY: generated_meanings()}

        cleaning = self.clean(
            new,
            [pull, draw],
            ENTRY,
            map_answer(3, 2),
            map_answer(4, 5),
            given=True,
            generated=meanings,
            added=[ADDED],
        )

        self.assertEqual(
            cleaning.kinds(),
            ["clean_meaning.map_note", "clean_meaning.map_note", "make_all_meanings.add"],
        )
        self.assertEqual(meanings[WORD_KEY], generated_meanings() + [ADDED])
        # Asked for the note's use alone, against the list as it was
        _, _, add = cleaning.adds.calls[0]
        self.assertEqual(add["inputs"]["usage"]["en_meaning"], "to pick a lot")
        self.assertEqual(add["inputs"]["possible_meanings"], generated_meanings())
        # Mapped again against the longer list, one level down
        (_, _, first), (_, _, second) = cleaning.calls.calls
        self.assertEqual([first["context"]["depth"], second["context"]["depth"]], [0, 1])
        self.assertEqual(second["inputs"]["possible_meanings"], generated_meanings() + [ADDED])
        self.assertEqual(new["english_meaning_field"], ADDED["en_meaning"])
        self.assertIn("meaning_mapping_score::5", new.tags)
        self.assertEqual(pull.meaning()[1], "to pull")
        self.assertEqual(draw.meaning()[1], "to draw (a line)")
        self.assertEqual((pull.tags, draw.tags), ([], []))

    def test_a_note_no_meaning_fits_is_left_as_it_was(self):
        pull, draw = self.siblings()
        new = WordNote(0, "名前。", "a name", placeholder="-5550009")
        meanings = {WORD_KEY: generated_meanings()}

        # Nothing to add, and the score stays 2
        cleaning = self.clean(
            new, [pull, draw], ENTRY, map_answer(3, 2), given=True, generated=meanings
        )

        self.assertEqual(cleaning.kinds(), ["clean_meaning.map_note", "make_all_meanings.add"])
        self.assertEqual(meanings, {WORD_KEY: generated_meanings()})
        self.assertEqual(new.meaning(), ("名前。", "a name"))
        self.assertEqual(new.tags, [])
        self.assertEqual(cleaning.result, cm.CleanResult(False, None))

    def test_the_loops_leave_a_note_with_nothing_to_map_to_as_it_was(self):
        # A note whose word has no generated meanings was reworked with its group instead, which
        # left nothing to say so: it stayed unmapped, and every target of its word reworked the
        # group again, in every run
        pull, draw = self.siblings()
        note = WordNote(1712000000003, "引っ張る。", "to tug")

        cleaning = self.clean(
            note, [pull, draw], ENTRY, rework_answer(), map_only=True, allow_reupdate_existing=True
        )

        self.assertEqual(cleaning.calls.calls, [])
        self.assertEqual(note.meaning(), ("引っ張る。", "to tug"))
        self.assertEqual(cleaning.to_update, {})
        self.assert_as_they_were(pull, draw)

        # With generated meanings, mapped as ever
        cleaning = self.clean(
            note,
            [pull, draw],
            ENTRY,
            map_answer(1, 5),
            generated=generated_meanings(),
            map_only=True,
            allow_reupdate_existing=True,
        )
        self.assertEqual(cleaning.kinds(), ["clean_meaning.map_note"])
        self.assertEqual(note.meaning()[1], "to pull")

    def test_a_note_of_a_word_with_no_dictionary_entry_is_cleaned_by_the_clean_op(self):
        # The note's own no-entry tag put it among the notes the run had edited, and the guard
        # against cleaning one of those again, read after it, skipped every such note
        pull, draw = self.siblings()
        note = WordNote(1712000000003, "引っ張る。", "to tug")

        cleaning = self.clean(note, [pull, draw], None, rework_answer())

        self.assertEqual(cleaning.kinds(), ["clean_meaning.rework_note"])
        self.assertEqual(note.meaning(), NEW_MEANING)
        self.assertIn(cm.NO_DICTIONARY_ENTRY_TAG, note.tags)
        self.assertEqual(cleaning.to_update, {note.id: note})

    def test_a_note_the_run_edited_is_cleaned_again_only_when_allowed(self):
        pull, _ = self.siblings()
        note = WordNote(1712000000003, "引っ張る。", "to tug")
        edited = WordNote(note.id, "引っ張る。", "to tug; to pull hard")

        cleaning = self.clean(note, [pull], ENTRY, rework_answer(), to_update={note.id: edited})
        self.assertEqual(cleaning.calls.calls, [])
        self.assertEqual(cleaning.result, cm.NOT_CHANGED)

        # Allowed, it is the run's edited copy that is cleaned
        cleaning = self.clean(
            note,
            [pull],
            ENTRY,
            rework_answer(),
            to_update={note.id: edited},
            allow_reupdate_existing=True,
        )
        _, kwargs = cleaning.calls.one()
        self.assertEqual(kwargs["inputs"]["target"]["en_meaning"], "to tug; to pull hard")
        self.assertEqual(edited.meaning(), NEW_MEANING)

    def test_changed_says_whether_the_note_changed(self):
        pull, _ = self.siblings()
        jp, en = NEW_MEANING
        note = WordNote(1712000000003, jp, en)

        cleaning = self.clean(note, [pull], ENTRY, rework_answer())

        self.assertFalse(cleaning.result.changed)
        # Tagged, so saved all the same
        self.assertEqual(note.tags, ["updated_jp_meaning"])
        self.assertEqual(cleaning.to_update, {note.id: note})

    def test_the_prompts_show_a_few_sentences_of_each_other_note(self):
        # A sibling of あの linked from 135 sentences made one mapping 30,000 characters long.
        # The note's own sentences are all shown
        pull, draw = self.siblings()
        cases = ((None, rework_answer()), (generated_meanings(), map_answer(1, 5)))
        for generated, answer in cases:
            with self.subTest(mapped=generated is not None):
                note = WordNote(1712000000003, "引っ張る。", "to tug")

                cleaning = self.clean(
                    note, [pull, draw], ENTRY, answer, generated=generated, sentence_count=5
                )

                _, kwargs = cleaning.calls.one()
                self.assertEqual(len(kwargs["inputs"]["target"]["sentences"]), 5)
                others = kwargs["inputs"]["others"]
                self.assertEqual([len(o["sentences"]) for o in others], [3, 3])
                # Each note's own sentence first
                self.assertEqual(others[0]["sentences"][0]["jp_sentence"], pull["sentence_field"])

    def test_a_note_not_added_with_no_placeholder_is_cleaned_under_id_0(self):
        # A vocab note added by hand reaches the add-note hook with id 0 and an empty new note
        # id field; reading that field as a placeholder raised and cost the note its cleaning
        jp, en = NEW_MEANING
        for kind, entry, answer in (
            ("clean_meaning.extract", ENTRY, {"cleaned_meaning": jp, "english_meaning": en}),
            ("clean_meaning.generate", None, {"new_meaning": jp, "english_meaning": en}),
        ):
            with self.subTest(kind=kind):
                note = WordNote(0, "", "")
                _, kwargs = self.clean(note, [], entry, answer).calls.one()

                self.assertEqual(kwargs["kind"], kind)
                self.assertEqual(kwargs["context"], {"note_id": 0})
                self.assertEqual(note.meaning(), (jp, en))

    def test_a_note_not_added_with_no_placeholder_takes_its_reworked_meaning(self):
        pull, _ = self.siblings()
        added = WordNote(0, "くじを抜く。", "to draw lots")

        _, kwargs = self.clean(added, [pull], ENTRY, rework_answer()).calls.one()

        self.assertEqual(kwargs["context"], {"target_note_id": 0, "other_note_ids": [PULL_NOTE]})
        self.assertEqual(added.meaning(), NEW_MEANING)
        self.assert_as_they_were(pull)


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


