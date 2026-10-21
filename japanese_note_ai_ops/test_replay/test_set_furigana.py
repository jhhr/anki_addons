"""Set furigana from audio (async_api_ops/set_furigana.py) in a real collection, with the real
Sudachi and JMdict, run through its NotesRunSpec as a script runs it: the furigana written as
the transcripts hear it, a name read from the show's other notes (the show by its `ep::` tag
among tags as Anki sorts them, its search whatever the show's name, notes of no show said
once), a note left for review tagged, a note with furigana left alone, the run one undo step,
and a word the arbiter's models agree on written. Then "Run all ops for new notes"
(async_api_ops/new_note_all_ops.py) over such notes: their furigana set from audio before
kanjify and word extraction read it, a note left for review given neither, all of it one undo
step. The models are a responder's.

The lines are made up for these tests. Skipped without the generator's resources
(word_array/resources.py), as the word array tests are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from anki.notes import NoteId

import replay
from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import base_ops, run_errors, set_furigana
from japanese_note_ai_ops.async_api_ops.new_note_all_ops import new_note_all_ops_spec
from japanese_note_ai_ops.configuration import (
    FURIGANA_REVIEW_LEFT,
    FURIGANA_REVIEW_TAG,
    REGENERATE_TAG,
)
from japanese_note_ai_ops.word_array import judge, resources

pytestmark = pytest.mark.skipif(
    not resources.is_ready(), reason="needs SudachiPy, a Sudachi dictionary and JMdict"
)

PACKAGE = "japanese_note_ai_ops"
SENTENCE = "Sentence note"
VOCAB = "Vocab note"
FIELDS = ["Sentence", "Sentence furigana", "Heard", "Words", "Audio", "Kanjified"]
CONFIG: dict[str, Any] = {
    SENTENCE: {
        "sentence_field": "Sentence",
        "furigana_sentence_field": "Sentence furigana",
        "sentence_transcription_field": "Heard",
        "word_list_field": "Words",
    },
    "transcribe_models": ["local-kana-anime-whisper", "local-ruby-mora"],
    "log_to_console": False,
}
# The two-type layout, so that a sentence note gets the sentence role's steps alone
NEW_NOTES_CONFIG = {
    **CONFIG,
    SENTENCE: {
        **CONFIG[SENTENCE],
        "vocab_note_type": VOCAB,
        "sentence_audio_field": "Audio",
        "kanjified_sentence_field": "Kanjified",
        "word_extraction_sentence_field": "Kanjified",
    },
    VOCAB: {"sentence_note_type": SENTENCE, "word_sort_field": "Word sort"},
}
# The show and episode of a note the tests make, unless one says otherwise
SHOW_TAG = "ep::show::01"


@pytest.fixture
def col(tmp_path: Path, monkeypatch) -> Iterator[Any]:
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs), stub.pm._profile_folder)
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    # "Run all ops for new notes" keeps its generated meanings in the profile's media folder
    stub.pm.set_profile_folder(tmp_path / "profile")
    # Never the names file of the checkout's own user_files
    monkeypatch.setattr(set_furigana, "NAMES_FILE", str(tmp_path / "show_names.json"))
    try:
        real_anki.make_note_type(collection, SENTENCE, FIELDS)
        real_anki.make_note_type(collection, VOCAB, ["Word sort"])
        stub.col = collection
        stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(CONFIG))
        stub.progress.set_cancel(False)
        yield collection
    finally:
        stub.progress.set_cancel(False)
        stub.col, stub.addonManager.configs, stub.pm._profile_folder = saved
        collection.close()


@pytest.fixture
def reported() -> Iterator[list]:
    """The run errors reported, as (title, text)."""
    errors: list = []
    previous = run_errors.deliver_with(lambda title, text: errors.append((title, text)))
    try:
        yield errors
    finally:
        run_errors.deliver_with(previous)


def sentence(
    col: Any, text: str, *heard: str, furigana: str = "", tags: tuple = (SHOW_TAG,)
) -> Any:
    """A sentence note with a clip, tagged with its show and episode as subs2srs' export tags
    a card (`ep::${deck_name}::${0:episode_num}`), unless `tags` says otherwise."""
    lines = "<br>".join(
        f"{name}: {kana}" for name, kana in zip(("kana-anime-whisper", "ruby-mora"), heard)
    )
    fields = {"Sentence": text, "Sentence furigana": furigana, "Heard": lines}
    return real_anki.add_note(
        col,
        SENTENCE,
        {**fields, "Audio": "[sound:clip.opus]"},
        deck_name="Sentences",
        tags=list(tags),
    )


def run(col: Any, notes: list, spec: Any = None, answer: Any = None) -> Any:
    """`spec`'s run over the notes, set furigana's by default, every model call answered by
    `answer`."""
    spec = spec or set_furigana.set_furigana_spec()
    previous = base_ops.set_responder(answer)
    try:
        # The gate's learned costs stay out of the user's memory_estimates.json
        with replay.memory_estimates({}):
            run_notes, result = spec.notes_run([NoteId(note.id) for note in notes])
            run_notes(col)
    finally:
        base_ops.set_responder(previous)
    return result


def furigana(col: Any, note: Any) -> str:
    return col.get_note(note.id)["Sentence furigana"]


def test_the_furigana_as_the_clip_says_it(col, reported):
    # 他 is ほか to Sudachi; JMdict also reads it た, which both models hear
    said = sentence(col, "（客）他の人はどこにいるの", "たのひとわどこにいるの", "タノヒトワドコニイルノ")
    unsure = sentence(col, "他の人はどこにいるの", "ほかのひとわどこにいるの", "たのひとわどこにいるの")
    typed = sentence(col, "他の人は", "たのひとわ", "たのひとわ", furigana="他[ほか]の 人[ひと]は")

    result = run(col, [said, unsure, typed])

    assert sorted(result.edited_nids) == sorted([said.id, unsure.id])
    assert furigana(col, said) == "他[た]の 人[ひと]はどこにいるの"
    assert col.get_note(said.id).tags == [SHOW_TAG]
    # The models heard it two ways: both are offered and the note left for review
    assert furigana(col, unsure) == "他[ほか？た]の 人[ひと]はどこにいるの"
    assert col.get_note(unsure.id).tags == [SHOW_TAG, FURIGANA_REVIEW_TAG]
    assert furigana(col, typed) == "他[ほか]の 人[ひと]は"
    assert reported == []
    assert col.undo_status().undo == "Setting furigana from audio for 3 notes."


def test_a_name_the_shows_other_notes_read(col, reported):
    # The show's first mention of the name read it; this note's line does not
    sentence(col, "猫猫(マオマオ)", "まおまお", "まおまお", tags=("ep::myshow::01",))
    heard = "まおまおがいたのわどこなのかしら"
    # Its other tags as Anki keeps them, sorted: two of them before the show's
    tags = ("ep::myshow::02", "JLPT::N3", "anime::seinen", REGENERATE_TAG)
    later = sentence(col, "猫猫がいたのはどこなのかしら", heard, heard, tags=tags)
    assert col.get_note(later.id).tags[:2] == ["anime::seinen", "ep::myshow::02"]
    # Another show reads the same spelling otherwise
    sentence(col, "猫猫(ネコネコ)", "ねこねこ", "ねこねこ", tags=("ep::othershow::01",))

    run(col, [later])

    assert furigana(col, later) == "猫猫[まおまお]がいたのはどこなのかしら"
    assert reported == []


def test_notes_of_no_show_are_read_alone_and_said_once(col, reported):
    # Tagged as subs2srs' default export tags a card, which names no show
    sentence(col, "猫猫(マオマオ)", "まおまお", "まおまお", tags=("JP_sentences_01",))
    heard = "まおまおがいたのわどこなのかしら"
    notes = [
        sentence(col, "猫猫がいたのはどこなのかしら", heard, heard, tags=("JP_sentences_02",)),
        sentence(col, "猫猫がいたのはどこなのかしら", heard, heard, tags=("JP_sentences_03",)),
    ]

    run(col, notes)

    # The first note's reading is no show's, so not theirs
    assert furigana(col, notes[0]) != "猫猫[まおまお]がいたのはどこなのかしら"
    [(_, text)] = reported
    assert text.startswith("2 notes have no tag ep::<show>::<episode>")
    assert text.endswith(f"search: nid:{notes[0].id},{notes[1].id}")


@pytest.mark.parametrize(
    "show",
    ["nc", "nc:café", "nc:show", "re:zero", "re:(x", "re:a+b", "a_b*c\\d", "Kusuriya",
     "薬屋のひとりごと", "-x(y)", "a::b"],
)
def test_the_shows_search_finds_its_notes_and_no_others(col, show):
    # Shows a search read as a mode or with a wildcard would find
    for other in ("show", "zero", "ab", "x", "b"):
        sentence(col, f"other {other}", "x", "x", tags=(f"ep::{other}::01",))
    note = sentence(col, "self", "x", "x", tags=(f"ep::{show}::01",))
    found = set_furigana.show_of(col.get_note(note.id))

    assert set_furigana.show_captions(found, SENTENCE, "Sentence") == ["self"]


def test_a_name_from_the_names_file(col):
    Path(set_furigana.NAMES_FILE).write_text(
        json.dumps({"myshow": {"猫猫": "マオマオ"}}, ensure_ascii=False), encoding="utf-8"
    )
    heard = "まおまおがいたのわどこなのかしら"
    note = sentence(col, "猫猫がいたのはどこなのかしら", heard, heard, tags=("ep::myshow::01",))

    run(col, [note])

    assert furigana(col, note) == "猫猫[まおまお]がいたのはどこなのかしら"


def test_a_word_the_arbiters_models_agree_on_is_written(col):
    asked: list = []

    def answer(request: Any) -> Any:
        assert request.kind == "furigana_arbiter.note"
        asked.append(request.model)
        return {"words": [{"n": 1, "why": "", "reading": "た", "sure": True}]}

    stub = real_anki.install()
    stub.addonManager.configs[PACKAGE]["furigana_models"] = ["first-model", "second-model"]
    note = sentence(col, "他の人はどこにいるの", "ほかのひとわどこにいるの", "たのひとわどこにいるの")

    run(col, [note], answer=answer)

    assert sorted(asked) == ["first-model", "second-model"]
    assert furigana(col, note) == "他[た]の 人[ひと]はどこにいるの"
    assert col.get_note(note.id).tags == [SHOW_TAG]


def test_new_notes_get_their_furigana_from_audio_before_kanjify_and_extraction(col, reported):
    stub = real_anki.install()
    stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(NEW_NOTES_CONFIG))
    asked: list = []

    def answer(request: Any) -> Any:
        asked.append((request.kind, request.inputs.get("sentence")))
        if request.kind == "kanjify.sentence":
            return {"kanjified_sentence": request.inputs["sentence"]}
        if request.kind == "proper_nouns.sentence":
            return {"proper_nouns": []}
        if request.kind == "judge.word":
            return {"reason": "", "decision": judge.DONT_MATCH}
        raise AssertionError(f"not asked: {request.kind}")

    said = sentence(col, "（客）他の人はどこにいるの", "たのひとわどこにいるの", "タノヒトワドコニイルノ")
    unsure = sentence(col, "他の人はどこにいるの", "ほかのひとわどこにいるの", "たのひとわどこにいるの")
    typed = sentence(col, "他の人は", "たのひとわ", "たのひとわ", furigana="他[ほか]の 人[ひと]は")

    run(col, [said, unsure, typed], spec=new_note_all_ops_spec(), answer=answer)

    def field(note: Any, name: str) -> str:
        return col.get_note(note.id)[name]

    # Set from the audio, then kanjified and its words extracted
    assert field(said, "Sentence furigana") == "他[た]の 人[ひと]はどこにいるの"
    assert field(said, "Kanjified") == "他[た]の 人[ひと]はどこにいるの"
    assert field(said, "Words").startswith("[")
    # Left for a person: neither kanjified nor extracted, which is said once
    assert field(unsure, "Sentence furigana") == "他[ほか？た]の 人[ひと]はどこにいるの"
    assert (field(unsure, "Kanjified"), field(unsure, "Words")) == ("", "")
    assert col.get_note(unsure.id).tags == [SHOW_TAG, FURIGANA_REVIEW_TAG]
    assert [text for _, text in reported] == [FURIGANA_REVIEW_LEFT]
    # Its furigana typed already: kept, and read as it is
    assert field(typed, "Sentence furigana") == "他[ほか]の 人[ひと]は"
    assert field(typed, "Kanjified") == "他[ほか]の 人[ひと]は"
    kanjified = sorted(text for kind, text in asked if kind == "kanjify.sentence")
    assert kanjified == sorted([field(said, "Sentence furigana"), "他[ほか]の 人[ひと]は"])
    assert col.undo_status().undo == "Running new note all ops for 3 notes."


def test_a_note_without_its_transcripts_is_left_and_reported(col, reported):
    note = sentence(col, "他の人は", "たのひとわ")

    result = run(col, [note])

    assert result.edited_nids == []
    assert furigana(col, note) == ""
    [(_, text)] = reported
    assert "No transcript from ruby-mora yet" in text
