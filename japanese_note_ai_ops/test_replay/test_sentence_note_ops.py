"""The two-type layout's small ops in a real collection: "Refresh example sentences"
(sync_local_ops/refresh_example_sentences.py) and Translate sentence copying a sentence note's
new translation into its vocab notes (async_api_ops/translate_field.py), each run through its
NotesRunSpec as a script runs it.

The vocab notes keep a copy of their example sentence note's translation and audio, and its id
(note_roles.copy_example). The refresh copies it again, finds another example for a note whose
example is gone (the oldest sentence note whose array links it: a search for `*<id>*` also
finds a longer id holding the digits, so the array is read), and tags a note with none.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from anki.notes import NoteId

import replay
from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import base_ops, run_errors
from japanese_note_ai_ops.async_api_ops.translate_field import bulk_translate_notes_op
from japanese_note_ai_ops.sync_local_ops.refresh_example_sentences import (
    MISSING_TAG,
    refresh_preflight_error,
    refresh_spec,
)
from japanese_note_ai_ops.word_array.match_flags import format_word_array

PACKAGE = "japanese_note_ai_ops"
VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"

SENTENCE_FIELDS = [
    "Sentence",
    "Sentence furigana",
    "Sentence translation",
    "Sentence audio",
    "Words",
]
VOCAB_FIELDS = ["Word sort", "Word", "Example sentence id", "Example translation", "Example audio"]
TWO_TYPE = {
    SENTENCE: {
        "vocab_note_type": VOCAB,
        "sentence_field": "Sentence",
        "furigana_sentence_field": "Sentence furigana",
        "translated_sentence_field": "Sentence translation",
        "sentence_audio_field": "Sentence audio",
        "word_list_field": "Words",
        "insert_deck": "Sentences",
    },
    VOCAB: {
        "sentence_note_type": SENTENCE,
        "example_sentence_id_field": "Example sentence id",
        "translated_sentence_field": "Example translation",
        "sentence_audio_field": "Example audio",
        "word_normal_field": "Word",
        "word_sort_field": "Word sort",
        "insert_deck": "Vocab",
    },
    "translate_sentence_model": "terminal-scripted",
    "log_to_console": False,
}
# The same collection read as the one-type layout: the vocab type holds its own sentences
ONE_TYPE = {
    VOCAB: {
        "sentence_field": "Word",
        "translated_sentence_field": "Example translation",
        "word_list_field": "Words",
        "word_sort_field": "Word sort",
    },
    "log_to_console": False,
}


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Any]:
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs))
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(collection, VOCAB, VOCAB_FIELDS)
        real_anki.make_note_type(collection, SENTENCE, SENTENCE_FIELDS)
        stub.col = collection
        stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(TWO_TYPE))
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


def set_config(config: dict) -> None:
    real_anki.install().addonManager.configs[PACKAGE] = json.loads(json.dumps(config))


def word(raw: str, form: str, match_data: list) -> list:
    return [raw, "noun", form, form, match_data, []]


def sentence(col: Any, text: str, words: list, translation: str = "", audio: str = "") -> Any:
    return real_anki.add_note(
        col,
        SENTENCE,
        {
            "Sentence": text,
            "Sentence furigana": text,
            "Sentence translation": translation,
            "Sentence audio": audio,
            "Words": format_word_array(words),
        },
        deck_name="Sentences",
    )


def vocab(col: Any, form: str, example_id: str = "", tags: tuple = ()) -> Any:
    return real_anki.add_note(
        col,
        VOCAB,
        {
            "Word sort": form,
            "Word": form,
            "Example sentence id": example_id,
            "Example translation": "stale",
            "Example audio": "[sound:stale.mp3]",
        },
        deck_name="Vocab",
        tags=list(tags),
    )


def refresh(col: Any, nids: list[int]) -> Any:
    run, result = refresh_spec().notes_run([NoteId(nid) for nid in nids])
    run(col)
    return result


def fields(col: Any, nid: int) -> dict[str, str]:
    return dict(col.get_note(nid).items())


def test_a_vocab_note_copies_its_example_again(col):
    cat = vocab(col, "猫", tags=(MISSING_TAG,))
    example = sentence(col, "猫がいる。", [word("猫", "猫", [cat.id])], "A cat.", "[sound:c.mp3]")
    cat["Example sentence id"] = str(example.id)
    col.update_note(cat)
    # Retranslated, and a later sentence links the note too: the example stays the example
    example["Sentence translation"] = "There is a cat."
    col.update_note(example)
    sentence(col, "猫だ。", [word("猫", "猫", [cat.id])], "It is a cat.")

    result = refresh(col, [cat.id])

    assert result.edited_nids == [cat.id]
    refreshed = col.get_note(cat.id)
    assert refreshed["Example sentence id"] == str(example.id)
    assert refreshed["Example translation"] == "There is a cat."
    assert refreshed["Example audio"] == "[sound:c.mp3]"
    assert not refreshed.has_tag(MISSING_TAG)
    # A run over notes already as their examples have them changes nothing
    assert refresh(col, [cat.id]).edited_nids == []


def test_a_gone_example_is_replaced_by_the_oldest_sentence_note_linking_the_note(col):
    dog = vocab(col, "犬")
    # Oldest, and found by `*<id>*`, but what it links is a longer id holding dog's digits
    longer = sentence(col, "犬小屋。", [word("犬小屋", "犬小屋", [dog.id * 10 + 7])], "Kennel.")
    # A sub-word's link counts: the oldest that really links the note
    linking = sentence(
        col,
        "子犬が来た。",
        [["子犬", "noun", "子犬", "こいぬ", ["dontmatch"], [word("犬", "犬", [dog.id])]]],
        "A puppy came.",
        "[sound:puppy.mp3]",
    )
    sentence(col, "犬が走る。", [word("犬", "犬", [dog.id])], "The dog runs.")
    gone = sentence(col, "犬がいた。", [word("犬", "犬", [dog.id])], "There was a dog.")
    dog["Example sentence id"] = str(gone.id)
    col.update_note(dog)
    col.remove_notes([gone.id])
    assert longer.id < linking.id

    refresh(col, [dog.id])

    assert fields(col, dog.id)["Example sentence id"] == str(linking.id)
    assert fields(col, dog.id)["Example translation"] == "A puppy came."
    assert fields(col, dog.id)["Example audio"] == "[sound:puppy.mp3]"


def test_a_note_no_sentence_links_is_cleared_and_tagged(col):
    bird = vocab(col, "鳥", example_id="1234")
    # Linked only by a longer id holding its digits, and by an id field of another type
    sentence(col, "鳥居。", [word("鳥居", "鳥居", [bird.id * 10 + 1])])
    fish = vocab(col, "魚", example_id=str(bird.id))
    before_fish = fields(col, fish.id)

    result = refresh(col, [bird.id, fish.id])

    assert sorted(result.edited_nids) == sorted([bird.id, fish.id])
    for nid in (bird.id, fish.id):
        note = col.get_note(nid)
        assert note["Example sentence id"] == ""
        assert note.has_tag(MISSING_TAG)
    # The copies are left as they were: there is nothing to copy from
    assert fields(col, fish.id)["Example translation"] == before_fish["Example translation"]
    # Tagged once, and a rerun changes nothing
    assert col.get_note(bird.id).tags == [MISSING_TAG]
    assert refresh(col, [bird.id, fish.id]).edited_nids == []


def test_one_undo_takes_a_refresh_back(col):
    cow = vocab(col, "牛")
    example = sentence(col, "牛だ。", [word("牛", "牛", [cow.id])], "A cow.")
    before = fields(col, cow.id)

    refresh(col, [cow.id])
    assert fields(col, cow.id)["Example sentence id"] == str(example.id)

    assert col.undo_status().undo == "Refreshing example sentences for 1 notes."
    col.undo()
    assert fields(col, cow.id) == before


def test_sentence_notes_in_the_selection_are_left_out_with_one_report(col, reported):
    hen = vocab(col, "鶏")
    first = sentence(col, "鶏だ。", [word("鶏", "鶏", [hen.id])], "A hen.")
    second = sentence(col, "鶏が鳴く。", [word("鶏", "鶏", ["match"])])
    before = {nid: fields(col, nid) for nid in (first.id, second.id)}

    result = refresh(col, [first.id, hen.id, second.id])

    assert result.edited_nids == [hen.id]
    assert {nid: fields(col, nid) for nid in before} == before
    [(title, text)] = reported
    assert f'"{SENTENCE}"' in title
    assert "runs on vocab notes" in text
    assert text.endswith("Left out of this run: 2 notes of it.")


def test_the_one_type_layout_is_refused(col, reported):
    hen = vocab(col, "鶏", example_id="5")
    set_config(ONE_TYPE)
    before = fields(col, hen.id)

    error = refresh_preflight_error(col, ONE_TYPE, [hen.id])
    assert error is not None
    assert "needs the two-type layout" in error
    assert f'"{VOCAB}"' in error
    # A script that starts the run anyway changes nothing, and says why
    assert refresh(col, [hen.id]).edited_nids == []
    assert fields(col, hen.id) == before
    assert [text for _, text in reported] == [f"{error} Left out of this run: 1 note of it."]
    # The two-type layout lets the same note through
    assert refresh_preflight_error(col, TWO_TYPE, [hen.id]) is None


def test_translating_a_sentence_note_copies_its_translation_into_its_vocab_notes(col):
    tea = vocab(col, "茶")
    cup = vocab(col, "杯")
    other = vocab(col, "水")
    example = sentence(col, "茶を一杯。", [word("茶", "茶", [tea.id]), word("杯", "杯", [cup.id])])
    for note in (tea, cup):
        note["Example sentence id"] = str(example.id)
        col.update_note(note)
    other["Example sentence id"] = str(example.id) + "0"
    col.update_note(other)

    asked: list = []

    def answer(request: Any) -> Any:
        asked.append(request.inputs)
        return {"english_sentence": "A cup of tea."}

    spec = base_ops.NotesRunSpec("Updated translation", "Translating", bulk_translate_notes_op)
    previous = base_ops.set_responder(answer)
    try:
        # The gate's learned costs stay out of the user's memory_estimates.json
        with replay.memory_estimates({}):
            run, result = spec.notes_run([NoteId(example.id)])
            run(col)
    finally:
        base_ops.set_responder(previous)

    assert asked == [{"sentence": "茶を一杯。"}]
    assert col.get_note(example.id)["Sentence translation"] == "A cup of tea."
    for nid in (tea.id, cup.id):
        assert col.get_note(nid)["Example translation"] == "A cup of tea."
    assert col.get_note(other.id)["Example translation"] == "stale"
    assert sorted(result.edited_other_nids) == sorted([tea.id, cup.id])


def test_translating_only_vocab_notes_asks_nothing_and_says_why(col, reported):
    tea = vocab(col, "茶")
    before = fields(col, tea.id)

    def answer(request: Any) -> Any:
        raise AssertionError(f"no request is made: {request.inputs}")

    spec = base_ops.NotesRunSpec("Updated translation", "Translating", bulk_translate_notes_op)
    previous = base_ops.set_responder(answer)
    try:
        with replay.memory_estimates({}):
            run, result = spec.notes_run([NoteId(tea.id)])
            run(col)
    finally:
        base_ops.set_responder(previous)

    assert not result.cancelled
    assert result.edited_nids == []
    assert fields(col, tea.id) == before
    [(title, text)] = reported
    assert title == f'Notes of "{VOCAB}"'
    assert text.startswith(f'This op runs on sentence notes, and "{VOCAB}" is the vocab')
    assert text.endswith("Left out of this run: 1 note of it.")
