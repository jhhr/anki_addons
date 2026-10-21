"""The furigana audio eval's scripts: reading a subs2srs export, finding the hard words and
picking the lines, copying the clips, scoring the runs. The caption cleaning and the word units
they use are the op's, tested with it (test/test_word_array_captions.py, _kanji_words.py).

Nothing here tokenizes with Sudachi: a stand-in segments the few lines the tests use, so the
suite runs without SudachiPy or the downloaded dictionaries."""

import sys
import tempfile
import unittest
import unittest.mock
from collections import Counter
from pathlib import Path

# See the note in test_vocab_morphology.py: the suite already has a `conftest` of its own, so
# the path is set here rather than in one more file by that name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import furigana_audio as fa  # noqa: E402
import furigana_audio_copy as copy_script  # noqa: E402
import furigana_audio_run as run_script  # noqa: E402
import furigana_audio_score as score  # noqa: E402
import furigana_audio_select as select  # noqa: E402

NOUN = ("名詞", "普通名詞", "一般", "*", "*", "*")
PROPER = ("名詞", "固有名詞", "人名", "一般", "*", "*")
PARTICLE = ("助詞", "格助詞", "*", "*", "*", "*")
NUMERAL = ("名詞", "数詞", "*", "*", "*", "*")
COUNTER = ("接尾辞", "名詞的", "助数詞", "*", "*", "*")
COUNTER_NOUN = ("名詞", "普通名詞", "助数詞可能", "*", "*", "*")
SPACE = ("空白", "*", "*", "*", "*", "*")
BRACKET = ("補助記号", "括弧開", "*", "*", "*", "*")


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
    # Sudachi reads digits one by one, kanji numerals as numbers
    "10": ("イチレイ", NUMERAL),
    "175": ("イチナナゴ", NUMERAL),
    "百七十五": ("ヒャクナナジュウゴ", NUMERAL),
    "七": ("ナナ", NUMERAL),
    # A name Sudachi cuts into kanji, and a compound it cuts into words it knows
    "響": ("ヒビキ", NOUN),
    "神": ("カミ", NOUN),
    "美": ("ビ", NOUN),
    "物置": ("モノオキ", NOUN),
    "小屋": ("コヤ", NOUN),
    "日": ("ニチ", COUNTER_NOUN),
    "十日": ("トオカ", NOUN),
    "3": ("サン", NUMERAL),
    "つ": ("ツ", COUNTER),
    # Sudachi's reading of a space or a bracket
    "\u3000": ("キゴウ", SPACE),
    "《": ("キゴウ", BRACKET),
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


class LabellingFilesTest(unittest.TestCase):
    def test_a_typed_reading_as_the_captions_write_one(self):
        for typed in ("父上[ちちうえ]が", " 父上（ちちうえ）が "):
            self.assertEqual(fa.typed_line(typed), "父上(ちちうえ)が")

    def test_names_and_fixes_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            names, fixes = Path(d) / "names.jsonl", Path(d) / "fixes.jsonl"
            fa.write_jsonl(names, [{"surface": "子翠", "reading": "しすい"}])
            fa.write_jsonl(fixes, [{"id": "c1", "line": 2, "text": "", "was": "響"}])
            self.assertEqual(fa.read_names(names), {"子翠": "しすい"})
            self.assertEqual(fa.read_fixes(fixes)[("c1", 2)]["was"], "響")

    def test_a_corrected_lines_labels_move_with_their_words(self):
        rows = [
            {"id": "c1", "line": 1, "start": 0, "surface": "養父", "reading": "ちち"},
            {"id": "c1", "line": 1, "start": 4, "end": 6, "surface": "座っ", "reading": "すわっ"},
        ]
        moved = fa.moved_labels(rows, "養父上が座った", "父上が座った")
        # 養父 is no word of the line now: its label stays where it was, stale
        self.assertEqual(
            [(r["surface"], r["start"], r.get("end")) for r in moved],
            [("養父", 0, None), ("座っ", 3, 5)],
        )

    def test_a_moved_label_takes_the_place_of_a_stale_one(self):
        rows = [
            {"id": "c1", "line": 0, "start": 0, "surface": "他", "reading": "ほか"},
            {"id": "c1", "line": 0, "start": 1, "surface": "物", "reading": "もの"},
        ]
        moved = fa.moved_labels(rows, "他物", "物")
        self.assertEqual([(r["surface"], r["start"]) for r in moved], [("物", 0)])

    def test_a_name_given_by_ear_is_a_show_name(self):
        with tempfile.TemporaryDirectory() as d:
            names, captions = Path(d) / "names.jsonl", Path(d) / "captions.jsonl"
            fa.write_jsonl(names, [{"surface": "子翠", "reading": "しすい"}])
            caption = {"surface": "猫猫", "script": "katakana", "readings": {"まおまお": 3}}
            fa.write_jsonl(captions, [caption])
            with unittest.mock.patch.object(fa, "NAMES", names):
                with unittest.mock.patch.object(fa, "CAPTION_READINGS", captions):
                    show = score.show_readings()
        self.assertEqual(show, {"猫猫": ("name", "まおまお"), "子翠": ("name", "しすい")})


class RewordTest(unittest.TestCase):
    def test_the_same_cards_in_the_same_strata_with_their_words_found_again(self):
        def card(card_id, surface):
            r = fa.Row(card_id, "01", f"{card_id}.opus", surface, "-")
            return select.Card(r, [surface], [select.Word(0, 0, len(surface), surface, "x")])

        picked = [
            select.selection_row("names", card("b", "日")),
            select.selection_row("random", card("a", "他")),
        ]
        cards = [card("a", "他"), card("b", "10日")]
        rows, changed = select.reword(picked, cards)
        self.assertEqual([(r["id"], r["stratum"]) for r in rows], [("b", "names"), ("a", "random")])
        self.assertEqual(rows[0]["words"][0]["surface"], "10日")
        self.assertEqual(changed, 1)


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

    def cards(self, fixes=None, named=()):
        base = fa.captions.ReadingBase((r.jp for r in self.ROWS), lambda form: set())
        show = select.ShowReadings()
        for r in self.ROWS:
            show.add(r, base)
        return select.read_cards(self.ROWS, tokenize, show, base, fixes, named)

    def test_a_corrected_line_is_read_as_corrected_and_an_empty_one_keeps_its_place(self):
        fixes = {(row(8, "").id, 0): {"text": "響迂 薬"}}
        cards = {c.row.id: c for c in self.cards(fixes, named={"響迂"})}
        eight = cards[row(8, "").id]
        self.assertEqual(eight.lines, ["響迂 薬"])
        words = [(w.surface, w.kinds) for w in eight.words]
        self.assertEqual(words, [("響迂", ["name"]), ("薬", [])])
        two_lines = row(9, "猫猫<br>薬")
        show = select.ShowReadings()
        fixes = {(two_lines.id, 0): {"text": ""}}
        card = select.read_cards([two_lines], tokenize, show, fa.whole_run, fixes)[0]
        self.assertEqual(card.lines, ["", "薬"])
        self.assertEqual([(w.line, w.surface) for w in card.words], [(1, "薬")])

    def test_noisy_cards_and_cards_without_kanji_are_never_picked(self):
        self.assertTrue(fa.is_noisy("これじゃ {\\an8}（客）おお～っ これじゃ"))
        self.assertFalse(fa.is_noisy("（猫猫）ん？"))
        ids = {c.row.id for c in self.cards()}
        self.assertNotIn(row(6, "").id, ids)
        self.assertNotIn(row(7, "").id, ids)

    def test_the_hard_words_of_a_card(self):
        cards = {c.row.id: c for c in self.cards()}
        self.assertEqual(cards[row(2, "").id].reasons, ["name:猫猫"])
        self.assertEqual(cards[row(4, "").id].reasons, ["ambiguous:他", "number:三人"])
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


class HeardTextTest(unittest.TestCase):
    """What a model's answer says was heard, whatever it writes: kana, ruby text or ordinary
    text. The comparison itself is the op's, tested with it (test_word_array_reading_check.py)."""

    def test_ruby_answers_keep_their_readings(self):
        heard = score.heard_text("ruby", "七[なな]年間[ねんかん]で", tokenize)
        self.assertEqual(heard, "ななねんかんで")
        heard = score.heard_text("ruby", "でも優[やさ]しい親父[おやじ]", tokenize)
        self.assertEqual(heard, "でもやさしいおやじ")
        self.assertEqual(score.heard_text("kana-whisper", "ナナネン", tokenize), "ナナネン")

    def test_ordinary_text_reads_digits_as_a_number(self):
        self.assertEqual(score.heard_text("anime-whisper", "10日", tokenize), "トオカ")

    def test_ordinary_text_is_read_as_sudachi_reads_it(self):
        # the kanji give Sudachi's reading back; a name written in kana gives the name's
        heard = score.heard_text("anime-whisper", "猫猫は薬", tokenize)
        self.assertEqual(heard, "ネコネコワクスリ")
        heard = score.heard_text("anime-whisper", "マオマオは薬", tokenize)
        self.assertEqual(heard, "マオマオワクスリ")


class ScoreCardTest(unittest.TestCase):
    """A card's words, and the readings the op may write for each: the dictionary's and, for a
    name, the show's."""

    def card(self, line, word):
        return {"id": "c1", "lines": [line], "words": [{"line": 0, "start": 0, **word}]}

    def reference(self, card):
        return score.rc.reference(*score.card_spans(card), tokenize)

    def test_digits_no_word_took_are_read_as_a_number(self):
        ref, _, _ = self.reference({"id": "c1", "lines": ["175"], "words": []})
        self.assertEqual(ref, score.morae("ヒャクナナジュウゴ"))

    def test_a_numbers_dictionary_readings_are_those_of_it_in_kanji(self):
        found = {"十日": {"とおか", "じゅうにち"}}
        with unittest.mock.patch.object(score, "jmdict_readings", lambda f: found.get(f, set())):
            readings = score.dictionary_readings()
            self.assertEqual(readings({"surface": "10日"}), ["じゅうにち", "とおか"])

    def test_a_label_given_to_another_word_at_its_place_is_stale(self):
        word = {"end": 1, "surface": "他", "sudachi": "ほか", "caption": None}
        card = self.card("他に", {**word, "kinds": []})
        said = {"id": "c1", "line": 0, "start": 0, "reading": "た", "verdict": "heard"}
        labels = {("c1", 0, 0): {**said, "surface": "七"}}
        words, _, _ = score.score_card(card, "タニ", "kana-whisper", tokenize, {}, labels=labels)
        self.assertIsNone(words[0].label)
        self.assertNotIn("label", words[0].verdicts)

    def test_a_space_or_a_bracket_is_no_sound(self):
        word = {"end": 2, "surface": "他", "sudachi": "ほか", "caption": None, "kinds": []}
        card = self.card("《他\u3000に", {**word, "start": 1})
        ref, owner, _ = self.reference(card)
        self.assertEqual(ref, ["ホ", "カ", "ニ"])
        self.assertEqual(owner, [0, 0, -1])

    def test_a_reading_the_dictionary_has_for_the_spelling(self):
        word = {"end": 1, "surface": "他", "sudachi": "ほか", "caption": None}
        card = self.card("他に", {**word, "kinds": ["ambiguous"]})
        words, _, _ = score.score_card(
            card, "タニ", "kana-whisper", tokenize, {}, lambda w: ["た", "ほか"]
        )
        self.assertEqual(words[0].op_verdicts, {"draft": False, "た": True})

    def test_the_shows_reading_of_a_name(self):
        word = {"end": 2, "surface": "猫猫", "sudachi": "ねこねこ", "caption": None}
        card = self.card("猫猫が", {**word, "kinds": ["name"]})
        show = {"猫猫": ("name", "まおまお")}
        words, _, _ = score.score_card(card, "マオマオガ", "kana-whisper", tokenize, show)
        self.assertEqual(words[0].verdicts, {"draft": False, "show_name": True})
        self.assertEqual(words[0].op_verdicts, {"draft": False, "まおまお": True})
        self.assertEqual(words[0].gold, "まおまお")

    def test_a_labelled_word_says_whether_the_model_heard_the_label(self):
        word = {"end": 1, "surface": "他", "sudachi": "ほか", "caption": None}
        card = self.card("他に", {**word, "kinds": ["ambiguous"]})
        said = {"id": "c1", "line": 0, "start": 0, "surface": "他", "reading": "た"}
        labels = {("c1", 0, 0): {**said, "verdict": "heard"}}
        words, _, _ = score.score_card(card, "タニ", "kana-whisper", tokenize, {}, labels=labels)
        self.assertTrue(words[0].verdicts["label"])
        self.assertEqual(words[0].row()["label"], "た")
        words, _, _ = score.score_card(card, "ホカニ", "kana-whisper", tokenize, {}, labels=labels)
        self.assertFalse(words[0].verdicts["label"])
        unclear = {("c1", 0, 0): {**said, "reading": None, "verdict": "not_said"}}
        words, _, _ = score.score_card(card, "タニ", "kana-whisper", tokenize, {}, labels=unclear)
        self.assertNotIn("label", words[0].verdicts)
        self.assertEqual(words[0].row()["label_verdict"], "not_said")

    def test_a_draft_with_a_kanji_sudachi_could_not_read_is_never_written(self):
        word = {"end": 2, "surface": "翠苓", "sudachi": "すい苓", "caption": None}
        card = self.card("翠苓は", {**word, "kinds": ["name"]})
        show = {"翠苓": ("name", "すいれい")}
        words, _, _ = score.score_card(card, "スイレイワ", "kana-whisper", tokenize, show)
        self.assertEqual(words[0].op_verdicts, {"draft": False, "すいれい": True})
        words, _, _ = score.score_card(card, "スイワ", "kana-whisper", tokenize, {})
        self.assertEqual(words[0].op_verdicts, {"draft": False})


class CombineTest(unittest.TestCase):
    """What the op would write, accepting a reading where a quorum of the models hear it."""

    def word(self, start, heard, op_hears, gold=None, kinds=(), clean=True):
        return {
            "id": "c1",
            "line": 0,
            "start": start,
            "surface": "他",
            "sudachi": "ほか",
            "caption": gold,
            "kinds": list(kinds),
            "heard": heard,
            "clean": clean,
            "gold": gold,
            "gold_is_draft": score.same_reading(gold, "ほか") if gold else None,
            "hears": {},
            "op_hears": op_hears,
        }

    def test_the_outcomes_of_each_kind_of_word(self):
        draft = {"draft": True, "た": False}
        other = {"draft": False, "た": True}
        neither = {"draft": False, "た": False}
        a = [
            self.word(0, "ホカ", draft, gold="た"),  # both hear the wrong draft
            self.word(1, "タ", other, gold="た"),  # both hear the caption's, which JMdict has
            self.word(2, "タ", other, gold="ほか"),  # both hear another reading than the right one
            self.word(3, "ホカ", draft),  # an ordinary word both confirm
            self.word(4, "タ", other),  # both hear a reading the op may write instead
            self.word(5, "ナニ", neither),  # both hear one reading nothing licenses
            self.word(6, "ナニ", neither),  # the models disagree
            self.word(7, "ホカ", draft, clean=False),  # a card that does not match its line
        ]
        b = [dict(w) for w in a]
        b[6]["heard"] = "ナン"
        text = self.combine({"a": a, "b": b})
        self.assertIn("7 words of 1 cards", text)
        self.assertIn("2 words: right 50.0%, wrong 50.0%, review 0.0%", text)
        self.assertIn("1 words: right 0.0%, wrong 100.0%, review 0.0%", text)
        self.assertIn(
            "4 words: draft 25.0%, other reading 25.0%,"
            " review 50.0% (25.0% one reading nothing licenses)",
            text,
        )
        # the card is of the random stratum: the line an op's review rate is read from
        self.assertIn("1 of 1 (100%) have a word to review, 2 of 7 words (28.6%)", text)

    def test_a_quorum_of_the_models(self):
        draft = {"draft": True, "た": False}
        other = {"draft": False, "た": True}
        a = [self.word(0, "ホカ", draft), self.word(1, "タ", other, gold="た")]
        b = [dict(w) for w in a]
        c = [self.word(0, "タ", other), self.word(1, "ホカ", draft, gold="た")]
        runs = {"a": a, "b": b, "c": c}
        every = self.combine(runs)
        self.assertIn("1 words: right 0.0%, wrong 0.0%, review 100.0%", every)
        self.assertIn("1 words: draft 0.0%, other reading 0.0%, review 100.0%", every)
        two = self.combine(runs, quorum=2)
        self.assertIn("a + b + c, 2 of 3", two)
        self.assertIn("right 100.0%, wrong 0.0%, review 0.0%", two)
        self.assertIn("draft 100.0%, other reading 0.0%, review 0.0%", two)

    def test_a_reading_written_a_vowel_short_of_the_one_said_is_wrong(self):
        # Both hear ババ and the op writes ばば, where the line says ばばあ
        a = [self.word(0, "ババ", {"draft": False, "ばば": True}, gold="ばばあ")]
        text = self.combine({"a": a, "b": [dict(w) for w in a]})
        self.assertIn("1 words: right 0.0%, wrong 100.0%, review 0.0%", text)

    def test_a_label_is_the_known_reading_and_each_bucket_is_counted_against_them(self):
        other = {"draft": False, "た": True}
        draft = {"draft": True, "た": False}
        a = [
            self.word(0, "タ", other),  # written た, said た
            self.word(1, "ホカ", draft),  # written ほか, said た
            self.word(2, "ナニ", {"draft": False, "た": False}),  # review, said ほか
            self.word(3, "ホカ", draft),  # can't tell
            self.word(4, "ホカ", draft),  # not labelled
        ]
        labels = [
            {"id": "c1", "line": 0, "start": 0, "reading": "た", "verdict": "heard"},
            {"id": "c1", "line": 0, "start": 1, "reading": "た", "verdict": "heard"},
            {"id": "c1", "line": 0, "start": 2, "reading": "ほか", "verdict": "heard"},
            {"id": "c1", "line": 0, "start": 3, "reading": None, "verdict": "unsure"},
        ]
        labels = [{**row, "surface": "他"} for row in labels]
        text = self.combine({"a": a, "b": [dict(w) for w in a]}, labels=labels)
        self.assertIn("2 words: right 50.0%, wrong 50.0%, review 0.0%", text)  # Sudachi wrong
        self.assertIn("1 words: right 0.0%, wrong 0.0%, review 100.0%", text)  # Sudachi right
        self.assertIn("labelled by ear: 4 words, of them 1 can't tell and 0 not said", text)
        self.assertIn("correction     1 written: right 1, wrong 0", text)
        self.assertIn("review         1: said as the draft 1, otherwise 0", text)
        self.assertIn("agreed         1 written: right 0, wrong 1", text)

    def combine(self, runs, quorum=None, labels=()):
        with tempfile.TemporaryDirectory() as d:
            here = Path(d)
            for name, words in runs.items():
                fa.write_jsonl(here / f"{name}.words.jsonl", words)
            selection = here / "selection.jsonl"
            fa.write_jsonl(selection, [{"id": "c1", "stratum": "random"}])
            fa.write_jsonl(here / "labels.jsonl", labels)
            with unittest.mock.patch.object(score, "RUNS", here):
                with unittest.mock.patch.object(fa, "SELECTION", selection):
                    with unittest.mock.patch.object(fa, "LABELS", here / "labels.jsonl"):
                        return score.combine(list(runs), quorum)


class FoldTest(unittest.TestCase):
    def test_a_runs_part_joins_its_file_in_the_selections_order(self):
        with tempfile.TemporaryDirectory() as d:
            runs = Path(d)
            selection = runs / "selection.jsonl"
            fa.write_jsonl(selection, [{"id": "a"}, {"id": "b"}, {"id": "c"}])
            out, part = runs / "m.jsonl", runs / "m.jsonl.part"
            fa.write_jsonl(out, [{"id": "c", "text": "C"}])
            fa.write_jsonl(part, [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}])
            with unittest.mock.patch.object(fa, "SELECTION", selection):
                run_script.fold(out, part)
            self.assertEqual([r["id"] for r in fa.read_jsonl(out)], ["a", "b", "c"])
            self.assertFalse(part.exists())


if __name__ == "__main__":
    unittest.main()
