"""The furigana audio eval's scripts: reading a subs2srs export, cleaning a caption line,
placing its inline readings, finding the hard words and picking the lines, copying the clips.

Nothing here tokenizes with Sudachi: a stand-in segments the few lines the tests use, so the
suite runs without SudachiPy or the downloaded dictionaries."""

import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

# See the note in test_vocab_morphology.py: the suite already has a `conftest` of its own, so
# the path is set here rather than in one more file by that name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import furigana_audio as fa  # noqa: E402
import furigana_audio_copy as copy_script  # noqa: E402
import furigana_audio_score as score  # noqa: E402
import furigana_audio_select as select  # noqa: E402

NOUN = ("名詞", "普通名詞", "一般", "*", "*", "*")
PROPER = ("名詞", "固有名詞", "人名", "一般", "*", "*")
PARTICLE = ("助詞", "格助詞", "*", "*", "*", "*")
NUMERAL = ("名詞", "数詞", "*", "*", "*", "*")
COUNTER = ("接尾辞", "名詞的", "助数詞", "*", "*", "*")


class Morpheme:
    def __init__(self, surface, reading, pos, begin, lemma=None):
        self._surface, self._reading, self._pos = surface, reading, pos
        self._begin, self._lemma = begin, lemma or surface

    def surface(self):
        return self._surface

    def reading_form(self):
        return self._reading

    def part_of_speech(self):
        return self._pos

    def begin(self):
        return self._begin

    def end(self):
        return self._begin + len(self._surface)

    def dictionary_form(self):
        return self._lemma


# Sudachi's take on the lines below: the names cut up and read as common words
LEXICON = {
    "猫": ("ネコ", NOUN),
    "白": ("シロ", NOUN),
    "鈴": ("スズ", NOUN),
    "姐": ("アネ", NOUN),
    "馬": ("ウマ", NOUN),
    "他": ("ホカ", NOUN),
    "薬": ("クスリ", NOUN),
    "三": ("サン", NUMERAL),
    "人": ("ニン", COUNTER),
    "高順": ("タカノブ", PROPER),
}


def tokenize(text):
    """Longest match over LEXICON; any other character is a morpheme of its own."""
    out, i = [], 0
    while i < len(text):
        for n in range(len(text) - i, 0, -1):
            piece = text[i : i + n]
            if piece in LEXICON:
                reading, pos = LEXICON[piece]
                out.append(Morpheme(piece, reading, pos, i))
                i += n
                break
        else:
            out.append(Morpheme(text[i], fa.to_hiragana(text[i]), PARTICLE, i))
            i += 1
    return out


def row(n, jp, en="-"):
    return fa.Row(f"01_{n:04d}_0.00.00.000", "01", f"clip_{n}.opus", jp, en)


class ExportTest(unittest.TestCase):
    def test_rows_and_the_line_that_is_no_card(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "export.tsv"
            path.write_text(
                "JP_01\t01_0001_0.01.37.700\t[sound:a.opus]\t<img src=\"a.webp\">\t猫猫\tMaomao.\n"
                "JP_01\t01_0006_0.02.52.942\t[sound:b.opus]\t<img src=\"b.webp\">\t6\n",
                encoding="utf-8",
            )
            rows, skipped = fa.read_export(path)
        self.assertEqual(
            rows, [fa.Row("01_0001_0.01.37.700", "01", "a.opus", "猫猫", "Maomao.")]
        )
        self.assertEqual(len(skipped), 1)
        self.assertIn("01_0006", skipped[0])


class CleaningTest(unittest.TestCase):
    def test_labels_sound_descriptions_and_ass_tags_are_not_spoken(self):
        jp = (
            "（羅門(ルォメン)）猫猫(マオマオ)<br>（猫猫）ん？<br>（赤ん坊のはしゃぎ声）"
            "<br>{\\an8}（客）おお～っ"
        )
        self.assertEqual(fa.spoken_lines(jp), ["猫猫(マオマオ)", "ん？", "おお～っ"])

    def test_a_label_in_the_middle_of_a_line(self):
        self.assertEqual(fa.spoken_lines("（白鈴）あら 猫猫 （猫猫）ん？"), ["あら 猫猫 ん？"])

    def test_noisy_cards_have_ass_tags(self):
        self.assertTrue(fa.is_noisy("これじゃ {\\an8}（客）おお～っ これじゃ"))
        self.assertFalse(fa.is_noisy("（猫猫）ん？"))

    def test_readings_are_taken_out_and_kept_by_place(self):
        text, readings = fa.split_readings("あら 猫猫(マオマオ) 緑青館(ろくしょうかん)に")
        self.assertEqual(text, "あら 猫猫 緑青館に")
        self.assertEqual(
            readings,
            [
                fa.InlineReading("猫猫", "まおまお", True, 3, 5),
                fa.InlineReading("緑青館", "ろくしょうかん", False, 6, 9),
            ],
        )

    def test_a_reading_after_other_kanji_takes_the_tail_base_gives(self):
        text, readings = fa.split_readings("白鈴姐(ねえ)ちゃん", base=lambda run, reading: 1)
        self.assertEqual(text, "白鈴姐ちゃん")
        self.assertEqual(readings, [fa.InlineReading("姐", "ねえ", False, 2, 3)])

    def test_labels_readings_count_for_the_show(self):
        readings = fa.all_inline_readings("（羅門(ルォメン)）ん？")
        self.assertEqual([(r.surface, r.reading) for r in readings], [("羅門", "るぉめん")])


class ReadingBaseTest(unittest.TestCase):
    def base(self, rows=(), dictionary=None):
        table = dictionary or {}
        return select.ReadingBase(rows, lambda form: table.get(form, set()))

    def test_a_tail_the_dictionary_reads_so(self):
        base = self.base(dictionary={"妓楼": {"ぎろう"}})
        self.assertEqual(base("高級妓楼", "ぎろう"), 2)

    def test_a_tail_the_captions_read_so_on_its_own(self):
        base = self.base(rows=[row(1, "公主(ひめ)")])
        self.assertEqual(base("鈴麗公主", "ひめ"), 2)

    def test_the_whole_run_when_the_reading_can_cover_it(self):
        self.assertEqual(self.base()("緑青館", "ろくしょうかん"), 3)

    def test_no_more_kanji_than_the_reading_has_kana(self):
        self.assertEqual(self.base()("白鈴姐", "ね"), 1)


class KanjiWordsTest(unittest.TestCase):
    def words(self, line, names=()):
        text, readings = fa.split_readings(line)
        return select.kanji_words(0, text, tokenize(text), readings, names)

    def test_a_name_the_tokenizer_cut_is_one_word(self):
        words = self.words("あら 猫猫", names={"猫猫"})
        self.assertEqual(
            [(w.surface, w.sudachi, w.kinds) for w in words], [("猫猫", "ねこねこ", ["name"])]
        )

    def test_an_inline_reading_is_the_words_caption(self):
        words = self.words("猫猫(マオマオ)")
        self.assertEqual(
            [(w.surface, w.caption, w.kinds) for w in words], [("猫猫", "まおまお", ["name"])]
        )

    def test_a_reading_of_a_words_kanji_takes_in_its_okurigana(self):
        LEXICON["噛みつい"] = ("カミツイ", NOUN)
        try:
            words = self.words("噛(か)みついた")
        finally:
            del LEXICON["噛みつい"]
        self.assertEqual([(w.surface, w.caption) for w in words], [("噛みつい", "かみつい")])

    def test_a_one_kanji_name_only_where_it_is_a_proper_noun(self):
        # 馬 is the horse here: the stand-in tags it a common noun
        self.assertEqual([w.kinds for w in self.words("馬", names={"馬"})], [[]])

    def test_a_proper_noun_of_sudachis_is_a_name(self):
        self.assertEqual([(w.surface, w.kinds) for w in self.words("高順")], [("高順", ["name"])])


class SecondReadingTest(unittest.TestCase):
    def test_a_reading_given_twice_in_hundreds_is_no_second_reading(self):
        self.assertFalse(select.second_reading(Counter({"さま": 225, "よう": 2})))

    def test_two_readings_each_given_often(self):
        self.assertTrue(select.second_reading(Counter({"ほか": 43, "た": 40})))

    def test_three_uses_are_too_few_a_share_of_a_hundred(self):
        self.assertFalse(select.second_reading(Counter({"なか": 97, "ちゅう": 3})))


class SelectTest(unittest.TestCase):
    ROWS = [
        row(1, "（白鈴(パイリン)）猫猫(マオマオ)"),
        row(2, "（白鈴）あら 猫猫 （猫猫）ん？"),
        row(3, "（猫猫）白鈴姐(ねえ)ちゃん"),
        row(4, "他に 三人"),
        row(5, "薬"),
        row(6, "これじゃ {\\an8}（客）薬 これじゃ"),
        row(7, "ん？"),
        row(8, "猫猫 薬"),
    ]

    def cards(self):
        base = select.ReadingBase(self.ROWS, lambda form: set())
        show = select.ShowReadings()
        for r in self.ROWS:
            show.add(r, base)
        return select.read_cards(self.ROWS, tokenize, show, base)

    def test_noisy_cards_and_cards_without_kanji_are_never_picked(self):
        ids = {c.row.id for c in self.cards()}
        self.assertNotIn(row(6, "").id, ids)
        self.assertNotIn(row(7, "").id, ids)

    def test_the_hard_words_of_a_card(self):
        cards = {c.row.id: c for c in self.cards()}
        self.assertEqual(cards[row(2, "").id].reasons, ["name:猫猫"])
        self.assertEqual(cards[row(4, "").id].reasons, ["ambiguous:他", "number:三", "number:人"])
        third = cards[row(3, "").id]
        self.assertEqual(
            [(w.surface, w.caption, w.kinds) for w in third.words],
            [("白鈴", None, ["name"]), ("姐", "ねえ", ["inline"])],
        )

    def test_the_same_seed_picks_the_same_cards_once_each(self):
        first = [(s, c.row.id) for s, c in select.select(self.cards(), 4, seed=3)]
        again = [(s, c.row.id) for s, c in select.select(self.cards(), 4, seed=3)]
        self.assertEqual(first, again)
        self.assertEqual(len({i for _, i in first}), 4)

    def test_every_card_when_there_are_fewer_than_asked(self):
        picked = select.select(self.cards(), 50, seed=1)
        self.assertEqual(len(picked), len(self.cards()))
        self.assertIn("names", {s for s, _ in picked})


class CopyTest(unittest.TestCase):
    def test_copies_by_name_from_anywhere_under_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d) / "out" / "deck.media"
            media.mkdir(parents=True)
            (media / "a.opus").write_bytes(b"a")
            (media / "b.opus").write_bytes(b"b")
            dest = Path(d) / "audio"
            dest.mkdir()
            (dest / "b.opus").write_bytes(b"old")
            selection = [{"audio": "a.opus"}, {"audio": "b.opus"}, {"audio": "c.opus"}]
            copied, kept, missing = copy_script.copy_clips(selection, Path(d) / "out", dest)
            self.assertEqual((copied, kept, missing), (1, 1, ["c.opus"]))
            self.assertEqual((dest / "a.opus").read_bytes(), b"a")
            self.assertEqual((dest / "b.opus").read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()


class ScoreTest(unittest.TestCase):
    """The comparison of a transcript with a reading, in morae as pronounced."""

    def test_morae_join_small_kana_and_lengthen_vowels(self):
        self.assertEqual(score.morae("きょうはいい"), ["キョ", "ウ", "ハ", "イ", "イ"])
        self.assertEqual(score.morae("キョーワ"), ["キョ", "オ", "ワ"])
        self.assertEqual(score.morae("ぢづを、！"), ["ジ", "ズ", "オ"])

    def test_one_sound_spelled_two_ways_agrees(self):
        self.assertTrue(score.agrees("とうきょう", "トーキョー"))
        self.assertTrue(score.agrees("せんせい", "センセー"))

    def test_speech_dropping_a_long_vowel_or_geminate_agrees(self):
        self.assertTrue(score.agrees("いじょう", "イジョ"))
        self.assertTrue(score.agrees("いった", "イタ"))

    def test_another_reading_does_not(self):
        self.assertFalse(score.agrees("ねこねこ", "まおまお"))
        self.assertFalse(score.agrees("ほか", "た"))
        # rendaku is a reading of its own: 洗濯係 is せんたくがかり
        self.assertFalse(score.agrees("かかり", "がかり"))

    def test_a_word_heard_as_another_of_its_length_lines_up_mora_by_mora(self):
        ref = score.morae("ですおうかです")
        hyp = score.morae("ですいんふぁです")
        _, pairs = score.align(ref, hyp)
        self.assertEqual([hyp[j] for j in pairs[2:5] if j is not None], ["イ", "ン", "ファ"])

    def test_ruby_answers_keep_their_readings(self):
        self.assertEqual(score.heard_text("ruby", "七[なな]年間[ねんかん]で"), "ななねんかんで")
        self.assertEqual(score.heard_text("kana-whisper", "ナナネン"), "ナナネン")
