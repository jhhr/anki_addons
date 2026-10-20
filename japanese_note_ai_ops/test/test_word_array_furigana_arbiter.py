"""The arbiter (word_array/furigana_arbiter.py): what its prompt is built from, how its answers
are read, and which answer is written for a word in review: every model sure of one reading,
which a transcript heard at the word and which keeps the word's kana.

The lines are made up; a Furigana is built by hand as audio_furigana leaves one."""

import unittest

from addon_modules import load_ops_module

af = load_ops_module("audio_furigana", subdir="word_array")
arbiter = load_ops_module("furigana_arbiter", subdir="word_array")

CAPTION = "（客）他の人は<br>開かないの？"
TRANSCRIPTS = [("kana-anime-whisper", "たのひとわ あかないの"), ("ruby-mora", "ホカノヒトワアカナイノ")]


def review(start, end, surface, draft, heard, known=()):
    r = af.Reading(0, start, end, surface, draft, None, "review", list(heard))
    r.known = list(known)
    return r


def furigana():
    """他 heard two ways, 開か heard as no reading JMdict has for the spelling."""
    other = review(0, 1, "他", "ほか", ["タ", "ホカ"], known=["た", "ほか"])
    person = af.Reading(0, 2, 3, "人", "ひと", "ひと", "draft", ["ヒト", "ヒト"])
    opened = af.Reading(1, 0, 2, "開か", "ひらか", None, "review", ["アカ", "アカ"])
    return af.Furigana(["他の人は", "開かないの？"], [other, person, opened], False)


def answer(reading, sure=True):
    return arbiter.Answer(reading, sure)


class InputsTest(unittest.TestCase):
    def test_the_words_in_review_with_where_they_are_and_what_was_heard(self):
        inputs = arbiter.review_inputs(CAPTION, "Where are the others?", TRANSCRIPTS, furigana())
        self.assertEqual(inputs["lines"], ["他の人は", "開かないの？"])
        self.assertEqual(inputs["translation"], "Where are the others?")
        self.assertEqual(
            inputs["transcripts"],
            [{"name": name, "text": text} for name, text in TRANSCRIPTS],
        )
        self.assertEqual(
            inputs["words"],
            [
                {"context": "［他］の人は", "draft": "ほか", "known": ["た", "ほか"], "heard": ["た", "ほか"]},
                {"context": "［開か］ないの？", "draft": "ひらか", "known": [], "heard": ["あか", "あか"]},
            ],
        )

    def test_the_prompt_lists_each_word_with_its_evidence(self):
        inputs = arbiter.review_inputs(CAPTION, "", TRANSCRIPTS, furigana())
        prompt = arbiter.review_prompt(**inputs)
        self.assertIn(
            "1. ［他］の人は\n"
            "   tokenizer's reading: ほか\n"
            "   known readings: た, ほか\n"
            "   heard: kana-anime-whisper た, ruby-mora ほか\n"
            "2. ［開か］ないの？\n"
            "   tokenizer's reading: ひらか\n"
            "   known readings: (none)\n"
            "   heard: kana-anime-whisper あか, ruby-mora あか\n",
            prompt,
        )
        self.assertIn("## Caption\n\n他の人は\n開かないの？\n\n## Transcripts\n\n", prompt)
        # No subtitle, no section for one
        self.assertNotIn("## English subtitle", prompt)

    def test_a_translation_field_is_its_lines_without_html(self):
        field = "Where are <b>the others</b>?<br>Won&#x27;t it open?"
        self.assertEqual(
            arbiter.translation_text(field), "Where are the others?\nWon't it open?"
        )

    def test_div_lines_are_lines_too(self):
        # Anki's editor writes a field's lines as <div> blocks
        for field in (
            "<div>Where are the others?</div><div>Won't it open?</div>",
            "Where are the others?<div>Won't it open?</div>",
            "<p>Where are the others?</p>\n<p>Won't it open?<br></p>",
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    arbiter.translation_text(field), "Where are the others?\nWon't it open?"
                )


class AnswersTest(unittest.TestCase):
    def test_answers_by_number_in_hiragana(self):
        response = {
            "words": [
                {"n": 1, "why": "", "reading": "タ", "sure": True},
                {"n": 2, "why": "", "reading": "あか", "sure": False},
            ]
        }
        self.assertEqual(
            arbiter.answers_from_response(response, 2), {1: answer("た"), 2: answer("あか", False)}
        )

    def test_an_answer_twice_not_kana_or_out_of_range_is_none(self):
        response = {
            "words": [
                {"n": 1, "reading": "た", "sure": True},
                {"n": 1, "reading": "ほか", "sure": True},
                {"n": 2, "reading": "開か", "sure": True},
                {"n": 3, "reading": "た", "sure": True},
                "た",
            ]
        }
        self.assertEqual(arbiter.answers_from_response(response, 2), {})
        self.assertEqual(arbiter.answers_from_response(None, 2), {})


class ChecksTest(unittest.TestCase):
    def test_heard_at_the_word_with_a_mora_of_a_neighbour_spared(self):
        self.assertTrue(arbiter.heard_at("ばなし", ["バナシ"]))
        self.assertTrue(arbiter.heard_at("はやり", ["", "ハヤリヤ"]))
        # Two morae are a word of their own, and no transcript at all is no hearing
        self.assertFalse(arbiter.heard_at("もつ", ["モツキサ"]))
        self.assertFalse(arbiter.heard_at("た", []))

    def test_a_reading_keeps_the_words_kana_where_they_are(self):
        self.assertTrue(arbiter.keeps_kana("困っ", "こまっ"))
        self.assertFalse(arbiter.keeps_kana("困っ", "こま"))
        self.assertTrue(arbiter.keeps_kana("お茶", "オチャ"))
        self.assertFalse(arbiter.keeps_kana("お茶", "ちゃ"))
        self.assertTrue(arbiter.keeps_kana("泣き明かし", "なきあかし"))


class AgreedTest(unittest.TestCase):
    word = review(1, 3, "開か", "ひらか", ["アカ", "アカ"])

    def test_every_model_sure_of_one_reading_heard_there(self):
        self.assertEqual(arbiter.agreed_reading(self.word, [answer("あか"), answer("アカ")]), "あか")

    def test_one_unsure_or_silent_model_leaves_it(self):
        self.assertIsNone(arbiter.agreed_reading(self.word, [answer("あか"), answer("あか", False)]))
        self.assertIsNone(arbiter.agreed_reading(self.word, [answer("あか"), None]))
        self.assertIsNone(arbiter.agreed_reading(self.word, []))

    def test_models_sure_of_two_readings_leave_it(self):
        word = review(0, 1, "他", "ほか", ["タ", "ホカ"])
        self.assertIsNone(arbiter.agreed_reading(word, [answer("た"), answer("ほか")]))

    def test_a_reading_no_transcript_heard_or_that_drops_the_words_kana_is_not_written(self):
        # Every model may prefer the dictionary's reading to the one said
        self.assertIsNone(arbiter.agreed_reading(self.word, [answer("ひらか")]))
        word = review(0, 2, "困っ", "こまっ", ["コマッ"])
        self.assertIsNone(arbiter.agreed_reading(word, [answer("こま")]))


class SettleTest(unittest.TestCase):
    def test_the_settled_words_are_written_and_the_rest_stay_in_doubt(self):
        result = furigana()
        answers = [{1: answer("た", False), 2: answer("あか")}, {1: answer("た"), 2: answer("あか")}]
        settled = arbiter.settle(result, answers)
        written = [(r.surface, r.written, r.why) for r in settled]
        self.assertEqual(written, [("開か", "あか", "arbiter")])
        self.assertEqual([r.surface for r in result.review], ["他"])
        self.assertEqual(result.text, "他[ほか？]の 人[ひと]は<br>開[あ]かないの？")


if __name__ == "__main__":
    unittest.main()
