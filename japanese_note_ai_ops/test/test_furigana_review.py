"""Whether a sentence note's furigana has been checked (async_api_ops/furigana_review.py): the
one test kanjify, word extraction, the match op, the kanjify export and "Run all ops for new
notes" make before they read the furigana. Each op's use of it is in that op's tests."""

from __future__ import annotations

import unittest

from addon_modules import load_ops_module

review = load_ops_module("furigana_review")

SENTENCE = "Sentence note"
CONFIG = {SENTENCE: {"furigana_sentence_field": "Furigana", "sentence_field": "Sentence"}}


class Note:
    def __init__(self, fields: dict[str, str], tags=()) -> None:
        self.fields = {"Sentence": "", "Furigana": "", **fields}
        self.tags = list(tags)

    def note_type(self):
        return {"name": SENTENCE}

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]


class UncheckedFuriganaTest(unittest.TestCase):
    def test_checked_furigana_is_read(self):
        # A ？ outside a word's brackets is the sentence's own
        self.assertIsNone(review.unchecked_furigana(CONFIG, Note({"Furigana": "他[ほか]の 人[ひと]は？"})))

    def test_by_its_review_tag_or_a_word_still_marked(self):
        tagged = Note({"Furigana": "他[ほか]の"}, tags=["furigana-review"])
        self.assertIn('tagged "furigana-review"', review.unchecked_furigana(CONFIG, tagged))
        # The tag taken off before every such word was fixed
        marked = Note({"Furigana": "他[ほか？た]の"})
        self.assertIn('"？"', review.unchecked_furigana(CONFIG, marked))

    def test_a_field_the_caller_reads_instead(self):
        # Word extraction's sentence, often kanjified from the furigana
        note = Note({"Furigana": "他[ほか]の", "Sentence": "他[ほか？た]の"})
        self.assertIsNone(review.unchecked_furigana(CONFIG, note))
        self.assertIn('"？"', review.unchecked_furigana(CONFIG, note, "Sentence"))
        self.assertIsNone(review.unchecked_furigana(CONFIG, Note({}), "", "Gone"))

    def test_a_furigana_field_named_by_no_string_is_none(self):
        # A list in a hand-edited config raised TypeError in the note's `in`
        config = {SENTENCE: {"furigana_sentence_field": ["Furigana"]}}
        self.assertIsNone(review.unchecked_furigana(config, Note({"Furigana": "他[ほか？た]の"})))


if __name__ == "__main__":
    unittest.main()
