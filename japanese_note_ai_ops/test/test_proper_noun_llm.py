"""Proper noun op: the prompt, reading the model's names, and fixing the array with them."""

import unittest

from addon_modules import load_ops_module

llm = load_ops_module("proper_noun_llm", subdir="word_array")


def word(raw, pos="noun", form=None, reading=None, match_data=None, subs=None):
    form = form or raw.strip()
    return [raw, pos, form, reading or form, match_data or [], subs or []]


class PromptTests(unittest.TestCase):
    def test_the_prompt_has_the_furigana_sentence_without_tags(self):
        arr = [["<b>"], word(" 山田[やまだ]", form="山田", reading="やまだ"), ["</b>"], word("だ")]
        self.assertTrue(llm.prompt(arr).endswith("\n\n 山田[やまだ]だ"))


class ResponseTests(unittest.TestCase):
    def test_names_lose_furigana_and_none_markers(self):
        response = {"proper_nouns": [" 山田[やまだ]", "N/A", "山田", "ひまりん", 3]}
        self.assertEqual(llm.names_from_response(response), ["山田", "ひまりん"])

    def test_a_response_without_a_list_raises(self):
        for response in [None, {"proper_nouns": "山田"}, ["山田"]]:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    llm.names_from_response(response)


class FixTests(unittest.TestCase):
    def test_words_of_a_name_merge_into_one_proper_noun(self):
        arr = [
            word("ひま", match_data=[7]),
            word("りん", "suffix", match_data=[8], subs=[word("り", match_data=[9])]),
            ["、"],
            word("さん", "suffix"),
        ]
        fix = llm.fix_array(arr, ["ひまりん"])
        self.assertEqual(arr[0], ["ひまりん", "proper noun", "ひまりん", "ひまりん", [7], []])
        self.assertEqual(arr[1:], [["、"], word("さん", "suffix")])
        self.assertEqual((fix.changed, fix.unaligned, fix.unlinked), (["ひまりん"], [], [8, 9]))

    def test_a_single_word_is_relabelled_and_loses_its_sub_words(self):
        subs = [word(" 山[やま]", form="山", match_data=[3]), word("田[た]", form="田")]
        arr = [
            word(" 山田[やまだ]", form="山田", reading="やまだ", match_data=["match"], subs=subs)
        ]
        fix = llm.fix_array(arr, ["山田"])
        self.assertEqual(arr, [[" 山田[やまだ]", "proper noun", "山田", "やまだ", ["match"], []]])
        self.assertEqual(fix.unlinked, [3])

    def test_a_tag_or_dot_inside_the_name_is_kept_in_its_raw_text(self):
        arr = [word("ナツキ"), ["・"], word("スバル")]
        llm.fix_array(arr, ["スバル", "ナツキ・スバル"])
        self.assertEqual(
            arr, [["ナツキ・スバル", "proper noun", "ナツキ・スバル", "ナツキ・スバル", [], []]]
        )

    def test_a_name_inside_a_word_or_missing_changes_nothing(self):
        arr = [word(" 日本語[にほんご]", form="日本語"), word("が")]
        fix = llm.fix_array(arr, ["日本", "東京"])
        self.assertEqual(arr[0][1], "noun")
        self.assertEqual((fix.changed, fix.unaligned), ([], ["日本", "東京"]))

    def test_a_word_cut_across_a_name_splits_where_the_rest_is_a_word(self):
        def rest_word(_word, raw):
            return word(raw, "particle") if raw == "と" else None

        arr = [word(" 凛[りん]と", "adverb", "凛と", "りんと", [4]), word("の", "particle")]
        fix = llm.fix_array(arr, ["凛"], rest_word)
        self.assertEqual(
            arr,
            [
                [" 凛[りん]", "proper noun", "凛", "りん", [], []],
                word("と", "particle"),
                word("の", "particle"),
            ],
        )
        self.assertEqual((fix.changed, fix.unaligned, fix.unlinked), (["凛"], [], [4]))

    def test_a_cut_at_a_sub_word_boundary_keeps_the_sub_words_links(self):
        subs = [
            word(" 少年[しょうねん]", form="少年", reading="しょうねん", match_data=[6]),
            word("京太郎[きょうたろう]", "proper noun", "京太郎", "きょうたろう", [7]),
        ]
        arr = [
            word(
                " 少年京太郎[しょうねんきょうたろう]",
                "noun",
                "少年京太郎",
                "しょうねんきょうたろう",
                [5],
                subs,
            ),
            word("と", "particle"),
        ]
        rest = word(" 少年[しょうねん]", form="少年", reading="しょうねん")
        fix = llm.fix_array(arr, ["京太郎"], lambda _w, raw: list(rest) if raw == rest[0] else None)
        self.assertEqual(
            arr[:2],
            [
                word(" 少年[しょうねん]", form="少年", reading="しょうねん", match_data=[6]),
                ["京太郎[きょうたろう]", "proper noun", "京太郎", "きょうたろう", [7], []],
            ],
        )
        self.assertEqual((fix.changed, fix.unaligned, fix.unlinked), (["京太郎"], [], [5]))

    def test_a_word_stays_whole_when_the_rest_is_no_word_or_the_cut_is_in_furigana(self):
        asked = []

        def rest_word(_word, raw):
            asked.append(raw)
            return None

        arr = [word(" 江戸時代[えどじだい]", form="江戸時代", reading="えどじだい"), word("に")]
        fix = llm.fix_array(arr, ["江戸"], rest_word)
        self.assertEqual((arr[0][1], fix.unaligned, asked), ("noun", ["江戸"], []))
        arr = [word(" 凛[りん]と", "adverb", "凛と", "りんと")]
        fix = llm.fix_array(arr, ["凛"], rest_word)
        self.assertEqual((arr[0][1], fix.unaligned, asked), ("adverb", ["凛"], ["と"]))

    def test_a_name_already_a_proper_noun_is_not_changed(self):
        arr = [word("東京", "proper noun"), word("と"), word("東京", "noun")]
        fix = llm.fix_array(arr, ["東京"])
        self.assertEqual([e[1] for e in arr], ["proper noun", "noun", "proper noun"])
        self.assertEqual(fix.changed, ["東京"])


class ModelConfigTests(unittest.TestCase):
    def test_the_model_falls_back_to_extract_words(self):
        op = load_ops_module("find_proper_nouns")
        self.assertEqual(op.proper_nouns_model({"proper_nouns_model": "a"}), "a")
        self.assertEqual(op.proper_nouns_model({"extract_words_model": "b"}), "b")


if __name__ == "__main__":
    unittest.main()
