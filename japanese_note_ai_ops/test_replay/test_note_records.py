"""What a notes run records of a note, read from a real collection: the record a replay builds
the note from again."""

from __future__ import annotations

from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import capture_notes


def test_a_note_stored_short_of_its_type_s_fields_is_recorded_as_stored(tmp_path):
    # A note saved before its note type gained fields holds fewer values than the type has
    # fields; note.items() raises for it, and the run lost the note's record
    col = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(col, "Vocab", ["Word", "Sentence", "Array"])
        note = real_anki.add_note(col, "Vocab", {"Word": "本", "Sentence": "s", "Array": "[]"})
        col.db.execute("update notes set flds = ? where id = ?", "本", note.id)

        record = capture_notes.note_record(col.get_note(note.id))

        assert record["fields"] == {"Word": "本"}
    finally:
        col.close()


def test_a_whole_note_is_recorded_field_by_field_in_its_type_s_order(tmp_path):
    col = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(col, "Vocab", ["Word", "Sentence", "Array"])
        note = real_anki.add_note(col, "Vocab", {"Word": "本", "Sentence": "s", "Array": "[]"})

        record = capture_notes.note_record(col.get_note(note.id))

        assert list(record["fields"].items()) == [("Word", "本"), ("Sentence", "s"), ("Array", "[]")]
    finally:
        col.close()
