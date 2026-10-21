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
from japanese_note_ai_ops.async_api_ops import (
    base_ops,
    capture_store,
    extract_words,
    run_errors,
    set_furigana,
    transcribe_audio,
)
from japanese_note_ai_ops.async_api_ops.base_ops import NotesRunSpec
from japanese_note_ai_ops.async_api_ops.new_note_all_ops import new_note_all_ops_spec
from japanese_note_ai_ops.configuration import (
    FURIGANA_DOUBT_LEFT,
    FURIGANA_REVIEW_LEFT,
    FURIGANA_REVIEW_TAG,
    REGENERATE_TAG,
)
from japanese_note_ai_ops.word_array import audio_furigana, furigana_arbiter, judge, resources

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


def test_a_replay_hands_each_arbiter_model_its_own_answers():
    # Every model is asked the same about a note's words. Two notes asking the same, their calls
    # interleaved as the pool ran them, were recorded m1, m1, m2, m2: a cassette answering a
    # request key's calls in turn must give note A's m2 call m2's answer, not m1's second
    transcripts = [("kana-anime-whisper", "たのひとわ"), ("ruby-mora", "ホカノヒトワ")]
    answers = {
        "m1": {"words": [{"n": 1, "why": "", "reading": "た", "sure": True}]},
        "m2": {"words": [{"n": 1, "why": "", "reading": "た", "sure": False}]},
    }

    def in_review() -> audio_furigana.Furigana:
        word = audio_furigana.Reading(0, 0, 1, "他", "ほか", None, "review", ["タ", "ホカ"])
        word.known = ["た", "ほか"]
        return audio_furigana.Furigana(["他の人は"], [word], False)

    def ask(answer: Any) -> tuple[list, str]:
        asked: list = []

        def responder(request: Any) -> Any:
            asked.append(request)
            return answer(request)

        previous = base_ops.set_responder(responder)
        try:
            result = in_review()
            set_furigana.settle_by_arbiter(["m1", "m2"], result, "他の人は", "", transcripts)
        finally:
            base_ops.set_responder(previous)
        return asked, result.text

    requests, _ = ask(lambda request: answers[request.model])
    by_model = {request.model: request for request in requests}
    keys = {model: capture_store.request_key(r.kind, r.inputs) for model, r in by_model.items()}
    assert keys["m1"] != keys["m2"]
    calls = [
        {
            "kind": by_model[model].kind,
            "request_key": keys[model],
            "inputs_json": json.dumps(by_model[model].inputs, ensure_ascii=False),
            "response_json": json.dumps(answers[model], ensure_ascii=False),
            "outcome": "ok",
            "prompt_key": None,
            "latency_ms": 0,
        }
        for model in ("m1", "m1", "m2", "m2")
    ]
    cassette = replay.Cassette(replay._cassette_entries(calls))

    replayed = [ask(cassette)[1], ask(cassette)[1]]

    # As captured: m2 was not sure, so the word stays in review in both notes
    assert replayed == ["他[ほか？]の人は", "他[ほか？]の人は"]
    assert cassette.misses == []
    # The prompt is the inputs' but for the model, which it does not show
    for request in requests:
        inputs = {k: v for k, v in request.inputs.items() if k != "model"}
        assert furigana_arbiter.review_prompt(**inputs) == request.prompt


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


def test_a_responder_hears_the_clips_with_no_server(col, reported, monkeypatch):
    # The check that the server is up goes to it past the responder: a replay answering the
    # transcriptions needed one all the same
    stub = real_anki.install()
    stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(NEW_NOTES_CONFIG))
    health: list = []

    class NoServer:
        def get(self, url: str, timeout: Any = None) -> Any:
            health.append(url)
            raise ConnectionError("no server in a replay")

    def answer(request: Any) -> Any:
        if request.kind == "transcribe.audio":
            return {"text": "たのひとわどこにいるの"}
        if request.kind == "kanjify.sentence":
            return {"kanjified_sentence": request.inputs["sentence"]}
        if request.kind == "proper_nouns.sentence":
            return {"proper_nouns": []}
        if request.kind == "judge.word":
            return {"reason": "", "decision": judge.DONT_MATCH}
        raise AssertionError(f"not asked: {request.kind}")

    monkeypatch.setattr(transcribe_audio, "get_session", lambda provider: NoServer())
    Path(col.media.dir(), "clip.opus").write_bytes(b"clip bytes")
    # Only the first model's line: the run asks for ruby-mora's
    note = sentence(col, "他の人はどこにいるの", "たのひとわどこにいるの")

    run(col, [note], spec=new_note_all_ops_spec(), answer=answer)

    held = col.get_note(note.id)
    assert health == []
    assert held["Heard"].endswith("ruby-mora: たのひとわどこにいるの")
    assert held["Sentence furigana"] == "他[た]の 人[ひと]はどこにいるの"
    assert reported == []


def test_a_clip_is_the_file_anki_plays_for_its_sound_tag(col):
    # Anki's editor writes a name HTML-escaped, and its player reads the file unescaped
    for name in ("Spice &amp; Wolf_01.opus", "a&nbsp;b.opus", "a&lt;b&gt;.opus", "a&amp;lt;b.opus",
                 "Tom&Jerry.opus", "a&copy.opus"):
        field = f"[sound:{name}]"
        [played] = col._backend.extract_av_tags(text=field, question_side=True).av_tags
        assert transcribe_audio.audio_file(field) == played.sound_or_video, field


def test_a_run_whose_server_is_down_says_so_once_and_goes_on(col, reported, monkeypatch):
    stub = real_anki.install()
    stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(NEW_NOTES_CONFIG))

    class NoServer:
        def get(self, url: str, timeout: Any = None) -> Any:
            raise ConnectionError("[Errno 111] Connection refused")

    def answer(request: Any) -> Any:
        if request.kind == "kanjify.sentence":
            return {"kanjified_sentence": request.inputs["sentence"]}
        if request.kind == "proper_nouns.sentence":
            return {"proper_nouns": []}
        if request.kind == "judge.word":
            return {"reason": "", "decision": judge.DONT_MATCH}
        raise AssertionError(f"not asked: {request.kind}")

    monkeypatch.setattr(transcribe_audio, "get_session", lambda provider: NoServer())
    # The check is made, as in Anki, where no responder hears the clips
    monkeypatch.setattr(transcribe_audio, "responder_active", lambda: False)
    # A clip each and no transcript yet
    new = [sentence(col, "他の人はどこにいるの"), sentence(col, "他の人は")]
    typed = sentence(col, "他の人は", furigana="他[ほか]の 人[ひと]は")

    run(col, [*new, typed], spec=new_note_all_ops_spec(), answer=answer)

    # Said once, not again for each note as "run Transcribe sentence audio first"
    [(_, text)] = reported
    assert "does not answer" in text
    assert [col.get_note(note.id)["Sentence furigana"] for note in new] == ["", ""]
    # The rest of the run went on
    assert col.get_note(typed.id)["Kanjified"] == "他[ほか]の 人[ひと]は"


@pytest.mark.parametrize("transcripts", [True, False])
def test_the_runs_undo_name_counts_the_selection_whichever_phase_is_first(col, transcripts):
    # The audio phases come first where the config keeps transcripts, and "New note ops" where
    # it does not; a note of half a broken pair is left out of the run either way
    stub = real_anki.install()
    config = json.loads(json.dumps(NEW_NOTES_CONFIG))
    config["Broken note"] = {"vocab_note_type": "Ghost note"}
    if not transcripts:
        del config[SENTENCE]["sentence_transcription_field"]
    stub.addonManager.configs[PACKAGE] = config
    real_anki.make_note_type(col, "Broken note", ["Sentence"])
    said = "たのひとわどこにいるの"
    notes = [
        sentence(col, "他の人はどこにいるの", said, said),
        real_anki.add_note(col, "Broken note", {"Sentence": "猫"}, deck_name="Sentences"),
    ]

    def answer(request: Any) -> Any:
        if request.kind == "kanjify.sentence":
            return {"kanjified_sentence": request.inputs["sentence"]}
        if request.kind == "proper_nouns.sentence":
            return {"proper_nouns": []}
        if request.kind == "judge.word":
            return {"reason": "", "decision": judge.DONT_MATCH}
        raise AssertionError(f"not asked: {request.kind}")

    run(col, notes, spec=new_note_all_ops_spec(), answer=answer)

    assert col.undo_status().undo == "Running new note all ops for 2 notes."


EXTRACT = NotesRunSpec(
    "Extracted words", "Extracting words", extract_words.bulk_extract_from_notes_op
)


def extracted_then_set_again(col: Any) -> list:
    """Two notes holding word arrays extracted from their kanjified furigana, then their
    furigana emptied and set again from audio that leaves 他 in review: one keeps its tag, the
    other's was taken off before its ？ word was fixed."""
    stub = real_anki.install()
    stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(NEW_NOTES_CONFIG))
    notes = [
        sentence(col, "他の人はどこにいるの", "ほかのひとわどこにいるの", "たのひとわどこにいるの")
        for _ in range(2)
    ]
    for note in notes:
        held = col.get_note(note.id)
        held["Kanjified"] = "他[ほか]の 人[ひと]はどこにいるの"
        col.update_note(held)
    run(col, notes, spec=EXTRACT, answer=lambda request: {"proper_nouns": []})
    run(col, notes)
    untagged = col.get_note(notes[1].id)
    untagged.remove_tag(FURIGANA_REVIEW_TAG)
    col.update_note(untagged)
    for note in notes:
        assert col.get_note(note.id)["Words"].startswith("[")
        assert "？" in col.get_note(note.id)["Sentence furigana"]
    return notes


def test_extracting_again_says_nothing_of_notes_in_review_that_hold_arrays(col, reported):
    # A re-run does nothing to a note holding an array: reported, it made an error pane and a
    # warning box of a run with nothing to do
    notes = extracted_then_set_again(col)
    reported.clear()
    arrays = [col.get_note(note.id)["Words"] for note in notes]

    result = run(col, notes, spec=EXTRACT, answer=lambda request: {"proper_nouns": []})

    assert result.edited_nids == []
    assert [col.get_note(note.id)["Words"] for note in notes] == arrays
    assert reported == []


def test_regenerating_notes_in_review_is_refused_and_said(col, reported):
    # Regenerating would read the furigana in doubt
    notes = extracted_then_set_again(col)
    reported.clear()
    regenerate = NotesRunSpec(
        "Regenerated word arrays", "Regenerating", extract_words.bulk_regenerate_from_notes_op
    )

    result = run(col, notes, spec=regenerate, answer=lambda request: {"proper_nouns": []})

    assert result.edited_nids == []
    said = sorted(text for _, text in reported)
    assert said == sorted([FURIGANA_DOUBT_LEFT, FURIGANA_REVIEW_LEFT])


def test_a_note_without_its_transcripts_is_left_and_reported(col, reported):
    note = sentence(col, "他の人は", "たのひとわ")

    result = run(col, [note])

    assert result.edited_nids == []
    assert furigana(col, note) == ""
    [(_, text)] = reported
    assert "No transcript from ruby-mora yet" in text
