"""Planning "Move sentences to sentence notes" (sync_local_ops/sentence_migration.py).

Plain data handling: the arrays are written by hand, as the generator would write them for the
sentence, so each case says exactly what a note holds. No SudachiPy, JMdict or collection.
"""

import json
import unittest

from addon_modules import load_ops_module

sm = load_ops_module("sentence_migration", subdir="sync_local_ops")
match_flags = load_ops_module("match_flags", subdir="word_array")

EXTRACTION = "word_extraction_sentence_field"
FURIGANA = "furigana_sentence_field"
TRANSLATION = "translated_sentence_field"
AUDIO = "sentence_audio_field"
WORD_LIST = "word_list_field"
SEEN = "sentence_seen_count_field"


def word(text, form, reading, match_data=None, subs=None, pos="noun"):
    return [text, pos, form, reading, [] if match_data is None else match_data, subs or []]


def particle(text):
    return [text, "particle", text, text, ["dontmatch"], []]


def fields(extraction, translation="The cat ate the fish.", audio="[sound:cat.mp3]"):
    return {
        "sentence_field": extraction,
        FURIGANA: extraction,
        "kanjified_sentence_field": extraction,
        EXTRACTION: extraction,
        TRANSLATION: translation,
        AUDIO: audio,
    }


def source(nid, extraction, arr=None, vocab_word=("魚", "さかな"), word_list=None, **kw):
    if word_list is None:
        word_list = "" if arr is None else json.dumps(arr, ensure_ascii=False)
    kw.setdefault("fields", fields(extraction))
    return sm.VocabRecord(
        note_id=nid,
        word_list=word_list,
        kanjified=vocab_word[0],
        normal=vocab_word[0],
        reading=vocab_word[1],
        **kw,
    )


def dependent(nid, extraction, vocab_word, tags=(), **kw):
    return source(nid, extraction, vocab_word=vocab_word, tags=("new_matched_jp_word", *tags), **kw)


def plan(vocab, sentences=(), selected=None, count_seen=False):
    if selected is None:
        selected = [v.note_id for v in vocab]
    return sm.plan_migration(vocab, sentences, selected, count_seen=count_seen)


def array_of(new_sentence):
    return json.loads(new_sentence.fields[WORD_LIST])


# 猫が魚を食べた。 The source's <b> marks its own word, 魚
CAT_FISH = " 猫[ねこ]が<b> 魚[さかな]</b>を 食[た]べた。"


def cat_fish_array(cat=None, fish=None, eat=None):
    return [
        word(" 猫[ねこ]", "猫", "ねこ", cat),
        particle("が"),
        word(" 魚[さかな]", "魚", "さかな", fish),
        particle("を"),
        word(" 食[た]べた", "食べる", "たべる", eat, pos="verb"),
        ["。"],
    ]


# 柊の小枝が。 Two sources of the same sentence, the second generated before the furigana of
# 小枝 was corrected: its rows differ where the correction reached
KOEDA = " 柊[ひいらぎ]の 小枝[こえだ]が。"
KOEDA_OLD = " 柊[ひいらぎ]の 小[しょう] 枝[えだ]が。"


def koeda_array(hiiragi=None, koeda=None):
    return [
        word(" 柊[ひいらぎ]", "柊", "ひいらぎ", hiiragi),
        particle("の"),
        word(" 小枝[こえだ]", "小枝", "こえだ", koeda),
        particle("が"),
        ["。"],
    ]


# 彼は此の無人島に行った。 with an <i> context sentence before it
MUJINTO_FIELD = (
    "<i>前[まえ]の 文[ぶん]。</i> 彼[かれ]は<k> 此[こ]の</k><b> 無人島[むじんとう]</b>に 行[い]った。"
)


def mujinto_array(island=None, mujin=None, subs=None):
    if subs is None:
        subs = [word(" 無人[むじん]", "無人", "むじん", mujin), word("島[とう]", "島", "とう", pos="suffix")]
    return [
        word(" 彼[かれ]", "彼", "かれ", pos="pronoun"),
        particle("は"),
        ["<k>"],
        word(" 此[こ]の", "此の", "この", pos="adnominal"),
        ["</k>"],
        word(" 無人島[むじんとう]", "無人島", "むじんとう", island, subs),
        particle("に"),
        word(" 行[い]った", "行く", "いく", pos="verb"),
        ["。"],
    ]


class SentenceKeyTests(unittest.TestCase):
    def test_an_arrays_key_is_the_key_of_the_field_it_was_made_from(self):
        # <k> spans, furigana, <b> and an <i> context sentence: the array's raw texts hold the
        # field without <b> and context, and both keys are its plain text
        self.assertEqual(sm.field_key(MUJINTO_FIELD), "彼は此の無人島に行った。")
        self.assertEqual(sm.array_key(mujinto_array()), "彼は此の無人島に行った。")

    def test_bold_tags_go_in_any_case_and_other_tags_stay(self):
        self.assertEqual(
            sm.strip_bold("<i>前の<b>文</b>。</i>A<B>b</B><br><blockquote>c</blockquote>"),
            "<i>前の文。</i>Ab<br><blockquote>c</blockquote>",
        )


class BoldSpanTests(unittest.TestCase):
    def test_the_span_of_a_whole_group(self):
        # 彼 は | 無 人 島 | に
        self.assertEqual(sm.bold_span(" 彼[かれ]は<b> 無人島[むじんとう]</b>に"), (2, 5))

    def test_a_boundary_inside_a_furigana_group_widens_to_the_group(self):
        self.assertEqual(sm.bold_span(" 彼[かれ]は<b> 無人</b>島[むじんとう]に"), (2, 5))
        self.assertEqual(sm.bold_span(" 彼[かれ]は 無<b>人島[むじんとう]</b>に"), (2, 5))

    def test_kana_before_a_group_without_a_space_is_not_part_of_the_group(self):
        # The furigana covers only the kanji run; the <b> after は is not inside the group
        self.assertEqual(sm.bold_span("は<b>無人島[むじんとう]</b>に"), (1, 4))
        self.assertEqual(sm.bold_span("<b>は</b>無人島[むじんとう]に"), (0, 1))

    def test_the_context_sentence_is_not_counted_and_its_b_is_ignored(self):
        self.assertEqual(sm.bold_span("<i><b>前</b>の文。</i> 猫[ねこ]が<b>好[す]き</b>"), (2, 4))
        self.assertEqual(sm.bold_span(MUJINTO_FIELD), (4, 7))

    def test_upper_case_tags(self):
        self.assertEqual(sm.bold_span("猫が<B>魚</B>"), (2, 3))

    def test_no_span_where_none_can_be_placed(self):
        for text in [
            "猫が魚を食べた",
            "猫が<b></b>魚",
            " 無<b></b>人島[むじんとう]",  # an empty one left by the editor, inside a group
            "猫が<b>魚",
            "猫が</b>魚<b>",
            "<b>猫が<b>魚</b></b>",
            " 魚[さか<b>な]</b>",  # inside the reading
        ]:
            with self.subTest(text=text):
                self.assertIsNone(sm.bold_span(text))

    def test_several_spans_give_the_range_over_all(self):
        self.assertEqual(sm.bold_span("<b>猫</b>が<b>魚</b>を"), (0, 3))


class ElementSpanTests(unittest.TestCase):
    def test_top_level_and_sub_word_spans(self):
        spans = [(e.elem[2], e.span) for e in sm._entries(mujinto_array())]
        self.assertEqual(
            spans,
            [
                ("彼", (0, 1)),
                ("は", (1, 2)),
                ("此の", (2, 4)),
                ("無人島", (4, 7)),
                ("無人", (4, 6)),
                ("島", (6, 7)),
                ("に", (7, 8)),
                ("行く", (8, 11)),
            ],
        )

    def test_sub_words_that_do_not_make_up_their_parent_have_no_place(self):
        arr = mujinto_array(subs=[word(" 無人[むじん]", "無人", "むじん")])
        spans = {e.elem[2]: e.span for e in sm._entries(arr)}
        self.assertEqual(spans["無人島"], (4, 7))
        self.assertIsNone(spans["無人"])
        self.assertEqual(spans["に"], (7, 8))


class NewSentenceTests(unittest.TestCase):
    def test_a_source_and_a_dependent_it_links_get_one_new_sentence_note(self):
        arr = cat_fish_array(fish=[1], eat=[2])
        result = plan([source(1, CAT_FISH, arr), dependent(2, CAT_FISH, ("食べる", "たべる"))])

        (new,) = result.new_sentences
        self.assertEqual(new.origin_id, 1)
        self.assertEqual(new.source_ids, [1])
        self.assertEqual(new.vocab_ids, [1, 2])
        self.assertEqual(new.fields[WORD_LIST], match_flags.format_word_array(arr))
        self.assertEqual(new.fields[EXTRACTION], " 猫[ねこ]が 魚[さかな]を 食[た]べた。")
        self.assertEqual(new.tags, [])
        self.assertEqual(result.examples, {1: sm.Example(new_index=0), 2: sm.Example(new_index=0)})
        self.assertEqual(result.updates, [])
        self.assertEqual(result.tags_to_remove, {})
        self.assertEqual(result.report.cases, {})

    def test_b_is_removed_from_every_text_field_but_audio_and_context_is_kept(self):
        extraction = "<i>前[まえ]の<b>文</b>。</i>" + CAT_FISH.replace("<b>", "<B>")
        record = source(
            1,
            extraction,
            cat_fish_array(fish=[1]),
            fields=fields(extraction, "The cat ate the <b>fish</b>.", "[sound:<b>fish</b>.mp3]"),
        )
        (new,) = plan([record]).new_sentences
        for key in ["sentence_field", FURIGANA, "kanjified_sentence_field", EXTRACTION]:
            self.assertEqual(new.fields[key], "<i>前[まえ]の文。</i> 猫[ねこ]が 魚[さかな]を 食[た]べた。")
        self.assertEqual(new.fields[TRANSLATION], "The cat ate the fish.")
        self.assertEqual(new.fields[AUDIO], "[sound:<b>fish</b>.mp3]")

    def test_only_the_keys_the_record_has_are_written(self):
        record = source(1, CAT_FISH, cat_fish_array(fish=[1]), fields={EXTRACTION: CAT_FISH})
        (new,) = plan([record]).new_sentences
        self.assertEqual(set(new.fields), {EXTRACTION, WORD_LIST})

    def test_the_seen_count_is_the_origins_review_count_alone(self):
        vocab = [
            source(1, CAT_FISH, cat_fish_array(fish=[1]), reps=5, card_count=2),
            source(3, CAT_FISH, cat_fish_array(fish=[1]), reps=9),
        ]
        (new,) = plan(vocab, count_seen=True).new_sentences
        self.assertEqual(new.fields[SEEN], "5")
        self.assertEqual(plan(vocab, count_seen=True).report.note_ids("several_cards"), [1])

        result = plan(vocab, count_seen=False)
        self.assertNotIn(SEEN, result.new_sentences[0].fields)
        self.assertEqual(result.report.note_ids("several_cards"), [])

    def test_the_origin_is_the_oldest_source_holding_an_array(self):
        vocab = [
            source(1, CAT_FISH, reps=1),
            source(2, CAT_FISH, cat_fish_array(cat=[2]), reps=2),
            source(3, CAT_FISH, cat_fish_array(fish=[3]), reps=3),
        ]
        (new,) = plan(vocab, count_seen=True).new_sentences
        self.assertEqual(new.origin_id, 2)
        self.assertEqual(new.source_ids, [1, 2, 3])
        self.assertEqual(new.fields[SEEN], "2")

    def test_a_group_without_an_array_gets_an_empty_field_tagged_needs_extract(self):
        for word_list in ["", "[]"]:
            with self.subTest(word_list=word_list):
                result = plan([source(1, CAT_FISH, word_list=word_list)])
                (new,) = result.new_sentences
                self.assertEqual(new.fields[WORD_LIST], "")
                self.assertEqual(new.tags, ["sentence-needs-extract"])
                self.assertEqual(result.report.note_ids("needs_extract"), [1])
                self.assertEqual(result.report.note_ids("link_no_array"), [1])

    def test_a_broken_array_is_carried_verbatim_and_tagged(self):
        broken = '[\n  [" 猫[ねこ]", "noun", "猫", "ねこ", [1]], [\n]'
        result = plan([source(1, CAT_FISH, word_list=broken), source(2, CAT_FISH)])
        (new,) = result.new_sentences
        self.assertEqual(new.origin_id, 1)
        self.assertEqual(new.fields[WORD_LIST], broken)
        self.assertEqual(new.tags, ["invalid_word_list_json"])
        self.assertEqual(result.report.note_ids("broken_array"), [1])

    def test_a_broken_array_beside_a_readable_one_is_reported_not_combined(self):
        broken = '[[" 猫[ねこ]", "noun", "猫", "ねこ", [99]]'
        cat = "<b> 猫[ねこ]</b>が 魚[さかな]を 食[た]べた。"
        vocab = [
            source(1, cat, word_list=broken, vocab_word=("猫", "ねこ")),
            source(2, CAT_FISH, cat_fish_array()),
        ]
        result = plan(vocab)
        (new,) = result.new_sentences
        self.assertEqual(new.origin_id, 2)
        # Each is linked by the check at its own <b>; the broken array's link is not taken
        self.assertEqual(array_of(new), cat_fish_array(cat=[1], fish=[2]))
        self.assertEqual(new.tags, [])
        self.assertEqual(result.report.note_ids("broken_array"), [1])

    def test_no_sentence_text_is_skipped_and_reported(self):
        empty = fields("<i>Only context.</i>")
        result = plan([source(1, "", cat_fish_array(fish=[1]), fields=empty)])
        self.assertEqual(result.new_sentences, [])
        self.assertEqual(result.examples, {})
        self.assertEqual(result.report.note_ids("no_sentence_text"), [1])

    def test_a_source_keyed_by_its_furigana_field(self):
        record = source(1, "", fields={EXTRACTION: "", FURIGANA: " 猫[ねこ]が 魚[さかな]を"})
        result = plan([record])
        self.assertEqual(len(result.new_sentences), 1)
        self.assertEqual(result.report.note_ids("keyed_by_furigana"), [1])

    def test_the_addons_own_sentence_tags_move_from_the_origin(self):
        vocab = [
            source(
                1, CAT_FISH, cat_fish_array(fish=[1]), tags=("Kanjify_Sentence_Mismatch", "mine")
            ),
            source(2, CAT_FISH, cat_fish_array(), tags=("invalid_word_list_json",)),
        ]
        result = plan(vocab)
        self.assertEqual(result.new_sentences[0].tags, ["Kanjify_Sentence_Mismatch"])
        self.assertEqual(result.tags_to_remove, {1: ["Kanjify_Sentence_Mismatch"]})

    def test_planning_twice_gives_the_same_plan(self):
        # The plan copies every array it changes: the records and their arrays are as they were
        vocab = [
            source(1, CAT_FISH, cat_fish_array(cat=[101], fish=["match"])),
            source(2, CAT_FISH, cat_fish_array(cat=[102], eat=[2, 3])),
            dependent(3, CAT_FISH, ("食べる", "たべる")),
        ]
        first, second = plan(vocab), plan(vocab)
        self.assertEqual(first, second)
        self.assertEqual(array_of(first.new_sentences[0])[2][4], [1])


class CombineTests(unittest.TestCase):
    def combined(self, origin_arr, other_arr, sentence=CAT_FISH, other_sentence=None):
        # No <b> and words of no element: the link check changes nothing here
        vocab = [
            source(10, sm.strip_bold(sentence), origin_arr, vocab_word=("x", "x")),
            source(11, sm.strip_bold(other_sentence or sentence), other_arr, vocab_word=("x", "x")),
        ]
        result = plan(vocab)
        (new,) = result.new_sentences
        return array_of(new), new, result.report

    def test_a_link_beats_match_beats_dontmatch_beats_unjudged(self):
        arr, new, report = self.combined(
            cat_fish_array(cat=["match"], fish=[10], eat=[]),
            cat_fish_array(cat=[11, 4], fish=["match"], eat=["dontmatch"]),
        )
        self.assertEqual(arr, cat_fish_array(cat=[11, 4], fish=[10], eat=["dontmatch"]))
        self.assertEqual(new.tags, [])
        # Each source is linked, by its own array, so neither is checked
        self.assertEqual(report.cases, {})

    def test_a_rated_link_beats_the_same_link_unrated_and_the_origin_wins_ties(self):
        arr, _, _ = self.combined(
            cat_fish_array(cat=[5], fish=[6, 2], eat=["match"]),
            cat_fish_array(cat=[5, 3], fish=[6, 4], eat=["match"]),
        )
        self.assertEqual(arr, cat_fish_array(cat=[5, 3], fish=[6, 2], eat=["match"]))

    def test_two_links_to_different_notes_keep_the_origins_and_tag_the_sentence(self):
        arr, new, report = self.combined(cat_fish_array(cat=[101]), cat_fish_array(cat=[102]))
        self.assertEqual(arr, cat_fish_array(cat=[101]))
        self.assertEqual(new.tags, ["sentence-migration-conflict"])
        ((note_id, detail),) = report.cases["conflict"]
        self.assertEqual(note_id, 11)
        self.assertIn("101 kept, 102 not", detail)

    def test_a_different_structure_is_aligned_onto_the_origins(self):
        other = koeda_array(hiiragi=[1378])
        other[2] = word(" 小[しょう] 枝[えだ]", "小枝", "しょうえだ", [7])
        arr, _, report = self.combined(koeda_array(), other, KOEDA, KOEDA_OLD)
        # The origin's text and structure, the other's links where they align
        self.assertEqual(arr, koeda_array(hiiragi=[1378], koeda=[7]))
        self.assertEqual(report.note_ids("links_lost") + report.note_ids("combine_failed"), [])

    def test_links_the_alignment_cannot_place_are_reported(self):
        other = koeda_array()
        other[2:3] = [word(" 小[しょう]", "小", "しょう", [8]), word(" 枝[えだ]", "枝", "えだ", [9])]
        arr, _, report = self.combined(koeda_array(koeda=["match"]), other, KOEDA, KOEDA_OLD)
        self.assertEqual(arr, koeda_array(koeda=["match"]))
        self.assertEqual(report.cases["links_lost"], [(11, "8, 9 (the sentence of 10)")])

    def test_an_array_that_cannot_be_combined_leaves_the_origins_untouched(self):
        other = koeda_array(hiiragi=[1378])
        other[2] = word(" 小[しょう] 枝[えだ]", "小枝", "しょうえだ", ["weird"])
        arr, _, report = self.combined(koeda_array(), other, KOEDA, KOEDA_OLD)
        self.assertEqual(arr, koeda_array())
        self.assertEqual(report.note_ids("combine_failed"), [11])

    def test_unknown_match_data_is_not_taken(self):
        arr, _, report = self.combined(cat_fish_array(), cat_fish_array(cat=["weird"], fish=[11]))
        self.assertEqual(arr, cat_fish_array(fish=[11]))
        self.assertEqual(report.note_ids("unknown_match_data"), [11])


class DependentTests(unittest.TestCase):
    def test_a_dependent_found_by_text_has_its_word_linked_though_its_b_marks_another(self):
        # The match op copied the source's sentence, <b> on the source's word, into the note
        vocab = [
            source(1, CAT_FISH, cat_fish_array(fish=[1], eat=["match"])),
            dependent(2, CAT_FISH, ("食べる", "たべる")),
        ]
        result = plan(vocab)
        self.assertEqual(array_of(result.new_sentences[0]), cat_fish_array(fish=[1], eat=[2]))
        self.assertEqual(result.examples[2], sm.Example(new_index=0))
        self.assertEqual(result.report.note_ids("linked"), [2])

    def test_a_dependent_whose_word_is_in_the_sentence_twice_is_reported(self):
        sentence = " 本[ほん]と<b> 猫[ねこ]</b>と 本[ほん]。"
        arr = [
            word(" 本[ほん]", "本", "ほん"),
            particle("と"),
            word(" 猫[ねこ]", "猫", "ねこ", [1]),
            particle("と"),
            word(" 本[ほん]", "本", "ほん"),
            ["。"],
        ]
        result = plan([source(1, sentence, arr, ("猫", "ねこ")), dependent(2, sentence, ("本", "ほん"))])
        self.assertEqual(array_of(result.new_sentences[0]), arr)
        self.assertEqual(result.report.note_ids("link_several"), [2])
        self.assertEqual(result.examples[2], sm.Example(new_index=0))

    def test_of_several_sentences_linking_it_the_one_with_its_text(self):
        other = " 犬[いぬ]が<b> 魚[さかな]</b>を 食[た]べた。"
        other_arr = cat_fish_array(fish=[3], eat=[5])
        other_arr[0] = word(" 犬[いぬ]", "犬", "いぬ")
        vocab = [
            source(1, CAT_FISH, cat_fish_array(fish=[1], eat=[5])),
            source(3, other, other_arr),
            dependent(5, other, ("食べる", "たべる")),
        ]
        result = plan(vocab)
        self.assertEqual(result.examples[5], sm.Example(new_index=1))
        self.assertEqual(result.new_sentences[1].origin_id, 3)
        self.assertEqual(
            result.report.counts["dependents linked from several sentences, chosen by their text"],
            1,
        )
        self.assertEqual(result.report.note_ids("dependent_several"), [])

    def test_of_several_sentences_none_with_its_text_the_oldest_and_reported(self):
        other = " 犬[いぬ]が<b> 魚[さかな]</b>を 食[た]べた。"
        other_arr = cat_fish_array(fish=[3], eat=[5])
        other_arr[0] = word(" 犬[いぬ]", "犬", "いぬ")
        vocab = [
            source(1, CAT_FISH, cat_fish_array(fish=[1], eat=[5])),
            source(3, other, other_arr),
            dependent(5, " 鳥[とり]が 食[た]べた。", ("食べる", "たべる")),
        ]
        result = plan(vocab)
        self.assertEqual(result.examples[5], sm.Example(new_index=0))
        self.assertEqual(result.report.note_ids("dependent_several"), [5])

    def test_the_one_sentence_linking_it_even_with_other_text_reported(self):
        vocab = [
            source(1, CAT_FISH, cat_fish_array(fish=[1], eat=[5])),
            dependent(5, " 鳥[とり]が 食[た]べた。", ("食べる", "たべる")),
        ]
        result = plan(vocab)
        self.assertEqual(result.examples[5], sm.Example(new_index=0))
        self.assertEqual(result.report.note_ids("dependent_other_sentence"), [5])

    def test_no_sentence_linking_it_and_none_with_its_text_no_example(self):
        vocab = [
            source(1, CAT_FISH, cat_fish_array(fish=[1])),
            dependent(5, " 鳥[とり]が 食[た]べた。", ("食べる", "たべる")),
        ]
        result = plan(vocab)
        self.assertNotIn(5, result.examples)
        self.assertEqual(result.report.note_ids("dependent_no_example"), [5])
        self.assertEqual(result.new_sentences[0].vocab_ids, [1])

    def test_a_dependents_own_array_is_reported_and_not_used(self):
        own = json.dumps(cat_fish_array(cat=[999], eat=[5]), ensure_ascii=False)
        vocab = [
            source(1, CAT_FISH, cat_fish_array(fish=[1], eat=[5])),
            dependent(5, CAT_FISH, ("食べる", "たべる"), word_list=own),
        ]
        result = plan(vocab)
        self.assertEqual(array_of(result.new_sentences[0]), cat_fish_array(fish=[1], eat=[5]))
        self.assertEqual(result.report.note_ids("dependent_array"), [5])
        self.assertEqual(result.report.cases.get("conflict"), None)


class LinkCheckTests(unittest.TestCase):
    def test_a_source_not_linked_yet_is_linked_at_its_b(self):
        result = plan([source(1, CAT_FISH, cat_fish_array(fish=["dontmatch"]))])
        self.assertEqual(array_of(result.new_sentences[0]), cat_fish_array(fish=[1]))
        self.assertEqual(
            result.report.cases["linked"], [(1, "魚[さかな] at its <b> (the sentence of 1)")]
        )

    def test_a_word_linked_to_another_note_is_left_and_reported(self):
        result = plan([source(1, CAT_FISH, cat_fish_array(fish=[77]))])
        self.assertEqual(array_of(result.new_sentences[0]), cat_fish_array(fish=[77]))
        self.assertEqual(result.report.note_ids("link_other_note"), [1])

    def test_no_b_is_reported(self):
        result = plan([source(1, sm.strip_bold(CAT_FISH), cat_fish_array())])
        self.assertEqual(array_of(result.new_sentences[0]), cat_fish_array())
        self.assertEqual(result.report.note_ids("link_no_bold"), [1])

    def test_a_source_whose_word_is_in_the_sentence_twice_is_linked_where_its_b_is(self):
        sentence = " 本[ほん]と 猫[ねこ]と<b> 本[ほん]</b>。"
        arr = [
            word(" 本[ほん]", "本", "ほん"),
            particle("と"),
            word(" 猫[ねこ]", "猫", "ねこ"),
            particle("と"),
            word(" 本[ほん]", "本", "ほん"),
            ["。"],
        ]
        result = plan([source(3, sentence, arr, ("本", "ほん"))])
        linked = array_of(result.new_sentences[0])
        self.assertEqual([linked[0][4], linked[4][4]], [[], [3]])

    def test_a_source_whose_b_cuts_a_furigana_group_marks_the_whole_group(self):
        # Read another way than the array has it, so only the place and the form find the word:
        # the <b> ends inside 無人島[むじんとう], whose reading covers the whole group
        field_text = " 彼[かれ]は<k> 此[こ]の</k><b> 無人</b>島[むじんとう]に 行[い]った。"
        record = source(1, field_text, mujinto_array(island=["match"]), ("無人島", "むにんとう"))
        result = plan([record])
        self.assertEqual(array_of(result.new_sentences[0]), mujinto_array(island=[1]))
        self.assertIn("at exactly its <b>", result.report.cases["linked"][0][1])

    def test_a_word_inside_a_group_is_found_at_the_widened_b(self):
        field_text = " 彼[かれ]は<k> 此[こ]の</k><b> 無人</b>島[むじんとう]に 行[い]った。"
        result = plan([source(1, field_text, mujinto_array(), ("無人", "むじん"))])
        self.assertEqual(array_of(result.new_sentences[0]), mujinto_array(mujin=[1]))

    def test_the_smallest_element_around_a_sources_b(self):
        # Spelt another way than the array has it: found by the place and the reading
        sentence = "<b> 食[た]</b>べた"
        subs = [word(" 食[た]べ", "食べる", "たべる"), word("た", "た", "た", pos="auxiliary")]
        arr = [word(" 食[た]べた", "食べた", "たべた", subs=subs)]
        result = plan([source(1, sentence, arr, ("喰べる", "たべる"))])
        linked = array_of(result.new_sentences[0])
        self.assertEqual([linked[0][4], linked[0][5][0][4]], [[], [1]])
        self.assertIn("around its <b>", result.report.cases["linked"][0][1])

    def test_no_link_by_place_where_sub_words_have_no_place(self):
        sentence = "<b> 食[た]</b>べた"
        subs = [word(" 食[た]", "食", "しょく")]  # do not make up 食べた
        arr = [word(" 食[た]べた", "食べた", "たべた", subs=subs)]
        result = plan([source(1, sentence, arr, ("喰べる", "たべる"))])
        self.assertEqual(array_of(result.new_sentences[0]), arr)
        self.assertEqual(result.report.note_ids("link_no_element"), [1])

    def test_a_sources_b_on_a_word_of_neither_its_form_nor_its_reading_links_nothing(self):
        # An untagged copy of another note's sentence whose <b> is still on that note's word,
        # unlinked: by place alone it was linked to this note, and bold on its cards for good
        subs = [word(" 食[た]べ", "食べる", "たべる"), word("た", "た", "た", pos="auxiliary")]
        eaten = [word(" 食[た]べた", "食べた", "たべた", subs=subs)]
        for sentence, arr, vocab_word, marked in [
            (CAT_FISH, cat_fish_array(fish=["match"]), ("鳥", "とり"), "魚[さかな]"),
            ("<b> 食[た]</b>べた", eaten, ("喰う", "くう"), "食べる[たべる]"),
        ]:
            with self.subTest(marked=marked):
                result = plan([source(1, sentence, arr, vocab_word)])
                self.assertEqual(array_of(result.new_sentences[0]), arr)
                self.assertEqual(
                    result.report.cases,
                    {
                        "link_bold_other_word": [
                            (
                                1,
                                f"{vocab_word[0]}[{vocab_word[1]}] in the sentence of 1; its <b>"
                                f" marks {marked}",
                            )
                        ]
                    },
                )

    def test_a_source_whose_array_is_not_of_its_text_is_not_linked(self):
        edited = CAT_FISH.replace("べた", "べる")
        result = plan([source(1, edited, cat_fish_array(fish=["match"]))])
        self.assertEqual(array_of(result.new_sentences[0]), cat_fish_array(fish=["match"]))
        self.assertEqual(result.report.note_ids("link_text_differs"), [1])
        self.assertEqual(result.report.note_ids("array_text_differs"), [1])


class SelectionTests(unittest.TestCase):
    def vocab(self):
        other = " 犬[いぬ]が<b> 魚[さかな]</b>を 食[た]べた。"
        other_arr = cat_fish_array(fish=[3])
        other_arr[0] = word(" 犬[いぬ]", "犬", "いぬ")
        return [
            source(1, CAT_FISH, cat_fish_array(fish=[1], eat=[2])),
            dependent(2, CAT_FISH, ("食べる", "たべる")),
            source(3, other, other_arr),
        ]

    def test_only_the_untagged_selected_makes_every_sentence_and_leaves_the_dependents(self):
        result = plan(self.vocab(), selected=[1, 3])
        self.assertEqual([s.vocab_ids for s in result.new_sentences], [[1], [3]])
        self.assertEqual(set(result.examples), {1, 3})
        reported = [nid for entries in result.report.cases.values() for nid, _ in entries]
        self.assertNotIn(2, reported)

    def test_a_sentence_is_made_only_when_a_selected_note_needs_it(self):
        result = plan(self.vocab(), selected=[3])
        self.assertEqual([s.origin_id for s in result.new_sentences], [3])

    def test_a_selected_dependent_makes_its_sources_sentence(self):
        result = plan(self.vocab(), selected=[2])
        (new,) = result.new_sentences
        self.assertEqual((new.origin_id, new.source_ids, new.vocab_ids), (1, [1], [2]))

    def test_a_selected_note_that_is_not_a_vocab_note(self):
        self.assertEqual(plan(self.vocab(), selected=[999]).report.note_ids("not_vocab"), [999])


class RerunTests(unittest.TestCase):
    def existing(self, nid=500, arr=None, sentence=CAT_FISH):
        arr = cat_fish_array(fish=[1], eat=[2]) if arr is None else arr
        return sm.SentenceRecord(
            note_id=nid,
            fields=fields(sm.strip_bold(sentence)),
            word_list=match_flags.format_word_array(arr),
        )

    def test_a_rerun_creates_nothing_and_joins_the_dependents(self):
        vocab = [
            source(1, CAT_FISH, cat_fish_array(fish=[1], eat=[2]), example_id="500"),
            dependent(2, CAT_FISH, ("食べる", "たべる")),
        ]
        result = plan(vocab, [self.existing()])
        self.assertEqual(result.new_sentences, [])
        self.assertEqual(
            result.updates,
            [
                sm.SentenceUpdate(
                    note_id=500, word_list=None, tags_to_add=[], source_ids=[], vocab_ids=[2]
                )
            ],
        )
        self.assertEqual(result.examples, {2: sm.Example(note_id=500)})
        self.assertEqual(result.report.counts["skipped: already migrated"], 1)

    def test_a_migrated_sources_old_array_does_not_undo_an_edit_of_its_sentence_note(self):
        # Since the first run the user judged 猫 not worth a note on the sentence note; the
        # vocab note still holds its old array, link and all
        vocab = [
            source(1, CAT_FISH, cat_fish_array(cat=[101], fish=[1], eat=[2]), example_id="500"),
            dependent(2, CAT_FISH, ("食べる", "たべる")),
        ]
        edited = self.existing(arr=cat_fish_array(cat=["dontmatch"], fish=[1], eat=[2]))
        (update,) = plan(vocab, [edited]).updates
        self.assertIsNone(update.word_list)

    def test_a_dependent_found_by_text_in_an_existing_note_is_linked(self):
        vocab = [dependent(2, CAT_FISH, ("食べる", "たべる"))]
        result = plan(vocab, [self.existing(arr=cat_fish_array(fish=[1], eat=["match"]))])
        (update,) = result.updates
        self.assertEqual(json.loads(update.word_list), cat_fish_array(fish=[1], eat=[2]))

    def test_a_source_not_migrated_yet_joins_and_merges_its_links(self):
        # A cancelled run added the sentence note but never changed the vocab note
        vocab = [source(1, CAT_FISH, cat_fish_array(cat=[101], fish=[1]), reps=7)]
        existing = self.existing(arr=cat_fish_array(cat=["match"], fish=[1]))
        result = plan(vocab, [existing], count_seen=True)
        self.assertEqual(result.new_sentences, [])
        (update,) = result.updates
        self.assertEqual(json.loads(update.word_list), cat_fish_array(cat=[101], fish=[1]))
        self.assertEqual((update.source_ids, update.vocab_ids, update.tags_to_add), ([1], [1], []))
        self.assertEqual(result.examples, {1: sm.Example(note_id=500)})

    def test_a_conflict_on_joining_keeps_the_existing_link_and_tags_the_note(self):
        vocab = [source(1, CAT_FISH, cat_fish_array(cat=[101], fish=[1]))]
        result = plan(vocab, [self.existing(arr=cat_fish_array(cat=[102], fish=[1]))])
        (update,) = result.updates
        self.assertIsNone(update.word_list)
        self.assertEqual(update.tags_to_add, ["sentence-migration-conflict"])

    def test_an_existing_note_without_a_readable_array_is_joined_without_its_links(self):
        existing = sm.SentenceRecord(note_id=500, fields=fields(CAT_FISH), word_list="[broken")
        result = plan([source(1, CAT_FISH, cat_fish_array(fish=[1]))], [existing])
        (update,) = result.updates
        self.assertEqual((update.note_id, update.word_list, update.vocab_ids), (500, None, [1]))
        self.assertEqual(result.report.note_ids("existing_no_array"), [1])
        self.assertEqual(result.report.note_ids("link_no_array"), [1])

    def test_an_example_id_naming_no_sentence_note_is_migrated_anyway(self):
        result = plan([source(1, CAT_FISH, cat_fish_array(fish=[1]), example_id="123")])
        self.assertEqual(len(result.new_sentences), 1)
        self.assertEqual(result.report.cases["example_id_unknown"], [(1, "123")])

    def test_the_older_of_two_existing_notes_with_one_sentence_is_joined(self):
        existing = [self.existing(600), self.existing(500)]
        result = plan([source(1, CAT_FISH, cat_fish_array(fish=[1]))], existing)
        self.assertEqual([u.note_id for u in result.updates], [500])
        self.assertEqual(result.report.note_ids("existing_duplicate"), [600])


class ReportTests(unittest.TestCase):
    def test_the_text_lists_counts_and_each_case_with_its_note_ids(self):
        result = plan([source(1, CAT_FISH, cat_fish_array(fish=[77])), source(2, "", fields={})])
        text = result.report.text()
        self.assertIn("  sentence notes to add: 1\n", text)
        self.assertIn(f"{sm.CASES['link_other_note']} (1)\n  1: 魚 links 77", text)
        self.assertIn(f"{sm.CASES['no_sentence_text']} (1)\n  2\n", text)


if __name__ == "__main__":
    unittest.main()
