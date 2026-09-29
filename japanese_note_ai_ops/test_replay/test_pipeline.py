"""Capture, export and replay, end to end, on a collection made up here: no note of anyone's.

A sentence note with two words: 本 to match against the one vocab note for it, and 買う linked
to its note already but not rated. A run of the match op with every AI call answered by a script
is captured with its notes, exported as a fixture, and replayed into a fresh collection with the
fixture's cassette answering: the replay must leave every note as the capture run did, ask for
exactly the answers the run was given, and do it again the same.

The new-note path needs the meaning-making prompts' answers, which a script cannot make up
convincingly; the fixtures of real capture runs (test_replay.py) cover it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import replay
from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import base_ops, capture
from japanese_note_ai_ops.async_api_ops.match_words_to_notes import (
    MATCH_FIELD_KEYS,
    match_words_spec,
)
from japanese_note_ai_ops.configuration import MEANING_MAPPED_TAG, MEANINGS_DICT_FILE

NOTETYPE = "Japanese vocab note"
FIELDS = {key: key.removesuffix("_field") for key in MATCH_FIELD_KEYS}
PACKAGE = "japanese_note_ai_ops"
CONFIG = {
    NOTETYPE: dict(FIELDS),
    "match_words_model": "terminal-scripted",
    "word_meaning_model": "terminal-scripted",
    "make_meanings_model": "terminal-scripted",
    "mdx_filenames": [],
    "capture_calls": True,
    "capture_notes": True,
    "log_to_console": False,
}
MEANINGS = {"本_ほん": [{"jp_meaning": "書物", "en_meaning": "book"}]}


def word(raw: str, pos: str, form: str, reading: str, match_data: list) -> list:
    return [raw, pos, form, reading, match_data, []]


def scripted(request: Any) -> Any:
    """The answers the capture run gets: the one listed meaning matches, and a rating."""
    if request.kind == "match.meanings":
        return {"is_matched_meaning": True, "meaning_number": 1, "match_quality": 4}
    if request.kind == "match.rating":
        return {"match_quality": 3}
    raise AssertionError(f"a call this scenario does not make: {request.kind}")


@pytest.fixture
def captured(tmp_path: Path):
    """The capture run: its store and run id, and the ids of its notes."""
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs), stub.pm._profile_folder)
    col = real_anki.open_collection(tmp_path / "collection.anki2")
    real_anki.make_note_type(col, NOTETYPE, list(FIELDS.values()))
    fields = FIELDS
    book = real_anki.add_note(
        col,
        NOTETYPE,
        {fields["word_kanjified_field"]: "本", fields["word_normal_field"]: "本",
         fields["word_reading_field"]: "ほん", fields["word_sort_field"]: "本",
         fields["meaning_field"]: "書物", fields["english_meaning_field"]: "book"},
        tags=[MEANING_MAPPED_TAG],
    )
    buy = real_anki.add_note(
        col,
        NOTETYPE,
        {fields["word_kanjified_field"]: "買う", fields["word_normal_field"]: "買う",
         fields["word_reading_field"]: "かう", fields["word_sort_field"]: "買う",
         fields["meaning_field"]: "代金を払って物を得る", fields["english_meaning_field"]: "buy"},
        tags=[MEANING_MAPPED_TAG],
    )
    array = [
        word("本", "名詞", "本", "ほん", ["match"]),
        ["を"],
        word("買う", "動詞", "買う", "かう", [buy.id]),
        ["。"],
    ]
    sentence = real_anki.add_note(
        col,
        NOTETYPE,
        {fields["word_kanjified_field"]: "店", fields["word_reading_field"]: "みせ",
         fields["word_sort_field"]: "店", fields["sentence_field"]: "本を買う。",
         fields["furigana_sentence_field"]: "本[ほん]を買[か]う。",
         fields["word_list_field"]: json.dumps(array, ensure_ascii=False)},
        tags=[MEANING_MAPPED_TAG],
    )
    store = tmp_path / "capture.sqlite3"
    try:
        stub.col = col
        stub.addonManager.configs[PACKAGE] = dict(CONFIG)
        stub.pm.set_profile_folder(tmp_path / "profile")
        (stub.pm.media_folder() / MEANINGS_DICT_FILE).write_text(
            json.dumps(MEANINGS, ensure_ascii=False), encoding="utf-8"
        )
        assert capture.install(str(store), keep_days=None)
        base_ops.set_responder(scripted)
        run, result = match_words_spec().notes_run([sentence.id])
        run(col)
        assert not result.cancelled
    finally:
        base_ops.set_responder(None)
        capture.shutdown(timeout=10.0)
        stub.col, stub.addonManager.configs, stub.pm._profile_folder = saved
        col.close()
    # A store's ids go on from the last store's in the process, so the run is not always 1
    with closing(sqlite3.connect(str(store))) as connection:
        [(run_id,)] = connection.execute("SELECT run_id FROM runs WHERE implicit = 0").fetchall()
    yield store, run_id, {"book": book.id, "buy": buy.id, "sentence": sentence.id}


def test_the_capture_holds_what_the_run_did(captured):
    store, run_id, ids = captured

    fixture = replay.export_fixture(store, run_id)

    corpus_notes = {note["fields"]["word_sort"]: note for note in fixture.corpus["notes"]}
    assert set(corpus_notes) == {"本", "買う", "店"}
    assert [note["selected"] for note in corpus_notes.values()].count(True) == 1
    # Note ids are synthetic, and the sentence's link to 買う names its synthetic id
    buy_id = corpus_notes["買う"]["id"]
    assert buy_id != ids["buy"]
    assert str(buy_id) in corpus_notes["店"]["fields"]["word_list"]
    assert str(ids["buy"]) not in json.dumps(fixture.corpus, ensure_ascii=False)
    kinds = sorted(entry["kind"] for entry in fixture.cassette["entries"])
    assert kinds == ["match.meanings", "match.rating"]
    [sentence] = [n for n in fixture.expected["notes"] if n["fields"]["word_sort"] == "店"]
    array = json.loads(sentence["fields"]["word_list"])
    book_id = corpus_notes["本"]["id"]
    assert array[0][4] == [book_id, 4]
    assert array[2][4] == [buy_id, 3]
    assert fixture.expected["new_notes"] == 0


def test_a_replay_reproduces_the_capture_run_every_time(captured, tmp_path):
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)
    fixture.write(tmp_path / "fixture")
    fixture = replay.Fixture.read(tmp_path / "fixture")

    for _ in range(2):
        result = replay.replay(fixture)
        assert result.differences(fixture.expected) == []
        assert len(result.decisions) == 1


def test_a_changed_answer_is_a_difference(captured):
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)
    [rating] = [e for e in fixture.cassette["entries"] if e["kind"] == "match.rating"]
    rating["answers"][0]["response"] = {"match_quality": 5}

    differences = replay.replay(fixture).differences(fixture.expected)

    assert len(differences) == 1 and differences[0].startswith("note ")


def test_a_request_the_cassette_lacks_is_reported_and_answered_as_a_failure(captured):
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)
    fixture.cassette["entries"] = [
        e for e in fixture.cassette["entries"] if e["kind"] != "match.rating"
    ]

    result = replay.replay(fixture)

    assert [miss["kind"] for miss in result.misses] == ["match.rating"]
    assert any(line.startswith("cassette had no answer") for line in
               result.differences(fixture.expected))


def test_a_timed_benchmark_run_does_the_same_work_every_time_and_records_its_figures(captured):
    import benchmark

    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)
    args = argparse.Namespace(latency="fixed:5", scale=1.0, memory="free:8")

    runs = [benchmark.run_once(fixture, args) for _ in range(2)]

    for run in runs:
        # The counts, never the seconds: those are the machine's
        assert run["answered"] == {"exact": 2}
        assert (run["calls_total"], run["decisions"], run["new_notes"]) == (2, 1, 0)
        assert sorted(metric["kind"] for metric in run["metrics"]) == [
            "metrics.caches",
            "metrics.gate",
        ]
        assert all(phase["rss"] for phase in run["phases"])
        [gate] = [metric for metric in run["metrics"] if metric["kind"] == "metrics.gate"]
        assert sum(gate["dwell_seconds"].values()) > 0


def test_a_background_note_holds_no_japanese_and_no_note_id_of_the_corpus(captured):
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)

    notes = replay.background_notes(fixture.corpus, 5)

    assert len(notes) == 5
    text = json.dumps([note["fields"] for note in notes], ensure_ascii=False)
    assert not any(replay._is_japanese(char) for char in text)
    assert not any(str(note["id"]) in text for note in fixture.corpus["notes"])
    # The shape stays: a word array is still one
    [array] = {note["fields"]["word_list"] for note in notes if note["fields"]["word_list"]}
    assert isinstance(json.loads(array), list)


def test_background_notes_change_nothing_the_run_does(captured):
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)

    result = replay.replay(fixture, background=60)

    assert result.differences(fixture.expected) == []


def test_a_run_that_recorded_no_notes_is_refused(tmp_path):
    store = tmp_path / "capture.sqlite3"
    assert capture.install(str(store), keep_days=None)
    try:
        run_id = capture.begin_run("calls only")
        capture.end_run(run_id, "completed")
    finally:
        capture.shutdown()

    with pytest.raises(replay.CaptureGap, match="recorded no notes"):
        replay.export_fixture(store, run_id)
