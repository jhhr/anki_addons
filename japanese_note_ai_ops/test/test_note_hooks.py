"""What the note hooks in `__init__.py` do (note_hooks.py): which ops an added note gets, which
notes translate on leaving their translation field, which added notes have their cards
suspended.

`__init__.py` cannot be imported by a test, so its hooks keep only the reading of the note and
the config; these are the decisions they take from them. The one-type layout must answer as the
hooks always did: "Japanese vocab note" cleaned and extracted when a person adds one, translated
on unfocus, nothing else. The detection of a note an op adds (call_logging.in_bulk_op during the
cleanup's adding) is tested in a real collection, test_replay/test_migrate_to_sentence_notes.py.
"""

from __future__ import annotations

import unittest

from addon_modules import load_ops_module

hooks = load_ops_module("note_hooks", subdir="")

VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"
BOTH = (hooks.CLEAN_MEANING, hooks.EXTRACT_WORDS)


def one_type_config() -> dict:
    return {
        VOCAB: {
            "sentence_field": "Sentence",
            "translated_sentence_field": "Sentence translation",
            "word_list_field": "Words",
            "word_sort_field": "Word sort",
        },
        "Kanji draw": {"kanji_field": "Kanji", "story_field": "Story"},
        "log_level": "ERROR",
    }


def two_type_config() -> dict:
    return {
        SENTENCE: {
            "vocab_note_type": VOCAB,
            "sentence_field": "Text",
            "translated_sentence_field": "Text translation",
            "word_list_field": "Words",
        },
        VOCAB: {
            "sentence_note_type": SENTENCE,
            "example_sentence_id_field": "Example id",
            "translated_sentence_field": "Example translation",
            "word_sort_field": "Word sort",
        },
        "Kanji draw": {"kanji_field": "Kanji", "story_field": "Story"},
        "log_level": "ERROR",
    }


def broken_config() -> dict:
    config = two_type_config()
    del config[VOCAB]["sentence_note_type"]
    return config


def added(config, name, tags=(), op_adding=False):
    return hooks.ops_on_added_note(config, name, list(tags), op_adding=op_adding)


class AddedNoteTests(unittest.TestCase):
    def test_one_type_a_vocab_note_added_by_hand_is_cleaned_then_extracted(self):
        self.assertEqual(added(one_type_config(), VOCAB), BOTH)

    def test_one_type_other_types_get_nothing(self):
        for name in ("Kanji draw", "Basic", SENTENCE):
            with self.subTest(name=name):
                self.assertEqual(added(one_type_config(), name), ())

    def test_two_type_a_sentence_note_added_by_hand_gets_its_words_extracted(self):
        self.assertEqual(added(two_type_config(), SENTENCE), (hooks.EXTRACT_WORDS,))

    def test_two_type_a_vocab_note_gets_nothing(self):
        # Vocab notes come from the match op only (D3); one added by hand is not cleaned either
        self.assertEqual(added(two_type_config(), VOCAB), ())
        self.assertEqual(added(two_type_config(), "Kanji draw"), ())

    def test_a_note_the_match_op_made_gets_nothing_in_either_layout(self):
        for config, name in ((one_type_config(), VOCAB), (two_type_config(), SENTENCE)):
            for tag in ("new_matched_jp_word", "New_Matched_JP_Word"):
                with self.subTest(name=name, tag=tag):
                    self.assertEqual(added(config, name, tags=["other", tag]), ())

    def test_a_note_an_op_adds_gets_nothing_in_either_layout(self):
        # The migration's sentence notes, some with an empty array, are added in its cleanup
        self.assertEqual(added(two_type_config(), SENTENCE, op_adding=True), ())
        self.assertEqual(added(one_type_config(), VOCAB, op_adding=True), ())

    def test_an_unconfigured_vocab_type_is_handled_as_before_roles(self):
        # Its ops then fail on the config, logged, as they always did
        self.assertEqual(added({"log_level": "ERROR"}, VOCAB), BOTH)

    def test_a_broken_layout_is_raised_for_its_types_only(self):
        for name in (VOCAB, SENTENCE):
            with self.subTest(name=name), self.assertRaises(hooks.LayoutError):
                added(broken_config(), name)
        self.assertEqual(added(broken_config(), "Kanji draw"), ())
        # The cheap answers need no layout
        self.assertEqual(added(broken_config(), SENTENCE, op_adding=True), ())


class UnfocusTests(unittest.TestCase):
    def test_one_type_only_the_vocab_type_translates(self):
        config = one_type_config()
        self.assertTrue(hooks.translates_on_unfocus(config, VOCAB))
        for name in ("Kanji draw", SENTENCE, "Basic"):
            self.assertFalse(hooks.translates_on_unfocus(config, name))

    def test_two_type_the_sentence_type_translates_instead(self):
        config = two_type_config()
        self.assertTrue(hooks.translates_on_unfocus(config, SENTENCE))
        # Its translation is a copy of its example sentence's, and it names no sentence
        self.assertFalse(hooks.translates_on_unfocus(config, VOCAB))
        self.assertFalse(hooks.translates_on_unfocus(config, "Kanji draw"))

    def test_a_sentence_type_of_another_vocab_type_does_not(self):
        config = two_type_config()
        config["Other sentences"] = {"vocab_note_type": "Other vocab", "word_list_field": "W"}
        config["Other vocab"] = {"sentence_note_type": "Other sentences", "word_sort_field": "S"}
        self.assertFalse(hooks.translates_on_unfocus(config, "Other sentences"))

    def test_a_broken_layout_does_not_raise(self):
        self.assertFalse(hooks.translates_on_unfocus(broken_config(), SENTENCE))
        self.assertTrue(hooks.translates_on_unfocus(broken_config(), VOCAB))


class SuspendTests(unittest.TestCase):
    def test_only_a_two_type_sentence_note_is_suspended(self):
        self.assertTrue(hooks.suspends_added_cards(two_type_config(), SENTENCE))
        for config, name in (
            (two_type_config(), VOCAB),
            (two_type_config(), "Kanji draw"),
            (one_type_config(), VOCAB),
            (broken_config(), SENTENCE),
        ):
            with self.subTest(name=name):
                self.assertFalse(hooks.suspends_added_cards(config, name))


if __name__ == "__main__":
    unittest.main()
