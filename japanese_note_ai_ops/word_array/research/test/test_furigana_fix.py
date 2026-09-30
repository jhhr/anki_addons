"""What furigana_fix writes into a note's furigana sentence, and what it leaves for the user."""

import sys
import unittest
from pathlib import Path

# See the note in test_vocab_morphology.py: the suite already has a `conftest` of its own, so
# the path is set here rather than in one more file by that name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import furigana_fix  # noqa: E402
import kanjify_fix  # noqa: E402
import kanjify_golden as golden  # noqa: E402


def fix_row(sentence: str, labeller=(), **kw) -> dict:
    return {"sid": "s1", "nids": [1, 2], "sentence": sentence, "labeller": list(labeller),
            "program": golden.furigana_suspects(sentence), **kw}  # fmt: skip


def labelled(group: str, fix: str, confidence: float = 0.9) -> dict:
    return {"group": group, "fix": fix, "confidence": confidence, "labeller": "batches/b0001"}


class FixRowTest(unittest.TestCase):
    def test_the_program_repairs_are_made_without_asking(self):
        row = fix_row(" 三[みっ]つ買[か]った。", [labelled(" 三[みっ]つ", " 三[みっ]つ")])
        after, made, left = furigana_fix.fix_row(row, labeller=False, min_confidence=0.8)
        self.assertEqual(after, " 三[みっ]つ 買[か]った。")
        self.assertEqual(len(made), 1)
        self.assertEqual(left, [])

    def test_a_labeller_fix_only_with_the_flag(self):
        row = fix_row("お 話[はなし]しください", [labelled(" 話[はなし]し", " 話[はな]し")])
        self.assertEqual(furigana_fix.fix_row(row, False, 0.8)[0], row["sentence"])
        self.assertEqual(furigana_fix.fix_row(row, True, 0.8)[0], "お 話[はな]しください")

    def test_doubtful_fixes_are_left_for_the_user(self):
        row = fix_row(" 月[つき]と 日[にち]と 日[にち]", [
            labelled(" 月[つき]", " 月[がつ]", confidence=0.5),
            labelled(" 日[にち]", " 日[ひ]"),
        ])  # fmt: skip
        after, made, left = furigana_fix.fix_row(row, True, 0.8)
        self.assertEqual(after, row["sentence"])
        self.assertEqual(made, [])
        self.assertTrue(any(x.startswith("confidence") for x in left))
        self.assertTrue(any(x.startswith("not in the sentence once") for x in left))

    def test_labellers_who_disagree_fix_nothing(self):
        row = fix_row(" 月[つき]", [labelled(" 月[つき]", " 月[がつ]"), labelled(" 月[つき]", " 月[げつ]")])
        after, _, left = furigana_fix.fix_row(row, True, 0.8)
        self.assertEqual(after, row["sentence"])
        self.assertTrue(left[0].startswith("labellers disagree"))

    def test_a_space_a_labeller_fix_needs_is_added(self):
        row = fix_row("まだ五 分[ふん]の", [labelled("五 分[ふん]", "五分[ごぶ]")])
        after, made, _ = furigana_fix.fix_row(row, True, 0.8)
        self.assertEqual(after, "まだ 五分[ごぶ]の")
        self.assertEqual(made[-1], "program: a space before a group a fix made")

    def test_the_words_a_fix_repeats_around_its_group_are_not_doubled(self):
        row = fix_row("彼[かれ]は 金[きん]を 払[はら]った。", [labelled(" 金[きん]", "は 金[かね]を")])
        after, made, _ = furigana_fix.fix_row(row, True, 0.8)
        self.assertEqual(after, "彼[かれ]は 金[かね]を 払[はら]った。")
        self.assertEqual(made, ["labeller:  金[きん] ->  金[かね]"])

    def test_a_whole_sentence_as_a_fix_fixes_only_its_group(self):
        row = fix_row("その 人[じん]に 会[あ]う。", [labelled(" 人[じん]", "その 人[ひと]に 会[あ]う。")])
        self.assertEqual(furigana_fix.fix_row(row, True, 0.8)[0], "その 人[ひと]に 会[あ]う。")

    def test_a_typo_fix_is_made_and_named_as_one(self):
        row = fix_row("その とろこに 行[い]く", [labelled("とろこ", "ところ")])
        after, made, _ = furigana_fix.fix_row(row, True, 0.8)
        self.assertEqual(after, "その ところに 行[い]く")
        self.assertEqual(made, ["labeller, text: とろこ -> ところ"])

    def test_what_no_one_can_fix_is_listed_not_written(self):
        rows = [fix_row("本がある"), fix_row(" 三[みっ]つ買[か]う")]
        fixes, lines = furigana_fix.plan(rows, labeller=True, min_confidence=0.8)
        self.assertEqual([f["after"] for f in fixes], [" 三[みっ]つ 買[か]う"])
        self.assertIn("    left   kanji with no reading: 本", lines)


class ReadingsTest(unittest.TestCase):
    SENTENCE = "一 日[にち]です"

    def row(self, fixed: str, confidence: float = 0.9, text_changed: bool = False) -> dict:
        reading = {"fixed": fixed, "changes": [{"was": "一", "now": " 一[いち]", "why": "w"}],
                   "text_changed": text_changed, "confidence": confidence, "unsure": ""}  # fmt: skip
        return fix_row(self.SENTENCE, [labelled(" 日[にち]", " 日[ひ]")], reading=reading)

    def test_the_reading_pass_sentence_replaces_the_other_fixes(self):
        after, made, left = furigana_fix.fix_row(self.row(" 一[いち] 日[にち]です"), True, 0.8,
                                                 readings=True)  # fmt: skip
        self.assertEqual(after, " 一[いち] 日[にち]です")
        self.assertEqual(made, ["reading: 一 ->  一[いち] (w)"])
        self.assertEqual(left, [])

    def test_only_with_the_flag(self):
        after, _, left = furigana_fix.fix_row(self.row(" 一[いち] 日[にち]です"), False, 0.8)
        self.assertEqual(after, self.SENTENCE)
        self.assertEqual(left, ["kanji with no reading: 一"])

    def test_a_sentence_that_fails_a_check_falls_back_to_the_other_fixes(self):
        for fixed, confidence, why in [(" 一[いち] 日[にち]だ", 0.9, "the text changed"),
                                       ("一 日[ひ]です", 0.9, "still kanji with no reading"),
                                       (" 一[いち] 日[にち]です", 0.5, "confidence 0.5")]:  # fmt: skip
            with self.subTest(why):
                after, made, left = furigana_fix.fix_row(self.row(fixed, confidence), True, 0.8,
                                                         readings=True)  # fmt: skip
                self.assertEqual(after, "一 日[ひ]です")
                self.assertTrue(left[0].startswith("reading pass not used: " + why), left[0])

    def test_a_typo_fix_the_agent_names_is_made_and_listed_as_one(self):
        after, made, _ = furigana_fix.fix_row(self.row(" 一[いち] 日[にち]だ", text_changed=True),
                                              True, 0.8, readings=True)  # fmt: skip
        self.assertEqual(after, " 一[いち] 日[にち]だ")
        self.assertTrue(made[0].startswith("reading, text: "))


class RedoTest(unittest.TestCase):
    SENTENCE = "その 人[じん]に 会[あ]う。"

    def setUp(self):
        self.rows = [fix_row(self.SENTENCE, [labelled(" 人[じん]", " 人[ひと]に")])]

    def undo(self, written: str, nid: int = 1) -> dict:
        return {"nid": nid, "field": "f", "before": " " + self.SENTENCE, "after": written}

    def test_a_write_this_version_makes_otherwise_is_redone_from_what_was_written(self):
        fixes, _ = furigana_fix.redo(self.rows, [self.undo("その 人[ひと]にに 会[あ]う。")], 0.8)
        self.assertEqual(fixes, [{"row": "s1", "nids": [1], "before": "その 人[ひと]にに 会[あ]う。",
                                  "after": "その 人[ひと]に 会[あ]う。"}])  # fmt: skip

    def test_writes_either_plan_makes_are_left_alone(self):
        # a run without --labeller wrote the sentence as it was; one with it, as it is now
        undo = [self.undo(self.SENTENCE, 1), self.undo("その 人[ひと]に 会[あ]う。", 2)]
        self.assertEqual(furigana_fix.redo(self.rows, undo, 0.8)[0], [])

    def test_writes_from_another_fix_list_are_left_alone(self):
        undo = [{"nid": 1, "field": "f", "before": "other", "after": "その 人[ひと]にに"}]
        self.assertEqual(furigana_fix.redo(self.rows, undo, 0.8)[0], [])


class FieldKeyTest(unittest.TestCase):
    def test_writes_go_to_the_field_named_by_the_key(self):
        config = {"Note": {"furigana_sentence_field": "furigana", "kanjified_sentence_field": "k"}}
        infos = [{"noteId": 1, "modelName": "Note",
                  "fields": {"furigana": {"value": " a "}, "k": {"value": "x"}}}]  # fmt: skip
        rows = [{"nids": [1], "before": "a", "after": "b"}]
        writes, refused = kanjify_fix.plan_writes(rows, infos, config, furigana_fix.FIELD_KEY)
        self.assertEqual(refused, [])
        self.assertEqual((writes[0].field, writes[0].after), ("furigana", " b "))


if __name__ == "__main__":
    unittest.main()
