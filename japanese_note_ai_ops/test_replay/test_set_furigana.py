"""Set furigana from audio (sync_local_ops/set_furigana.py) in a real collection, with the real
Sudachi and JMdict, run through its NotesRunSpec as a script runs it: the furigana written as
the transcripts hear it, a name read from the show's other notes, a note left for review
tagged, a note with furigana left alone, and the run one undo step.

The lines are made up for these tests. Skipped without the generator's resources
(word_array/resources.py), as the word array tests are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from anki.notes import NoteId

from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import run_errors
from japanese_note_ai_ops.sync_local_ops import set_furigana
from japanese_note_ai_ops.word_array import resources

pytestmark = pytest.mark.skipif(
    not resources.is_ready(), reason="needs SudachiPy, a Sudachi dictionary and JMdict"
)

PACKAGE = "japanese_note_ai_ops"
SENTENCE = "Sentence note"
FIELDS = ["Sentence", "Sentence furigana", "Heard", "Words"]
CONFIG = {
    SENTENCE: {
        "sentence_field": "Sentence",
        "furigana_sentence_field": "Sentence furigana",
        "sentence_transcription_field": "Heard",
        "word_list_field": "Words",
    },
    "transcribe_models": ["local-kana-anime-whisper", "local-ruby-mora"],
    "log_to_console": False,
}


@pytest.fixture
def col(tmp_path: Path, monkeypatch) -> Iterator[Any]:
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs))
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    # Never the names file of the checkout's own user_files
    monkeypatch.setattr(set_furigana, "NAMES_FILE", str(tmp_path / "show_names.json"))
    try:
        real_anki.make_note_type(collection, SENTENCE, FIELDS)
        stub.col = collection
        stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(CONFIG))
        stub.progress.set_cancel(False)
        yield collection
    finally:
        stub.progress.set_cancel(False)
        stub.col, stub.addonManager.configs = saved
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


def sentence(col: Any, text: str, *heard: str, furigana: str = "", tags: tuple = ()) -> Any:
    lines = "<br>".join(
        f"{name}: {kana}" for name, kana in zip(("kana-anime-whisper", "ruby-mora"), heard)
    )
    return real_anki.add_note(
        col,
        SENTENCE,
        {"Sentence": text, "Sentence furigana": furigana, "Heard": lines, "Words": ""},
        deck_name="Sentences",
        tags=list(tags),
    )


def run(col: Any, notes: list) -> Any:
    spec = set_furigana.set_furigana_spec()
    run_notes, result = spec.notes_run([NoteId(note.id) for note in notes])
    run_notes(col)
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
    assert col.get_note(said.id).tags == []
    # The models heard it two ways: the draft is written and the note left for review
    assert furigana(col, unsure) == "他[ほか]の 人[ひと]はどこにいるの"
    assert col.get_note(unsure.id).tags == [set_furigana.REVIEW_TAG]
    assert furigana(col, typed) == "他[ほか]の 人[ひと]は"
    assert reported == []
    assert col.undo_status().undo == "Setting furigana from audio for 3 notes."


def test_a_name_the_shows_other_notes_read(col):
    # The show's first mention of the name read it; this note's line does not
    sentence(col, "猫猫(マオマオ)", "まおまお", "まおまお", tags=("myshow::01",))
    heard = "まおまおがいたのわどこなのかしら"
    later = sentence(col, "猫猫がいたのはどこなのかしら", heard, heard, tags=("myshow::02",))
    # Another show reads the same spelling otherwise
    sentence(col, "猫猫(ネコネコ)", "ねこねこ", "ねこねこ", tags=("othershow::01",))

    run(col, [later])

    assert furigana(col, later) == "猫猫[まおまお]がいたのはどこなのかしら"


def test_a_name_from_the_names_file(col):
    Path(set_furigana.NAMES_FILE).write_text(
        json.dumps({"myshow": {"猫猫": "マオマオ"}}, ensure_ascii=False), encoding="utf-8"
    )
    heard = "まおまおがいたのわどこなのかしら"
    note = sentence(col, "猫猫がいたのはどこなのかしら", heard, heard, tags=("myshow::01",))

    run(col, [note])

    assert furigana(col, note) == "猫猫[まおまお]がいたのはどこなのかしら"


def test_a_note_without_its_transcripts_is_left_and_reported(col, reported):
    note = sentence(col, "他の人は", "たのひとわ")

    result = run(col, [note])

    assert result.edited_nids == []
    assert furigana(col, note) == ""
    [(_, text)] = reported
    assert "No transcript from ruby-mora yet" in text
