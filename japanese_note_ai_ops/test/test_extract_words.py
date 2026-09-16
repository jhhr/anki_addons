"""The "Extract words" op: generate the word array, then ask about proper nouns.

The guards are the whole of what it decides on its own, and both of them protect a field that
already holds something: an array means the op has run (a re-run is free), and an old
extract_words word list means the note ids of its matched words would be thrown away, which
only the migration is allowed to carry over.
"""

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


def run(note, updates, array=None):
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
        changed = extract_words.extract_words_in_note({}, note, {}, updates)
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

    def test_an_old_word_list_is_left_for_the_migration(self):
        old_list = '{"nouns": [["本", "ほん", "本", 1378555077520]]}'
        note = FakeNote({"sentence": "本を読む。", "words": old_list})
        updates: dict = {}

        changed, generate, proper_nouns = run(note, updates)

        self.assertFalse(changed)
        generate.assert_not_called()
        proper_nouns.assert_not_called()
        self.assertEqual(note["words"], old_list)
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
