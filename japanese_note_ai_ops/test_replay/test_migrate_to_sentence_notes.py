"""Move sentences to sentence notes (sync_local_ops/migrate_to_sentence_notes.py) in a real
collection: a vocab note type that still has the old sentence fields, named as on the sentence
note type, and the run started through its NotesRunSpec, as a script starts it.

The notes, all made up here, in the order they are added (so the first of a sentence is the
oldest):

- 猫が魚を食べた。: the dependent CAT, then the source FISH, whose array links CAT and leaves
  its own word for the link check. FISH carries a child of a moved tag, a copied tag and the
  addon's kanjify_sentence_mismatch; CAT a moved tag put on by hand and a copied tag, which a
  dependent does not give.
- 犬が走る。: two sources, DOG and RUN, each linking its own word in its own array: one sentence
  note, both links.
- 箱がある。: BOX, a source with no array: a sentence note for Extract words to fill.
- 本を読む。: the dependent READ, then BOOK, the origin of its sentence, which carries the
  addon's tag and a moved tag. Left unselected, it still loses the addon's tag once its
  sentence note exists, and keeps the moved one until a run selects it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from anki import hooks
from anki.notes import NoteId

from anki_shared.testing import real_anki
from japanese_note_ai_ops import call_logging, note_hooks
from japanese_note_ai_ops.async_api_ops import base_ops
from japanese_note_ai_ops.link_ids import new_link_id
from japanese_note_ai_ops.sync_local_ops import migrate_to_sentence_notes as migrate_module
from japanese_note_ai_ops.note_roles import note_type_search
from japanese_note_ai_ops.sync_local_ops.migrate_to_sentence_notes import (
    PreflightError,
    SentenceMigration,
    collection_preflight_error,
)
from japanese_note_ai_ops.word_array.match_flags import format_word_array

PACKAGE = "japanese_note_ai_ops"
VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"
SHOW = "Made_Up_Show"
BAND = "freq-band-a"
MISMATCH = "kanjify_sentence_mismatch"
DEPENDENT = "new_matched_jp_word"
NEEDS_EXTRACT = "sentence-needs-extract"
SUSPENDED = -1

SENTENCE_FIELDS = {
    "sentence_field": "Sentence",
    "furigana_sentence_field": "Sentence furigana",
    "kanjified_sentence_field": "Sentence kanjified",
    "word_extraction_sentence_field": "Sentence for extraction",
    "translated_sentence_field": "Sentence translation",
    "sentence_audio_field": "Sentence audio",
    "word_list_field": "Word array",
}
CONFIG = {
    SENTENCE: {
        "vocab_note_type": VOCAB,
        **SENTENCE_FIELDS,
        "sentence_seen_count_field": "Times seen",
        "sentence_id_field": "Sentence ID",
        "migration_move_tags": [SHOW],
        "migration_copy_tags": [BAND],
        "insert_deck": "Sentences",
    },
    VOCAB: {
        "sentence_note_type": SENTENCE,
        "example_sentence_id_field": "Example sentence id",
        "translated_sentence_field": "Example translation",
        "sentence_audio_field": "Example audio",
        "word_kanjified_field": "Word kanjified",
        "word_normal_field": "Word",
        "word_reading_field": "Word reading",
        "word_sort_field": "Word sort key",
        "new_note_id_field": "Word ID",
        "insert_deck": "Vocab",
    },
    "log_to_console": False,
}
# The vocab type as it is when the migration runs: its own fields, then the old sentence fields
# under the sentence type's names (the sentence type began as a copy of it)
VOCAB_FIELDS = [
    "Word sort key",
    "Word",
    "Word kanjified",
    "Word reading",
    "Word ID",
    "Example sentence id",
    "Example translation",
    "Example audio",
    *SENTENCE_FIELDS.values(),
]
SENTENCE_TYPE_FIELDS = [*SENTENCE_FIELDS.values(), "Times seen", "Sentence ID"]

CAT_FISH = "猫が魚を食べた。"
DOG_RUNS = "犬が走る。"
BOX = "箱がある。"
BOOK = "本を読む。"
CAT_FISH_TRANSLATION = "The cat ate the fish."


def word(raw: str, form: str, reading: str, match_data: list) -> list:
    return [raw, "noun", form, reading, match_data, []]


def particle(raw: str) -> list:
    return [raw, "particle", raw, raw, ["dontmatch"], []]


def cat_fish_array(cat: list, fish: list) -> list:
    return [
        word(" 猫[ねこ]", "猫", "ねこ", cat),
        particle("が"),
        word(" 魚[さかな]", "魚", "さかな", fish),
        particle("を"),
        word(" 食[た]べた", "食べる", "たべる", ["match"]),
        ["。"],
    ]


def dog_runs_array(dog: list, runs: list) -> list:
    return [
        word(" 犬[いぬ]", "犬", "いぬ", dog),
        particle("が"),
        word(" 走[はし]る", "走る", "はしる", runs),
        ["。"],
    ]


def book_array(book: list, read: list) -> list:
    return [
        word(" 本[ほん]", "本", "ほん", book),
        particle("を"),
        word(" 読[よ]む", "読む", "よむ", read),
        ["。"],
    ]


def add_vocab(
    col: Any,
    form: str,
    reading: str,
    sentence: str,
    furigana: str,
    translation: str = "",
    audio: str = "",
    tags: tuple = (),
    array: Any = None,
    reps: int = 0,
    word_id: str = "",
    example_id: str = "",
) -> int:
    """A vocab note as the one-type layout made them: `sentence` and `furigana` with its word in
    <b>; `array(own_id)` its old array, which may link the note itself."""
    fields = {
        "Word sort key": form,
        "Word": form,
        "Word kanjified": form,
        "Word reading": reading,
        "Word ID": word_id,
        "Example sentence id": example_id,
        "Sentence": sentence,
        "Sentence furigana": furigana,
        "Sentence kanjified": sentence,
        "Sentence for extraction": furigana,
        "Sentence translation": translation,
        "Sentence audio": audio,
    }
    note = real_anki.add_note(col, VOCAB, fields, deck_name="Vocab", tags=list(tags))
    if array is not None:
        note["Word array"] = json.dumps(array(note.id), ensure_ascii=False)
        col.update_note(note)
    if reps:
        col.db.execute("update cards set reps = ? where nid = ?", reps, note.id)
    return note.id


def build(col: Any) -> dict[str, int]:
    ids: dict[str, int] = {}
    ids["cat"] = add_vocab(
        col,
        "猫",
        "ねこ",
        "<b>猫</b>が魚を食べた。",
        "<b> 猫[ねこ]</b>が 魚[さかな]を 食[た]べた。",
        translation="A cat, as its own note had it.",
        audio="[sound:cat.mp3]",
        tags=(DEPENDENT, SHOW, BAND),
    )
    ids["fish"] = add_vocab(
        col,
        "魚",
        "さかな",
        "猫が<b>魚</b>を食べた。",
        " 猫[ねこ]が<b> 魚[さかな]</b>を 食[た]べた。",
        translation=CAT_FISH_TRANSLATION,
        audio="[sound:cat_fish.mp3]",
        tags=(f"{SHOW}::ep01", BAND, MISMATCH),
        array=lambda _own: cat_fish_array([ids["cat"]], []),
        reps=7,
    )
    ids["dog"] = add_vocab(
        col,
        "犬",
        "いぬ",
        "<b>犬</b>が走る。",
        "<b> 犬[いぬ]</b>が 走[はし]る。",
        translation="The dog runs.",
        array=lambda own: dog_runs_array([own], []),
        reps=3,
    )
    ids["runs"] = add_vocab(
        col,
        "走る",
        "はしる",
        "犬が<b>走る</b>。",
        " 犬[いぬ]が<b> 走[はし]る</b>。",
        translation="The dog runs.",
        array=lambda own: dog_runs_array([], [own]),
        reps=1,
    )
    ids["box"] = add_vocab(col, "箱", "はこ", "<b>箱</b>がある。", "<b> 箱[はこ]</b>がある。")
    ids["read"] = add_vocab(
        col,
        "読む",
        "よむ",
        "本を<b>読む</b>。",
        " 本[ほん]を<b> 読[よ]む</b>。",
        tags=(DEPENDENT,),
    )
    ids["book"] = add_vocab(
        col,
        "本",
        "ほん",
        "<b>本</b>を読む。",
        "<b> 本[ほん]</b>を 読[よ]む。",
        translation="I read a book.",
        tags=(MISMATCH, SHOW),
        array=lambda own: book_array([own], [ids["read"]]),
        reps=2,
    )
    return ids


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Any]:
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs))
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(collection, VOCAB, VOCAB_FIELDS)
        real_anki.make_note_type(collection, SENTENCE, SENTENCE_TYPE_FIELDS)
        collection.decks.id("Sentences")
        stub.col = collection
        stub.addonManager.configs[PACKAGE] = json.loads(json.dumps(CONFIG))
        stub.progress.set_cancel(False)
        yield collection
    finally:
        stub.progress.set_cancel(False)
        stub.col, stub.addonManager.configs = saved
        collection.close()


def migrate(col: Any, nids: list[int], report_dir: Path) -> tuple[SentenceMigration, Any]:
    """One run over `nids`, as a script runs it: its migration (the report's path) and result."""
    migration = SentenceMigration(report_dir=str(report_dir))
    run, result = migration.spec().notes_run([NoteId(nid) for nid in nids])
    run(col)
    return migration, result


def sentence_notes(col: Any) -> dict[str, Any]:
    """The sentence notes by their sentence; two of one sentence fail the test."""
    by_sentence: dict[str, Any] = {}
    for nid in col.find_notes(note_type_search(SENTENCE)):
        note = col.get_note(nid)
        assert note["Sentence"] not in by_sentence, f"two sentence notes of {note['Sentence']}"
        by_sentence[note["Sentence"]] = note
    return by_sentence


def state(col: Any, nids: Any) -> dict[int, tuple[dict[str, str], set[str]]]:
    """The notes' fields and tags, to compare before and after."""
    out = {}
    for nid in nids:
        note = col.get_note(nid)
        out[nid] = (dict(note.items()), set(note.tags))
    return out


def card_column(col: Any, column: str, nid: int) -> list:
    return col.db.list(f"select {column} from cards where nid = ?", nid)


def test_a_run_moves_the_sentences_and_one_undo_takes_it_all_back(col, tmp_path):
    ids = build(col)
    selected = [nid for name, nid in ids.items() if name != "book"]
    before = state(col, ids.values())
    undo_before = col.undo_status().undo

    migration, result = migrate(col, selected, tmp_path / "reports")

    assert not result.cancelled
    assert result.new_notes == base_ops.NewNotesCounts(added=4)
    notes = sentence_notes(col)
    assert set(notes) == {CAT_FISH, DOG_RUNS, BOX, BOOK}
    sentences_deck = col.decks.id_for_name("Sentences")
    for note in notes.values():
        assert card_column(col, "did", note.id) == [sentences_deck]
        assert card_column(col, "queue", note.id) == [SUSPENDED]

    # The origin's fields without <b>, the array as combined and linked, the seen count
    cat_fish = notes[CAT_FISH]
    assert cat_fish["Sentence furigana"] == " 猫[ねこ]が 魚[さかな]を 食[た]べた。"
    assert cat_fish["Sentence kanjified"] == CAT_FISH
    assert cat_fish["Sentence for extraction"] == " 猫[ねこ]が 魚[さかな]を 食[た]べた。"
    assert cat_fish["Sentence translation"] == CAT_FISH_TRANSLATION
    assert cat_fish["Sentence audio"] == "[sound:cat_fish.mp3]"
    assert cat_fish["Word array"] == format_word_array(
        cat_fish_array([ids["cat"]], [ids["fish"]])
    )
    assert cat_fish["Times seen"] == "7"
    # Moved from the origin, from a source's child tag and a dependent's; copied from the source
    assert set(cat_fish.tags) == {MISMATCH, f"{SHOW}::ep01", SHOW, BAND}
    dog_runs = notes[DOG_RUNS]
    assert dog_runs["Word array"] == format_word_array(dog_runs_array([ids["dog"]], [ids["runs"]]))
    assert dog_runs["Times seen"] == "3"
    assert dog_runs.tags == []
    assert notes[BOX]["Word array"] == ""
    assert notes[BOX].tags == [NEEDS_EXTRACT]
    assert notes[BOX]["Times seen"] == "0"
    # Made from the unselected origin: its addon's tag, not the moved one it keeps
    assert notes[BOOK]["Times seen"] == "2"
    assert notes[BOOK].tags == [MISMATCH]

    # Each holds the ID generated for it, the first from its origin's link ID, its note id
    assert cat_fish["Sentence ID"] == str(new_link_id(f"sentence|{ids['fish']}", set()))
    sentence_ids = {note["Sentence ID"] for note in notes.values()}
    assert len(sentence_ids) == 4 and all(len(held) == 16 for held in sentence_ids)

    after = state(col, ids.values())
    example = {
        "cat": cat_fish,
        "fish": cat_fish,
        "dog": dog_runs,
        "runs": dog_runs,
        "box": notes[BOX],
        "read": notes[BOOK],
    }
    for name, sentence_note in example.items():
        fields, tags = after[ids[name]]
        old_fields, old_tags = before[ids[name]]
        assert fields["Example sentence id"] == sentence_note["Sentence ID"], name
        assert fields["Example translation"] == sentence_note["Sentence translation"], name
        assert fields["Example audio"] == sentence_note["Sentence audio"], name
        # Its ID field held none: it holds the note id now, the link ID it had all along
        assert fields["Word ID"] == str(ids[name]), name
        # The old fields stay until the user deletes them
        for field in SENTENCE_FIELDS.values():
            assert fields[field] == old_fields[field], (name, field)
    assert after[ids["cat"]][0]["Example translation"] == CAT_FISH_TRANSLATION
    assert after[ids["cat"]][1] == {DEPENDENT, BAND}
    assert after[ids["fish"]][1] == {BAND}
    assert after[ids["read"]][1] == {DEPENDENT}
    # Saved for the tag it loses, its ID field filled with that
    assert after[ids["book"]][0] == {**before[ids["book"]][0], "Word ID": str(ids["book"])}
    assert after[ids["book"]][1] == {SHOW}

    report = Path(migration.report_path).read_text(encoding="utf-8")
    assert "sentence notes to add: 4" in report
    assert "vocab notes whose ID field gets their note id: 7" in report
    assert f'tag "{SHOW}" moved from vocab notes: 2' in report
    assert f'tag "{BAND}" copied from vocab notes: 1' in report
    assert f"Linked to its word by the link check (1)\n  {ids['fish']}: " in report

    assert col.undo_status().undo == "Moving sentences to sentence notes for 6 notes."
    col.undo()
    assert sentence_notes(col) == {}
    assert state(col, ids.values()) == before
    assert col.undo_status().undo == undo_before


def test_the_add_hook_runs_nothing_on_the_sentence_notes_the_run_adds(col, tmp_path):
    """The add hook (__init__.run_op_on_add_note) asks call_logging.in_bulk_op whether an op is
    adding the note: the migration's sentence notes are added in its cleanup, some with an
    empty array, and must get no extract (a request each). A sentence note added by hand
    after the run gets its words extracted."""
    ids = build(col)
    config = real_anki.install().addonManager.configs[PACKAGE]
    seen: list[tuple[str, bool, tuple]] = []

    def on_add(_col: Any, note: Any, _deck_id: Any) -> None:
        name = note.note_type()["name"]
        op_adding = call_logging.in_bulk_op()
        ops = note_hooks.ops_on_added_note(config, name, note.tags, op_adding=op_adding)
        seen.append((name, op_adding, ops))

    hooks.note_will_be_added.append(on_add)
    try:
        _, result = migrate(col, list(ids.values()), tmp_path / "reports")
        assert result.new_notes == base_ops.NewNotesCounts(added=4)
        assert seen == [(SENTENCE, True, ())] * 4
        assert not call_logging.in_bulk_op()

        seen.clear()
        real_anki.add_note(col, SENTENCE, {"Sentence": "猫だ。"}, deck_name="Sentences")
        assert seen == [(SENTENCE, False, (note_hooks.EXTRACT_WORDS,))]
    finally:
        hooks.note_will_be_added.remove(on_add)


def test_a_rerun_over_every_note_joins_the_sentence_notes_a_first_run_made(col, tmp_path):
    ids = build(col)
    sources = [ids[name] for name in ("fish", "dog", "runs", "box", "book")]
    migrate(col, sources, tmp_path / "first")
    first = {sentence: note.id for sentence, note in sentence_notes(col).items()}
    assert set(first) == {CAT_FISH, DOG_RUNS, BOX, BOOK}
    assert state(col, [ids["cat"]])[ids["cat"]][0]["Example sentence id"] == ""

    migration, result = migrate(col, list(ids.values()), tmp_path / "second")

    assert result.new_notes.prepared == 0
    notes = sentence_notes(col)
    assert {sentence: note.id for sentence, note in notes.items()} == first
    cat_fields, cat_tags = state(col, [ids["cat"]])[ids["cat"]]
    assert cat_fields["Example sentence id"] == notes[CAT_FISH]["Sentence ID"]
    assert cat_fields["Word ID"] == str(ids["cat"])
    assert cat_tags == {DEPENDENT, BAND}
    # The dependent's moved tag goes to the sentence note it joins
    assert SHOW in notes[CAT_FISH].tags
    read_fields, _ = state(col, [ids["read"]])[ids["read"]]
    assert read_fields["Example sentence id"] == notes[BOOK]["Sentence ID"]
    report = Path(migration.report_path).read_text(encoding="utf-8")
    assert "sentence notes to add: 0" in report
    assert "existing sentence notes joined: 2" in report
    assert "skipped: already migrated: 5" in report


def every_note_has_its_example(col: Any, ids: dict[str, int]) -> None:
    notes = sentence_notes(col)
    assert set(notes) == {CAT_FISH, DOG_RUNS, BOX, BOOK}
    sentence_of = {
        "cat": CAT_FISH,
        "fish": CAT_FISH,
        "dog": DOG_RUNS,
        "runs": DOG_RUNS,
        "box": BOX,
        "read": BOOK,
        "book": BOOK,
    }
    for name, sentence in sentence_of.items():
        note = col.get_note(ids[name])
        assert note["Example sentence id"] == notes[sentence]["Sentence ID"], name
        assert note["Word ID"] == str(ids[name]), name


def test_a_cancelled_adding_leaves_the_rest_untouched_and_a_rerun_finishes(
    col, tmp_path, monkeypatch
):
    ids = build(col)
    before = state(col, ids.values())
    adds: list = []
    add_note = col.add_note

    def counted_add(note: Any, deck_id: Any) -> Any:
        adds.append(note)
        return add_note(note, deck_id)

    # The cleanup's own Cancel, pressed once two sentence notes are in
    monkeypatch.setattr(col, "add_note", counted_add)
    monkeypatch.setattr(
        base_ops.AsyncTaskProgressUpdater, "cleanup_cancel_requested", lambda self: len(adds) >= 2
    )
    # Selected newest first: the notes are processed, and their sentence notes added, in the
    # plan's order all the same
    _, result = migrate(col, list(reversed(ids.values())), tmp_path / "cancelled")
    monkeypatch.undo()

    assert result.new_notes == base_ops.NewNotesCounts(added=2, not_added=2)
    # Added in the plan's order, oldest origin first
    assert set(sentence_notes(col)) == {CAT_FISH, DOG_RUNS}
    after = state(col, ids.values())
    for name in ("box", "read", "book"):
        assert after[ids[name]] == before[ids[name]], name
    for name in ("cat", "fish", "dog", "runs"):
        assert after[ids[name]][0]["Example sentence id"], name

    _, result = migrate(col, list(ids.values()), tmp_path / "rerun")

    assert result.new_notes == base_ops.NewNotesCounts(added=2)
    every_note_has_its_example(col, ids)


def test_a_run_cancelled_between_notes_adds_what_it_registered_and_a_rerun_finishes(
    col, tmp_path, monkeypatch
):
    ids = build(col)
    before = state(col, ids.values())
    stub = real_anki.install()
    note_op = SentenceMigration.note_op
    processed: list = []

    def cancelling(self: SentenceMigration, **kwargs: Any) -> bool:
        done = note_op(self, **kwargs)
        processed.append(kwargs["note"].id)
        if len(processed) == 2:
            stub.progress.set_cancel(True)
        return done

    monkeypatch.setattr(SentenceMigration, "note_op", cancelling)
    _, result = migrate(col, list(reversed(ids.values())), tmp_path / "cancelled")
    monkeypatch.undo()
    stub.progress.set_cancel(False)

    # The first two notes are the two of the first sentence in the plan, which they registered
    assert sorted(processed) == sorted([ids["cat"], ids["fish"]])
    assert result.cancelled
    assert result.new_notes == base_ops.NewNotesCounts(added=1)
    assert set(sentence_notes(col)) == {CAT_FISH}
    after = state(col, ids.values())
    for name in ("dog", "runs", "box", "read", "book"):
        assert after[ids[name]] == before[ids[name]], name

    _, result = migrate(col, list(ids.values()), tmp_path / "rerun")

    assert not result.cancelled
    assert result.new_notes == base_ops.NewNotesCounts(added=3)
    every_note_has_its_example(col, ids)


def test_a_failed_example_copy_leaves_the_vocab_notes_and_the_run_stays_one_undo(
    col, tmp_path, monkeypatch
):
    ids = build(col)
    before = state(col, ids.values())

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the copy broke")

    monkeypatch.setattr(migrate_module, "copy_example", broken)
    _, result = migrate(col, list(ids.values()), tmp_path / "reports")

    # Added and suspended, and no vocab note changed: the next run joins them
    assert result.new_notes == base_ops.NewNotesCounts(added=4)
    notes = sentence_notes(col)
    assert len(notes) == 4
    for note in notes.values():
        assert card_column(col, "queue", note.id) == [SUSPENDED]
    assert state(col, ids.values()) == before
    # The suspension too is in the run's undo entry, though there was no vocab note to save
    assert col.undo_status().undo == "Moving sentences to sentence notes for 7 notes."
    col.undo()
    assert sentence_notes(col) == {}


def test_the_run_refuses_a_vocab_type_without_an_old_field_and_writes_nothing(col, tmp_path):
    ids = build(col)
    notetype = col.models.by_name(VOCAB)
    [audio] = [field for field in notetype["flds"] if field["name"] == "Sentence audio"]
    col.models.remove_field(notetype, audio)
    col.models.update_dict(notetype)
    undo_before = col.undo_status().undo
    nids = list(ids.values())
    config = real_anki.install().addonManager.configs[PACKAGE]

    error = collection_preflight_error(col, config, nids)
    assert error is not None and '"Sentence audio" (sentence_audio_field)' in error
    with pytest.raises(PreflightError, match="Sentence audio"):
        migrate(col, nids, tmp_path / "refused")

    assert sentence_notes(col) == {}
    assert col.undo_status().undo == undo_before
    assert not (tmp_path / "refused").exists()


def test_the_run_refuses_the_one_type_layout(col, tmp_path):
    ids = build(col)
    config = real_anki.install().addonManager.configs[PACKAGE]
    del config[VOCAB]["sentence_note_type"]
    del config[SENTENCE]

    with pytest.raises(PreflightError, match="names no sentence_note_type"):
        migrate(col, list(ids.values()), tmp_path / "refused")
    assert sentence_notes(col) == {}


# Link IDs no note has as its note id (link_ids.py), and a legacy placeholder: the negative id a
# match op from before link IDs linked a new note by until it was added
FISH_ID = 1160697660039337
DOG_RUNS_ID = 4000000000000001
PLACEHOLDER = -777


def add_sentence(col: Any, sentence: str, furigana: str, array: list, sentence_id: str = "") -> int:
    """A sentence note already there, as a first run or the user made it."""
    fields = {
        "Sentence": sentence,
        "Sentence furigana": furigana,
        "Sentence kanjified": sentence,
        "Sentence for extraction": furigana,
        "Word array": format_word_array(array),
        "Sentence ID": sentence_id,
    }
    return int(real_anki.add_note(col, SENTENCE, fields, deck_name="Sentences").id)


def eating(cat: list, fish: list, eat: list) -> list:
    arr = cat_fish_array(cat, fish)
    arr[4][4] = eat
    return arr


def test_links_are_read_as_link_ids_and_each_note_written_holds_its_own(col, tmp_path):
    """IDs other than note ids: the source 魚 holds a generated ID, which its array links it
    by; the dependent 食べる's add was never resolved, so its field and that array hold a
    legacy placeholder; the sentence note of 犬が走る。 holds an ID of its own, which 犬 names
    as its example (migrated by a first run), and the one of 本を読む。 holds none."""
    ids: dict[str, int] = {}
    ids["cat"] = add_vocab(
        col,
        "猫",
        "ねこ",
        "<b>猫</b>が魚を食べた。",
        "<b> 猫[ねこ]</b>が 魚[さかな]を 食[た]べた。",
        tags=(DEPENDENT,),
    )
    ids["eat"] = add_vocab(
        col,
        "食べる",
        "たべる",
        "猫が魚を<b>食べた</b>。",
        " 猫[ねこ]が 魚[さかな]を<b> 食[た]べた</b>。",
        tags=(DEPENDENT,),
        word_id=str(PLACEHOLDER),
    )
    ids["fish"] = add_vocab(
        col,
        "魚",
        "さかな",
        "猫が<b>魚</b>を食べた。",
        " 猫[ねこ]が<b> 魚[さかな]</b>を 食[た]べた。",
        word_id=str(FISH_ID),
        array=lambda _own: eating([ids["cat"]], [FISH_ID], [PLACEHOLDER]),
    )
    ids["dog"] = add_vocab(
        col,
        "犬",
        "いぬ",
        "<b>犬</b>が走る。",
        "<b> 犬[いぬ]</b>が 走[はし]る。",
        array=lambda own: dog_runs_array([own], []),
        example_id=str(DOG_RUNS_ID),
    )
    ids["runs"] = add_vocab(
        col,
        "走る",
        "はしる",
        "犬が<b>走る</b>。",
        " 犬[いぬ]が<b> 走[はし]る</b>。",
        array=lambda own: dog_runs_array([], [own]),
    )
    ids["book"] = add_vocab(
        col,
        "本",
        "ほん",
        "<b>本</b>を読む。",
        "<b> 本[ほん]</b>を 読[よ]む。",
        array=lambda own: book_array([own], ["match"]),
    )
    dog_runs = add_sentence(
        col,
        DOG_RUNS,
        " 犬[いぬ]が 走[はし]る。",
        dog_runs_array([ids["dog"]], ["match"]),
        str(DOG_RUNS_ID),
    )
    book = add_sentence(col, BOOK, " 本[ほん]を 読[よ]む。", book_array(["match"], ["match"]))
    everything = [*ids.values(), dog_runs, book]
    before = state(col, everything)

    migration, result = migrate(col, list(ids.values()), tmp_path / "reports")

    assert result.new_notes == base_ops.NewNotesCounts(added=1)
    notes = sentence_notes(col)
    cat_fish = notes[CAT_FISH]
    # From its origin's ID, and none a sentence note holds: the one held, the other's note id
    assert cat_fish["Sentence ID"] == str(
        new_link_id(f"sentence|{FISH_ID}", {DOG_RUNS_ID, book})
    )
    # The placeholder is the ID of the note holding it: its note id
    assert cat_fish["Word array"] == format_word_array(
        eating([ids["cat"]], [FISH_ID], [ids["eat"]])
    )
    for name in ("cat", "eat", "fish"):
        assert col.get_note(ids[name])["Example sentence id"] == cat_fish["Sentence ID"], name
    # Joined by the IDs they hold
    assert col.get_note(ids["runs"])["Example sentence id"] == str(DOG_RUNS_ID)
    assert notes[DOG_RUNS]["Word array"] == format_word_array(
        dog_runs_array([ids["dog"]], [ids["runs"]])
    )
    assert notes[DOG_RUNS]["Sentence ID"] == str(DOG_RUNS_ID)
    assert col.get_note(ids["book"])["Example sentence id"] == str(book)
    # Written for its link to 本, and holding no ID: its note id
    assert notes[BOOK]["Sentence ID"] == str(book)
    word_ids = {name: col.get_note(nid)["Word ID"] for name, nid in ids.items()}
    assert word_ids == {
        "cat": str(ids["cat"]),
        "eat": str(ids["eat"]),
        "fish": str(FISH_ID),
        # Migrated already: not written
        "dog": "",
        "runs": str(ids["runs"]),
        "book": str(ids["book"]),
    }
    report = Path(migration.report_path).read_text(encoding="utf-8")
    assert "legacy placeholders linked to the note holding them: 1" in report
    assert "existing sentence notes whose ID field gets their note id: 1" in report
    assert "skipped: already migrated: 1" in report

    col.undo()
    assert state(col, everything) == before
    assert set(sentence_notes(col)) == {DOG_RUNS, BOOK}


def assert_refused_unchanged(col: Any, nids: list[int], tmp_path: Path, *in_error: str) -> None:
    """The pre-flight's message holds each of `in_error`, and the run refuses with it before
    writing anything."""
    config = real_anki.install().addonManager.configs[PACKAGE]
    error = collection_preflight_error(col, config, [NoteId(nid) for nid in nids])
    assert error is not None
    for text in in_error:
        assert text in error
    undo_before = col.undo_status().undo
    count_before = col.note_count()
    with pytest.raises(PreflightError) as refused:
        migrate(col, nids, tmp_path / "refused")
    assert str(refused.value) == error
    assert col.note_count() == count_before
    assert col.undo_status().undo == undo_before
    assert not (tmp_path / "refused").exists()


def test_the_run_refuses_while_two_vocab_notes_hold_one_link_id(col, tmp_path):
    # Anki's Create Copy of a note whose ID field holds its note id, as the match op left the
    # notes it made: the copy holds the original's
    ids = build(col)
    fish = col.get_note(ids["fish"])
    fish["Word ID"] = str(fish.id)
    col.update_note(fish)
    copy = real_anki.add_note(col, VOCAB, dict(fish.items()), deck_name="Vocab")
    assert_refused_unchanged(
        col,
        [*ids.values(), copy.id],
        tmp_path,
        f'Notes of "{VOCAB}" hold the same link ID in their field "Word ID"',
        f"{fish.id} by notes {fish.id}, {copy.id}",
        'Run "Fill link IDs" on these notes first',
    )


def test_the_run_refuses_while_two_sentence_notes_hold_one_link_id(col, tmp_path):
    ids = build(col)
    first, second = (
        add_sentence(col, sentence, sentence, [], str(DOG_RUNS_ID))
        for sentence in ("猫だ。", "犬だ。")
    )
    assert_refused_unchanged(
        col,
        list(ids.values()),
        tmp_path,
        f'Notes of "{SENTENCE}" hold the same link ID in their field "Sentence ID"',
        f"{DOG_RUNS_ID} by notes {first}, {second}",
        'Run "Fill link IDs" on these notes first',
    )


def test_the_run_refuses_a_link_id_field_no_search_can_name(col, tmp_path):
    ids = build(col)
    notetype = col.models.by_name(VOCAB)
    col.models.add_field(notetype, col.models.new_field("nid"))
    col.models.update_dict(notetype)
    real_anki.install().addonManager.configs[PACKAGE][VOCAB]["new_note_id_field"] = "nid"
    assert_refused_unchanged(
        col,
        list(ids.values()),
        tmp_path,
        f'The link ID field "nid" of note type "{VOCAB}" cannot be searched',
    )
