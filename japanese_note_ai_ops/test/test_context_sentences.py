"""Stripping the <i> context sentences off a word extraction field.

Three things depend on this being exactly one rule: extract_words, whose prompt must not see
the neighbouring sentences; the word array migration, whose array must cover the same words
the old list was made of; and research/migrate_fit.py, which measures the second.
"""

import unittest

from addon_modules import load_addon_module

html_stripping = load_addon_module("html_stripping", subdir="")
strip = html_stripping.strip_context_sentences


class StripContextSentencesTests(unittest.TestCase):
    def test_a_context_sentence_before_the_focus_one_goes(self):
        self.assertEqual(
            strip("<i> 前[まえ]の 文[ぶん]。</i> 本題[ほんだい]。"), " 本題[ほんだい]。"
        )

    def test_context_on_both_sides_goes(self):
        self.assertEqual(strip("<i>A</i>B<i>C</i>"), "B")

    def test_a_context_sentence_spanning_lines_goes(self):
        self.assertEqual(strip("<i>A\nB</i>C"), "C")

    def test_the_tags_inside_a_context_sentence_go_with_it(self):
        self.assertEqual(strip("<i><k> 其[そ]の</k> 拍子[ひょうし]。</i>X"), "X")

    def test_a_sentence_with_no_context_is_left_alone(self):
        for sentence in ["", " 本題[ほんだい]。", "<b>A</b><k>B</k>"]:
            self.assertEqual(strip(sentence), sentence)

    def test_an_unclosed_tag_is_not_taken_as_context(self):
        # A greedy match would swallow the rest of the field; <i>.*?</i> needs the closing tag
        self.assertEqual(strip("<i>A"), "<i>A")


if __name__ == "__main__":
    unittest.main()
