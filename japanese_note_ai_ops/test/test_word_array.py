"""The word array generator's structural guarantees, on the hand-made gold examples.

Accuracy against the gold is measured by word_array/research/evaluate.py; these tests pin what
must always hold whatever the word choices: the array partitions the sentence, sub-words make
up their parent, <b> can wrap any word, and a few behaviors the design depends on.

Skipped unless SudachiPy, a Sudachi dictionary and JMdict are all available. The add-on
downloads the last two on first use; word_array/research/setup_resources.py does the same
from a shell.
"""

import importlib
import re
import unittest

from addon_modules import PACKAGE, load_addon_module, load_ops_module

resources = load_ops_module("resources", subdir="word_array")

TAG_RE = re.compile(r"<(/?)([a-zA-Z]+)[^>]*>")


def balanced(html: str) -> bool:
    stack = []
    for m in TAG_RE.finditer(html):
        if not m.group(1):
            stack.append(m.group(2))
        elif not stack or stack.pop() != m.group(2):
            return False
    return not stack


def find_word(arr: list, dict_form: str) -> list:
    for e in arr:
        if len(e) > 1:
            if e[2] == dict_form:
                return e
            if e[5]:
                found = find_word(e[5], dict_form)
                if found:
                    return found
    return []


@unittest.skipUnless(resources.is_ready(), "needs SudachiPy, a Sudachi dictionary and JMdict")
class WordArrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = load_ops_module("generator", subdir="word_array")
        cls.gold = load_addon_module("gold", subdir="word_array/research")
        cls.examples = cls.gold.load()
        cls.tag_cleaning = importlib.import_module(
            f"{PACKAGE}.shared.jp_text_processing.word.use_tag_cleaning"
        )

    def test_every_gold_sentence_is_partitioned(self):
        for num, (sentence, _) in self.examples.items():
            with self.subTest(example=num):
                self.assertEqual(
                    self.gold.validate(sentence, self.generator.generate(sentence)), []
                )

    def test_b_can_wrap_every_word(self):
        for num, (sentence, _) in self.examples.items():
            arr = self.generator.generate(sentence)
            for i, e in enumerate(arr):
                if len(e) == 1:
                    continue
                html = (
                    self.gold.concat_raw(arr[:i])
                    + "<b>"
                    + e[0]
                    + "</b>"
                    + self.gold.concat_raw(arr[i + 1 :])
                )
                with self.subTest(example=num, word=e[0]):
                    self.assertTrue(balanced(self.tag_cleaning.apply_tag_fixes(html)), html)

    def test_lexicon_names_are_one_proper_noun(self):
        # 山田 is a 普通名詞 to Sudachi, with 山 + 田 as sub-words
        names = load_ops_module("names", subdir="word_array")
        sentence = (
            "図書室[としょしつ]施錠[せじょう]の時間[じかん]まで山田[やまだ]を"
            "足止[あしどめ]為[し]て良[よ]かったぜ。"
        )
        self.assertEqual(find_word(self.generator.generate(sentence), "山田")[1], "noun")
        lexicon = {"山田": names.NameEntry(2, {names.HONORIFIC}, {"やまだ"})}
        yamada = find_word(self.generator.generate(sentence, lexicon), "山田")
        self.assertEqual(yamada[1:4], ["proper noun", "山田", "やまだ"])
        self.assertEqual(yamada[5], [])
        # Built from the gold: 里樹 is anchored by 様, which stays its own word
        sentence = self.examples[6][0]
        lexicon = self.generator.build_name_lexicon([sentence])
        arr = self.generator.generate(sentence, lexicon)
        self.assertEqual(find_word(arr, "里樹")[1], "proper noun")
        self.assertTrue(find_word(arr, "様"))

    def test_per_sentence_proper_noun_rules(self):
        # Katakana nouns joined by ・ with a name-like part are one name; word pairs stay apart
        arr = self.generator.generate("ナツキ・スバルはテレビ・カメラを見[み]た。")
        self.assertEqual(find_word(arr, "ナツキ・スバル")[1], "proper noun")
        self.assertEqual(find_word(arr, "ナツキ・スバル")[5], [])
        self.assertTrue(find_word(arr, "テレビ"))
        self.assertFalse(find_word(arr, "テレビ・カメラ"))
        # Katakana Sudachi and JMdict don't know is a name
        arr = self.generator.generate("フェザーンに向[む]かう。")
        self.assertEqual(find_word(arr, "フェザーン")[1], "proper noun")
        # A proper noun read otherwise by the furigana takes JMdict's label for that reading
        arr = self.generator.generate("亜人[あじん]が現[あらわ]れた。")
        self.assertEqual(find_word(arr, "亜人")[1], "noun")
        arr = self.generator.generate("日本[にほん]に行[い]く。")
        self.assertEqual(find_word(arr, "日本")[1], "proper noun")

    def test_a_word_cut_across_a_name_splits_only_off_a_word_of_its_own(self):
        llm = load_ops_module("proper_noun_llm", subdir="word_array")
        cases = [
            # the rest is a particle, or a JMdict word of a word JMdict doesn't have
            ("凛[りん]との 事[こと]", "凛", ["凛", "と"]),
            ("陰[かげ]キャ 少年京太郎[しょうねんきょうたろう]と", "京太郎", ["少年", "京太郎"]),
            # a JMdict compound, a suffix-like rest, a bigger name: whole
            ("江戸時代[えどじだい]に", "江戸", ["江戸時代"]),
            ("ドイツ 語[ご]を 話[はな]す", "ドイツ", ["ドイツ語"]),
        ]
        for sentence, name, forms in cases:
            with self.subTest(sentence=sentence):
                arr = self.generator.generate(sentence)
                llm.fix_array(arr, [name], self.generator.name_rest_word)
                self.assertTrue(all(find_word(arr, f) for f in forms), arr)
                # a cut between sub-words takes their furigana, so only the plain text is kept
                self.assertEqual(
                    llm.plain_text(self.gold.concat_raw(arr)), llm.plain_text(sentence)
                )
                if len(forms) > 1:
                    self.assertEqual(find_word(arr, name)[1], "proper noun")

    def test_sub_words_get_their_own_share_of_the_furigana(self):
        arr = self.generator.generate(self.examples[2][0])
        compound = find_word(arr, "見下ろす")
        self.assertEqual([s[0] for s in compound[5]], [" 見[み]", "下[お]ろせた"])

    def test_k_words_are_read_as_their_original_kana(self):
        # <k> 遣[や]っ read as kanji would be 遣う (つかう); as the original やっ it is やる
        arr = self.generator.generate(self.examples[13][0])
        self.assertEqual(find_word(arr, "遣る")[3], "やる")
        # 為[さ]れ is される, not なる
        arr = self.generator.generate(self.examples[12][0])
        self.assertEqual(find_word(arr, "為れる")[3], "される")

    def test_readings_come_from_the_notes_furigana(self):
        # Sudachi reads a bare 私 as わたくし; the note says わたし
        arr = self.generator.generate(self.examples[1][0])
        self.assertEqual(find_word(arr, "私")[3], "わたし")

    def test_a_furigana_group_is_never_split_between_words(self):
        # Sudachi cuts 八紘一宇 into two words, 八紘 + 一宇; they can only be its sub-words
        arr = self.generator.generate(self.examples[5][0])
        word = find_word(arr, "八紘一宇")
        self.assertEqual((word[0], word[3]), ("八紘一宇[はっこういちう]", "はっこういちう"))
        self.assertEqual([s[0] for s in word[5]], ["八紘[はっこう]", "一宇[いちう]"])

    def test_multi_word_expressions_come_from_jmdict(self):
        arr = self.generator.generate(self.examples[11][0])
        expression = find_word(arr, "そう言えば")
        self.assertEqual([s[2] for s in expression[5]], ["そう", "言う"])

    def test_a_jmdict_match_inside_another_nests_in_it(self):
        arr = self.generator.generate(self.examples[5][0])
        outer = find_word(arr, "様に成る")
        self.assertEqual([s[2] for s in outer[5] if len(s) > 1], ["様に", "成る"])
        self.assertEqual([s[2] for s in find_word(arr, "様に")[5]], ["様", "に"])

    def test_of_two_crossing_matches_the_kanji_spelled_one_wins(self):
        # 一つ and つの (角) share the つ; only 一つ is a word here
        arr = self.generator.generate(self.examples[8][0])
        self.assertEqual([s[2] for s in find_word(arr, "一つ")[5]], ["一", "つ"])
        self.assertEqual(find_word(arr, "つの"), [])

    def test_kana_homophones_across_a_particle_are_refused(self):
        # は + 幾つ is also JMdict's はいくつ (背屈), and を + 持って its をもって (を以って)
        for num, form in [(21, "はいくつ"), (17, "をもって")]:
            with self.subTest(form=form):
                arr = self.generator.generate(self.examples[num][0])
                self.assertEqual(find_word(arr, form), [])

    def test_kana_homophones_spelled_otherwise_are_refused(self):
        # <k> kana 成ると is JMdict's なると (鳴門), 事に its ことに (殊に); 如何して is spelled
        # alike though the text kanjifies its し
        arr = self.generator.generate("夜[よる]に<k> 成[な]ると</k> 事[こと]に 困[こま]る。")
        self.assertEqual(find_word(arr, "成ると"), [])
        self.assertEqual(find_word(arr, "事に"), [])
        self.assertEqual(find_word(arr, "成る")[1], "verb")
        self.assertTrue(self.generator.spelled_alike("如何為て", "如何して"))
        self.assertTrue(self.generator.spelled_alike("為易い", "し易い"))
        self.assertFalse(self.generator.spelled_alike("成ると", "鳴門"))
        self.assertFalse(self.generator.spelled_alike("事に", "殊に"))

    def test_homographs_the_furigana_reads_otherwise_are_refused(self):
        # JMdict's 彼の is あの and its 今日は こんにちは
        arr = self.generator.generate("彼[かれ]の 車[くるま]は 今日[きょう]は 新[あたら]しい。")
        self.assertEqual(find_word(arr, "彼の"), [])
        self.assertEqual(find_word(arr, "今日は"), [])
        # JMdict reads 慈悲深い じひぶかい; furigana split between words has no group to voice
        arr = self.generator.generate("彼女[かのじょ]は 慈悲[じひ] 深[ふか]い 人[ひと]だ。")
        self.assertEqual(find_word(arr, "慈悲深い")[3], "じひふかい")

    def test_an_expression_starting_on_the_copula_keeps_its_whole_reading(self):
        arr = self.generator.generate(
            "彼[かれ]は 作家[さっか]で<k> 有[あ]り</k> 学者[がくしゃ]です。"
        )
        self.assertEqual(find_word(arr, "で有る")[3], "である")

    def test_a_kanjified_expression_matches_through_its_kana(self):
        # JMdict writes だけの事はある; the note kanjified ある to 有る
        arr = self.generator.generate("だけの 事[こと]は<k> 有[あ]って</k>")
        self.assertEqual(find_word(arr, "だけの事は有る")[3], "だけのことはある")

    def test_long_units_are_parents_of_their_short_units(self):
        arr = self.generator.generate(self.examples[3][0])
        self.assertEqual([s[2] for s in find_word(arr, "飛行機")[5]], ["飛行", "機"])
        arr = self.generator.generate(self.examples[14][0])
        self.assertEqual([s[2] for s in find_word(arr, "私達")[5] if len(s) > 1], ["私", "達"])

    def test_words_decompose_into_jmdict_words_read_as_the_furigana_says(self):
        arr = self.generator.generate(self.examples[23][0])
        self.assertEqual([s[0] for s in find_word(arr, "耳元")[5]], [" 耳[みみ]", "元[もと]"])
        # 最 and 近 are JMdict words too, but lone on'yomi kanji don't make sub-words
        arr = self.generator.generate(self.examples[10][0])
        self.assertEqual(find_word(arr, "最近")[5], [])

    def test_okurigana_is_no_sub_word(self):
        # JMdict has らか, か and く as words, but they are the okurigana here
        for sentence, form in [
            ("<b> 柔[やわ]らか</b>な 布[ぬの]", "柔らか"),
            ("静[しず]かな 夜[よる]", "静か"),
            ("悉[ことごと]く 失敗[しっぱい]した", "悉く"),
        ]:
            with self.subTest(sentence=sentence):
                self.assertEqual(find_word(self.generator.generate(sentence), form)[5], [])
        # An adverb's particle still is a word
        arr = self.generator.generate("正[まさ]に その 通[とお]り")
        self.assertEqual([s[2] for s in find_word(arr, "正に")[5]], ["正", "に"])

    def test_noun_okurigana_is_no_sub_word(self):
        # a noun's lone kana, or the rest of a verb's ます-stem, is okurigana
        for sentence, form in [
            ("道[みち]の 窪[くぼ]みに 水[みず]が 溜[た]まる", "窪み"),
            ("夕[ゆう]べ 雨[あめ]が 降[ふ]った", "夕べ"),
            ("独特[どくとく]の 味[あじ]わいが 有[あ]る", "味わい"),
        ]:
            with self.subTest(sentence=sentence):
                self.assertEqual(find_word(self.generator.generate(sentence), form)[5], [])
        # も and a word of its own after a noun still split
        for sentence, form, subs in [
            ("何時[いつ]も 忙[いそが]しい", "何時も", ["何時", "も"]),
            ("赤[あか]ちゃんが 泣[な]く", "赤ちゃん", ["赤", "ちゃん"]),
        ]:
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), form)
                self.assertEqual([s[2] for s in word[5]], subs)

    def test_modal_auxiliary_is_a_word_of_its_own(self):
        # らしい, べき and まい are not the verb's inflection chain: 捌いとる + らしい, not 捌いとるらしい
        arr = self.generator.generate("プラチナを 売[う]り<k> 捌[さば]いとる</k>らしい")
        self.assertEqual(find_word(arr, "捌く")[0], " 捌[さば]いとる")
        for sentence, raw, form in [
            ("プラチナを 売[う]り<k> 捌[さば]いとる</k>らしい", "らしい", "らしい"),
            ("早[はや]く 帰[かえ]るべきだ", "べき", "べし"),
            ("二度[にど]と 行[い]くまい", "まい", "まい"),
        ]:
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), form)
                self.assertEqual((word[0], word[1]), (raw, "auxiliary"))

    def test_conjunction_and_pronoun_okurigana_is_no_sub_word(self):
        for sentence, form in [
            ("但[ただ]し、 注意[ちゅうい]", "但し"),
            ("県[けん] 並[なら]びに 市[し]", "並びに"),
            ("<k> 其[そ]こ</k>に 含[ふく]まれる 冗談[じょうだん]", "其こ"),
            # Sudachi's 感動詞 here, which JMdict would split as 済み + ません
            ("<k> 済[す]みません</k> 実[じつ]は", "済みません"),
        ]:
            with self.subTest(sentence=sentence):
                self.assertEqual(find_word(self.generator.generate(sentence), form)[5], [])
        arr = self.generator.generate("私[わたし]たちは 話[はな]した")
        self.assertEqual([s[2] for s in find_word(arr, "私たち")[5]], ["私", "達"])

    def test_a_conjunction_opening_a_clause_is_one_word(self):
        # function words only (つー + か, で + も), which elsewhere are no word of their own
        for sentence, raw in [
            ("つーかもう１５ 時[じ]じゃん、<b> 撤収[てっしゅう]</b>作業[さぎょう]", "つーか"),
            ("「 分[わ]かった。でも 無理[むり]だ」", "でも"),
        ]:
            with self.subTest(sentence=sentence):
                word = next(e for e in self.generator.generate(sentence) if e[0] == raw)
                self.assertEqual(word[1:3], ["conjunction", raw])
        # after a closing bracket, or inside a clause, they stay apart
        for sentence in ["｢ 前略[ぜんりゃく]｣ですが", "誰[だれ]でも 来[く]る"]:
            with self.subTest(sentence=sentence):
                arr = self.generator.generate(sentence)
                self.assertFalse([e for e in arr if e[0] in ("ですが", "でも")])

    def test_sou_naru_is_the_verb_naru(self):
        # After すりゃ Sudachi has そう as the stem of そうだ and なる as the classical copula なり
        sentence = "言[い]い 方[かた]<k> 為[す]りゃ 然[そ]う 成[な]る</k>よ"
        arr = self.generator.generate(sentence)
        self.assertEqual(find_word(arr, "成る")[1:4], ["verb", "成る", "なる"])
        self.assertEqual(find_word(arr, "然う")[1], "adverb")
        # The classical copula after a na-adjective stays
        self.assertEqual(find_word(self.generator.generate("切[せつ]なる 願[ねが]い"), "成る"), [])

    def test_negation_nai_is_part_of_the_word_before_it(self):
        # Sudachi calls these ない the adjective 無い rather than the auxiliary
        for sentence, raw, pos, form in [
            (" 風上[かざかみ]にも 置[お]け 無[な]い", " 置[お]け 無[な]い", "verb", "置く"),
            (" 知[し]ら 無[な]い 人[ひと]", " 知[し]ら 無[な]い", "verb", "知る"),
            ("住所[じゅうしょ]を 教[おし]えたくない。", " 教[おし]えたくない", "verb", "教える"),
            ("少[すこ]しも 悪[わる]くない。", " 悪[わる]くない", "adjective", "悪い"),
            ("酒[さけ]を 飲[の]んでなかったら", " 飲[の]んでなかったら", "verb", "飲む"),
        ]:
            with self.subTest(sentence=sentence):
                arr = self.generator.generate(sentence)
                self.assertEqual(find_word(arr, form)[:3], [raw, pos, form])
                self.assertEqual(find_word(arr, "無い"), [])
        # With a particle between, ない is a word of its own
        for sentence in ["欲[ほ]しくもない 物[もの]", "時間[じかん]がない"]:
            with self.subTest(sentence=sentence):
                self.assertEqual(
                    find_word(self.generator.generate(sentence), "無い")[1], "adjective"
                )

    def test_classical_taru_naru_and_tari(self):
        # The classical copula's attributive is the word as written, not たり or なり
        arr = self.generator.generate("確固[かっこ]たる<k> 物[もの]</k>")
        self.assertEqual(find_word(arr, "たる")[1:4], ["auxiliary", "たる", "たる"])
        self.assertEqual(find_word(arr, "たり"), [])
        # 如く after it is a word of its own, not part of 成る
        arr = self.generator.generate(
            "必要物[ひつようぶつ]<k> 成[な]る</k> 如[ごと]く 親子[おやこ]や"
        )
        self.assertEqual(find_word(arr, "成る")[0], " 成[な]る")
        self.assertTrue(find_word(arr, "如し"))
        self.assertEqual(find_word(arr, "成り"), [])
        # たり after a 連用形 is part of the word; たりとも is one word
        arr = self.generator.generate("肉[にく]を 焼[や]いたり 口[くち]を 挟[はさ]んだり")
        self.assertEqual(find_word(arr, "焼く")[0], " 焼[や]いたり")
        self.assertEqual(find_word(arr, "挟む")[0], " 挟[はさ]んだり")
        self.assertEqual(find_word(arr, "たり") + find_word(arr, "だり"), [])
        arr = self.generator.generate("一本[いっぽん]たりとも 渡[わた]さない")
        top = [e[2] for e in arr if len(e) > 1]
        self.assertNotIn("たり", top)
        self.assertTrue({"足りとも", "たりとも"} & set(top), top)

    def test_colloquial_function_words_take_the_standard_base_word(self):
        for sentence, raw, word in [
            (
                "<k> 此処等[ここいら]</k>が<b> 潮時[しおどき]</b>じゃろう。",
                "じゃろう",
                ["expression", "だろう", "だろう"],
            ),
            ("恥[はじ]を 知[し]るのじゃ。", "じゃ", ["copula", "だ", "だ"]),
            ("大事[だいじ]やで。", "や", ["copula", "だ", "だ"]),
            ("調査[ちょうさ]はどうじゃった｡", "じゃった", ["copula", "だ", "だ"]),
            (
                "言[い]い 方[かた]<k> 為[し]</k>てんじゃねえ。",
                "じゃねえ",
                ["expression", "じゃ無い", "じゃない"],
            ),
            (
                "ユニットっちゅうもんが 必要[ひつよう]",
                "っちゅう",
                ["expression", "という", "という"],
            ),
        ]:
            with self.subTest(sentence=sentence):
                arr = self.generator.generate(sentence)
                found = find_word(arr, word[1])
                self.assertEqual(found[:4], [raw] + word, arr)
        # という after a verb is a word of its own
        arr = self.generator.generate("なに<k> 為[し]た</k>っちゅうんや？")
        self.assertEqual(find_word(arr, "為る")[0], " 為[し]た")
        self.assertTrue(find_word(arr, "という"), arr)
        # the verb やろう is not だろう
        arr = self.generator.generate("一緒[いっしょ]にやろう。")
        self.assertEqual(find_word(arr, "だろう"), [])

    def test_katakana_furigana_before_okurigana_is_read_in_the_script_that_reads_the_word(self):
        arr = self.generator.generate(
            "「<k> 褒[ホ]める</k>と<k> 直[す]ぐ</k> 思[おも]い 上[あ]がる」"
        )
        self.assertEqual(
            find_word(arr, "褒める")[:5], [" 褒[ホ]める", "verb", "褒める", "ほめる", []]
        )
        self.assertEqual(find_word(arr, "褒める")[5], [], arr)
        arr = self.generator.generate("<k> 此[こ]の</k><k> 儘[まま]</k><k> 茶化[チャカ]して</k>")
        self.assertEqual(find_word(arr, "茶化す")[:2], [" 茶化[チャカ]して", "verb"], arr)
        # a tokenizer that reads the katakana as a word keeps it: フラれた is 振る, ふられた ふる
        arr = self.generator.generate("恋人[こいびと]に<k> 振[フラ]れた</k>")
        self.assertEqual(find_word(arr, "振る")[:2], [" 振[フラ]れた", "verb"], arr)
        # a whole katakana reading keeps its word
        arr = self.generator.generate("<k> 本当[ホント]</k>に")
        self.assertEqual(find_word(arr, "本当")[:4], [" 本当[ホント]", "noun", "本当", "ほんと"])

    def test_k_reading_the_tokenizer_cuts_before_its_okurigana_goes_in_as_kanji(self):
        for sentence, raw, form in [
            # あれる is あ + れる, ほどけば after a counter ほど + けば, できがねます でき + が + ね
            ("だが、<k> 良[よ]く</k><k> 荒[あ]れる</k>。", " 荒[あ]れる", "荒れる"),
            ("１ 本[ほん]<k> 解[ほど]けば</k> 早[はや]そうだな。", " 解[ほど]けば", "解く"),
            (
                "御[お] 任[まか]せ<k> 出来[でき]兼[が]ねます</k>",
                " 出来[でき]兼[が]ねます",
                "出来兼ねる",
            ),
        ]:
            with self.subTest(sentence=sentence):
                arr = self.generator.generate(sentence)
                word = find_word(arr, form)
                self.assertEqual(word[:2], [raw, "verb"], arr)
                self.assertNotIn("auxiliary", [s[1] for s in word[5] if len(s) > 1], arr)
                self.assertNotIn("particle", [s[1] for s in word[5] if len(s) > 1], arr)

    def test_okurigana_sudachi_cuts_off_is_part_of_the_word(self):
        for sentence, raw, form in [
            # a suffix that inflects as a verb or adjective takes its inflection
            ("寝[ね]<k> 難[にく]かった</k>。", " 難[にく]かった", "難い"),
            ("変人[へんじん]<k> 振[ぶ]ってる</k> 感[かん]", " 振[ぶ]ってる", "振る"),
            # a verb stem Sudachi calls a noun, before an auxiliary only a verb takes
            (
                "伝言[でんごん]を<b> 言[こと]<k> 付[づ]けた</k></b>の。",
                " 言[こと]<k> 付[づ]けた</k>",
                "言付ける",
            ),
            # a verb Sudachi doesn't know, cut before a classical る
            ("酒[さけ]を<b>侑める</b>。", "侑める", "侑める"),
            ("生[う]まれで<k> 御[お]座[じゃ]る</k>。", " 御[お]座[じゃ]る", "御座る"),
        ]:
            with self.subTest(sentence=sentence):
                arr = self.generator.generate(sentence)
                self.assertEqual(find_word(arr, form)[0], raw)
                self.assertFalse(any(e[1] == "auxiliary" for e in arr if len(e) > 1), arr)
        # A noun before the copula stays a noun
        arr = self.generator.generate("明日[あした]は 晴[は]れです。")
        self.assertEqual(find_word(arr, "晴れ")[1], "noun")

    def test_a_noun_verb_sudachi_lacks_is_one_verb(self):
        # Sudachi has 裏目 + っ (記号) + た; JMdict has 裏目る
        arr = self.generator.generate("完全[かんぜん]に<b> 裏目[うらめ]った</b>なあ。")
        self.assertEqual(
            [(e[0], e[1], e[2]) for e in arr if len(e) > 1][2:],
            [(" 裏目[うらめ]った", "verb", "裏目る"), ("なあ", "particle", "なあ")],
        )
        self.assertEqual(find_word(arr, "っ"), [])

    def test_an_adjective_stem_and_ge_are_one_na_adjective(self):
        # JMdict has 寂しげ but not 儚げ; both come out alike, and 忌々しげに isn't 忌々し + げに
        for sentence, form in [
            ("<b> 儚[はかな]げな</b> 笑顔[えがお]", "儚げ"),
            ("寂[さび]しげな 笑顔[えがお]", "寂しげ"),
            ("忌々[いまいま]しげに 言[い]う", "忌々しげ"),
        ]:
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), form)
                self.assertEqual(word[1], "na-adjective")
                self.assertEqual([s[1] for s in word[5]], ["adjective", "suffix"])

    def test_sub_word_readings_drop_the_compounds_rendaku(self):
        arr = self.generator.generate(self.examples[20][0])
        self.assertEqual([s[3] for s in find_word(arr, "大空")[5]], ["おお", "そら"])

    def test_a_jukujikun_group_splits_between_words_by_their_own_readings(self):
        # かわせそうば has no per-kanji split, but 為替 and 相場 read it between them, so each
        # sub-word keeps furigana of its own: never " 為替" + "相場[かわせそうば]"
        arr = self.generator.generate("<div> 為替相場[かわせそうば]が 気[き]になる</div>")
        word = find_word(arr, "為替相場")
        self.assertEqual([s[0] for s in word[5]], [" 為替[かわせ]", "相場[そうば]"])
        self.assertEqual([s[3] for s in word[5]], ["かわせ", "そうば"])

    def test_a_character_sudachi_normalizes_into_several_still_partitions(self):
        # … normalizes to ... and comes back as the one morpheme covering it plus empty ones,
        # the last of which starts past the end of the text
        for sentence in ["<div> 嫌[いや]…だ…</div>", "<div> 嫌[いや]だ…</div>"]:
            with self.subTest(sentence=sentence):
                arr = self.generator.generate(sentence)
                self.assertEqual(self.gold.concat_raw(arr), sentence)

    def test_adjective_forms_take_the_adjectives_dictionary_form(self):
        # JMdict lists 大きく as an adverb too; only adverbs of their own keep the く-form
        cases = [
            ("口[くち]を 大[おお]きく 開[あ]けて", " 大[おお]きく", "大きい", "おおきい"),
            ("彼女[かのじょ]は<k> 良[よ]く</k> 喋[しゃべ]る", " 良[よ]く", "良い", "よい"),
            ("大[おお]きな 音[おと]", "大[おお]きな", "大きい", "おおきい"),
            ("毎年[まいとし] 多[おお]くの 人[ひと]が 来[く]る", " 多[おお]く", "多い", "おおい"),
            ("もっと 近[ちか]くに 来[き]て", " 近[ちか]く", "近い", "ちかい"),
        ]
        for sentence, raw, form, reading in cases:
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), form)
                self.assertEqual(word, [raw, "adjective", form, reading, [], []])
        for sentence, form in [
            ("危[あや]うく 死[し]ぬ", "危うく"),
            ("全[まった]く 違[ちが]う", "全く"),
        ]:
            with self.subTest(sentence=sentence):
                self.assertEqual(find_word(self.generator.generate(sentence), form)[1], "adverb")

    def test_a_word_partly_in_k_is_spelled_as_jmdict_spells_it(self):
        # The 為 and the second 当 were kana before kanjify_sentence; JMdict has 私達 as it is
        cases = [
            ("彼[かれ]に 対[たい]<k> 為[し]て</k> 怒[おこ]る", "に対して", "にたいして"),
            ("日当[ひあ]<k> 当[た]り</k>が 良[い]い", "日当たり", "ひあたり"),
            ("私[わたし]<k> 達[たち]</k>が 行[い]く", "私達", "わたしたち"),
        ]
        for sentence, form, reading in cases:
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), form)
                self.assertIsNotNone(word)
                self.assertEqual(word[3], reading)

    def test_an_inflected_compound_keeps_its_rendaku(self):
        for sentence in ("義務[ぎむ]<k> 付[づ]けられる</k>", "義務[ぎむ] 付[づ]けられた"):
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), "義務付ける")
                self.assertEqual(word[3], "ぎむづける")

    def test_only_a_number_makes_the_noun_after_it_a_counter(self):
        self.assertEqual(
            find_word(self.generator.generate("3 年[ねん] 経[た]つ"), "年")[1], "counter"
        )
        # 一日中 starts on a number but is not one
        arr = self.generator.generate("一日中 家[うち]に 居[い]た")
        self.assertNotEqual(find_word(arr, "家")[1], "counter")

    def test_a_suffix_jmdict_has_only_as_a_word_takes_its_label(self):
        # Sudachi has 家 after 一日中 as a suffix, but JMdict's 家[うち] is no suffix
        arr = self.generator.generate("一日中 家[うち]に 居[い]た")
        self.assertEqual(find_word(arr, "家")[1], "noun")
        arr = self.generator.generate(" 少年[しょうねん]<k> 達[たち]</k>が 居[い]る")
        self.assertEqual(find_word(arr, "達")[1], "suffix")
        # inside a compound it stays a suffix, though JMdict's 官[かん] is a noun
        arr = self.generator.generate(" 警察官[けいさつかん]が 来[き]た")
        self.assertEqual(find_word(arr, "官")[1], "suffix")

    def test_a_verb_stem_listed_as_its_verb_is_a_verb(self):
        # Sudachi has 買い of 買い物 and 継ぎ of 跡継ぎ as nouns
        cases = [
            ("買[か]い 物[もの]に 行[い]く", "買う"),
            ("跡継[あとつ]ぎが 居[い]る", "継ぐ"),
        ]
        for sentence, form in cases:
            with self.subTest(sentence=sentence):
                self.assertEqual(find_word(self.generator.generate(sentence), form)[1], "verb")

    def test_a_verb_stem_of_its_own_stays_the_noun_jmdict_has(self):
        # 動き and 嫌い alone are lexicalized nouns, not listed as 動く and 嫌う
        for sentence, form in (
            ("猫[ねこ]の 動[うご]き", "動き"),
            ("犬[いぬ]が 嫌[きら]いだ", "嫌い"),
        ):
            with self.subTest(sentence=sentence):
                word = find_word(self.generator.generate(sentence), form)
                self.assertIsNotNone(word)
                self.assertNotEqual(word[1], "verb")

    def test_a_suffix_jmdict_has_as_one_keeps_its_form(self):
        # 振り read ぶり is a JMdict suffix of its own, not a form of 振る
        word = find_word(self.generator.generate("十 年[ねん]<k> 振[ぶ]り</k>に 会[あ]う"), "振り")
        self.assertEqual((word[1], word[3]), ("suffix", "ぶり"))

    def test_a_reading_put_on_the_last_kanji_is_not_all_its_own(self):
        # The note reads 空域 on 域 alone, and ネット上 on its whole group
        cases = [
            ("<b> 領[りょう] 空</b>域[くういき]です。", "域", "いき"),
            ("ネット上[ねっとじょう]では", "上", "じょう"),
        ]
        for sentence, form, reading in cases:
            with self.subTest(sentence=sentence):
                self.assertEqual(find_word(self.generator.generate(sentence), form)[3], reading)

    def test_sudachis_readings_of_unread_sub_words_must_add_up(self):
        # Sudachi splits 無人島 as 無人[むじん] + 島[むじんとう]: 島 takes JMdict's とう instead
        word = find_word(self.generator.generate("無人島に 行[い]く"), "無人島")
        self.assertEqual([(s[2], s[3]) for s in word[5]], [("無人", "むじん"), ("島", "とう")])
        # 一 + 日中[にっちゅう] can't read いちにちじゅう, so 一日中 keeps no sub-words
        word = find_word(self.generator.generate("一日中 家[うち]に 居[い]た"), "一日中")
        self.assertEqual((word[3], word[5]), ("いちにちじゅう", []))

    def test_a_group_whose_reading_cannot_be_shared_out_is_not_split(self):
        # Sudachi has 土産物 as 土産 + 物, but nothing reads へんなよみ that way
        arr = self.generator.generate("<div> 土産物[へんなよみ]を 買[か]う</div>")
        word = find_word(arr, "土産物")
        self.assertEqual((word[0], word[5]), (" 土産物[へんなよみ]", []))


if __name__ == "__main__":
    unittest.main()
