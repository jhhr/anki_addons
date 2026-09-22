"""The "Extract words" op: generate the word array, then ask about proper nouns.

The guards are the whole of what it decides on its own, and both of them protect a field that
already holds something: an array means the op has run (a re-run is free), and an old
extract_words word list means the note ids of its matched words would be thrown away, which
only the migration is allowed to carry over.

"Regenerate words" (`overwrite=True`) is the way past the first guard and not the second, and
what it writes is the merge of the two arrays (`word_array/merge.py`, tested on its own in
test_word_array_merge.py); the cases here are about which of the two paths a field takes.
"""

import json
import sys
import unittest
from types import ModuleType
from unittest import mock

from addon_modules import load_ops_module

sudachipy = ModuleType("sudachipy")
setattr(sudachipy, "Dictionary", object)
setattr(sudachipy, "SplitMode", object)
with mock.patch.dict(sys.modules, {"sudachipy": sudachipy}):
    extract_words = load_ops_module("extract_words")

FIELDS = {
    "word_extraction_sentence_field": "sentence",
    "word_list_field": "words",
}


class FakeNote:
    def __init__(self, fields, note_id=1):
        self.id = note_id
        self.fields = fields

    def note_type(self):
        return {"name": "Sentence"}

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def __setitem__(self, field, value):
        self.fields[field] = value


def run(note, updates, array=None, overwrite=False):
    """The op with the generator and the proper noun call stubbed out."""
    with (
        mock.patch.object(
            extract_words,
            "get_field_config",
            side_effect=lambda _config, key, _note_type: FIELDS[key],
        ),
        mock.patch.object(
            extract_words, "generate_word_array", return_value=array or []
        ) as generate,
        mock.patch.object(extract_words, "add_proper_nouns", return_value=True) as proper_nouns,
    ):
        changed = extract_words.extract_words_in_note({}, note, {}, updates, overwrite=overwrite)
    return changed, generate, proper_nouns


class ExtractWordsInNoteTests(unittest.TestCase):
    def test_an_empty_field_gets_the_generated_array(self):
        note = FakeNote({"sentence": "本を読む。", "words": ""})
        updates: dict = {}

        changed, generate, proper_nouns = run(note, updates, array=[["本"], ["を"]])

        self.assertTrue(changed)
        generate.assert_called_once()
        proper_nouns.assert_called_once()
        # Written through match_flags.format_word_array: a row per top-level word.
        self.assertEqual(note["words"], '[\n  ["本"],\n  ["を"]\n]')
        self.assertEqual(updates, {note.id: note})

    def test_the_context_sentences_are_stripped_before_generating(self):
        note = FakeNote({"sentence": "<i>前の文。</i>本を読む。", "words": ""})

        _changed, generate, _proper_nouns = run(note, {})

        self.assertEqual(generate.call_args.args[0], "本を読む。")

    def test_a_note_that_already_holds_an_array_is_skipped(self):
        note = FakeNote({"sentence": "本を読む。", "words": '[["本"]]'})
        updates: dict = {}

        changed, generate, proper_nouns = run(note, updates)

        self.assertFalse(changed)
        generate.assert_not_called()
        proper_nouns.assert_not_called()
        self.assertEqual(note["words"], '[["本"]]')
        self.assertEqual(updates, {})

    def test_a_broken_array_is_left_for_a_person(self):
        # A bracket lost in a hand edit: the matched note id must not be generated over
        broken = '[["本", "noun", "本", "ほん", [1378555077520], []]'
        note = FakeNote({"sentence": "本を読む。", "words": broken})
        updates: dict = {}

        changed, generate, proper_nouns = run(note, updates)

        self.assertFalse(changed)
        generate.assert_not_called()
        proper_nouns.assert_not_called()
        self.assertEqual(note["words"], broken)
        self.assertEqual(updates, {})

    def test_a_note_with_no_sentence_gets_nothing(self):
        note = FakeNote({"sentence": "", "words": ""})

        changed, generate, _proper_nouns = run(note, {})

        self.assertFalse(changed)
        generate.assert_not_called()

    def test_a_generator_failure_leaves_the_field_alone(self):
        note = FakeNote({"sentence": "本を読む。", "words": ""})
        updates: dict = {}

        with (
            mock.patch.object(
                extract_words,
                "get_field_config",
                side_effect=lambda _config, key, _note_type: FIELDS[key],
            ),
            mock.patch.object(
                extract_words, "generate_word_array", side_effect=RuntimeError("no dictionary")
            ),
            mock.patch.object(extract_words, "add_proper_nouns") as proper_nouns,
        ):
            changed = extract_words.extract_words_in_note({}, note, {}, updates)

        self.assertFalse(changed)
        proper_nouns.assert_not_called()
        self.assertEqual(note["words"], "")
        self.assertEqual(updates, {})

    def test_a_new_note_is_written_but_not_queued_for_update(self):
        # A note being added has no id yet: the hook writing into it is the whole point, and
        # putting id 0 in the update dict would have Anki look for a note that does not exist.
        note = FakeNote({"sentence": "本を読む。", "words": ""}, note_id=0)
        updates: dict = {}

        changed, _generate, _proper_nouns = run(note, updates, array=[["本"]])

        self.assertTrue(changed)
        self.assertEqual(note["words"], '[\n  ["本"]\n]')
        self.assertEqual(updates, {})


class RegenerateWordsTests(unittest.TestCase):
    def word(self, text, form, reading, match_data=None):
        return [text, "noun", form, reading, match_data if match_data is not None else [], []]

    def test_only_the_rows_the_correction_reached_are_replaced(self):
        held = [self.word("A", "甲", "こう", ["match"]), self.word("B", "乙", "おつ")]
        note = FakeNote({"sentence": "AB", "words": json.dumps(held, ensure_ascii=False)})
        updates: dict = {}
        regenerated = [self.word("A", "甲", "こう"), self.word("C", "乙", "おつ")]

        changed, generate, proper_nouns = run(note, updates, array=regenerated, overwrite=True)

        self.assertTrue(changed)
        generate.assert_called_once()
        # The whole sentence goes to both steps: the correction changes how the words around
        # it are read, and a name can appear in what it changed.
        proper_nouns.assert_called_once()
        written = json.loads(note["words"])
        self.assertEqual([elem[0] for elem in written], ["A", "C"])
        self.assertEqual(written[0][4], ["match"])
        self.assertEqual(updates, {note.id: note})

    def test_a_broken_array_is_left_for_a_person_even_with_overwrite(self):
        broken = '[["本", "noun", "本", "ほん", [1378555077520], []]'
        note = FakeNote({"sentence": "本を読む。", "words": broken})
        updates: dict = {}

        changed, generate, proper_nouns = run(note, updates, overwrite=True)

        self.assertFalse(changed)
        generate.assert_not_called()
        proper_nouns.assert_not_called()
        self.assertEqual(note["words"], broken)
        self.assertEqual(updates, {})

    def test_a_regeneration_that_changes_nothing_writes_nothing(self):
        held = [self.word("A", "甲", "こう", ["match"])]
        text = extract_words.format_word_array(held)
        note = FakeNote({"sentence": "A", "words": text})
        updates: dict = {}

        changed, _generate, _proper_nouns = run(note, updates, array=held, overwrite=True)

        self.assertFalse(changed)
        self.assertEqual(note["words"], text)
        self.assertEqual(updates, {})

    def test_a_field_the_editor_mangled_is_written_back_clean(self):
        # Anki's editor turns the rows into <br> and &nbsp; as soon as anyone edits the note
        # by hand, which is exactly what a correction is.
        held = [self.word("A", "甲", "こう", ["match"])]
        text = extract_words.format_word_array(held)
        mangled = text.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
        note = FakeNote({"sentence": "A", "words": mangled})

        changed, _generate, _proper_nouns = run(note, {}, array=held, overwrite=True)

        self.assertTrue(changed)
        self.assertEqual(note["words"], text)

    def test_without_overwrite_an_array_is_still_left_alone(self):
        note = FakeNote({"sentence": "本を読む。", "words": '[["本"]]'})

        changed, generate, _proper_nouns = run(note, {}, array=[["本"], ["を"]])

        self.assertFalse(changed)
        generate.assert_not_called()


class ExtractWordsPhasesTests(unittest.TestCase):
    def test_the_lexicon_is_loaded_once_for_a_run(self):
        lexicon = {"里樹": None}
        with mock.patch.object(extract_words.names, "load_lexicon", return_value=lexicon) as load:
            op = extract_words.extract_words_op()

        load.assert_called_once()
        self.assertIs(op.keywords["name_lexicon"], lexicon)

    def test_the_judge_is_a_phase_of_its_own(self):
        with (
            mock.patch.object(extract_words, "with_generator_resources") as resources,
            mock.patch.object(extract_words, "selected_notes_op") as run_op,
            mock.patch.object(extract_words, "AsyncTaskProgressUpdater"),
        ):
            resources.side_effect = lambda _parent, then: then()
            extract_words.extract_words_and_judge_from_selected_notes([1], parent=None)

        phases = run_op.call_args.args[1]
        self.assertEqual(
            [phase.name for phase in phases], ["Extracting words", "Judging words matchability"]
        )
        self.assertIs(phases[0].bulk_op, extract_words.bulk_extract_from_notes_op)


if __name__ == "__main__":
    unittest.main()
