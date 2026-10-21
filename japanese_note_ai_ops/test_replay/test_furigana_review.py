"""A note whose furigana awaits a person's check (async_api_ops/furigana_review.py), in a real
collection with the real Sudachi and JMdict: "Match extracted words" leaves it alone, and the
kanjify training export leaves it out, as kanjify and word extraction leave it alone
(test_set_furigana.py).

The one-type layout, where a new vocab note copies the sentence's fields, the furigana among
them. The note already holds a word array: its furigana was emptied to have it set again
(config.md), after it had been kanjified and extracted, and set again from audio that hears 他
two ways. Every model call is answered by a responder. Skipped without the generator's
resources, as the word array tests are.
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
from japanese_note_ai_ops.async_api_ops.match_words_to_notes import (
    MATCH_FIELD_KEYS,
    match_words_spec,
)
from japanese_note_ai_ops.configuration import (
    FURIGANA_DOUBT_LEFT,
    FURIGANA_REVIEW_LEFT,
    FURIGANA_REVIEW_TAG,
    MEANING_MAPPED_TAG,
    MEANINGS_DICT_FILE,
)
from japanese_note_ai_ops.sync_local_ops import make_fine_tuning_data
from japanese_note_ai_ops.word_array import resources
from japanese_note_ai_ops.word_array.audio_furigana import has_doubt

pytestmark = pytest.mark.skipif(
    not resources.is_ready(), reason="needs SudachiPy, a Sudachi dictionary and JMdict"
)

PACKAGE = "japanese_note_ai_ops"
NOTETYPE = "Japanese vocab note"
FIELDS = {key: key.removesuffix("_field") for key in MATCH_FIELD_KEYS}
CONFIG: dict[str, Any] = {
    NOTETYPE: {
        **FIELDS,
        "word_field": FIELDS["word_kanjified_field"],
        "translated_sentence_field": "translation",
        "sentence_transcription_field": "heard",
        "word_extraction_sentence_field": FIELDS["kanjified_sentence_field"],
        "insert_deck": "Default",
    },
    "match_words_model": "terminal-scripted",
    "word_meaning_model": "terminal-scripted",
    "make_meanings_model": "terminal-scripted",
    "mdx_filenames": [],
    "transcribe_models": ["local-kana-anime-whisper", "local-ruby-mora"],
    "log_to_console": False,
}
FURIGANA = FIELDS["furigana_sentence_field"]
OLD_FURIGANA = "他[ほか]の 人[ひと]はどこにいるの"
DOUBT_FURIGANA = "他[ほか？た]の 人[ひと]はどこにいるの"


@pytest.fixture
def col(tmp_path: Path, monkeypatch) -> Iterator[Any]:
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs), stub.pm._profile_folder)
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    monkeypatch.setattr(set_furigana, "NAMES_FILE", str(tmp_path / "show_names.json"))
    monkeypatch.setattr(make_fine_tuning_data, "_OUTPUT_DIR", str(tmp_path / "output"))
    try:
        real_anki.make_note_type(
            collection, NOTETYPE, list(FIELDS.values()) + ["translation", "heard"]
        )
        stub.col = collection
        stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(CONFIG))
        stub.pm.set_profile_folder(tmp_path / "profile")
        meanings = {"人_ひと": [{"jp_meaning": "人間。", "en_meaning": "person"}]}
        (stub.pm.media_folder() / MEANINGS_DICT_FILE).write_text(
            json.dumps(meanings, ensure_ascii=False), encoding="utf-8"
        )
        stub.progress.set_cancel(False)
        yield collection
    finally:
        stub.progress.set_cancel(False)
        stub.col, stub.addonManager.configs, stub.pm._profile_folder = saved
        collection.close()


@pytest.fixture
def reported() -> Iterator[list]:
    errors: list = []
    previous = run_errors.deliver_with(lambda title, text: errors.append((title, text)))
    try:
        yield errors
    finally:
        run_errors.deliver_with(previous)


def run(col: Any, notes: list, spec: Any, answer: Any = None) -> Any:
    previous = base_ops.set_responder(answer)
    try:
        with replay.memory_estimates({}):
            run_notes, result = spec.notes_run([NoteId(note.id) for note in notes])
            run_notes(col)
    finally:
        base_ops.set_responder(previous)
    return result


def word(raw: str, form: str, reading: str) -> list:
    return [raw, "名詞", form, reading, ["match"], []]


def build(col: Any) -> tuple[Any, Any]:
    """人's vocab note, and the sentence note, its furigana set again from audio that hears 他
    two ways: left for review."""
    f = FIELDS
    person = real_anki.add_note(
        col,
        NOTETYPE,
        {
            f["word_kanjified_field"]: "人",
            f["word_normal_field"]: "人",
            f["word_reading_field"]: "ひと",
            f["word_sort_field"]: "人",
            f["meaning_field"]: "人間。",
            f["english_meaning_field"]: "person",
        },
        tags=[MEANING_MAPPED_TAG],
    )
    array = [word("他", "他", "ほか"), ["の"], word("人", "人", "ひと"), ["はどこにいるの"]]
    sentence = real_anki.add_note(
        col,
        NOTETYPE,
        {
            f["word_kanjified_field"]: "店",
            f["word_reading_field"]: "みせ",
            f["word_sort_field"]: "店",
            f["sentence_field"]: "他の人はどこにいるの",
            f["sentence_audio_field"]: "[sound:clip.opus]",
            f["kanjified_sentence_field"]: OLD_FURIGANA,
            f["word_list_field"]: json.dumps(array, ensure_ascii=False),
            "heard": "kana-anime-whisper: ほかのひとわどこにいるの<br>ruby-mora: たのひとわどこにいるの",
        },
        tags=[MEANING_MAPPED_TAG, "ep::show::01"],
    )
    run(col, [sentence], set_furigana.set_furigana_spec())
    note = col.get_note(sentence.id)
    assert (note[FURIGANA], FURIGANA_REVIEW_TAG in note.tags) == (DOUBT_FURIGANA, True)
    return person, sentence


def matching_answer(asked: list):
    def answer(request: Any) -> Any:
        asked.append(request.kind)
        if request.kind == "match.meanings":
            return {"is_matched_meaning": True, "meaning_number": 1, "match_quality": 4}
        if request.kind == "clean_meaning.generate":
            return {"new_meaning": "ほかのもの。", "english_meaning": "other"}
        raise AssertionError(f"not asked: {request.kind}")

    return answer


def test_matching_leaves_a_note_with_unchecked_furigana_alone(col, reported):
    person, sentence = build(col)
    reported.clear()
    asked: list = []

    run(col, [sentence], match_words_spec(), answer=matching_answer(asked))

    # No new note, which would copy the furigana as it is, with no tag to say so
    assert set(col.find_notes(f'"note:{NOTETYPE}"')) == {person.id, sentence.id}
    assert asked == []
    assert [text for _, text in reported] == [FURIGANA_REVIEW_LEFT]


def test_a_word_still_marked_keeps_the_note_unmatched_without_its_tag(col, reported):
    person, sentence = build(col)
    note = col.get_note(sentence.id)
    note.remove_tag(FURIGANA_REVIEW_TAG)  # taken off before the word was fixed
    col.update_note(note)
    reported.clear()
    asked: list = []

    run(col, [sentence], match_words_spec(), answer=matching_answer(asked))

    assert set(col.find_notes(f'"note:{NOTETYPE}"')) == {person.id, sentence.id}
    assert asked == []
    assert [text for _, text in reported] == [FURIGANA_DOUBT_LEFT]


def test_once_checked_the_note_is_matched_and_its_furigana_copied(col, reported):
    person, sentence = build(col)
    # A person listens, fixes the word and takes the tag off
    note = col.get_note(sentence.id)
    note[FURIGANA] = "他[た]の 人[ひと]はどこにいるの"
    note.remove_tag(FURIGANA_REVIEW_TAG)
    col.update_note(note)
    reported.clear()

    run(col, [sentence], match_words_spec(), answer=matching_answer([]))

    [new_id] = set(col.find_notes(f'"note:{NOTETYPE}"')) - {person.id, sentence.id}
    assert col.get_note(new_id)[FURIGANA] == "他[た]の 人[ひと]はどこにいるの"
    assert reported == []


def test_the_kanjify_export_leaves_out_a_note_with_unchecked_furigana(col, tmp_path):
    _, sentence = build(col)

    message = make_fine_tuning_data._write_kanjify_sentence_data(CONFIG, [NoteId(sentence.id)])

    rows = (tmp_path / "output" / "kanjify_sentence_data.jsonl").read_text(encoding="utf-8")
    assert rows == ""
    assert not has_doubt(rows)
    assert message.endswith("Left out 1 notes whose furigana awaits a check.")
