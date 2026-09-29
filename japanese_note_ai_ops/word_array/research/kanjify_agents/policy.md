# Kanjification policy

Policy version: 0.7

The one written policy for kanjifying a furigana sentence: which kana words are rewritten in
kanji, with which kanji, and how the result is written. Every kanjify agent reads it (the golden
set's word and sentence agents, the label-fix agents); it is the only place it is written. Each
rule has an id (`KANJI-3`, `SPLIT-よる`) that an agent cites for the choice it made. Examples
here are short generic phrases; examples from real sentences belong in the word decisions.

Rules marked **pending Qn** wait for the user's answer. A use that turns on one is not decided:
the agent names the question instead (see "Pending questions" at the end).

## BASIC: the test

Kanjify a word when it carries a meaning of its own; leave it in kana when it only does grammar.
Whether the word conjugates is not the test. When nothing below settles a use, this test does,
together with SOURCE and SPELL.

## SOURCE: does the word have a kanji spelling at all

- SOURCE-1 A kana word is kanjified only with a spelling a dictionary gives for that word, in
  that reading and part of speech. Enough: JMdict (jisho.org), where rarely used forms and ateji
  count (此れ, 一寸 for ちょっと) but outdated and search-only forms don't, and so does a spelling
  a sense note gives ("occ. written as 嗚呼" under ああ); the headword of a Japanese dictionary
  (大辞林, 大辞泉, 広辞苑, 明鏡, 新明解: the local MDX files, or weblio / kotobank pages of
  them); wiktionary (en or ja), even alone. Not enough: ateji quiz and trivia sites, a kanji
  borrowed from a synonym that is read differently (徐々 read そろそろ, 確 read ちゃん, 沢山 read
  たっぷり), or a kanji that fits the meaning but no source gives. Such made-up gikun are
  un-kanjified.
- SOURCE-2 Lengthened and sound-spelled forms stay kana even when the plain form is kanjified:
  ああ is 嗚呼, but あー, あぁ, あーあ stay; まー, おーい, うーん too.

## SPELL: which spelling

- SPELL-1 Homophones (different dictionary words with the same kana: 付く / 就く / 着く / 点く /
  突く / 吐く, 取る / 撮る / 採る, 寄る / 依る, 内 / 家 for うち, 置く / 於く for おく): the word
  this sentence means, each with its own spelling.
- SPELL-2 One dictionary word with several spellings: its main spelling (JMdict's first kanji
  form that is not outdated or search-only) for every sense, unless SPLIT lists the word. So
  よい / いい / よく are 良 in every sense (not 善, 能, 好), ちょっと is 一寸 (not 鳥渡).
- SPELL-3 A word already spelled in kanji in the source is never respelled, even with a kanji
  this policy would not choose.

## SPLIT: the words whose kanji follow the sense

Only these single dictionary words change kanji by sense. An agent that thinks another word
needs a split asks (a policy question); it never splits on its own.

- SPLIT-ある 在る when something is located somewhere (机の上に在る, 向こうに在る); 有る for
  possession, events and abstract existence (自信が有る, 祭りが有る, 事が有る). The copula である
  stays kana (KANA-1).
- SPLIT-おさえる 押さえる for pressing or holding down, securing (手で押さえる, 要点を押さえる);
  抑える for restraining or suppressing (怒りを抑える, 物価を抑える).
- SPLIT-よる 因る for a cause or reason (事故に因る故障, 過労に因る); 由る for a means or method,
  the tool or procedure something is done with (石に由る撲殺, 手作業に由る, 投票に由って決める);
  依る for "depending on", "according to", "based on" (人に依る, 予報に依ると, 法律に依る). Pending
  Q2: the doer of an action, the passive agent or による before an action noun (彼に依って
  書かれた, 市民に依る運動), draft 依る. 拠る is not used: its own sense, taking something as
  grounds or a source (法律に拠る, 資料に拠る), goes with 依る, since in real sentences it can't
  be told apart from "depending on" consistently.
- SPLIT-もの 物 for a thing, 者 for a person, also as もん (物[もん], 馬鹿者[バカもん]).
- SPLIT-はじめ 初め for the first time or the beginning of a period (初めて, 年の初め); 始め for
  starting something (仕事を始める, 始めに述べた).

## KANA: leave in kana (un-kanjify if a label kanjified it)

- KANA-1 The copula: だ, です, and である in all forms (であった, であり, であって, であれば,
  であろう). It is the copula, not で + 有る. The classical copula なり (〜なり, 〜なるべし) too, not 也.
- KANA-2 ない as the negative auxiliary: 食べない, ではない / じゃない / ではなかった after nouns
  and na-adjectives, くない / くなかった after i-adjectives. Not 無い in kana, but grammar.
- KANA-3 て-form helper verbs in all their forms: ている, てある, てみる, てくる, ていく, てくれる,
  てしまう, ておく, てもらう, ていただく, てください, てあげる, てやる, ておる, ていらっしゃる,
  てまいる, and their contractions (てる, とく, ちゃう, ちまう, とる = ておる). Alone the same
  verbs keep their meaning and are kanjified (見る, 来る, 置く, 貰う, 呉れる, 遣る, 仕舞う, 頂く,
  下さい): 見てみる, not 見て見る.
- KANA-4 て-form patterns: てほしい, てもいい / てもよい, てはいけない, てはならない, てもかまわない,
  てはだめ. Alone the words are kanjified (欲しい, 良い, 行けない, 構わない, 駄目).
- KANA-5 として meaning "as, in the role of" (教師として): it holds no する. An adverb or noun +
  と + する describing a state is 為る (KANJI-3).
- KANA-6 あげる meaning "to give": kept in kana to set it apart from 上げる / 揚げる / 挙げる. An
  exception to BASIC.
- KANA-7 そんな, こんな, あんな, どんな (and そんなに etc.).
- KANA-8 なんか as a filler or particle that could be dropped (今日なんか暑い); もう as an
  exclamation (もう！, もう、やめてよ); もっと.
- KANA-9 Auxiliaries: れる / られる, せる / させる, ます, た, たい, らしい, ようだ's だ, ぬ, まい, and
  the そう of appearance and hearsay (降りそう, 来るそうだ): they inflect or join and add no meaning a
  kanji could carry. A kanjified one is a slip (れる written as 様). The auxiliaries that have a
  dictionary spelling and a meaning of their own are kanjified instead (KANJI-17).
- KANA-10 Particles (は, が, を, に, で, と, も, の, へ, や, か, ね, よ, しか, さえ, こそ...).
- KANA-11 Loanwords (gairaigo) stay katakana unless KANJI-16 kanjifies them.
- KANA-12 Names of people, places, works and brands, however they are written.
- KANA-13 そう is always kana, the demonstrative adverb too (そう言う, そうする, そうですね): 然う
  only spells the demonstrative, and telling it from the そう of appearance or hearsay is a
  call not worth making in every sentence. 言う and 為る after it are still kanjified:
  そう<k> 言[い]う</k>. Pending Q8: こう and ああ as adverbs (こう言う, ああする) likewise, draft kana.

## KANJI: kanjify

- KANJI-1 無い as a word of its own, in every form (なかった, なくて, なく): after a noun and は, が,
  も or しか (事は無かった, 一膳しか無い, 必要無い, 見た事無い), and in set phrases, also after
  kanji (絶え間無く). Only では / じゃ + ない after a noun stays kana (KANA-2).
- KANJI-2 居る and 行く as verbs of their own (家に居る, あっちに行く), おる too (居る, humble);
  ある as a verb of its own, 有る or 在る by SPLIT-ある.
- KANJI-3 する is 為る everywhere, suru-verbs included: in every form and whatever follows
  (勉強為る, 為た, 為よう, 為て, 為ない, 為ず, 為ぬ, passive and causative 為れる / 為せる,
  為なさい), before a て-helper or ください (約束為てください), after a noun already in kanji (関為て,
  支給為れる), after お / ご + noun (御願い為る), after kana words and onomatopoeia (にこにこ為る),
  after a volitional form ("try to": 立ち上がろうと為た) and after an adverb or noun + と
  (平然と為て). どうして is 如何為て; どうしよう is 如何 + 為よう. But どうしようもない is 如何 +
  仕様 + も + 無い: 仕様 is the noun "way, means" (仕様が無い).
- KANJI-4 なる is 成る in every use: ようになる, ことになる, そうになる, くなる, となる,
  なければならない / なくてはならない (探らなければ成らない), いかなくなる (行かなく成る). Only the
  て-pattern てはならない stays kana. But 実が生る (fruit grows) is 生る, a different word.
- KANJI-5 The formal nouns in every use, grammatical ones included: 事 (こと), 物 (もの, もん:
  SPLIT-もの), 為 (ため), 様 (よう, also ような, ように, ようだ), 所 (ところ), and every other
  formal noun with a dictionary kanji: 訳 (わけ), 筈 (はず), 積もり (つもり), 内 (うち), 儘 (まま),
  癖 (くせ), 方 (ほう), 通り (とおり), 所為 (せい), 御蔭 (おかげ), 程 (ほど), 許り (ばかり). So are
  the conjunctions and sentence endings made from them: 所が, 所で, 物の, 事に, 事だ, and a
  sentence-final もの / もん (嫌なんだ物[もん]).
- KANJI-6 The demonstratives: 此の / 其の / 彼の / 何の, 此れ / 其れ / 彼れ / 何れ, 此処 / 其処 /
  彼処 / 何処, 此方 / 其方 / 彼方 / 何方, the adverb 如何 (どう, also in どうも, どうして,
  どうにも), and いう after these and after そう / こう / ああ as 言う (如何言う, そう言う).
  そう, こう, ああ themselves stay kana (KANA-13). どうぞ is not どう + ぞ.
- KANJI-7 Particle-like words and set phrases with a kanji spelling: まで 迄, だけ 丈, くらい /
  ぐらい 位, ばかり 許り, ほど 程, ながら 乍ら, まま 儘, など 等, ら 等 (plural: 彼等, 其奴等),
  たち 達, とても 迚も, について に就いて, という と言う, みたい 見たい, いつ 何時, まるで 丸で.
  For example 此れ丈は, 何れ位, 気違い見たいに.
- KANJI-8 The honorific prefix お / ご as 御 (御茶, 御願い, 御前).
- KANJI-9 いい / よい / よく as 良 (SPELL-2), except いい加減 as 好い加減 (a word of its own).
- KANJI-10 ただ and たった meaning "only, just" as 只 (只今, 只一回).
- KANJI-11 Suffixes after a verb stem: やすい 易い, にくい / がたい 難い (付け難い), づらい 辛い,
  すぎる 過ぎる.
- KANJI-12 The kana part of a word partly written in kanji, when that part has a kanji spelling:
  引っかける -> 引っ掛ける, 近づく -> 近付く.
- KANJI-13 Also: なに / なん 何 (なんか / なんて as 何か when it means "something": なんか食べたい),
  いや 否, ああ 嗚呼 (the exclamation), また 又, 遣る (やる as a verb of its own), 君, 時, 方, 序で,
  御蔭 (おかげ), 済む, 彼奴 / 此奴 / 其奴, 出来る, 振り, 小父 / 叔父 / 伯父 by meaning, 貴方 (あなた).
- KANJI-14 Native Japanese and Sino-Japanese words written in katakana are kanjified with the
  katakana kept in the furigana: 林檎[リンゴ], 塵[ゴミ], 奴[ヤツ], 駄目[ダメ], 馬鹿[バカ],
  不味[マズ]い.
- KANJI-15 Pending Q7: honorific verbs after a verb stem (寝なさい, お待ちください, お帰りになる,
  お持ちする, ご覧いただく). お〜になる and お〜する already follow KANJI-4 and KANJI-3.
- KANJI-16 A loanword (gairaigo) is kanjified, the katakana kept in the reading, when JMdict
  gives it a kanji spelling not marked rarely used or search-only, or a Japanese dictionary
  has one as its headword: 珈琲[コーヒー], 煙草[タバコ], 麦酒[ビール], 頁[ページ],
  倶楽部[クラブ]. A spelling JMdict marks rare stays out (米 for メートル, 瓦斯, 硝子, 洋袴 for
  ズボン, 釦 for ボタン): pending Q4, draft. Names of countries, places and people stay as written
  (KANA-12), whatever kanji they once had (亜米利加, 仏蘭西). The same loanword is kanjified in
  every sentence or in none: extract_words records a kanjified one under its kanji and a
  katakana one under its katakana, so a mix splits one word over two notes.
- KANJI-17 An auxiliary with a dictionary spelling and a meaning of its own: べし 可し (in every
  form: 可き, 可く, 可からず), ごとし 如し (如き, 如く), よう 様 (KANJI-5), みたい 見たい
  (KANJI-7).

## FMT: how the result is written

- FMT-1 A kanjified word is wrapped in `<k>` tags with a space before its kanji and its reading
  in brackets: `これ` -> `<k> 此[こ]れ</k>`. The reading is exactly the kana the kanji replaces,
  okurigana and inflection follow in kana inside the tags: `まわってた` -> `<k> 回[まわ]ってた</k>`,
  `しかないの` -> `しか<k> 無[な]い</k>の`.
- FMT-2 Two kanji in one word each get their own group: `つきあい` -> `<k> 付[つ]き 合[あ]い</k>`.
  A word whose kanji spelling is one unit gets one group: `とうもろこし` -> `<k> 玉蜀黍[とうもろこし]</k>`.
- FMT-3 A `<k>` span covers a run of kanjified words and stops at a word already in kanji:
  `タンパク 質[しつ]` -> `<k> 蛋白[たんぱく]</k> 質[しつ]`. Kanjified words next to each other
  share one span, a space between their groups: `<k> 其[そ]れ 程[ほど]</k>`.
- FMT-4 A kana helper after a kanjified verb goes inside that verb's span as okurigana:
  `してみます` -> `<k> 為[し]てみます</k>`. After a verb already in kanji the helper gets no tag:
  `行[い]ってみましょう` stays.
- FMT-5 Katakana is kept in the reading: `バカ` -> `<k> 馬鹿[バカ]</k>`; colloquial shortenings
  are kept: `もん` -> `<k> 物[もん]</k>`, `バカもん` -> `<k> 馬鹿者[バカもん]</k>`.
- FMT-6 Existing HTML tags stay where they are and outermost: `<b>これ見よがしに</b>` ->
  `<b><k> 此[こ]れ</k> 見[み]よがしに</b>`. Text inside `<i>` (a context sentence) is kanjified
  like the rest.
- FMT-7 Everything outside the new spans is unchanged: the words, their furigana, spaces,
  punctuation and tags. Turning every span back into the kana it reads must give the input.
- FMT-8 Numbers: furigana may be added to a number without `<k>` tags, and nothing else about it
  changes (`１つ` -> `１[ひと]つ`, `10分[ぷん]` -> `10分[じゅっぷん]`).

## Pending questions

Put to the user; until answered, a use that turns on one is left as the input has it and named
with the question's id.

- Q2 よる: is the doer of an action (彼によって書かれた, 市民による運動) 依る or 由る? (Means
  and method are 由る: settled.)
- Q4 Loanwords: only a kanji spelling JMdict doesn't mark rare (KANJI-16's draft), or every
  spelling SOURCE-1 accepts, the rare ones too?
- Q7 Honorific verbs after a verb stem: kanjified (寝為さい, 御待ち下さい, 御覧頂く), only the
  て-form keeps helpers in kana?
- Q8 こう and ああ as demonstrative adverbs: kana like そう (KANA-13's draft), or 斯う / 彼あ?
