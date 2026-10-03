"""kanjify_sentence's answer cleanup and the kanjify eval's span scoring."""

import sys
import unittest

from addon_modules import ADDON_ROOT, load_ops_module

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import kanjify_eval  # noqa: E402

kanjify_sentence = load_ops_module("kanjify_sentence")


class CleanKanjifiedTests(unittest.TestCase):
    def test_common_mistakes_are_fixed_and_the_sentence_reverses(self):
        clean = kanjify_sentence.clean_kanjified
        self.assertEqual(
            clean("それをする", "<k> 其[そ]れ</k>を<k> 為[し]る</k>"),
            ("<k> 其[そ]れ</k>を<k> 為[す]る</k>", True),
        )
        self.assertEqual(clean("その本", " <k> 其[その]</k>本"), ("<k> 其[そ]の</k>本", True))
        self.assertEqual(clean("これ", "「<k> 此[こ]れ</k>」"), ("<k> 此[こ]れ</k>", True))

    def test_k_tags_on_words_already_in_kanji_are_dropped(self):
        sentence = " 本[ほん]がある"
        self.assertEqual(
            kanjify_sentence.clean_kanjified(sentence, "<k> 本[ほん]</k>が<k> 有[あ]る</k>"),
            (" 本[ほん]が<k> 有[あ]る</k>", True),
        )

    def test_changed_text_fails_the_reverse_check(self):
        _, reverses = kanjify_sentence.clean_kanjified(
            "本をしてきた", "本を<k> 為[し]て 来[き]る</k>"
        )
        self.assertFalse(reverses)

    def test_a_number_given_furigana_still_reverses(self):
        self.assertTrue(kanjify_sentence.clean_kanjified("１つ", "１[ひと]つ")[1])


class PromptTests(unittest.TestCase):
    def test_the_prompt_holds_the_policy_whole_and_ends_with_the_sentence(self):
        prompt = kanjify_sentence.get_kanjify_sentence_prompt("これを 読[よ]む。")
        policy = kanjify_sentence.kanjify_policy()
        self.assertIn("Policy version: ", policy)
        self.assertIn(policy, prompt)
        self.assertTrue(prompt.endswith("The sentence to process: これを 読[よ]む。\n"))
        self.assertIn(f'"{kanjify_sentence.KANJIFIED_SENTENCE_RETURN_FIELD}"', prompt)

    def test_the_agents_read_the_policy_file_the_op_ships(self):
        import kanjify_golden

        self.assertEqual(kanjify_golden.POLICY.resolve(), kanjify_sentence.POLICY_PATH.resolve())
        # research/ is left out of the release zip: the file has to be outside it
        self.assertNotIn("research", kanjify_sentence.POLICY_PATH.parts)


class ScoreRowTests(unittest.TestCase):
    def outcomes(self, label, output, sentence, policy=None):
        comps, unaligned = kanjify_eval.score_row(label, output, sentence, policy)
        return [(c.outcome, c.cls) for c in comps], unaligned

    def test_furigana_cut_and_k_boundaries_dont_count(self):
        self.assertEqual(
            self.outcomes("<k> 付[つ]き 合[あ]い</k>", "<k> 付き合[つきあ]い</k>", "つきあい"),
            ([("right", "other")], 0),
        )
        self.assertEqual(
            self.outcomes("<k> 抑[そも] 抑[そも]</k>", "<k> 抑々[そもそも]</k>", "そもそも"),
            ([("right", "other")], 0),
        )

    def test_missed_extra_and_wrong(self):
        sentence = "これにある"
        label = "<k> 此[こ]れ</k>に<k> 有[あ]る</k>"
        self.assertEqual(
            self.outcomes(label, "これに<k> 在[あ]る</k>", sentence),
            ([("missed", "other"), ("wrong", "other")], 0),
        )
        self.assertEqual(
            self.outcomes("これにある", "これに<k> 有[あ]る</k>", sentence),
            ([("extra", "other")], 0),
        )

    def test_classes(self):
        sentence = "することにしてみる"
        label = "<k> 為[す]る</k><k> 事[こと]</k>に<k> 為[し]てみる</k>"
        output = "<k> 為[す]る</k><k> 事[こと]</k>に<k> 為[し]て 見[み]る</k>"
        # みる at kana offsets 7-9 is a て-helper
        self.assertEqual(
            self.outcomes(label, output, sentence, [(7, 9)]),
            (
                [
                    ("right", "為る"),
                    ("right", "formal noun"),
                    ("right", "為る"),
                    ("extra", "policy"),
                ],
                0,
            ),
        )

    def test_changed_text_leaves_label_groups_unaligned(self):
        comps, unaligned = kanjify_eval.score_row("<k> 此[こ]れ</k>だ", "あれだ", "これだ")
        self.assertEqual((comps, unaligned), ([], 1))

    def test_number_furigana_the_source_lacks_reads_as_the_number(self):
        self.assertEqual(kanjify_eval.kana_text("１[ひと]つ", "１つ").kana, "１つ")
        self.assertEqual(
            kanjify_eval.kana_text(" 1000[せん]円[えん]", " 1000[せん]円[えん]").kana, "せんえん"
        )


class SampleTests(unittest.TestCase):
    def test_a_sample_is_the_same_rows_whatever_order_they_come_in(self):
        from kanjify_audit import Row

        rows = [Row([i], f"文{i}です", "") for i in range(40)]
        dev = kanjify_eval.sample(rows, 10)
        self.assertEqual(kanjify_eval.sample(rows[::-1], 10), dev)
        # not the file's first rows, and the held-back part shares none with it
        self.assertNotEqual(dev, rows[:10])
        held = kanjify_eval.sample(rows, 10, 10)
        self.assertEqual(len(held), 10)
        self.assertFalse({r.sentence for r in dev} & {r.sentence for r in held})


if __name__ == "__main__":
    unittest.main()
