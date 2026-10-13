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


REGENERATE = "sentence::regenerate"


class FakeNote:
    def __init__(self, fields, note_id=1, tags=()):
        self.id = note_id
        self.fields = fields
        self.tags = list(tags)

    def note_type(self):
        return {"name": "Sentence"}

    # A tag is matched without case, as anki.notes.Note matches it
    def has_tag(self, tag):
        return tag.lower() in (held.lower() for held in self.tags)

    def remove_tag(self, tag):
        self.tags = [held for held in self.tags if held.lower() != tag.lower()]

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def __setitem__(self, field, value):
        self.fields[field] = value


def run(note, updates, array=None, overwrite=False, config=None):
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
        changed = extract_words.extract_words_in_note(
            config or {}, note, {}, updates, overwrite=overwrite
        )
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

    def test_the_compounds_the_config_lists_are_the_ones_the_generator_splits(self):
        # The list is the user's, edited in Anki: the generator has none of its own, and an
        # extraction and a regeneration both go by the config of the run
        config = {"split_compound_words": ["毛羽立つ", "心地良い"]}
        for overwrite, held in ((False, ""), (True, '[["本"]]')):
            with self.subTest(overwrite=overwrite):
                note = FakeNote({"sentence": "本を読む。", "words": held})

                _changed, generate, _proper_nouns = run(
                    note, {}, array=[["本"]], overwrite=overwrite, config=config
                )

                self.assertEqual(generate.call_args.args[2], {"毛羽立つ", "心地良い"})

    def test_a_config_that_lists_no_compounds_splits_none(self):
        # A meta.json older than the key, or a fixture's config: nothing is split by default
        note = FakeNote({"sentence": "本を読む。", "words": ""})

        _changed, generate, _proper_nouns = run(note, {})

        self.assertEqual(generate.call_args.args[2], frozenset())

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


class RegenerateTagTests(unittest.TestCase):
    """`sentence::regenerate` marks the notes whose array is still to be generated again, so it
    comes off a note whose array has been, and stays on one the op left alone."""

    def word(self, text, match_data=None):
        return [text, "noun", text, "よみ", match_data if match_data is not None else [], []]

    def test_the_name_is_the_one_the_user_tags_by(self):
        # Hardcoded, in the add-on's docs, and in searches the user has saved
        self.assertEqual(extract_words.REGENERATE_TAG, REGENERATE)

    def test_a_regenerated_note_loses_the_tag(self):
        held = [self.word("A", ["match"]), self.word("B")]
        note = FakeNote(
            {"sentence": "AC", "words": extract_words.format_word_array(held)},
            tags=["subs2srs", REGENERATE],
        )
        updates: dict = {}

        changed, _generate, _proper_nouns = run(
            note, updates, array=[self.word("A"), self.word("C")], overwrite=True
        )

        self.assertTrue(changed)
        self.assertEqual(note.tags, ["subs2srs"])
        self.assertEqual(updates, {note.id: note})

    def test_a_regeneration_that_changes_no_word_still_takes_the_tag_off_and_saves_the_note(self):
        # A compound listed to split that has no two words to split into: its note comes out
        # as it went in, and is done with all the same
        held = [self.word("A", ["match"])]
        text = extract_words.format_word_array(held)
        note = FakeNote({"sentence": "A", "words": text}, tags=[REGENERATE])
        updates: dict = {}

        changed, _generate, _proper_nouns = run(note, updates, array=held, overwrite=True)

        self.assertTrue(changed)
        self.assertEqual(note.tags, [])
        self.assertEqual(note["words"], text)
        self.assertEqual(updates, {note.id: note})

    def test_a_first_array_takes_the_tag_off_too(self):
        note = FakeNote({"sentence": "本を読む。", "words": ""}, tags=[REGENERATE, "other"])

        changed, _generate, _proper_nouns = run(note, {}, array=[["本"]])

        self.assertTrue(changed)
        self.assertEqual(note.tags, ["other"])

    def test_the_tag_comes_off_whatever_its_case(self):
        note = FakeNote({"sentence": "本を読む。", "words": ""}, tags=["Sentence::Regenerate"])

        run(note, {}, array=[["本"]])

        self.assertEqual(note.tags, [])

    def test_a_note_the_op_left_alone_keeps_the_tag(self):
        array = extract_words.format_word_array([self.word("A")])
        broken = '[["本", "noun", "本", "ほん", [1378555077520], []]'
        cases = {
            "an array, and no overwrite": ({"sentence": "A", "words": array}, False),
            "a broken array": ({"sentence": "本を読む。", "words": broken}, True),
            "no sentence": ({"sentence": "", "words": array}, True),
        }
        for name, (fields, overwrite) in cases.items():
            with self.subTest(case=name):
                note = FakeNote(fields, tags=[REGENERATE])
                updates: dict = {}

                changed, _generate, _proper_nouns = run(
                    note, updates, array=[self.word("A")], overwrite=overwrite
                )

                self.assertFalse(changed)
                self.assertEqual(note.tags, [REGENERATE])
                self.assertEqual(updates, {})

    def test_a_note_whose_array_could_not_be_generated_or_merged_keeps_the_tag(self):
        text = extract_words.format_word_array([self.word("A")])
        failures = {
            "the generator fails": mock.patch.object(
                extract_words, "generate_word_array", side_effect=RuntimeError("no dictionary")
            ),
            "the merge fails": mock.patch.object(
                extract_words.merge, "merge_arrays", side_effect=ValueError("no such span")
            ),
        }
        for name, failure in failures.items():
            with self.subTest(case=name):
                note = FakeNote({"sentence": "A", "words": text}, tags=[REGENERATE])
                updates: dict = {}
                with (
                    mock.patch.object(
                        extract_words,
                        "get_field_config",
                        side_effect=lambda _config, key, _note_type: FIELDS[key],
                    ),
                    mock.patch.object(
                        extract_words, "generate_word_array", return_value=[self.word("B")]
                    ),
                    mock.patch.object(extract_words, "add_proper_nouns", return_value=True),
                    failure,
                ):
                    changed = extract_words.extract_words_in_note(
                        {}, note, {}, updates, overwrite=True
                    )

                self.assertFalse(changed)
                self.assertEqual(note.tags, [REGENERATE])
                self.assertEqual((note["words"], updates), (text, {}))


class SplitCompoundsConfigTests(unittest.TestCase):
    """`split_compound_words` as the generator reads it off a config, which is typed by hand in
    Anki's config editor."""

    def listed(self, value):
        return extract_words.generator.split_compounds_of({"split_compound_words": value})

    def test_a_list_of_words_is_those_words(self):
        self.assertEqual(self.listed(["毛羽立つ", "仲睦まじい"]), {"毛羽立つ", "仲睦まじい"})
        self.assertEqual(self.listed([" 毛羽立つ ", "", "毛羽立つ"]), {"毛羽立つ"})

    def test_words_typed_into_one_string_count_too(self):
        self.assertEqual(self.listed("毛羽立つ"), {"毛羽立つ"})
        for typed in ("毛羽立つ, 仲睦まじい", "毛羽立つ、仲睦まじい", "毛羽立つ　仲睦まじい"):
            with self.subTest(typed=typed):
                self.assertEqual(self.listed(typed), {"毛羽立つ", "仲睦まじい"})
        self.assertEqual(
            self.listed(["毛羽立つ,仲睦まじい", "心地良い"]), {"毛羽立つ", "仲睦まじい", "心地良い"}
        )

    def test_a_value_that_holds_no_words_lists_none(self):
        for value in (None, [], "", 5, True, {"毛羽立つ": True}, [5, None, ["毛羽立つ"]]):
            with self.subTest(value=value):
                self.assertEqual(self.listed(value), frozenset())
        self.assertEqual(extract_words.generator.split_compounds_of({}), frozenset())
        # What is no word is left out, and the words beside it still count
        self.assertEqual(self.listed(["毛羽立つ", 5, None]), {"毛羽立つ"})

    def with_added(self, value, forms):
        return extract_words.generator.split_compounds_with({"split_compound_words": value}, forms)

    def test_adding_to_the_list_keeps_what_was_typed_and_appends_what_it_lacks(self):
        # For the sync sweep (split_compound_tag), which writes the list without a person typing
        self.assertEqual(self.with_added(["毛羽立つ"], ["心地良い"]), ["毛羽立つ", "心地良い"])
        self.assertEqual(
            self.with_added(["毛羽立つ"], ["心地良い", "毛羽立つ", "", "心地良い", "仲睦まじい"]),
            ["毛羽立つ", "心地良い", "仲睦まじい"],
        )
        # Words typed into one string are found in it, and it stays as it was typed
        self.assertEqual(
            self.with_added("毛羽立つ, 仲睦まじい", ["仲睦まじい", "心地良い"]),
            ["毛羽立つ, 仲睦まじい", "心地良い"],
        )
        self.assertEqual(
            self.with_added([" 毛羽立つ ", 5], ["心地良い"]), [" 毛羽立つ ", 5, "心地良い"]
        )

    def test_adding_to_a_config_with_no_list_makes_one(self):
        for value in (None, [], "", 5, {"毛羽立つ": True}):
            with self.subTest(value=value):
                self.assertEqual(self.with_added(value, ["毛羽立つ"]), ["毛羽立つ"])
        with_added = extract_words.generator.split_compounds_with
        self.assertEqual(with_added({}, ["毛羽立つ"]), ["毛羽立つ"])

    def test_nothing_is_written_when_the_list_has_them_all(self):
        self.assertIsNone(self.with_added(["毛羽立つ", "心地良い"], ["心地良い"]))
        self.assertIsNone(self.with_added("毛羽立つ 心地良い", ["心地良い", "毛羽立つ"]))
        self.assertIsNone(self.with_added(["毛羽立つ"], []))
        self.assertIsNone(self.with_added([], [""]))


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
            resources.side_effect = lambda _parent, then, chain=None: then()
            extract_words.extract_words_and_judge_from_selected_notes([1], parent=None)

        phases = run_op.call_args.args[1]
        self.assertEqual(
            [phase.name for phase in phases], ["Extracting words", "Judging words matchability"]
        )
        self.assertIs(phases[0].bulk_op, extract_words.bulk_extract_from_notes_op)


if __name__ == "__main__":
    unittest.main()
