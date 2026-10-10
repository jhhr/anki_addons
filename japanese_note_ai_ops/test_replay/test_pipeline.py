"""Capture, export and replay, end to end, on a collection made up here: no note of anyone's.

A sentence note with two words: 本 to match against the one vocab note for it, and 買う linked
to its note already but not rated. A run of the match op with every AI call answered by a script
is captured with its notes, exported as a fixture, and replayed into a fresh collection with the
fixture's cassette answering: the replay must leave every note as the capture run did, ask for
exactly the answers the run was given, and do it again the same.

The new-note path needs the meaning-making prompts' answers, which a script cannot make up
convincingly; the fixtures of real capture runs (test_replay.py) cover it. One scripted new note
is here, for what its failed add leaves behind: a placeholder the fixture must name by a symbol.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Optional, cast

import pytest

import replay
from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import base_ops, capture, run_errors
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


def capture_run(
    tmp_path: Path, build: Any, config: dict, responder: Any
) -> tuple[Path, int, dict]:
    """A capture run of the match op over the sentence note `build(col)` makes, answered by
    `responder`: its store, its run id, and the ids `build` names."""
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs), stub.pm._profile_folder)
    col = real_anki.open_collection(tmp_path / "collection.anki2")
    real_anki.make_note_type(col, NOTETYPE, list(FIELDS.values()))
    store = tmp_path / "capture.sqlite3"
    try:
        sentence_id, ids = build(col)
        stub.col = col
        stub.addonManager.configs[PACKAGE] = dict(config)
        stub.pm.set_profile_folder(tmp_path / "profile")
        (stub.pm.media_folder() / MEANINGS_DICT_FILE).write_text(
            json.dumps(MEANINGS, ensure_ascii=False), encoding="utf-8"
        )
        assert capture.install(str(store), keep_days=None)
        base_ops.set_responder(responder)
        run, result = match_words_spec().notes_run([sentence_id])
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
    return store, run_id, ids


@pytest.fixture
def captured(tmp_path: Path):
    """The capture run: its store and run id, and the ids of its notes."""
    yield capture_run(tmp_path, build_book_and_buy, CONFIG, scripted)


def build_book_and_buy(col: Any) -> tuple[int, dict]:
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
    return sentence.id, {"book": book.id, "buy": buy.id, "sentence": sentence.id}


def build_box_to_create(col: Any) -> tuple[int, dict]:
    """A sentence whose word 箱 has no vocab note: the run makes one."""
    fields = FIELDS
    array = [word("箱", "名詞", "箱", "はこ", ["match"]), ["。"]]
    sentence = real_anki.add_note(
        col,
        NOTETYPE,
        {fields["word_kanjified_field"]: "店", fields["word_reading_field"]: "みせ",
         fields["word_sort_field"]: "店", fields["sentence_field"]: "箱。",
         fields["furigana_sentence_field"]: "箱[はこ]。",
         fields["word_list_field"]: json.dumps(array, ensure_ascii=False)},
        tags=[MEANING_MAPPED_TAG],
    )
    return sentence.id, {"sentence": sentence.id}


def generated(request: Any) -> Any:
    """The one answer a new word with no dictionary to look it up in needs."""
    if request.kind == "clean_meaning.generate":
        return {"new_meaning": "物を入れる器。", "english_meaning": "box"}
    raise AssertionError(f"a call this scenario does not make: {request.kind}")


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


@pytest.mark.parametrize("compress", [False, True], ids=["plain", "gzipped"])
def test_a_fixture_file_holds_only_what_the_run_changed_and_reads_back_whole(
    captured, tmp_path, compress
):
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)
    # A note the run removed, as a deduplicating op's would be
    [removed] = [n for n in fixture.expected["notes"] if n["fields"]["word_sort"] == "本"]
    fixture.expected["notes"] = [n for n in fixture.expected["notes"] if n is not removed]

    fixture.write(tmp_path / "fixture", compress=compress)

    suffix = ".json.gz" if compress else ".json"
    assert sorted(p.name for p in (tmp_path / "fixture").iterdir()) == [
        f"{name}{suffix}" for name in ("cassette", "corpus", "expected")
    ]
    stored = replay._read_json(tmp_path / "fixture" / "expected.json")
    # Only the sentence note's word list changed
    assert [note["fields"]["word_sort"] for note in stored["notes"]] == ["店"]
    assert stored["removed"] == [removed["note"]]
    read = replay.Fixture.read(tmp_path / "fixture")
    assert read.expected["notes"] == fixture.expected["notes"]
    assert (read.corpus, read.cassette) == (fixture.corpus, fixture.cassette)
    # Written the other way over it, the old form goes: a reader must not find both
    fixture.write(tmp_path / "fixture", compress=not compress)
    assert not any(p.name.endswith(suffix) for p in (tmp_path / "fixture").iterdir())


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


def test_a_note_added_past_the_background_notes_is_the_run_s(captured, tmp_path):
    # A note added in a millisecond whose id a note already has gets max(id) + 1 from Anki,
    # which with background notes in the collection is past them, and a note added from 2033
    # on has an id in their range: a replay left either out of its notes, by the range
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)
    # Not collection.anki2: the capture run's collection is that
    col, background_ids = replay.build_collection(
        fixture.corpus, tmp_path / "replayed.anki2", background=3
    )
    try:
        added = [max(background_ids) + 1, replay.BACKGROUND_BASE + 5]
        for new_id in added:
            note = real_anki.add_note(col, NOTETYPE, {FIELDS["word_sort_field"]: str(new_id)})
            col.db.execute("update cards set nid = ? where nid = ?", new_id, note.id)
            col.db.execute("update notes set id = ? where id = ?", new_id, note.id)

        ids = {note["id"] for note in replay._collection_notes(col, background_ids)}
    finally:
        col.close()

    assert len(background_ids) == 3
    assert set(added) <= ids
    assert not ids & background_ids
    assert len(ids) == len(fixture.corpus["notes"]) + len(added)


def test_a_replay_puts_back_the_responder_and_error_deliverer_it_replaced(captured):
    # It set both to None when it was done, whatever a caller (a script's Headless, which
    # prints run errors) had set before it
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)
    delivered: list = []
    previous = run_errors.deliver_with(lambda title, text: delivered.append(text))
    base_ops.set_responder(scripted)
    try:
        assert replay.replay(fixture).differences(fixture.expected) == []

        run_errors.report_error("after the replay")
        assert delivered == ["after the replay"]
        assert base_ops.set_responder(None) is scripted
    finally:
        base_ops.set_responder(None)
        run_errors.deliver_with(previous)


def test_a_replay_refuses_to_close_an_installed_capture_store(captured, tmp_path):
    # Installing its own store closed the one installed, and nothing installed it again: a
    # Headless session's capture stopped recording for the rest of the process
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id)
    outer = tmp_path / "outer.sqlite3"
    assert capture.install(str(outer), keep_days=None)
    try:
        with pytest.raises(RuntimeError, match="capture store is installed"):
            replay.replay(fixture)

        installed = capture.current_store()
        assert installed is not None and Path(installed.path) == outer
        assert capture.installed()
    finally:
        capture.shutdown()


def unbuildable_fixture() -> replay.Fixture:
    """A fixture whose corpus names a note type it does not hold, which fill_collection
    refuses."""
    note = {"id": replay.SYNTHETIC_BASE, "notetype": "Missing", "deck": "Default",
            "guid": "g", "fields": {}, "tags": [], "selected": True}
    corpus = {"config": {}, "meanings": {}, "dictionary": [], "decks": [], "notetypes": [],
              "notes": [note]}
    return replay.Fixture(corpus, {"entries": []}, {"notes": [], "meanings": {}, "new_notes": 0})


def test_a_corpus_that_fails_to_build_leaves_its_collection_closed(tmp_path):
    # It was opened before the try that closes it, and stayed locked while the exception lived,
    # which pytest keeps for a failed test
    with pytest.raises(AssertionError) as raised:
        replay.replay(unbuildable_fixture(), workdir=tmp_path)

    assert raised.value is not None
    real_anki.open_collection(tmp_path / "collection.anki2").close()


def test_a_corpus_that_fails_to_build_leaves_no_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with pytest.raises(AssertionError):
        replay.replay(unbuildable_fixture())

    assert list(tmp_path.iterdir()) == []


def test_a_failed_add_s_placeholder_replays_as_a_symbol(tmp_path):
    # The new note's deck is missing, so its add fails and the sentence keeps its placeholder,
    # a random number every run: a fixture holding it could never replay
    # word_field and translated_sentence_field are the meaning cleaning's, which a new note needs
    note_config = {
        **FIELDS,
        "word_field": FIELDS["word_kanjified_field"],
        "translated_sentence_field": FIELDS.get("translated_sentence_field", "sentence"),
        "insert_deck": "No such deck",
    }
    config = {**CONFIG, NOTETYPE: note_config}
    store, run_id, _ = capture_run(tmp_path, build_box_to_create, config, generated)

    fixture = replay.export_fixture(store, run_id)

    [sentence] = [n for n in fixture.expected["notes"] if n["fields"]["word_sort"] == "店"]
    # In place of the number, as a new note's placeholder:new-N is
    assert "[placeholder:failed-1]" in sentence["fields"]["word_list"]
    assert not replay.PLACEHOLDER_RE.search(sentence["fields"]["word_list"])
    assert fixture.expected["new_notes"] == 0
    for _ in range(2):
        result = replay.replay(fixture)
        assert result.differences(fixture.expected) == []


def copy_definition(on_add: bool, note_types: list, deck_names: list, change_deck: str) -> dict:
    """A format 2 CopyAnywhere definition, down to what an export reads of it."""
    from copy_anywhere.logic.definition_schema import EMPTY_EFFECTS, FORMAT_VERSION

    return {
        "format_version": FORMAT_VERSION,
        "definition_name": "definition",
        "effects": dict(EMPTY_EFFECTS),
        "triggers": {"on_add": on_add, "note_types": note_types, "deck_names": deck_names},
        "stages": [{"type": "cards", "card_actions": [{"change_deck": change_deck}]}],
    }


def test_an_export_holds_nothing_of_the_user_s_a_replay_never_reads(tmp_path):
    # The run's config and CopyAnywhere's were copied as they were: the claude CLI's path, the
    # menu exports' searches, a deck no recorded note is in, and every definition with the
    # user's note types and decks
    private = ["C:/Users/someone/claude.exe", "Private deck", "Private type", "Private deck 2"]
    config = {
        **CONFIG,
        "claude_cli_path": private[0],
        "kanji_sentence_fine_tuning_data_query": 'deck:"Private deck" note:"Private type"',
        NOTETYPE: {**FIELDS, "insert_deck": "Private deck"},
    }
    store, run_id, _ = capture_run(tmp_path, build_book_and_buy, config, scripted)
    definitions = [
        copy_definition(True, [NOTETYPE, "Private type"], ["Private deck"], "Private deck 2"),
        copy_definition(True, ["Private type"], [], ""),
        copy_definition(False, [NOTETYPE], [], ""),
    ]

    fixture = replay.export_fixture(
        store, run_id, copy_anywhere={"copy_definitions": definitions}
    )

    text = json.dumps(fixture.corpus, ensure_ascii=False)
    assert [name for name in private if name in text] == []
    assert fixture.corpus["config"][NOTETYPE]["insert_deck"] == "Unseen deck 1"
    # Only what the add hook runs for the corpus's notes, naming what the replay has
    [kept] = fixture.corpus["copy_anywhere"]["copy_definitions"]
    assert kept["triggers"]["note_types"] == [NOTETYPE]
    assert kept["triggers"]["deck_names"] == ["Unseen deck 1"]
    assert kept["stages"][0]["card_actions"][0]["change_deck"] == "Unseen deck 2"
    assert "Unseen deck 1" not in fixture.corpus["decks"]
    assert replay.replay(fixture).differences(fixture.expected) == []


def test_a_replay_logs_beside_the_addons_own_logs_and_leaves_nothing_attached(
    captured, tmp_path, monkeypatch
):
    from copy_anywhere import logging_setup

    from japanese_note_ai_ops import call_logging

    # Where the addons' own logs are: the user's, which a replay's run is not one of
    monkeypatch.setattr(call_logging, "logs_dir", lambda: str(tmp_path / "jnaio" / "logs"))
    monkeypatch.setattr(logging_setup, "logs_dir", lambda: str(tmp_path / "ca" / "logs"))
    store, run_id, _ = captured
    fixture = replay.export_fixture(store, run_id, copy_anywhere={"copy_definitions": []})
    fixture.corpus["config"]["log_level"] = "INFO"
    seen = []

    @contextmanager
    def probe():
        seen.append(logging_setup.logs_dir())
        yield

    assert replay.replay(fixture, around_run=probe).differences(fixture.expected) == []

    [log] = (tmp_path / "jnaio" / replay.REPLAY_LOGS).iterdir()
    assert log.name.startswith("match_words_") and log.read_text(encoding="utf-8")
    assert not (tmp_path / "jnaio" / "logs").exists()
    # Nothing after the replay writes into its folder
    assert not [h for h in call_logging.addon_logger().handlers
                if getattr(h, call_logging._ADDON_HANDLER_FLAG, False)]
    # CopyAnywhere's, for as long as its definitions are on the add hook
    stub = real_anki.install()
    with replay.copy_anywhere_on_add(stub, {"copy_definitions": []}):
        seen.append(logging_setup.logs_dir())
    assert seen == [str(tmp_path / "ca" / "logs"), str(tmp_path / "ca" / replay.REPLAY_LOGS)]
    assert logging_setup.logs_dir() == str(tmp_path / "ca" / "logs")


KANJI_TYPE = "Kanji note"


def kanji_grades_and_fonts(fonts_file: str) -> dict:
    """A CopyAnywhere add definition that reads what the capture never recorded: the grade of
    the word's kanji from a kanji note, found by a search, and the fonts for its kanji from a
    media file."""
    from copy_anywhere.logic.definition_schema import CopyDefinitionV2
    from copy_anywhere.logic.flow_analysis import compute_effects

    def stage(guid: str, stage_type: str, **fields: Any) -> dict:
        return {"guid": guid, "type": stage_type, "name": stage_type, "enabled": True, **fields}

    def text(value: str, process_chain: Optional[list] = None) -> dict:
        return {"mode": "text", "text": value, "code": "", "process_chain": process_chain or []}

    fonts_check = {
        "guid": "fonts", "name": "Fonts check", "fonts_dict_file": fonts_file,
        "limit_to_fonts": None, "character_limit_regex": None,
    }
    definition = {
        "guid": "kanji-grades",
        "format_version": 2,
        "definition_name": "kanji grades and fonts",
        "triggers": {
            "note_types": [NOTETYPE], "deck_names": [], "include_subdecks": False,
            "on_sync": False, "on_add": True, "on_review": False,
            "on_unfocus": {"edit_fields": [], "add_fields": []},
        },
        "stages": [
            stage("query", "note_query", result="found",
                  query=text("Kanji:{{trigger.word_kanjified}}"),
                  selection={"strategy": "all", "count": None, "sort_field": None,
                             "sort_order": "descending"},
                  if_empty="continue"),
            stage("list", "list_variable", result="grades", item_type="Text"),
            stage("each", "for_each_note", input={"binding": "found"}, item_binding="note",
                  body=[stage("store", "store", target={"kind": "list", "binding": "grades"},
                              value=text("{{note.Category}}"))]),
            stage("join", "reduce", input={"binding": "grades"}, result="joined",
                  initial=text(""), item_binding="item", accumulator_binding="accumulator",
                  value=text(""), operation="join", separator=", "),
            stage("write", "edit_note", target={"binding": "trigger"},
                  fields=[
                      {"field": "sentence_audio", "value": text("{{joined}}"),
                       "write_if": "always"},
                      {"field": "meaning_audio",
                       "value": text("{{trigger.word_kanjified}}", [fonts_check]),
                       "write_if": "always"},
                  ],
                  tags={"add": [], "remove": []}, card_actions=[],
                  read_semantics="stage_snapshot"),
        ],
        "exports": [],
    }
    definition["effects"] = compute_effects(cast(CopyDefinitionV2, definition))
    return definition


def build_box_and_its_kanji(col: Any) -> tuple[int, dict]:
    """The sentence whose word 箱 the run makes a note for, and a note of another type the run
    never reads: the kanji's, with its grade."""
    real_anki.make_note_type(col, KANJI_TYPE, ["Kanji", "Category"])
    kanji = real_anki.add_note(col, KANJI_TYPE, {"Kanji": "箱", "Category": "N3"})
    sentence_id, ids = build_box_to_create(col)
    return sentence_id, {**ids, "kanji": kanji.id}


def test_an_export_carries_what_copy_anywhere_reads_that_the_capture_never_recorded(tmp_path):
    note_config = {
        **FIELDS,
        "word_field": FIELDS["word_kanjified_field"],
        "translated_sentence_field": FIELDS.get("translated_sentence_field", "sentence"),
        "insert_deck": "Default",
    }
    config = {**CONFIG, NOTETYPE: note_config}
    store, run_id, ids = capture_run(tmp_path, build_box_and_its_kanji, config, generated)
    media = tmp_path / "media"
    media.mkdir()
    fonts = {"箱": ["Mincho.ttf", "Gothic.ttf"], "龍": ["Brush.ttf"], "all_fonts": ["Mincho.ttf"]}
    (media / "fonts.json").write_text(json.dumps(fonts, ensure_ascii=False), encoding="utf-8")
    source = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        # Made after the run started: not what the run's collection held
        later = real_anki.add_note(source, KANJI_TYPE, {"Kanji": "箱", "Category": "N1"})
        fixture, carried = replay.export_with_copy_anywhere(
            store, run_id, {"copy_definitions": [kanji_grades_and_fonts("fonts.json")]},
            source, media,
        )
    finally:
        source.close()

    assert list(carried.records) == [ids["kanji"]] and later.id not in carried.records
    [kanji] = [n for n in fixture.corpus["notes"] if n["fields"].get("Kanji") == "箱"]
    assert not kanji["selected"] and kanji["notetype"].startswith("Note type ")
    # Carried, and nothing the run read renamed or renumbered for it
    assert kanji["id"] >= replay.SYNTHETIC_UNKNOWN_BASE
    assert fixture.corpus["config"].keys() == replay.export_fixture(store, run_id).corpus[
        "config"].keys()
    # Down to the characters the notes hold
    assert fixture.corpus["media"] == {"fonts.json": {"箱": fonts["箱"], "all_fonts": ["Mincho.ttf"]}}
    assert replay.replay(fixture).differences(fixture.expected) == []
    with_hooks = replay.replay(fixture, copy_anywhere=True)
    assert with_hooks.misses == [] and with_hooks.new_notes == 1
    [box] = [n for n in with_hooks.notes if n["note"] == "new-1"]
    # The grade of the carried kanji note, and not the later one's
    assert box["fields"]["sentence_audio"] == "N3"
    assert box["fields"]["meaning_audio"] == '["Gothic.ttf", "Mincho.ttf"]'


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


@pytest.mark.parametrize(
    "end, ops, refusal",
    [
        # Its end never written: what it lost since is unknown, its finals included
        (None, [replay.MATCH_OP], "no recorded end"),
        ("cancelled", [replay.MATCH_OP], "ended 'cancelled'"),
        ("failed", [replay.MATCH_OP], "ended 'failed'"),
        # The replay runs the match op whatever the run ran
        ("completed", ["bulk_deduplicate_existing_meaning_notes"], "a replay runs"),
    ],
)
def test_a_run_a_replay_cannot_reproduce_is_refused(tmp_path, end, ops, refusal):
    store = tmp_path / "capture.sqlite3"
    assert capture.install(str(store), keep_days=None)
    try:
        run_id = capture.begin_run("notes", ops=ops, notes=True)
        if end is not None:
            capture.end_run(run_id, end)
    finally:
        capture.shutdown()

    with pytest.raises(replay.CaptureGap, match=refusal):
        replay.export_fixture(store, run_id)
