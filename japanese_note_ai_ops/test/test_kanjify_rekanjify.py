"""Re-kanjify proposals: which words are targets, the op's input, placing the answer's kanji."""

import sys
import unittest
from types import SimpleNamespace

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

POS = {"n": ("名詞", "普通名詞", "一般", "*", "*", "*"), "p": ("助詞", "格助詞", "*", "*", "*", "*"),
       "v": ("動詞", "一般", "*", "*", "五段-ラ行", "終止形-一般"),
       "pn": ("代名詞", "*", "*", "*", "*", "*"), "s": ("補助記号", "句点", "*", "*", "*", "*")}  # fmt: skip


def fake_tokenize(words):
    """A tokenizer giving these (surface, norm, pos) morphs, in order."""

    def tokenize(natural):
        assert natural == "".join(w[0] for w in words), natural
        out, at = [], 0
        for surface, norm, pos in words:
            out.append(
                SimpleNamespace(
                    start=at, end=at + len(surface), surface=surface, norm=norm,
                    lemma=norm, pos=POS[pos],
                )
            )  # fmt: skip
            at += len(surface)
        return out

    return tokenize


class RekanjifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import kanjify_audit
        import kanjify_rekanjify

        cls.audit = kanjify_audit
        cls.rk = kanjify_rekanjify

    def run_propose(self, base, answer, words, targets):
        toks = self.audit.analyze_row(0, base, tokenize=fake_tokenize(words))
        sentence = self.rk.source_sentence(base)
        return self.rk.propose(base, answer, sentence, toks, targets)

    def test_read_targets_takes_kana_uses_and_minority_kanji(self):
        tasks = [
            {"class": "left-kana", "word": "依る", "pos": "動詞", "kanjified": {"依": 3},
             "items": [{"row": 4}, {"row": 7}]},
            {"class": "meaning", "word": "有る", "pos": "動詞", "kanjified": {"有": 9, "在": 2},
             "items": [{"row": 1, "kanji": "有"}, {"row": 2, "kanji": "在"}]},
        ]  # fmt: skip
        targets = self.rk.read_targets(tasks)
        self.assertEqual(targets.rows, {2, 4, 7})
        self.assertEqual(targets.left, {("依る", "動詞")})
        self.assertEqual(targets.top, {("有る", "動詞"): {"有"}})

    def test_source_sentence_turns_every_span_back_into_kana(self):
        self.assertEqual(
            self.rk.source_sentence("<i><k> 此[こ]の</k></i> 本[ほん]<k> 為[し]て</k>ください"),
            "<i>この</i> 本[ほん]してください",
        )

    def test_left_kana_word_takes_the_answers_kanji_only(self):
        targets = self.rk.Targets({0}, {("依る", "動詞")}, {})
        words = [
            ("それ", "其れ", "pn"),
            ("に", "に", "p"),
            ("よる", "依る", "v"),
            ("。", "。", "s"),
        ]
        after, outcomes = self.run_propose(
            "それによる。", "<k> 其[そ]れ</k>に<k> 依[よ]る</k>。", words, targets
        )
        self.assertEqual(after, "それに<k> 依[よ]る</k>。")
        self.assertEqual(outcomes, [self.rk.Outcome("left-kana", "よる", "proposed", "依[よ]る")])

    def test_left_kana_word_after_bold_is_placed_in_the_field(self):
        targets = self.rk.Targets({0}, {("依る", "動詞")}, {})
        words = [("それ", "其れ", "pn"), ("に", "に", "p"), ("よる", "依る", "v")]
        after, _ = self.run_propose(
            "<b>それ</b>による", "<b>それ</b>に<k> 依[よ]る</k>", words, targets
        )
        self.assertEqual(after, "<b>それ</b>に<k> 依[よ]る</k>")

    def test_left_kana_word_next_to_a_span_joins_it(self):
        targets = self.rk.Targets({0}, {("程", "名詞")}, {})
        words = [("それ", "其れ", "pn"), ("ほど", "程", "n")]
        after, _ = self.run_propose(
            "<k> 其[そ]れ</k>ほど", "<k> 其[そ]れ 程[ほど]</k>", words, targets
        )
        self.assertEqual(after, "<k> 其[そ]れ 程[ほど]</k>")

    def test_left_kana_word_inside_a_span_gets_no_tags_of_its_own(self):
        targets = self.rk.Targets({0}, {("為る", "動詞")}, {})
        words = [("どう", "如何", "n"), ("し", "為る", "v"), ("て", "て", "p")]
        after, _ = self.run_propose(
            "<k> 如何[どう]して</k>", "<k> 如何[どう]為[し]て</k>", words, targets
        )
        self.assertEqual(after, "<k> 如何[どう] 為[し]て</k>")

    def test_answer_leaving_it_kana_changes_nothing(self):
        targets = self.rk.Targets({0}, {("依る", "動詞")}, {})
        words = [("に", "に", "p"), ("よる", "依る", "v")]
        after, outcomes = self.run_propose("による", "による", words, targets)
        self.assertEqual(after, "による")
        self.assertEqual([o.result for o in outcomes], ["agrees"])

    def test_meaning_word_takes_the_answers_other_kanji(self):
        targets = self.rk.Targets({0}, set(), {("有る", "動詞"): {"有"}})
        words = [("本", "本", "n"), ("が", "が", "p"), ("ある", "有る", "v"), ("。", "。", "s")]
        base = " 本[ほん]が<k> 在[あ]る</k>。"
        after, outcomes = self.run_propose(base, " 本[ほん]が<k> 有[あ]る</k>。", words, targets)
        self.assertEqual(after, " 本[ほん]が<k> 有[あ]る</k>。")
        self.assertEqual(outcomes, [self.rk.Outcome("meaning", "在[あ]る", "proposed", "有[あ]る")])

    def test_answer_cut_across_the_word_is_unplaced(self):
        targets = self.rk.Targets({0}, {("為る", "動詞")}, {})
        words = [("どう", "どう", "n"), ("し", "為る", "v"), ("て", "て", "p")]
        after, outcomes = self.run_propose("どうして", "<k> 如何為[どうし]</k>て", words, targets)
        self.assertEqual(after, "どうして")
        self.assertEqual([o.result for o in outcomes], ["unplaced"])


if __name__ == "__main__":
    unittest.main()
