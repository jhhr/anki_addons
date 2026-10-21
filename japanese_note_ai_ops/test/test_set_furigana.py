"""Set furigana from audio (async_api_ops/set_furigana.py): which notes are written, the
furigana and review tag written, the arbiter's models asked about the words left in review, the
show a note is of and the names read from its captions and the names file. How a caption becomes
furigana is test_word_array_audio_furigana.py's, how the arbiter's answers are read
test_word_array_furigana_arbiter.py's.

Nothing here tokenizes with Sudachi: a stand-in segments the few lines the tests use the way
Sudachi does. The transcripts are written as the kana models write them (ひとわ)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from addon_modules import load_ops_module

op = load_ops_module("set_furigana")
run_errors = load_ops_module("run_errors")
arbiter = load_ops_module("furigana_arbiter", subdir="word_array")

SENTENCE = "Sentence note"
CONFIG: dict = {
    SENTENCE: {
        "word_list_field": "Words",
        "sentence_field": "Sentence",
        "furigana_sentence_field": "Furigana",
        "sentence_transcription_field": "Heard",
    },
    "transcribe_models": ["local-kana-anime-whisper", "local-ruby-mora"],
}

NOUN = ("名詞", "普通名詞", "一般", "*", "*", "*")
PARTICLE = ("助詞", "格助詞", "*", "*", "*", "*")
LEXICON = {"他": ("ホカ", NOUN), "人": ("ヒト", NOUN), "猫": ("ネコ", NOUN)}
DICTIONARY = {"他": {"ほか", "た"}, "人": {"ひと", "にん", "じん"}}


class Morpheme:
    def __init__(self, surface, reading, pos, begin):
        self._surface, self._reading, self._pos, self._begin = surface, reading, pos, begin

    def surface(self):
        return self._surface

    def reading_form(self):
        return self._reading

    def part_of_speech(self):
        return self._pos

    def begin(self):
        return self._begin

    def end(self):
        return self._begin + len(self._surface)


def tokenize(text):
    """Longest match over LEXICON; any other character is a morpheme of its own, read as
    itself and a particle (は read ワ)."""
    out, i = [], 0
    while i < len(text):
        for n in range(len(text) - i, 0, -1):
            piece = text[i : i + n]
            if piece in LEXICON:
                reading, pos = LEXICON[piece]
                out.append(Morpheme(piece, reading, pos, i))
                i += n
                break
        else:
            out.append(Morpheme(text[i], text[i], PARTICLE, i))
            i += 1
    return out


def dictionary(form):
    return DICTIONARY.get(form, set())


class Note:
    def __init__(self, fields: dict[str, str], tags=(), note_id: int = 7) -> None:
        self.id = note_id
        self.fields = {"Sentence": "", "Furigana": "", "Heard": "", **fields}
        # Sorted, as Anki keeps a note's tags
        self.tags = sorted(tags, key=str.lower)

    def note_type(self):
        return {"name": SENTENCE}

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]

    def __setitem__(self, field: str, value: str) -> None:
        self.fields[field] = value

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def add_tag(self, tag: str) -> None:
        if tag not in self.tags:
            self.tags.append(tag)

    def remove_tag(self, tag: str) -> None:
        self.tags = [t for t in self.tags if t != tag]


def heard(kana_anime_whisper: str, ruby_mora: str) -> str:
    return f"kana-anime-whisper: {kana_anime_whisper}<br>ruby-mora: {ruby_mora}"


LINE = "他の人はどこにいるの"
# A line long enough that 猫猫 read ねこねこ in it still lines up with まおまお heard
NAME_LINE = "猫猫がいたのはどこなのかしら"
NAME_HEARD = heard("まおまおがいたのわどこなのかしら", "まおまおがいたのわどこなのかしら")


class OpHarness(unittest.TestCase):
    def setUp(self):
        self.reports: list[str] = []
        deliver = run_errors._deliver
        run_errors.deliver_with(lambda _title, text: self.reports.append(text))
        self.addCleanup(run_errors.deliver_with, deliver)
        # The captions of each show's notes, as the collection would give them
        self.captions: dict[str, list[str]] = {}
        self.asked: list[tuple] = []
        self.user_names: dict = {}

    def captions_of(self, show, note_type_name, field):
        self.asked.append((show, note_type_name, field))
        return self.captions.get(show, [])

    def shows(self):
        return op.Shows(self.captions_of, dictionary, self.user_names)

    def run_op(self, *notes, shows=None, config=CONFIG, arbiters=()):
        shows = shows or self.shows()
        updated: dict = {}
        changed = [
            op.set_furigana_in_note(
                config,
                note,
                {},
                updated,
                shows=shows,
                tokenize=tokenize,
                dictionary=dictionary,
                arbiters=arbiters,
            )
            for note in notes
        ]
        return changed, updated


class OpTest(OpHarness):
    def test_furigana_as_the_clip_says_it_and_no_review(self):
        note = Note({"Sentence": "（白鈴）" + LINE, "Heard": heard("たのひとわどこにいるの", "そのひとわどこにいるの")})
        changed, updated = self.run_op(note)
        self.assertEqual((changed, updated), ([True], {7: note}))
        self.assertEqual(note["Furigana"], "他[た]の 人[ひと]はどこにいるの")
        self.assertEqual(note.tags, [])

    def test_a_word_left_unsettled_tags_the_note_for_review(self):
        note = Note({"Sentence": LINE, "Heard": heard("ほかのひとわどこにいるの", "たのひとわどこにいるの")})
        self.assertEqual(self.run_op(note)[0], [True])
        self.assertEqual(note["Furigana"], "他[ほか？た]の 人[ひと]はどこにいるの")
        self.assertEqual(note.tags, [op.REVIEW_TAG])

    def test_a_review_tag_goes_when_the_furigana_is_set_again_and_settled(self):
        said = "ほかのひとわどこにいるの"
        note = Note({"Sentence": LINE, "Heard": heard(said, said)}, tags=[op.REVIEW_TAG])
        self.run_op(note)
        self.assertEqual(note.tags, [])

    def test_furigana_already_there_is_never_replaced(self):
        said = "ほかのひとわどこにいるの"
        note = Note({"Sentence": LINE, "Furigana": "他[た]の", "Heard": heard(said, said)})
        self.assertEqual(self.run_op(note), ([False], {}))
        self.assertEqual(note["Furigana"], "他[た]の")

    def test_a_note_lacking_a_transcript_is_left_and_reported(self):
        note = Note({"Sentence": LINE, "Heard": "kana-anime-whisper: ほかのひとわどこにいるの"})
        self.assertEqual(self.run_op(note), ([False], {}))
        self.assertEqual(note["Furigana"], "")
        self.assertIn("No transcript from ruby-mora yet", self.reports[0])

    def test_the_furigana_field_must_be_another_than_the_sentences(self):
        config = {SENTENCE: {**CONFIG[SENTENCE], "furigana_sentence_field": "Sentence"}}
        note = Note({"Sentence": LINE})
        shows = self.shows()
        changed = op.set_furigana_in_note(
            config, note, {}, {}, shows=shows, tokenize=tokenize, dictionary=dictionary
        )
        self.assertFalse(changed)
        self.assertIn("name another field", self.reports[0])

    def test_a_type_without_a_field_configured_is_reported(self):
        config = {SENTENCE: {"word_list_field": "Words"}}
        changed = op.set_furigana_in_note(
            config, Note({}), {}, {}, shows=self.shows(), tokenize=tokenize, dictionary=dictionary
        )
        self.assertFalse(changed)
        self.assertIn("sentence_field", self.reports[0])

    def test_a_name_the_shows_captions_read_elsewhere(self):
        # The episode's first mention gave 猫猫 its reading; this one has none
        self.captions["show"] = ["猫猫(マオマオ)", NAME_LINE]
        # Whatever other tags it has, one of them sorted first
        tags = ["JLPT::N3", "anime::seinen", "ep::show::01", "sentence::regenerate"]
        note = Note({"Sentence": NAME_LINE, "Heard": NAME_HEARD}, tags=tags)
        self.assertEqual(note.tags[0], "anime::seinen")
        self.run_op(note)
        self.assertEqual(note["Furigana"], "猫猫[まおまお]がいたのはどこなのかしら")
        self.assertEqual(note.tags, sorted(tags, key=str.lower))
        self.assertEqual(self.asked, [("show", SENTENCE, "Sentence")])

    def test_a_name_in_the_names_file_wins_over_the_captions(self):
        self.captions["show"] = ["猫猫(ネコネコ)"]
        self.user_names = {"show": {"猫猫": "まおまお"}}
        note = Note({"Sentence": NAME_LINE, "Heard": NAME_HEARD}, tags=["ep::show::01"])
        self.run_op(note)
        self.assertEqual(note["Furigana"], "猫猫[まおまお]がいたのはどこなのかしら")

    def test_a_show_is_read_once_a_run_and_a_note_of_none_by_its_own_caption(self):
        notes = [
            Note({"Sentence": NAME_LINE, "Heard": NAME_HEARD}, tags=["ep::show::01"]),
            Note({"Sentence": NAME_LINE, "Heard": NAME_HEARD}, tags=["ep::show::02"]),
            Note({"Sentence": "猫猫(マオマオ)", "Heard": heard("まおまお", "まおまお")}),
        ]
        self.run_op(*notes)
        self.assertEqual(self.asked, [("show", SENTENCE, "Sentence")])
        self.assertEqual(notes[2]["Furigana"], "猫猫[まおまお]")

    def test_the_notes_read_with_no_show_are_kept_for_the_runs_one_report(self):
        shows = self.shows()
        notes = [
            # As subs2srs' default export tags a card
            Note({"Sentence": NAME_LINE, "Heard": NAME_HEARD}, ["JP_sentences_33"], note_id=1),
            Note({"Sentence": NAME_LINE, "Heard": NAME_HEARD}, ["ep::a::01", "ep::b::01"], 2),
            Note({"Sentence": NAME_LINE, "Heard": NAME_HEARD}, ["ep::show::01"], note_id=3),
            # Not read at all: its furigana is there already
            Note({"Sentence": NAME_LINE, "Furigana": "猫猫[ねこねこ]", "Heard": NAME_HEARD}, (), 4),
        ]
        self.run_op(*notes, shows=shows)
        self.assertEqual(sorted(shows.showless), [1, 2])
        self.assertEqual(self.reports, [])
        report = op.no_show_report(shows.showless)
        self.assertTrue(report.startswith("2 notes have no tag ep::<show>::<episode>"))
        self.assertTrue(report.endswith("search: nid:1,2"))


def answer(reading, sure=True):
    return {"words": [{"n": 1, "why": "", "reading": reading, "sure": sure}]}


class ArbiterTest(OpHarness):
    MODELS = ["first-model", "second-model"]
    # 他 heard two ways: the audio leaves it to the arbiter
    TWO_WAYS = heard("ほかのひとわどこにいるの", "たのひとわどこにいるの")

    def ask(self, note, answers, config=CONFIG):
        """The op with each model answering as `answers` says; the calls made."""
        calls: list = []

        def get_response(model, prompt, **kwargs):
            calls.append((model, prompt, kwargs))
            return answers.get(model)

        with mock.patch.object(op, "get_response", get_response):
            self.run_op(note, config=config, arbiters=self.MODELS)
        return calls

    def test_a_reading_every_model_gives_is_written(self):
        config = {SENTENCE: {**CONFIG[SENTENCE], "translated_sentence_field": "English"}}
        fields = {"Sentence": LINE, "Heard": self.TWO_WAYS, "English": "Where are the<br>others?"}
        note = Note(fields, tags=[op.REVIEW_TAG])
        calls = self.ask(note, {m: answer("タ") for m in self.MODELS}, config)
        self.assertEqual(note["Furigana"], "他[た]の 人[ひと]はどこにいるの")
        self.assertEqual(note.tags, [])
        self.assertEqual([model for model, _, _ in calls], self.MODELS)
        for _, prompt, kwargs in calls:
            self.assertEqual(kwargs["kind"], "furigana_arbiter.note")
            self.assertEqual(kwargs["response_schema"], arbiter.RESPONSE_SCHEMA)
            self.assertEqual(arbiter.review_prompt(**kwargs["inputs"]), prompt)
        inputs = calls[0][2]["inputs"]
        self.assertEqual(inputs["translation"], "Where are the\nothers?")
        self.assertEqual([w["context"] for w in inputs["words"]], ["［他］の人はどこにいるの"])

    def test_models_that_differ_leave_the_word_in_review(self):
        note = Note({"Sentence": LINE, "Heard": self.TWO_WAYS})
        self.ask(note, {"first-model": answer("た"), "second-model": answer("ほか")})
        self.assertEqual(note["Furigana"], "他[ほか？た]の 人[ひと]はどこにいるの")
        self.assertEqual(note.tags, [op.REVIEW_TAG])

    def test_a_failed_call_settles_nothing_and_asks_no_further(self):
        note = Note({"Sentence": LINE, "Heard": self.TWO_WAYS})
        calls = self.ask(note, {"second-model": answer("た")})
        self.assertEqual([model for model, _, _ in calls], ["first-model"])
        self.assertEqual(note.tags, [op.REVIEW_TAG])

    def test_no_word_in_review_asks_nothing(self):
        said = heard("ほかのひとわどこにいるの", "ほかのひとわどこにいるの")
        self.assertEqual(self.ask(Note({"Sentence": LINE, "Heard": said}), {}), [])

    def test_a_word_nothing_was_heard_at_asks_nothing(self):
        # Both transcripts pass over 他: no answer could be written, so no model is asked
        skipped = heard("のひとわどこにいるの", "のひとわどこにいるの")
        note = Note({"Sentence": LINE, "Heard": skipped})
        self.assertEqual(self.ask(note, {m: answer("ほか") for m in self.MODELS}), [])
        self.assertEqual(note.tags, [op.REVIEW_TAG])

    def test_the_models_of_the_config(self):
        self.assertEqual(op.arbiter_models({"furigana_models": [" a ", "", 3, "b"]}), ["a", "b"])
        self.assertEqual(op.arbiter_models({"furigana_models": "a"}), ["a"])
        self.assertEqual(op.arbiter_models({}), [])


class NewNotesTest(unittest.TestCase):
    """What "Run all ops for new notes" asks of a note."""

    config: dict = {
        SENTENCE: {**CONFIG[SENTENCE], "sentence_audio_field": "Audio"},
        "transcribe_models": CONFIG["transcribe_models"],
    }

    def test_a_note_with_a_clip_and_a_sentence_but_no_furigana_yet(self):
        def wants(**fields):
            base = {"Sentence": LINE, "Audio": "[sound:clip.opus]"}
            return op.wants_audio_furigana(self.config, Note({**base, **fields}))

        self.assertTrue(wants())
        self.assertFalse(wants(Furigana="他[ほか]の 人[ひと]は"))
        self.assertFalse(wants(Audio=""))
        self.assertFalse(wants(Sentence=" "))
        # A type keeping no transcripts has its furigana from elsewhere
        no_transcripts = {SENTENCE: {**self.config[SENTENCE], "sentence_transcription_field": ""}}
        note = Note({"Sentence": LINE, "Audio": "[sound:clip.opus]"})
        self.assertFalse(op.wants_audio_furigana(no_transcripts, note))

    def test_furigana_awaiting_a_check_by_tag_or_by_a_word_still_marked(self):
        checked = Note({"Furigana": "他[ほか]の 人[ひと]は？"})
        tagged = Note({"Furigana": "他[ほか]の"}, tags=[op.REVIEW_TAG])
        marked = Note({"Furigana": "他[ほか？た]の"})
        self.assertIsNone(op.unchecked_furigana(CONFIG, checked))
        self.assertIn("furigana-review", op.unchecked_furigana(CONFIG, tagged))
        self.assertIn('"？"', op.unchecked_furigana(CONFIG, marked))


class ShowTest(unittest.TestCase):
    class Tagged:
        def __init__(self, *tags):
            # Sorted, as Anki keeps a note's tags
            self.tags = sorted(tags, key=str.lower)

    def test_the_show_of_the_episode_tag_whatever_sorts_before_it(self):
        tags = ("new", "ep::kusuri::05", "JLPT::N3", "freq::high", "sentence::regenerate")
        self.assertEqual(op.show_of(self.Tagged(*tags)), "kusuri")
        self.assertEqual(op.show_of(self.Tagged("ep::kusuri::s2::05")), "kusuri")

    def test_no_show_without_an_episode_tag(self):
        for tags in [(), ("JP_sentences_33",), ("kusuri::05",), ("ep::kusuri",), ("epi::a::01",)]:
            with self.subTest(tags):
                self.assertIsNone(op.show_of(self.Tagged(*tags)))

    def test_no_show_when_the_tags_name_two(self):
        self.assertIsNone(op.show_of(self.Tagged("ep::a::01", "ep::b::01")))
        # One show in two cases is one show to Anki, whose tags ignore case
        self.assertEqual(op.show_of(self.Tagged("EP::Kusuri::01", "ep::kusuri::02")), "Kusuri")

    def test_the_shows_search_escapes_what_anki_reads_as_syntax(self):
        self.assertEqual(op.show_search("show"), '"tag:ep::show::*"')
        self.assertEqual(op.show_search('a_b*"c'), '"tag:ep::a\\_b\\*\\"c::*"')
        # A colon as every search's names have it (note_roles.search_escape)
        self.assertEqual(op.show_search("re:zero"), '"tag:ep::re\\:zero::*"')

    def test_the_report_of_the_notes_of_no_show_names_them_in_one_search(self):
        self.assertIsNone(op.no_show_report([]))
        self.assertIn("1 note has no tag", op.no_show_report([5]))
        report = op.no_show_report(range(1, 302))
        self.assertIn("301 notes have", report)
        self.assertTrue(report.endswith(",200 (the first 200 of 301 notes)"))


class NamesFileTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "show_names.json"
        self.reports: list[str] = []
        deliver = run_errors._deliver
        run_errors.deliver_with(lambda _title, text: self.reports.append(text))
        self.addCleanup(run_errors.deliver_with, deliver)

    def test_names_by_show_read_in_hiragana(self):
        names = {"show": {"響迂": "キョウウ", "高順": "がおしゅん", "x": ""}, "other": "?"}
        self.path.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(
            op.read_names_file(str(self.path)),
            {"show": {"響迂": "きょうう", "高順": "がおしゅん"}},
        )

    def test_no_file_is_no_names(self):
        self.assertEqual(op.read_names_file(str(self.path)), {})
        self.assertEqual(self.reports, [])

    def test_a_file_that_is_not_json_is_reported(self):
        self.path.write_text("{", encoding="utf-8")
        self.assertEqual(op.read_names_file(str(self.path)), {})
        self.assertIn("could not be read", self.reports[0])


if __name__ == "__main__":
    unittest.main()
