"""What a note's sentences look like by the time they reach the new-meaning prompt.

`get_new_meaning_from_model` is handed what `get_sentences_for_note` returns, which is a list
of `EnAndJPSentence` dicts, but it declared and formatted them as plain strings. Every
sentence therefore went into the prompt as its dict repr - `{'jp_sentence': ..., 'en_sentence':
...}` - and both branches did it: the loop over several sentences and the single-sentence one
that used `sentences[0]` directly. The prompt asks for a definition in the sentence's own
language, so what belongs there is the Japanese sentence.
"""

import unittest

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
import addon_modules  # noqa: F401
from addon_modules import load_ops_module

cm = load_ops_module("clean_meaning")

ONE = {"jp_sentence": "犬が綱を引く", "en_sentence": "The dog pulls the leash"}
TWO = {"jp_sentence": "線を引く", "en_sentence": "To draw a line"}


class NewMeaningPromptTests(unittest.TestCase):
    def setUp(self):
        self.prompts: list[str] = []
        original = cm.get_response

        def fake_get_response(model, prompt, **kwargs):
            self.prompts.append(prompt)
            return {"new_meaning": "引っぱること", "english_meaning": "to pull"}

        cm.get_response = fake_get_response
        self.addCleanup(setattr, cm, "get_response", original)

    def ask(self, sentences):
        result = cm.get_new_meaning_from_model({}, "引く", "ひく", sentences)
        self.assertEqual(result, ("引っぱること", "to pull"))
        return self.prompts[0]

    def test_one_sentence_goes_in_as_its_japanese_text(self):
        prompt = self.ask([ONE])
        self.assertIn("犬が綱を引く", prompt)
        self.assertNotIn("jp_sentence", prompt)

    def test_several_sentences_each_go_in_as_their_japanese_text(self):
        prompt = self.ask([ONE, TWO])
        self.assertIn("- 犬が綱を引く", prompt)
        self.assertIn("- 線を引く", prompt)
        self.assertNotIn("jp_sentence", prompt)


if __name__ == "__main__":
    unittest.main()
