"""Choosing which readings to ask about, and turning the answers back into repairs."""

import json
import sys
import unittest

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import vocab_reading_judge as j  # noqa: E402


def word(dict_form: str, reading: str, match_data: list) -> list:
    return ["", "noun", dict_form, reading, match_data, []]


def note(nid: int, spelling: str, reading: str, links: str = "", **extra) -> dict:
    row = {
        "nid": nid,
        "vocab-key": spelling,
        "vocab": spelling,
        "vocab-kanjified": spelling,
        "vocab-kana": reading,
        "vocab-translation": "",
        "sentence-vocab-list": links,
        "sentence-kanjified-furigana": "",
    }
    row.update(extra)
    return row


def sentence(nid: int, text: str, *words) -> dict:
    return note(
        nid, "", "", json.dumps(list(words), ensure_ascii=False),
        **{"sentence-kanjified-furigana": text},
    )


def case(**kw):
    base = dict(
        note_id=1,
        key="一段落",
        spelling="一段落",
        note_reading="ひとだんらく",
        array_reading="いちだんらく",
        meaning="",
        links=1,
        sentences=(),
        family=j.vocab_morphology.TWO_READINGS,
        owned_by=(),
    )
    base.update(kw)
    return j.Case(**base)


class TestChoosingWhatToAsk(unittest.TestCase):
    def test_two_real_readings_are_asked_about(self):
        rows = [
            note(1, "一段落", "ひとだんらく"),
            sentence(9, "仕事[しごと]が 一段落[いちだんらく]", word("一段落", "いちだんらく", [1])),
        ]
        cases = j.collect(rows)
        self.assertEqual([(c.spelling, c.array_reading) for c in cases],
                         [("一段落", "いちだんらく")])
        self.assertEqual(cases[0].sentences, ("仕事[しごと]が 一段落[いちだんらく]",))

    def test_a_reading_the_evidence_settles_is_left_to_the_repair(self):
        # カーネーション transliterates, so vocab_reading_fix writes it without asking.
        rows = [
            note(1, "カーネーション", "かーのーしょん"),
            sentence(9, "", word("カーネーション", "かーねーしょん", [1])),
        ]
        self.assertEqual(j.collect(rows), [])

    def test_an_owned_voicing_case_is_left_to_vocab_unlink(self):
        rows = [
            note(1, "会社", "がいしゃ"),
            note(2, "会社", "かいしゃ"),
            sentence(9, "", word("会社", "かいしゃ", [1])),
        ]
        self.assertEqual(j.collect(rows), [])

    def test_an_owned_two_reading_case_is_still_asked_about(self):
        # vocab_unlink holds these rather than unlinking them, so nothing else would.
        rows = [
            note(1, "一人", "いちにん"),
            note(2, "一人", "ひとり"),
            sentence(9, "", word("一人", "ひとり", [1])),
        ]
        cases = j.collect(rows)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].owned_by, (2,))

    def test_a_different_word_is_not_asked_about(self):
        rows = [note(1, "行き", "ゆき"), sentence(9, "", word("行く", "ゆく", [1]))]
        self.assertEqual(j.collect(rows), [])

    def test_a_dump_without_the_array_field_is_refused(self):
        with self.assertRaises(j.anki_connect.AnkiConnectError):
            j.collect([{"nid": 1, "vocab-kanjified": "犬"}])


class TestThePrompt(unittest.TestCase):
    def test_it_names_both_readings(self):
        text = j.prompt(case())
        self.assertIn("ひとだんらく", text)
        self.assertIn("いちだんらく", text)

    def test_an_existing_note_for_the_other_reading_is_said(self):
        self.assertIn("a separate vocabulary note already exists", j.prompt(case(owned_by=(2,))))

    def test_nothing_is_said_when_no_other_note_exists(self):
        self.assertNotIn("separate vocabulary note", j.prompt(case()))


class TestReadingTheAnswers(unittest.TestCase):
    def answers(self, response):
        one = case()
        cached = {j.key_for(j.MODEL, j.prompt(one)): {"response": response}}
        return j.verdicts([one], j.MODEL, cached)

    def test_a_missing_answer_is_unsure(self):
        self.assertEqual(j.verdicts([case()], j.MODEL, {})[0][1], j.UNSURE)

    def test_an_unknown_verdict_is_unsure(self):
        judged = self.answers({"verdict": "definitely", "reading": "x", "why": ""})
        self.assertEqual(judged[0][1], j.UNSURE)

    def test_an_array_verdict_becomes_a_repair(self):
        judged = self.answers(
            {"verdict": "array", "reading": "いちだんらく", "why": "standard reading"}
        )
        fixes, refused = j.repairs(judged)
        self.assertEqual((len(fixes), refused), (1, []))
        self.assertEqual((fixes[0].was, fixes[0].now), ("ひとだんらく", "いちだんらく"))
        self.assertTrue(fixes[0].furigana)

    def test_a_note_verdict_writes_nothing(self):
        judged = self.answers({"verdict": "note", "reading": "ひとだんらく", "why": ""})
        self.assertEqual(j.repairs(judged)[0], [])

    def test_a_split_verdict_writes_nothing(self):
        judged = self.answers({"verdict": "split", "reading": "", "why": ""})
        self.assertEqual(j.repairs(judged)[0], [])

    def test_a_verdict_naming_the_reading_the_note_already_has_is_refused(self):
        judged = self.answers({"verdict": "array", "reading": "ひとだんらく", "why": ""})
        fixes, refused = j.repairs(judged)
        self.assertEqual(fixes, [])
        self.assertIn("already has", refused[0])

    def test_a_reading_that_cannot_be_drawn_is_refused(self):
        one = case(spelling="ｎｍ", note_reading="えぬえむ", array_reading="なのめーとる")
        cached = {
            j.key_for(j.MODEL, j.prompt(one)): {
                "response": {"verdict": "array", "reading": "なのめーとる", "why": ""}
            }
        }
        fixes, refused = j.repairs(j.verdicts([one], j.MODEL, cached))
        self.assertEqual(fixes, [])
        self.assertIn("cannot be drawn", refused[0])


if __name__ == "__main__":
    unittest.main()
